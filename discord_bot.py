#!/usr/bin/env python3
"""
CSFloat AuctionRadar: Discord-бот.

Команда /auctions показывает аукционы CSFloat, которые скоро закончатся,
лучшие по скидке первыми. Если задан ALERT_CHANNEL_ID, бот ещё и сам проверяет
CSFloat каждые несколько минут и пишет в канал о новых выгодных аукционах.

Установка:  pip install -r requirements-bot.txt
Настройка:  скопируй .env.example в .env и заполни (файл .env никому не показывай)
Запуск:     python discord_bot.py
"""
import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone

import discord
import requests
from discord import app_commands
from discord.ext import tasks

try:  # .env подхватывается, если установлен python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API = "https://csfloat.com/api/v1/listings"
ITEM_URL = "https://csfloat.com/item/{}"

TOKEN = os.getenv("DISCORD_TOKEN")
CSFLOAT_KEY = os.getenv("CSFLOAT_API_KEY")
GUILD_ID = os.getenv("GUILD_ID")
ALERT_CHANNEL_ID = os.getenv("ALERT_CHANNEL_ID")
ALERT_INTERVAL_MIN = float(os.getenv("ALERT_INTERVAL_MIN") or 5)
ALERT_MAX_PRICE = float(os.getenv("ALERT_MAX_PRICE") or 30)
ALERT_HOURS = float(os.getenv("ALERT_HOURS") or 6)
ALERT_MIN_DISCOUNT = float(os.getenv("ALERT_MIN_DISCOUNT") or 15)


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


def ref_price(lot):
    """Референсная цена в центах и источник: 'ref' (CSFloat) или 'steam' (запасной вариант)."""
    ref = lot.get("reference") or {}
    for key in ("predicted_price", "base_price"):
        if ref.get(key):
            return ref[key], "ref"
    scm = (lot.get("item") or {}).get("scm") or {}
    if scm.get("price"):
        return scm["price"], "steam"
    return None, None


def find_auctions(max_price, hours, pages=4):
    """Аукционы, которые закончатся в ближайшие `hours` часов, лучшие по скидке первыми.

    Блокирующая функция: из бота её нужно вызывать через run_in_executor.
    """
    session = requests.Session()
    session.headers["User-Agent"] = "csfloat-auctionradar-discord/1.0"
    if CSFLOAT_KEY:
        session.headers["Authorization"] = CSFLOAT_KEY

    params = {
        "type": "auction",
        "sort_by": "expires_soon",
        "max_price": int(round(max_price * 100)),  # API принимает центы
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
            ref, src = ref_price(lot)
            if not ref:
                continue
            bid = details.get("min_next_bid") or lot.get("price") or 0  # сколько ставить сейчас
            item = lot.get("item") or {}
            rows.append({
                "id": lot.get("id"), "exp": exp, "bid": bid, "ref": ref, "steam": src != "ref",
                "disc": (ref - bid) / ref * 100,
                "float": item.get("float_value"), "name": item.get("market_hash_name", "?"),
            })
        if past_window or not cursor:
            break
    rows.sort(key=lambda r: r["disc"], reverse=True)
    return rows


# ---------- оформление сообщений ----------

def fmt_left(exp):
    secs = max(int((exp - datetime.now(timezone.utc)).total_seconds()), 0)
    h, m = divmod(secs // 60, 60)
    return f"{h}ч {m:02d}м"


def fmt_row(r):
    star = "*" if r["steam"] else ""
    fv = f"{r['float']:.4f}" if r["float"] is not None else "-"
    name = r["name"].replace("[", "(").replace("]", ")")
    return (f"**{r['disc']:+.1f}%** [{name}]({ITEM_URL.format(r['id'])})\n"
            f"ставка ${r['bid'] / 100:.2f} · реф ${r['ref'] / 100:.2f}{star} · "
            f"{fmt_left(r['exp'])} · float {fv}")


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
            alert_loop.change_interval(minutes=ALERT_INTERVAL_MIN)
            alert_loop.start()

    async def on_ready(self):
        print(f"Бот запущен как {self.user}. Команда: /auctions")


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
        await interaction.followup.send(f"Не получилось получить данные CSFloat: {e}")
        return
    rows = [r for r in rows if r["disc"] >= min_discount][:top]
    if not rows:
        await interaction.followup.send(
            "Ничего не нашлось. Попробуй поднять max_price или hours либо снизить min_discount.")
        return
    await interaction.followup.send(
        embed=build_embed(rows, f"Аукционы до ${max_price:g}, ближайшие {hours:g} ч"))


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
            client.seen.update({r["id"]: r["exp"] for r in new})
            await channel.send(embed=build_embed(new, f"Новые выгодные аукционы: {len(new)}"))
    except Exception as e:  # цикл не должен умирать из-за одной ошибки
        print(f"[alerts] ошибка: {e}")


@alert_loop.before_loop
async def _wait_until_ready():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Нет DISCORD_TOKEN. Скопируй .env.example в .env и впиши токен бота.")
    client.run(TOKEN)
