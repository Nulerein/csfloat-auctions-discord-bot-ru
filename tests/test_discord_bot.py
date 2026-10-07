import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


class ApiPayloadTests(unittest.TestCase):
    def test_unpack_accepts_list_and_paginated_payloads(self):
        rows = [{"id": "one"}]
        self.assertEqual(discord_bot.unpack(rows), (rows, None))
        self.assertEqual(
            discord_bot.unpack({"data": rows, "cursor": "next"}),
            (rows, "next"),
        )

    def test_ref_price_prefers_csfloat_and_falls_back_to_steam(self):
        self.assertEqual(
            discord_bot.ref_price({
                "reference": {"predicted_price": 1200, "base_price": 1000},
                "item": {"scm": {"price": 900}},
            }),
            (1200, "ref"),
        )
        self.assertEqual(
            discord_bot.ref_price({"item": {"scm": {"price": 900}}}),
            (900, "steam"),
        )
        self.assertEqual(discord_bot.ref_price({}), (None, None))


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
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "larger-discount",
                "state": "listed",
                "price": 500,
                "auction_details": {"expires_at": expires_later, "min_next_bid": 500},
                "reference": {"predicted_price": 1000},
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

    def test_find_deals_filters_unlisted_and_unpriced_rows_and_sorts(self):
        payload = [
            {
                "id": "lower-discount",
                "state": "listed",
                "price": 800,
                "reference": {"predicted_price": 1000},
                "created_at": NOW.isoformat(),
            },
            {
                "id": "higher-discount",
                "state": "listed",
                "price": 500,
                "reference": {"predicted_price": 1000},
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


class ListingFormatTests(unittest.TestCase):
    def test_auction_includes_time_remaining_and_float(self):
        row = {
            "kind": "auction",
            "id": "auction-id",
            "exp": NOW + timedelta(hours=2, minutes=15),
            "bid": 1490,
            "ref": 1820,
            "steam": False,
            "disc": 18.2,
            "float": 0.1234,
            "name": "AK-47 | Neon Revolution",
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**+18.2%** [AK-47 | Neon Revolution]"
            "(https://csfloat.com/item/auction-id)\n"
            "ставка $14.90 · реф $18.20 · 2ч 15м · float 0.1234",
        )

    def test_deal_includes_float_and_listing_age(self):
        row = {
            "kind": "deal",
            "id": "deal-id",
            "bid": 950,
            "ref": 1080,
            "steam": False,
            "disc": 12.5,
            "float": 0.0345,
            "name": "USP-S | Royal Blue",
            "offer": None,
            "created": NOW - timedelta(minutes=18),
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**+12.5%** [USP-S | Royal Blue]"
            "(https://csfloat.com/item/deal-id)\n"
            "цена $9.50 · реф $10.80 · float 0.0345 · 18м назад",
        )


if __name__ == "__main__":
    unittest.main()
