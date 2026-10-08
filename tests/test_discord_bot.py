import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot

UTC = timezone.utc
NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


class ParseTimeTests(unittest.TestCase):
    def test_parses_utc_z_suffix_and_fractional_seconds(self):
        result = discord_bot.parse_time("2025-01-01T12:00:00.123456789Z")
        self.assertEqual(result, datetime(2025, 1, 1, 12, 0, 0, 123456, UTC))

    def test_assigns_utc_to_naive_datetime(self):
        result = discord_bot.parse_time("2025-01-01T12:00:00")
        self.assertEqual(result, datetime(2025, 1, 1, 12, 0, tzinfo=UTC))

    def test_returns_none_for_missing_or_invalid_value(self):
        self.assertIsNone(discord_bot.parse_time(None))
        self.assertIsNone(discord_bot.parse_time("not a date"))


class ConfigurationValidationTests(unittest.TestCase):
    def test_float_setting_uses_default_for_missing_or_blank_values(self):
        self.assertEqual(discord_bot.parse_float_setting("SETTING", None, 5), 5)
        self.assertEqual(discord_bot.parse_float_setting("SETTING", "  ", 5), 5)

    def test_float_setting_parses_valid_number_and_boundaries(self):
        self.assertEqual(
            discord_bot.parse_float_setting(
                "ALERT_MAX_PRICE", "5000", 30, minimum=1, maximum=5000
            ),
            5000,
        )
        self.assertEqual(
            discord_bot.parse_float_setting(
                "ALERT_INTERVAL_MIN", "0.5", 5, minimum=0, minimum_exclusive=True
            ),
            0.5,
        )

    def test_float_setting_rejects_non_numeric_non_finite_and_out_of_range_values(self):
        invalid_values = (
            ("SETTING", "abc", 5, None, None, False, "must be a number"),
            ("SETTING", "NaN", 5, None, None, False, "finite number"),
            ("SETTING", "Infinity", 5, None, None, False, "finite number"),
            ("ALERT_INTERVAL_MIN", "0", 5, 0, None, True, "greater than 0"),
            ("ALERT_MAX_PRICE", "0", 30, 1, 5000, False, "at least 1"),
            ("ALERT_MAX_PRICE", "5001", 30, 1, 5000, False, "at most 5000"),
        )
        for name, value, default, minimum, maximum, exclusive, message in invalid_values:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, message),
            ):
                discord_bot.parse_float_setting(
                    name,
                    value,
                    default,
                    minimum=minimum,
                    maximum=maximum,
                    minimum_exclusive=exclusive,
                )

    def test_optional_discord_id_accepts_empty_or_numeric_value(self):
        self.assertIsNone(discord_bot.parse_optional_discord_id("GUILD_ID", " "))
        self.assertEqual(
            discord_bot.parse_optional_discord_id("GUILD_ID", "123456789"),
            "123456789",
        )

    def test_optional_discord_id_rejects_non_numeric_value(self):
        with self.assertRaisesRegex(ValueError, "GUILD_ID.*only digits"):
            discord_bot.parse_optional_discord_id("GUILD_ID", "abc123")


class ApiPayloadTests(unittest.TestCase):
    def test_unpack_accepts_list_and_paginated_payloads(self):
        rows = [{"id": "one"}]
        self.assertEqual(discord_bot.unpack(rows), (rows, None))
        self.assertEqual(
            discord_bot.unpack({"data": rows, "cursor": "next"}),
            (rows, "next"),
        )

    def test_price_references_separates_base_predicted_and_steam(self):
        self.assertEqual(
            discord_bot.price_references({
                "reference": {
                    "predicted_price": 1200,
                    "base_price": 1000,
                    "float_factor": 1.2,
                },
                "item": {"scm": {"price": 900}},
            }),
            (1000, 1200, 900),
        )
        self.assertEqual(
            discord_bot.price_references({"item": {"scm": {"price": 900}}}),
            (None, None, 900),
        )
        self.assertEqual(discord_bot.price_references({}), (None, None, None))

    def test_select_reference_prefers_base_then_predicted_then_steam(self):
        self.assertEqual(discord_bot.select_reference(1000, 1200, 900), (1000, "base"))
        self.assertEqual(discord_bot.select_reference(None, 1200, 900), (1200, "predicted"))
        self.assertEqual(discord_bot.select_reference(None, None, 900), (900, "steam"))
        self.assertEqual(discord_bot.select_reference(None, None, None), (None, None))


class ApiRequestTests(unittest.TestCase):
    def test_api_get_retries_rate_limit_then_returns_response(self):
        rate_limited = Mock(status_code=429, headers={"Retry-After": "3"})
        success = Mock(status_code=200)
        success.json.return_value = {"data": []}
        session = Mock()
        session.get.side_effect = [rate_limited, success]

        with patch.object(discord_bot.time, "sleep") as sleep:
            result = discord_bot.api_get(session, {"limit": 5})

        self.assertEqual(result, {"data": []})
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_api_get_raises_for_unauthorized_without_retry(self):
        unauthorized = Mock(status_code=401)
        session = Mock()
        session.get.return_value = unauthorized

        with self.assertRaisesRegex(discord_bot.CSFloatError, "401.*CSFLOAT_API_KEY"):
            discord_bot.api_get(session, {})

        session.get.assert_called_once()

    def test_api_get_raises_after_exhausting_rate_limit_retries(self):
        rate_limited = Mock(status_code=429, headers={"Retry-After": "120"})
        session = Mock()
        session.get.return_value = rate_limited

        with (
            patch.object(discord_bot.time, "sleep") as sleep,
            self.assertRaisesRegex(discord_bot.CSFloatError, "429"),
        ):
            discord_bot.api_get(session, {})

        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 60, 60])


class SeenAuctionsStateTests(unittest.TestCase):
    def test_load_seen_returns_empty_when_state_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            self.assertEqual(discord_bot.load_seen(path, now=NOW), {})

    def test_save_and_load_seen_preserves_active_auctions_and_drops_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            discord_bot.save_seen(
                {
                    "active": NOW + timedelta(hours=1),
                    "expired": NOW - timedelta(minutes=1),
                },
                path,
            )
            loaded = discord_bot.load_seen(path, now=NOW)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded, {"active": NOW + timedelta(hours=1)})
        self.assertEqual(saved, {
            "active": "2025-01-01T13:00:00+00:00",
            "expired": "2025-01-01T11:59:00+00:00",
        })

    def test_load_seen_skips_invalid_entries_and_keeps_valid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({
                    "valid": (NOW + timedelta(hours=1)).isoformat(),
                    "invalid-date": "not a date",
                    "invalid-type": 123,
                }),
                encoding="utf-8",
            )

            with self.assertLogs(discord_bot.logger, level="WARNING") as logs:
                loaded = discord_bot.load_seen(path, now=NOW)

        self.assertEqual(loaded, {"valid": NOW + timedelta(hours=1)})
        self.assertEqual(len(logs.records), 2)


class SetupHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_hook_starts_alerts_if_state_file_cannot_be_loaded(self):
        client = discord_bot.RadarBot()
        with (
            patch.object(discord_bot, "GUILD_ID", ""),
            patch.object(discord_bot, "ALERT_CHANNEL_ID", "123456"),
            patch.object(discord_bot, "ALERT_INTERVAL_MIN", 5),
            patch.object(client.tree, "sync", new=AsyncMock()),
            patch.object(discord_bot, "load_seen", side_effect=RuntimeError("invalid JSON")),
            patch.object(discord_bot.alert_loop, "change_interval") as change_interval,
            patch.object(discord_bot.alert_loop, "start") as start,
            self.assertLogs(discord_bot.logger, level="WARNING") as logs,
        ):
            await client.setup_hook()

        self.assertEqual(client.seen, {})
        change_interval.assert_called_once_with(minutes=5)
        start.assert_called_once_with()
        self.assertIn("starting with an empty state", logs.output[0])


class ListingSearchTests(unittest.TestCase):
    def test_find_auctions_filters_invalid_rows_and_sorts_by_discount(self):
        expires_soon = (NOW + timedelta(hours=1)).isoformat()
        expires_later = (NOW + timedelta(hours=2)).isoformat()
        payload = [
            {
                "id": "smaller-discount",
                "state": "listed",
                "price": 800,
                "auction_details": {"expires_at": expires_soon, "min_next_bid": 800},
                "reference": {"base_price": 1000, "predicted_price": 2000},
            },
            {
                "id": "larger-discount",
                "state": "listed",
                "price": 500,
                "auction_details": {"expires_at": expires_later, "min_next_bid": 500},
                "reference": {"base_price": 1000, "predicted_price": 600},
            },
            {
                "id": "expired",
                "state": "listed",
                "auction_details": {
                    "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
                },
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "too-late",
                "state": "listed",
                "auction_details": {
                    "expires_at": (NOW + timedelta(hours=5)).isoformat(),
                },
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "not-listed",
                "state": "sold",
                "auction_details": {"expires_at": expires_soon},
                "reference": {"predicted_price": 1000},
            },
        ]
        with (
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload),
        ):
            results = discord_bot.find_auctions(25, 4, pages=1)

        self.assertEqual([row["id"] for row in results], [
            "larger-discount",
            "smaller-discount",
        ])
        self.assertEqual(results[0]["disc"], 50)
        self.assertAlmostEqual(results[0]["float_disc"], 100 / 600 * 100)
        self.assertEqual(results[1]["float_disc"], 60)

    def test_find_auctions_uses_predicted_price_before_steam(self):
        payload = [{
            "id": "predicted-auction",
            "state": "listed",
            "price": 1500,
            "auction_details": {
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "min_next_bid": 1500,
            },
            "reference": {"predicted_price": 2000},
            "item": {"scm": {"price": 1000}, "float_value": 0.2},
        }]
        with (
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload),
        ):
            results = discord_bot.find_auctions(25, 4, pages=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ref"], 2000)
        self.assertEqual(results[0]["reference_source"], "predicted")
        self.assertFalse(results[0]["steam"])
        self.assertEqual(results[0]["disc"], 25)
        self.assertIsNone(results[0]["float_disc"])

    def test_find_deals_filters_unlisted_and_unpriced_rows_and_sorts(self):
        payload = [
            {
                "id": "lower-discount",
                "state": "listed",
                "price": 800,
                "reference": {"base_price": 1000, "predicted_price": 2000},
                "created_at": NOW.isoformat(),
            },
            {
                "id": "higher-discount",
                "state": "listed",
                "price": 500,
                "reference": {"base_price": 1000, "predicted_price": 600},
                "created_at": NOW.isoformat(),
            },
            {
                "id": "sold",
                "state": "sold",
                "price": 100,
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "no-reference",
                "state": "listed",
                "price": 100,
            },
        ]
        with (
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload) as api_get,
        ):
            results = discord_bot.find_deals(100, 10, "highest_discount", 20, 1)

        self.assertEqual([row["id"] for row in results], [
            "higher-discount",
            "lower-discount",
        ])
        self.assertEqual(results[0]["disc"], 50)
        self.assertEqual(api_get.call_args.args[1]["max_price"], 10000)
        self.assertEqual(api_get.call_args.args[1]["min_price"], 1000)
        self.assertEqual(api_get.call_args.args[1]["min_ref_qty"], 20)
        self.assertEqual(results[0]["float_disc"], 100 / 600 * 100)

    def test_find_deals_uses_predicted_price_before_steam_when_base_is_missing(self):
        payload = [{
            "id": "predicted-fallback",
            "state": "listed",
            "price": 1500,
            "reference": {"predicted_price": 2000},
            "item": {"scm": {"price": 1000}, "float_value": 0.2},
        }]
        with (
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload),
        ):
            results = discord_bot.find_deals(50, 1, "highest_discount", 0, 1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ref"], 2000)
        self.assertEqual(results[0]["reference_source"], "predicted")
        self.assertFalse(results[0]["steam"])
        self.assertEqual(results[0]["disc"], 25)
        self.assertEqual(results[0]["float_ref"], 2000)
        self.assertIsNone(results[0]["float_disc"])

    def test_find_deals_uses_steam_only_when_csfloat_references_are_missing(self):
        payload = [{
            "id": "steam-fallback",
            "state": "listed",
            "price": 900,
            "item": {"scm": {"price": 1000}, "float_value": 0.2},
        }]
        with (
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload),
        ):
            results = discord_bot.find_deals(50, 1, "highest_discount", 0, 1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ref"], 1000)
        self.assertEqual(results[0]["reference_source"], "steam")
        self.assertTrue(results[0]["steam"])
        self.assertEqual(results[0]["disc"], 10)
        self.assertIsNone(results[0]["float_ref"])
        self.assertIsNone(results[0]["float_disc"])


class ListingFormatTests(unittest.TestCase):
    def test_auction_includes_time_remaining_and_float(self):
        row = {
            "kind": "auction",
            "id": "auction-id",
            "exp": NOW + timedelta(hours=2, minutes=15),
            "bid": 1490,
            "ref": 1820,
            "reference_source": "base",
            "float_ref": 2000,
            "steam": False,
            "disc": 18.2,
            "float_disc": 25.5,
            "float": 0.1234,
            "name": "AK-47 | Neon Revolution",
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**к обычной цене: +18.2% · с учётом float: +25.5%** "
            "[AK-47 | Neon Revolution]"
            "(https://csfloat.com/item/auction-id)\n"
            "ставка $14.90 · обычная цена $18.20 · оценка CSFloat с float "
            "$20.00 · 2ч 15м · float 0.1234",
        )

    def test_deal_includes_float_and_listing_age(self):
        row = {
            "kind": "deal",
            "id": "deal-id",
            "bid": 950,
            "ref": 1080,
            "reference_source": "base",
            "float_ref": 1150,
            "steam": False,
            "disc": 12.5,
            "float_disc": 17.4,
            "float": 0.0345,
            "name": "USP-S | Royal Blue",
            "offer": None,
            "created": NOW - timedelta(minutes=18),
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**к обычной цене: +12.5% · с учётом float: +17.4%** "
            "[USP-S | Royal Blue]"
            "(https://csfloat.com/item/deal-id)\n"
            "цена $9.50 · обычная цена $10.80 · оценка CSFloat с float "
            "$11.50 · float 0.0345 · 18м назад",
        )

    def test_unadjusted_price_marks_steam_fallback_and_omits_float_discount(self):
        row = {
            "kind": "deal",
            "id": "deal-id",
            "bid": 6100,
            "ref": 2036,
            "reference_source": "base",
            "float_ref": 6100,
            "steam": False,
            "disc": (2036 - 6100) / 2036 * 100,
            "float_disc": 0,
            "float": 0.000267,
            "name": "Desert Eagle | The Bronze",
            "offer": None,
            "created": None,
        }
        result = discord_bot.fmt_row(row)

        self.assertIn("к обычной цене: -199.6% · с учётом float: +0.0%", result)
        self.assertIn("цена $61.00 · обычная цена $20.36 · оценка CSFloat с float $61.00", result)

    def test_predicted_fallback_is_labeled_as_float_adjusted_price(self):
        row = {
            "kind": "deal",
            "id": "deal-id",
            "bid": 1500,
            "ref": 2000,
            "reference_source": "predicted",
            "float_ref": 2000,
            "steam": False,
            "disc": 25,
            "float_disc": None,
            "float": 0.2,
            "name": "Example Skin",
            "offer": None,
            "created": None,
        }

        result = discord_bot.fmt_row(row)

        self.assertIn("к оценке CSFloat с float: +25.0%", result)
        self.assertIn("цена $15.00 · оценка CSFloat с float $20.00", result)
        self.assertNotIn("обычная цена", result)
        self.assertEqual(result.count("оценка CSFloat с float"), 1)


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_slash_commands_have_expected_names_and_options(self):
        auctions = discord_bot.client.tree.get_command("auctions")
        deals = discord_bot.client.tree.get_command("deals")

        self.assertIsNotNone(auctions)
        self.assertIsNotNone(deals)
        self.assertEqual(
            {parameter.name for parameter in auctions.parameters},
            {"max_price", "hours", "top", "min_discount"},
        )
        self.assertEqual(
            {parameter.name for parameter in deals.parameters},
            {"max_price", "min_price", "min_discount", "top", "sort", "min_sales"},
        )

    async def test_auctions_command_defers_and_reports_csfloat_errors(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        loop = SimpleNamespace(
            run_in_executor=AsyncMock(side_effect=discord_bot.CSFloatError("API error"))
        )

        with patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop):
            await discord_bot.auctions.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once_with(
            "Не получилось получить данные CSFloat: API error"
        )

    async def test_deals_command_uses_sort_choice_and_sends_results(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        rows = [{
            "kind": "deal",
            "id": "deal",
            "bid": 800,
            "ref": 1000,
            "reference_source": "base",
            "float_ref": None,
            "steam": False,
            "disc": 20,
            "float_disc": None,
            "float": None,
            "name": "Example Skin",
            "offer": None,
            "created": None,
        }]
        loop = SimpleNamespace(run_in_executor=AsyncMock(return_value=rows))
        sort = discord_bot.app_commands.Choice(name="Самые новые", value="most_recent")

        with patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop):
            await discord_bot.deals.callback(interaction, sort=sort)

        loop.run_in_executor.assert_awaited_once_with(
            None, discord_bot.find_deals, 30.0, 1.0, "most_recent", 20, 4
        )
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once()
        self.assertIn("embed", interaction.followup.send.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
