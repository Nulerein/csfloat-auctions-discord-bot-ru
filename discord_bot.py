"""
CSFloat AuctionRadar: Discord-бот.

Команды:
  /auctions  аукционы, которые скоро закончатся, лучшие по скидке первыми
  /deals     выгодные обычные лоты (не аукционы): с лучшей скидкой или самые новые

Если задан ALERT_CHANNEL_ID, бот ещё и сам проверяет аукционы каждые несколько минут
и пишет в канал о новых выгодных.

Установка:  pip install requests discord.py python-dotenv
Настройка:  скопируй .env.example в .env и заполни (файл .env никому не показывай)
Запуск:     python discord_bot.py
"""
import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import requests
from discord import app_commands
from discord.ext import tasks

logger = logging.getLogger(__name__)

try:  # .env подхватывается, если установлен python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API = "https://csfloat.com/api/v1/listings"
ITEM_URL = "https://csfloat.com/item/{}"

TOKEN = os.getenv("DISCORD_TOKEN")
CSFLOAT_KEY = os.getenv("CSFLOAT_API_KEY")
SEEN_AUCTIONS_FILE = Path(__file__).with_name("seen_auctions.json")


def parse_float_setting(
    name,
    raw_value,
    default,
    *,
    minimum=None,
    maximum=None,
    minimum_exclusive=False,
):
    """Parse and validate a finite floating-point environment setting."""
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError:
        raise ValueError(f"{name} must be a number.") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and (value <= minimum if minimum_exclusive else value < minimum):
        operator = "greater than" if minimum_exclusive else "at least"
        raise ValueError(f"{name} must be {operator} {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum:g}.")
    return value


def parse_optional_discord_id(name, value):
    """Return an optional Discord snowflake string, rejecting non-numeric IDs."""
    if value is None or not value.strip():
        return None
    if not value.isdigit():
        raise ValueError(f"{name} must contain only digits.")
    return value


try:
    GUILD_ID = parse_optional_discord_id("GUILD_ID", os.getenv("GUILD_ID"))
    ALERT_CHANNEL_ID = parse_optional_discord_id(
        "ALERT_CHANNEL_ID", os.getenv("ALERT_CHANNEL_ID")
    )
    ALERT_INTERVAL_MIN = parse_float_setting(
        "ALERT_INTERVAL_MIN", os.getenv("ALERT_INTERVAL_MIN"), 5,
        minimum=0, minimum_exclusive=True,
    )
    ALERT_MAX_PRICE = parse_float_setting(
        "ALERT_MAX_PRICE", os.getenv("ALERT_MAX_PRICE"), 30,
        minimum=1, maximum=5000,
    )
    ALERT_HOURS = parse_float_setting(
        "ALERT_HOURS", os.getenv("ALERT_HOURS"), 6,
        minimum=0.1, maximum=168,
    )
    ALERT_MIN_DISCOUNT = parse_float_setting(
        "ALERT_MIN_DISCOUNT", os.getenv("ALERT_MIN_DISCOUNT"), 15
    )
except ValueError as e:
    raise SystemExit(f"Ошибка конфигурации .env: {e}") from None


# ---------- работа с CSFloat ----------

class CSFloatError(Exception):
    """Понятная пользователю ошибка при запросе к CSFloat."""


def parse_time(s):
    """ISO-время из API -> datetime (UTC). None, если не удалось разобрать."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*?\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:  # приводим доли секунды к 6 знакам, чтобы работало на любой версии Python
        head, frac, tail = m.groups()
        s = head + (frac or ".0")[:7].ljust(7, "0") + tail
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_seen(path=SEEN_AUCTIONS_FILE, now=None):
    """Load unexpired notified auction IDs from the local JSON state file."""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    try:
        with path.open(encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Не удалось прочитать состояние уведомлений {path}: {e}") from e

    if not isinstance(payload, dict):
        raise TypeError(f"Некорректный формат состояния уведомлений в {path}: ожидался JSON-объект.")

    seen = {}
    for listing_id, expiration in payload.items():
        exp = parse_time(expiration) if isinstance(expiration, str) else None
        if exp is None:
            raise ValueError(f"Некорректное время окончания аукциона для ID {listing_id!r} в {path}.")
        if exp > now:
            seen[listing_id] = exp
    return seen


def save_seen(seen, path=SEEN_AUCTIONS_FILE):
    """Atomically save notified auction IDs and their expiration times."""
    path = Path(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temp_path = Path(state_file.name)
            json.dump(
                {listing_id: expiration.isoformat() for listing_id, expiration in seen.items()},
                state_file,
                ensure_ascii=False,
                indent=2,
            )
            state_file.write("\n")
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as e:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Не удалось сохранить состояние уведомлений {path}: {e}") from e


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "csfloat-auctionradar-discord/1.0"
    if CSFLOAT_KEY:
        session.headers["Authorization"] = CSFLOAT_KEY
    return session


def api_get(session, params, retries=3):
    for _ in range(retries):
        r = session.get(API, params=params, timeout=20)
        if r.status_code == 429:
            try:
                wait = int(r.headers.get("Retry-After", 10))
            except ValueError:
                wait = 10
            time.sleep(min(wait, 60))
            continue
        if r.status_code in (401, 403):
            raise CSFloatError(f"CSFloat вернул {r.status_code}: проверь CSFLOAT_API_KEY.")
        r.raise_for_status()
        return r.json()
    raise CSFloatError("CSFloat просит притормозить (429). Попробуй чуть позже.")


def unpack(payload):
    """API отдаёт либо список, либо {"data": [...], "cursor": "..."}."""
    if isinstance(payload, list):
        return payload, None
    return (payload.get("data") or payload.get("listings") or []), payload.get("cursor")


def price_references(lot):
    """Return base, float-adjusted and Steam prices in cents."""
    ref = lot.get("reference") or {}
    scm = (lot.get("item") or {}).get("scm") or {}
    return (
        ref.get("base_price") or None,
        ref.get("predicted_price") or None,
        scm.get("price") or None,
    )


def find_auctions(max_price, hours, pages=4):
    """Аукционы, которые закончатся в ближайшие `hours` часов, лучшие по скидке первыми.

    Блокирующая функция: из бота её нужно вызывать через run_in_executor.
    """
    session = make_session()
    params = {
        "type": "auction",
        "sort_by": "expires_soon",
        "max_price": round(max_price * 100),  # API принимает центы
        "limit": 50,
    }
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=hours)
    rows, cursor = [], None
    for _ in range(pages):
        if cursor:
            params["cursor"] = cursor
        batch, cursor = unpack(api_get(session, params))
        if not batch:
            break
        past_window = False
        for lot in batch:
            if lot.get("state", "listed") != "listed":
                continue
            details = lot.get("auction_details") or {}
            exp = parse_time(details.get("expires_at"))
            if exp is None or exp <= now:
                continue
            if exp > deadline:
                past_window = True  # список идёт по времени окончания, дальше только поздние
                break
            base, predicted, steam = price_references(lot)
            normal_ref = base or steam
            if not normal_ref:
                continue
            bid = details.get("min_next_bid") or lot.get("price") or 0  # сколько ставить сейчас
            item = lot.get("item") or {}
            rows.append({
                "kind": "auction", "id": lot.get("id"), "exp": exp, "bid": bid,
                "ref": normal_ref, "float_ref": predicted,
                "steam": base is None, "disc": (normal_ref - bid) / normal_ref * 100,
                "float_disc": (predicted - bid) / predicted * 100 if predicted else None,
                "float": item.get("float_value"), "name": item.get("market_hash_name", "?"),
            })
        if past_window or not cursor:
            break
    rows.sort(key=lambda r: r["disc"], reverse=True)
    return rows


def find_deals(max_price, min_price, sort_by, min_sales, pages):
    """Обычные лоты (buy_now) со скидкой к референсной цене, лучшие первыми.

    sort_by: "highest_discount" (лучшие по версии CSFloat) или "most_recent" (самые новые).
    min_sales: референс должен быть построен минимум на стольких продажах (0 - не проверять).
    Блокирующая функция: из бота её нужно вызывать через run_in_executor.
    """
    session = make_session()
    params = {
        "type": "buy_now",
        "sort_by": sort_by,
        "max_price": round(max_price * 100),  # API принимает центы
        "limit": 50,
    }
    if min_price > 0:
        params["min_price"] = round(min_price * 100)
    if min_sales > 0:
        params["min_ref_qty"] = min_sales
    rows, cursor = [], None
    for _ in range(pages):
        if cursor:
            params["cursor"] = cursor
        batch, cursor = unpack(api_get(session, params))
        if not batch:
            break
        for lot in batch:
            if lot.get("state", "listed") != "listed":
                continue
            base, predicted, steam = price_references(lot)
            price = lot.get("price") or 0
            normal_ref = base or steam
            if not normal_ref or not price:
                continue
            item = lot.get("item") or {}
            rows.append({
                "kind": "deal", "id": lot.get("id"), "bid": price, "ref": normal_ref,
                "float_ref": predicted,
                "steam": base is None, "disc": (normal_ref - price) / normal_ref * 100,
                "float_disc": (predicted - price) / predicted * 100 if predicted else None,
                "float": item.get("float_value"), "name": item.get("market_hash_name", "?"),
                "offer": lot.get("min_offer_price"), "created": parse_time(lot.get("created_at")),
            })
        if not cursor:
            break
    rows.sort(key=lambda r: r["disc"], reverse=True)
    return rows


# ---------- оформление сообщений ----------

def fmt_left(exp):
    secs = max(int((exp - datetime.now(timezone.utc)).total_seconds()), 0)
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m:02d}м"


def fmt_age(created):
    secs = max(int((datetime.now(timezone.utc) - created).total_seconds()), 0)
    if secs < 60:
        return "только что"
    if secs < 3600:
        return f"{secs // 60}м назад"
    if secs < 86400:
        return f"{secs // 3600}ч назад"
    return f"{secs // 86400}д назад"


def fmt_row(r):
    star = "*" if r["steam"] else ""
    fv = f"{r['float']:.4f}" if r["float"] is not None else "-"
    name = r["name"].replace("[", "(").replace("]", ")")  # квадратные скобки ломают ссылку
    normal_label = "к Steam-цене" if r["steam"] else "к обычной цене"
    comparison = f"{normal_label}: {r['disc']:+.1f}%"
    if r.get("float_disc") is not None:
        comparison += f" · с учётом float: {r['float_disc']:+.1f}%"
    head = f"**{comparison}** [{name}]({ITEM_URL.format(r['id'])})"
    price, ref = r["bid"] / 100, r["ref"] / 100
    info = f"обычная цена ${ref:.2f}{star}"
    if r.get("float_ref"):
        info += f" · оценка CSFloat с float ${r['float_ref'] / 100:.2f}"
    if r["kind"] == "auction":
        return f"{head}\nставка ${price:.2f} · {info} · {fmt_left(r['exp'])} · float {fv}"
    info = f"цена ${price:.2f} · {info} · float {fv}"
    if r.get("offer") and r["offer"] < r["bid"]:
        info += f" · можно предложить от ${r['offer'] / 100:.2f}"
    if r.get("created"):
        info += f" · {fmt_age(r['created'])}"
    return f"{head}\n{info}"


def build_embed(rows, title):
    lines, total = [], 0
    for r in rows:
        line = fmt_row(r)
        if total + len(line) > 3800:  # лимит описания embed - 4096 символов
            break
        lines.append(line)
        total += len(line) + 2
    embed = discord.Embed(title=title, description="\n\n".join(lines), color=0x2B7FFF)
    if any(r["steam"] for r in rows[: len(lines)]):
        embed.set_footer(text="* нет референса CSFloat, сравнение со Steam-ценой (скидка завышена)")
    return embed


# ---------- бот ----------

class RadarBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.seen = {}  # id лота -> время окончания; о таких лотах уже писали в канал

    async def setup_hook(self):
        if GUILD_ID and GUILD_ID.isdigit():  # на конкретном сервере команды появляются сразу
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if ALERT_CHANNEL_ID and ALERT_CHANNEL_ID.isdigit():
            self.seen = load_seen()
            alert_loop.change_interval(minutes=ALERT_INTERVAL_MIN)
            alert_loop.start()

    async def on_ready(self):
        logger.info("Бот запущен как %s. Команды: /auctions, /deals", self.user)


client = RadarBot()


@client.tree.command(name="auctions", description="Скоро заканчивающиеся аукционы CSFloat, лучшие по скидке первыми")
@app_commands.describe(
    max_price="Максимальная цена в $",
    hours="Закончатся в ближайшие N часов",
    top="Сколько лотов показать",
    min_discount="Минимальная скидка в % (можно отрицательную)",
)
async def auctions(
    interaction: discord.Interaction,
    max_price: app_commands.Range[float, 1.0, 5000.0] = 30.0,
    hours: app_commands.Range[float, 0.1, 168.0] = 12.0,
    top: app_commands.Range[int, 1, 15] = 8,
    min_discount: float = 0.0,
):
    await interaction.response.defer(thinking=True)  # запрос к CSFloat может занять несколько секунд
    try:
        rows = await asyncio.get_running_loop().run_in_executor(None, find_auctions, max_price, hours)
    except (CSFloatError, requests.RequestException) as e:
        logger.warning("CSFloat request failed for /auctions: %s", e)
        await interaction.followup.send(f"Не получилось получить данные CSFloat: {e}")
        return
    rows = [r for r in rows if r["disc"] >= min_discount][:top]
    if not rows:
        await interaction.followup.send(
            "Ничего не нашлось. Попробуй поднять max_price или hours либо снизить min_discount.")
        return
    await interaction.followup.send(
        embed=build_embed(rows, f"Аукционы до ${max_price:g}, ближайшие {hours:g} ч"))


@client.tree.command(name="deals", description="Выгодные обычные лоты (не аукционы) на CSFloat")
@app_commands.describe(
    max_price="Максимальная цена в $",
    min_price="Минимальная цена в $ (отсекает копеечный мусор)",
    min_discount="Минимальная скидка к референсной цене, %",
    top="Сколько лотов показать",
    sort="Какие лоты просматривать: с лучшей скидкой или самые новые",
    min_sales="Референс построен минимум на стольких продажах (0 - не проверять)",
)
@app_commands.choices(sort=[
    app_commands.Choice(name="С лучшей скидкой", value="highest_discount"),
    app_commands.Choice(name="Самые новые", value="most_recent"),
])
async def deals(
    interaction: discord.Interaction,
    max_price: app_commands.Range[float, 1.0, 5000.0] = 30.0,
    min_price: app_commands.Range[float, 0.0, 5000.0] = 1.0,
    min_discount: float = 10.0,
    top: app_commands.Range[int, 1, 15] = 8,
    sort: app_commands.Choice[str] | None = None,
    min_sales: app_commands.Range[int, 0, 1000] = 20,
):
    await interaction.response.defer(thinking=True)
    sort_by = sort.value if sort else "highest_discount"
    pages = 2 if sort_by == "highest_discount" else 4  # новых лотов просматриваем больше
    try:
        rows = await asyncio.get_running_loop().run_in_executor(
            None, find_deals, max_price, min_price, sort_by, min_sales, pages)
    except (CSFloatError, requests.RequestException) as e:
        logger.warning("CSFloat request failed for /deals: %s", e)
        await interaction.followup.send(f"Не получилось получить данные CSFloat: {e}")
        return
    rows = [r for r in rows if r["disc"] >= min_discount][:top]
    if not rows:
        await interaction.followup.send(
            "Ничего не нашлось. Попробуй поднять max_price, снизить min_discount или min_sales.")
        return
    mode = "с лучшей скидкой" if sort_by == "highest_discount" else "самые новые"
    await interaction.followup.send(
        embed=build_embed(rows, f"Обычные лоты ${min_price:g}-${max_price:g}, {mode}"))


@tasks.loop(minutes=5)  # реальный интервал берётся из ALERT_INTERVAL_MIN
async def alert_loop():
    try:
        channel_id = int(ALERT_CHANNEL_ID)
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        rows = await asyncio.get_running_loop().run_in_executor(None, find_auctions, ALERT_MAX_PRICE, ALERT_HOURS)
        now = datetime.now(timezone.utc)
        client.seen = {i: e for i, e in client.seen.items() if e > now}  # забываем закончившиеся
        new = [r for r in rows if r["disc"] >= ALERT_MIN_DISCOUNT and r["id"] not in client.seen][:10]
        if new:
            await channel.send(embed=build_embed(new, f"Новые выгодные аукционы: {len(new)}"))
            client.seen.update({r["id"]: r["exp"] for r in new})
        save_seen(client.seen)
    except Exception:  # цикл не должен умирать из-за одной ошибки
        logger.exception("[alerts] ошибка")


@alert_loop.before_loop
async def _wait_until_ready():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Нет DISCORD_TOKEN. Скопируй .env.example в .env и впиши токен бота.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client.run(TOKEN, log_handler=None)
