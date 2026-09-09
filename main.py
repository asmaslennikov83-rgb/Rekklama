import asyncio
import base64
import hashlib
import logging
import os
from datetime import datetime
from typing import Any

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BIND_CODE = os.getenv("BIND_CODE", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "wb_ads_bot.db")
DEFAULT_CHECK_INTERVAL = int(os.getenv("DEFAULT_CHECK_INTERVAL", "15"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
MAX_CABINETS = 2

WB_ADVERT_BASE = "https://advert-api.wildberries.ru"
WB_COMMON_BASE = "https://common-api.wildberries.ru"

STATUS_NAMES = {
    -1: "Удаляется",
    4: "Готова к запуску",
    7: "Завершена",
    8: "Отменена",
    9: "Активна",
    11: "На паузе",
}

# Для мониторинга считаем актуальными только кампании, которыми ещё можно управлять:
# готовые к запуску, активные и на паузе.
CURRENT_CAMPAIGN_STATUSES = {4, 9, 11}


def is_current_campaign_status(status: int | None) -> bool:
    return status in CURRENT_CAMPAIGN_STATUSES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("wb_ads_bot")

router = Router()
poll_wakeup = asyncio.Event()
check_lock = asyncio.Lock()


class AddCabinet(StatesGroup):
    waiting_name = State()
    waiting_token = State()


class SetInterval(StatesGroup):
    waiting_minutes = State()


def make_fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str) -> str:
    return make_fernet(APP_SECRET).encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(token_enc: str) -> str:
    try:
        return make_fernet(APP_SECRET).decrypt(token_enc.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Не удалось расшифровать API-токен. Проверьте APP_SECRET.") from exc


async def db_execute(query: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


async def db_fetchone(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchone()


async def db_fetchall(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        return await cur.fetchall()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cabinets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_sid TEXT UNIQUE,
                seller_name TEXT NOT NULL,
                trade_mark TEXT,
                token_enc TEXT NOT NULL,
                initialized INTEGER NOT NULL DEFAULT 0,
                api_error_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                cabinet_id INTEGER NOT NULL,
                advert_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                status INTEGER,
                last_budget REAL,
                warning_300_sent INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (cabinet_id, advert_id),
                FOREIGN KEY(cabinet_id) REFERENCES cabinets(id) ON DELETE CASCADE
            );
            """
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('check_interval', ?)",
            (str(DEFAULT_CHECK_INTERVAL),),
        )
        await db.commit()


async def get_setting(key: str, default: str | None = None) -> str | None:
    row = await db_fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await db_execute(
        """
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


async def get_owner_id() -> int | None:
    value = await get_setting("owner_id")
    return int(value) if value else None


def get_env_allowed_user_ids() -> set[int]:
    result: set[int] = set()
    for raw in ALLOWED_USER_IDS_RAW.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            result.add(int(raw))
        except ValueError:
            logger.warning("Некорректный Telegram ID в ALLOWED_USER_IDS: %s", raw)
    return result


async def get_allowed_user_ids() -> set[int]:
    ids = get_env_allowed_user_ids()
    owner_id = await get_owner_id()
    if owner_id:
        ids.add(owner_id)
    return ids


async def is_owner(user_id: int) -> bool:
    return user_id in await get_allowed_user_ids()


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="status"),
                InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="check_now"),
            ],
            [
                InlineKeyboardButton(text="🏢 Кабинеты", callback_data="cabinets"),
                InlineKeyboardButton(text="➕ Добавить кабинет", callback_data="add_cabinet"),
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал проверки", callback_data="interval"),
                InlineKeyboardButton(text="📋 Последние события", callback_data="events"),
            ],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]
    )


async def guard_message(message: Message) -> bool:
    if not message.from_user:
        return False
    if not await is_owner(message.from_user.id):
        await message.answer("⛔ Бот не привязан к вашему Telegram. Используйте /bind КОД.")
        return False
    return True


async def guard_callback(callback: CallbackQuery) -> bool:
    if not callback.from_user or not await is_owner(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True


class WBClient:
    def __init__(self, token: str):
        self.headers = {"Authorization": token}

    async def _get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict | None = None,
        max_retries: int = 3,
    ) -> Any:
        timeout = aiohttp.ClientTimeout(total=45)
        backoff_seconds = [30, 60, 120]

        for attempt in range(max_retries + 1):
            async with session.get(url, headers=self.headers, params=params, timeout=timeout) as resp:
                body = await resp.text()

                if resp.status == 401:
                    raise RuntimeError("WB API: токен не авторизован (401)")

                if resp.status == 429:
                    if attempt >= max_retries:
                        raise RuntimeError(
                            f"WB API: превышен лимит запросов (429) после {max_retries + 1} попыток"
                        )

                    retry_after_raw = resp.headers.get("Retry-After")
                    wait_seconds = None
                    if retry_after_raw:
                        try:
                            wait_seconds = float(retry_after_raw)
                        except ValueError:
                            wait_seconds = None

                    if wait_seconds is None or wait_seconds <= 0:
                        wait_seconds = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]

                    wait_seconds = min(max(wait_seconds + 0.5, 1.0), 60.0)
                    logger.warning(
                        "WB API 429 для %s. Повтор %s/%s через %.1f сек.",
                        url,
                        attempt + 1,
                        max_retries,
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue

                if resp.status >= 500:
                    if attempt < max_retries:
                        wait_seconds = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                        logger.warning(
                            "WB API HTTP %s для %s. Повтор через %s сек.",
                            resp.status,
                            url,
                            wait_seconds,
                        )
                        await asyncio.sleep(wait_seconds)
                        continue
                    raise RuntimeError(f"WB API: HTTP {resp.status}: {body[:300]}")

                if resp.status >= 400:
                    raise RuntimeError(f"WB API: HTTP {resp.status}: {body[:300]}")

                if resp.status == 204 or not body:
                    return None

                try:
                    return await resp.json()
                except Exception:
                    raise RuntimeError(f"WB API вернул не-JSON ответ: {body[:300]}")

        raise RuntimeError("WB API: не удалось выполнить запрос")

    async def seller_info(self, session: aiohttp.ClientSession) -> dict:
        data = await self._get(session, f"{WB_COMMON_BASE}/api/v1/seller-info")
        return data or {}

    async def campaign_count(self, session: aiohttp.ClientSession) -> dict:
        data = await self._get(session, f"{WB_ADVERT_BASE}/adv/v1/promotion/count")
        return data or {}

    async def current_campaigns(self, session: aiohttp.ClientSession) -> list[dict]:
        data = await self._get(
            session,
            f"{WB_ADVERT_BASE}/api/advert/v2/adverts",
            params={"statuses": "4,9,11"},
        )
        if isinstance(data, dict):
            adverts = data.get("adverts")
            return adverts if isinstance(adverts, list) else []
        if isinstance(data, list):
            return data
        return []

    async def campaigns_by_ids(self, session: aiohttp.ClientSession, ids: list[int]) -> list[dict]:
        result: list[dict] = []
        for pos in range(0, len(ids), 50):
            chunk = ids[pos:pos + 50]
            data = await self._get(
                session,
                f"{WB_ADVERT_BASE}/api/advert/v2/adverts",
                params={"ids": ",".join(map(str, chunk))},
            )
            if isinstance(data, dict):
                adverts = data.get("adverts")
                if isinstance(adverts, list): result.extend(adverts)
            elif isinstance(data, list):
                result.extend(data)
            await asyncio.sleep(0.8)
        return result

    async def budget(self, session: aiohttp.ClientSession, advert_id: int) -> float:
        data = await self._get(
            session,
            f"{WB_ADVERT_BASE}/adv/v1/budget",
            params={"id": advert_id},
        )
        if not isinstance(data, dict):
            return 0.0
        return float(data.get("total", 0) or 0)


def extract_ids(count_data: dict) -> set[int]:
    ids: set[int] = set()
    for group in count_data.get("adverts", []) or []:
        for item in group.get("advert_list", []) or []:
            advert_id = item.get("advertId") or item.get("advert_id") or item.get("id")
            if advert_id is not None:
                ids.add(int(advert_id))
    return ids


def extract_current_ids(count_data: dict) -> set[int]:
    ids: set[int] = set()
    for group in count_data.get("adverts", []) or []:
        try:
            group_status = int(group.get("status"))
        except (TypeError, ValueError):
            continue

        if group_status not in CURRENT_CAMPAIGN_STATUSES:
            continue

        for item in group.get("advert_list", []) or []:
            advert_id = item.get("advertId") or item.get("advert_id") or item.get("id")
            if advert_id is not None:
                ids.add(int(advert_id))
    return ids


def count_active(count_data: dict) -> tuple[int, int]:
    total = int(count_data.get("all", 0) or 0)
    active = 0
    for group in count_data.get("adverts", []) or []:
        if int(group.get("status", 0) or 0) == 9:
            active += int(group.get("count", 0) or 0)
    return total, active


def parse_campaign(item: dict) -> tuple[int, str, int | None]:
    """Разбор ответа GET /api/advert/v2/adverts."""
    advert_id = item.get("id")
    if advert_id is None:
        raise ValueError("WB API V2: в данных кампании отсутствует поле id")

    settings = item.get("settings") or {}
    raw_name = settings.get("name")
    name = str(raw_name).strip() if raw_name is not None else ""
    if not name:
        name = f"Без названия (ID {advert_id})"

    status = item.get("status")
    return int(advert_id), name, int(status) if status is not None else None


async def save_event(text: str) -> None:
    # Храним последние события компактно в settings, без отдельной тяжёлой таблицы.
    raw = await get_setting("events", "[]")
    try:
        events = __import__("json").loads(raw or "[]")
    except Exception:
        events = []
    events.insert(0, {"at": datetime.now().strftime("%d.%m %H:%M"), "text": text})
    events = events[:30]
    await set_setting("events", __import__("json").dumps(events, ensure_ascii=False))


async def notify(bot: Bot, text: str) -> None:
    user_ids = await get_allowed_user_ids()
    if not user_ids:
        return

    for user_id in sorted(user_ids):
        try:
            await bot.send_message(user_id, text)
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление пользователю %s: %s", user_id, exc)

    await save_event(text.replace("<b>", "").replace("</b>", ""))


async def fetch_budget_safe(
    client: WBClient,
    session: aiohttp.ClientSession,
    advert_id: int,
) -> float | None:
    try:
        value = await client.budget(session, advert_id)
        await asyncio.sleep(0.75)  # дополнительный запас по лимитам WB API
        return value
    except Exception as exc:
        logger.warning("Budget error for %s: %s", advert_id, exc)
        return None


async def process_cabinet(bot: Bot, cabinet) -> tuple[int, int]:
    cabinet_id = cabinet["id"]
    cabinet_name = cabinet["seller_name"]
    token = decrypt_token(cabinet["token_enc"])
    client = WBClient(token)

    async with aiohttp.ClientSession() as session:
        details = await client.current_campaigns(session)
        parsed: dict[int, tuple[str, int | None]] = {}
        for item in details:
            try:
                advert_id, name, status = parse_campaign(item)
                if status not in CURRENT_CAMPAIGN_STATUSES:
                    continue
                parsed[advert_id] = (name, status)
            except Exception as exc:
                logger.warning("Bad V2 campaign row: %s | %s", item, exc)

        current_ids = set(parsed)
        existing_rows = await db_fetchall("SELECT * FROM campaigns WHERE cabinet_id = ?", (cabinet_id,))
        existing = {int(row["advert_id"]): row for row in existing_rows}
        first_sync = not bool(cabinet["initialized"])

        missing_ids = sorted(set(existing) - current_ids)
        missing_details = {}
        if missing_ids:
            try:
                rows = await client.campaigns_by_ids(session, missing_ids)
                for item in rows:
                    try:
                        aid, nm, st = parse_campaign(item)
                        missing_details[aid] = (nm, st)
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Не удалось уточнить статусы исчезнувших кампаний: %s", exc)

        for advert_id in missing_ids:
            old = existing[advert_id]
            old_name = old["name"] or f"Кампания {advert_id}"
            name, new_status = missing_details.get(advert_id, (old_name, None))
            if new_status in (7, 8, -1):
                title = "⛔ <b>Кампания завершена</b>" if new_status == 7 else ("❌ <b>Кампания отменена</b>" if new_status == 8 else "🗑 <b>Кампания удалена</b>")
                await notify(bot, f"{title}\nID кампании: <code>{advert_id}</code>\nНазвание: <b>{name}</b>\nКабинет: <b>{cabinet_name}</b>")
            await db_execute("DELETE FROM campaigns WHERE cabinet_id=? AND advert_id=?", (cabinet_id, advert_id))

        for advert_id, (name, status) in parsed.items():
            old = existing.get(advert_id)
            is_new = old is None
            need_budget = status == 9 or is_new or (old is not None and old["status"] != status and status == 11)
            budget = await fetch_budget_safe(client, session, advert_id) if need_budget else (float(old["last_budget"]) if old and old["last_budget"] is not None else None)

            if is_new:
                await db_execute("""INSERT INTO campaigns(cabinet_id, advert_id, name, status, last_budget, warning_300_sent) VALUES (?, ?, ?, ?, ?, 0)""", (cabinet_id, advert_id, name, status, budget))
                if not first_sync:
                    budget_text = f"{budget:.0f} ₽" if budget is not None else "не удалось получить"
                    await notify(bot, "🆕 <b>Новая рекламная кампания</b>\n" f"ID кампании: <code>{advert_id}</code>\n" f"Название: <b>{name}</b>\n" f"Кабинет: <b>{cabinet_name}</b>\n" f"Статус: {STATUS_NAMES.get(status, status)}\n" f"Баланс: <b>{budget_text}</b>")
            else:
                old_status = old["status"]
                if old_status != status and status is not None:
                    budget_text = f"{budget:.0f} ₽" if budget is not None else "не удалось получить"
                    title = "⏸ <b>Кампания остановлена / на паузе</b>" if status == 11 else ("✅ <b>Кампания возобновлена</b>" if status == 9 and old_status != 9 else ("🟡 <b>Кампания готова к запуску</b>" if status == 4 else None))
                    if title:
                        await notify(bot, f"{title}\nID кампании: <code>{advert_id}</code>\nНазвание: <b>{name}</b>\nКабинет: <b>{cabinet_name}</b>\nБаланс: <b>{budget_text}</b>")
                await db_execute("""UPDATE campaigns SET name=?, status=?, last_budget=COALESCE(?, last_budget), updated_at=CURRENT_TIMESTAMP WHERE cabinet_id=? AND advert_id=?""", (name, status, budget, cabinet_id, advert_id))

            if status == 9 and budget is not None:
                row = await db_fetchone("SELECT warning_300_sent FROM campaigns WHERE cabinet_id=? AND advert_id=?", (cabinet_id, advert_id))
                warned_300 = bool(row["warning_300_sent"]) if row else False
                if budget <= 100:
                    await notify(bot, "🔴 <b>КРИТИЧЕСКИЙ БАЛАНС РЕКЛАМЫ</b>\n" f"ID кампании: <code>{advert_id}</code>\n" f"Название: <b>{name}</b>\n" f"Кабинет: <b>{cabinet_name}</b>\n" f"Баланс: <b>{budget:.0f} ₽</b>")
                    await db_execute("UPDATE campaigns SET warning_300_sent=1 WHERE cabinet_id=? AND advert_id=?", (cabinet_id, advert_id))
                elif budget <= 300:
                    if not warned_300:
                        await notify(bot, "🟡 <b>Баланс рекламы ниже 300 ₽</b>\n" f"ID кампании: <code>{advert_id}</code>\n" f"Название: <b>{name}</b>\n" f"Кабинет: <b>{cabinet_name}</b>\n" f"Баланс: <b>{budget:.0f} ₽</b>")
                        await db_execute("UPDATE campaigns SET warning_300_sent=1 WHERE cabinet_id=? AND advert_id=?", (cabinet_id, advert_id))
                elif warned_300:
                    await db_execute("UPDATE campaigns SET warning_300_sent=0 WHERE cabinet_id=? AND advert_id=?", (cabinet_id, advert_id))

        if first_sync:
            await db_execute("UPDATE cabinets SET initialized=1 WHERE id=?", (cabinet_id,))
        if cabinet["api_error_active"]:
            await db_execute("UPDATE cabinets SET api_error_active=0 WHERE id=?", (cabinet_id,))
            await notify(bot, "✅ <b>Связь с WB API восстановлена</b>\n" f"Кабинет: <b>{cabinet_name}</b>")

        return len(parsed), sum(1 for _, st in parsed.values() if st == 9)


async def check_all(
    bot: Bot,
    manual: bool = False,
) -> list[tuple[str, int, int, str | None]] | None:
    # Не допускаем параллельные проверки WB API.
    if check_lock.locked():
        if manual:
            return None
        # Фоновая проверка пропускается, если предыдущая ещё не закончилась.
        logger.info("Проверка WB уже выполняется — фоновый запуск пропущен.")
        return []

    async with check_lock:
        cabinets = await db_fetchall("SELECT * FROM cabinets ORDER BY id")
        results = []

        for index, cabinet in enumerate(cabinets):
            if index > 0:
                await asyncio.sleep(5)

            try:
                total, active = await process_cabinet(bot, cabinet)
                results.append((cabinet["seller_name"], total, active, None))
            except Exception as exc:
                logger.exception("Cabinet check failed: %s", cabinet["seller_name"])
                error = str(exc)
                results.append((cabinet["seller_name"], 0, 0, error))

                if not cabinet["api_error_active"]:
                    await db_execute(
                        "UPDATE cabinets SET api_error_active=1 WHERE id=?",
                        (cabinet["id"],),
                    )
                    await notify(
                        bot,
                        "⚠️ <b>Ошибка проверки WB API</b>\n"
                        f"Кабинет: <b>{cabinet['seller_name']}</b>\n"
                        f"Ошибка: <code>{error[:400]}</code>",
                    )

        return results


async def polling_loop(bot: Bot) -> None:
    # Небольшая задержка после старта процесса, чтобы бот успел запуститься.
    await asyncio.sleep(3)
    while True:
        try:
            await check_all(bot)
        except Exception:
            logger.exception("Global polling error")

        interval = int(await get_setting("check_interval", str(DEFAULT_CHECK_INTERVAL)) or DEFAULT_CHECK_INTERVAL)
        interval = max(1, min(interval, 1440))

        try:
            poll_wakeup.clear()
            await asyncio.wait_for(poll_wakeup.wait(), timeout=interval * 60)
        except asyncio.TimeoutError:
            pass


@router.message(Command("bind"))
async def cmd_bind(message: Message):
    if not message.from_user:
        return
    owner_id = await get_owner_id()
    if owner_id:
        if owner_id == message.from_user.id:
            await message.answer("✅ Бот уже привязан к вашему Telegram.", reply_markup=menu_keyboard())
        else:
            await message.answer("⛔ Этот бот уже привязан.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != BIND_CODE:
        await message.answer("Использование: <code>/bind ВАШ_КОД</code>")
        return

    await set_setting("owner_id", str(message.from_user.id))
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        "✅ <b>Бот привязан.</b>\nТеперь добавьте первый кабинет WB.",
        reply_markup=menu_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return
    allowed_ids = await get_allowed_user_ids()
    if not allowed_ids:
        await message.answer(
            "👋 Бот ещё не настроен.\n"
            "Введите <code>/bind КОД</code> или задайте ALLOWED_USER_IDS в переменных окружения."
        )
        return
    if message.from_user.id not in allowed_ids:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        "📣 <b>WB Ads Monitor</b>\n"
        "Контроль рекламных кампаний Wildberries.",
        reply_markup=menu_keyboard(),
    )


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot):
    if not await guard_message(message):
        return
    await send_status(message, bot)


async def send_status(target: Message | CallbackQuery, bot: Bot):
    cabinets = await db_fetchall("SELECT * FROM cabinets ORDER BY id")
    if not cabinets:
        text = "Кабинеты ещё не добавлены."
    else:
        lines = ["📊 <b>Состояние рекламы</b>"]
        for cabinet in cabinets:
            try:
                client = WBClient(decrypt_token(cabinet["token_enc"]))
                async with aiohttp.ClientSession() as session:
                    campaigns = await client.current_campaigns(session)
                current_total = 0
                active = 0
                for item in campaigns:
                    try:
                        _, _, status = parse_campaign(item)
                    except Exception:
                        continue
                    if status in CURRENT_CAMPAIGN_STATUSES:
                        current_total += 1
                        if status == 9:
                            active += 1
                lines.append(
                    f"\n🏢 <b>{cabinet['seller_name']}</b>\n"
                    f"Актуальных кампаний: <b>{current_total}</b>\n"
                    f"Запущено: <b>{active}</b>"
                )
            except Exception as exc:
                lines.append(
                    f"\n🏢 <b>{cabinet['seller_name']}</b>\n"
                    f"⚠️ Ошибка: <code>{str(exc)[:200]}</code>"
                )
        interval = await get_setting("check_interval", str(DEFAULT_CHECK_INTERVAL))
        lines.append(f"\n⏱ Проверка: каждые <b>{interval} мин.</b>")
        text = "\n".join(lines)

    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=back_keyboard())
    else:
        await target.answer(text, reply_markup=back_keyboard())


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "📣 <b>WB Ads Monitor</b>\nВыберите действие:",
            reply_markup=menu_keyboard(),
        )


@router.callback_query(F.data == "status")
async def cb_status(callback: CallbackQuery, bot: Bot):
    if not await guard_callback(callback):
        return
    await callback.answer()
    await send_status(callback, bot)


@router.callback_query(F.data == "check_now")
async def cb_check_now(callback: CallbackQuery, bot: Bot):
    if not await guard_callback(callback):
        return
    await callback.answer("Проверяю…")
    results = await check_all(bot, manual=True)
    if callback.message:
        if results is None:
            text = (
                "⏳ <b>Проверка уже выполняется</b>\n\n"
                "Дождитесь её завершения. Параллельные запросы к WB API отключены, "
                "чтобы не получать ошибку 429."
            )
        elif not results:
            text = "Кабинеты ещё не добавлены."
        else:
            lines = ["🔄 <b>Проверка завершена</b>"]
            for name, total, active, error in results:
                if error:
                    lines.append(f"\n🏢 <b>{name}</b>\n⚠️ {error[:200]}")
                else:
                    lines.append(
                        f"\n🏢 <b>{name}</b>\nАктуальных: <b>{total}</b> | Запущено: <b>{active}</b>"
                    )
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=back_keyboard())


@router.callback_query(F.data == "add_cabinet")
async def cb_add_cabinet(callback: CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
    count = await db_fetchone("SELECT COUNT(*) AS c FROM cabinets")
    if int(count["c"]) >= MAX_CABINETS:
        await callback.answer("Можно подключить максимум 2 кабинета.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AddCabinet.waiting_name)
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Добавление кабинета</b>\n\n"
            "Введите название кабинета вручную.\n"
            "Например: <code>ООО СПб-ТТ</code>",
            reply_markup=back_keyboard(),
        )


@router.message(AddCabinet.waiting_name)
async def receive_cabinet_name(message: Message, state: FSMContext):
    if not await guard_message(message):
        return

    cabinet_name = (message.text or "").strip()
    if len(cabinet_name) < 2:
        await message.answer("Введите название кабинета текстом, минимум 2 символа.")
        return

    await state.update_data(cabinet_name=cabinet_name)
    await state.set_state(AddCabinet.waiting_token)
    await message.answer(
        f"🏢 Кабинет: <b>{cabinet_name}</b>\n\n"
        "Теперь отправьте API-токен Wildberries категории «Продвижение».\n"
        "Сообщение с токеном бот постарается сразу удалить."
    )


@router.message(AddCabinet.waiting_token)
async def receive_cabinet_token(message: Message, state: FSMContext):
    if not await guard_message(message):
        return

    token = (message.text or "").strip()
    state_data = await state.get_data()
    seller_name = str(state_data.get("cabinet_name") or "WB кабинет").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if len(token) < 30:
        await message.answer("❌ Похоже, это не API-токен. Попробуйте ещё раз.")
        return

    status_msg = await message.answer("🔎 Проверяю доступ к Promotion API Wildberries…")

    try:
        client = WBClient(token)
        async with aiohttp.ClientSession() as session:
            # Проверяем только Promotion API. seller-info намеренно не вызываем,
            # чтобы не зависеть от 429 на common-api.
            await client.current_campaigns(session)

        # Для уникальности кабинета используем стабильный хэш токена.
        # Сам токен в открытом виде в БД не сохраняется.
        seller_sid = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
        trade_mark = ""

        current = await db_fetchone("SELECT * FROM cabinets WHERE seller_sid=?", (seller_sid,))
        if current:
            await db_execute(
                "UPDATE cabinets SET token_enc=?, seller_name=?, trade_mark=? WHERE seller_sid=?",
                (encrypt_token(token), seller_name, trade_mark, seller_sid),
            )
            text_msg = f"✅ API-токен кабинета <b>{seller_name}</b> обновлён."
        else:
            count = await db_fetchone("SELECT COUNT(*) AS c FROM cabinets")
            if int(count["c"]) >= MAX_CABINETS:
                raise RuntimeError("Уже подключено 2 кабинета.")

            await db_execute(
                """
                INSERT INTO cabinets(seller_sid, seller_name, trade_mark, token_enc)
                VALUES (?, ?, ?, ?)
                """,
                (seller_sid, seller_name, trade_mark, encrypt_token(token)),
            )
            text_msg = (
                f"✅ Кабинет <b>{seller_name}</b> добавлен.\n"
                "Сейчас существующие кампании будут синхронизированы без уведомлений «Новая кампания»."
            )

        await state.clear()
        await status_msg.edit_text(
            text_msg + "\n\n⏱ Первая полная проверка пройдёт по установленному интервалу.",
            reply_markup=menu_keyboard(),
        )

    except Exception as exc:
        await status_msg.edit_text(
            f"❌ Не удалось добавить кабинет:\n<code>{str(exc)[:400]}</code>\n\n"
            "Проверьте API-токен категории «Продвижение» и попробуйте ещё раз.",
            reply_markup=back_keyboard(),
        )


@router.callback_query(F.data == "cabinets")
async def cb_cabinets(callback: CallbackQuery):
    if not await guard_callback(callback):
        return
    await callback.answer()
    rows = await db_fetchall("SELECT * FROM cabinets ORDER BY id")
    if not rows:
        text = "🏢 Кабинеты не добавлены."
        kb = back_keyboard()
    else:
        lines = ["🏢 <b>Подключённые кабинеты</b>"]
        buttons = []
        for row in rows:
            mark = f" / {row['trade_mark']}" if row["trade_mark"] else ""
            lines.append(f"\n• <b>{row['seller_name']}</b>{mark}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"🗑 Удалить: {row['seller_name'][:25]}",
                    callback_data=f"delete_cabinet:{row['id']}",
                )
            ])
        buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        text = "\n".join(lines)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("delete_cabinet:"))
async def cb_delete_cabinet(callback: CallbackQuery):
    if not await guard_callback(callback):
        return
    cabinet_id = int(callback.data.split(":", 1)[1])
    row = await db_fetchone("SELECT * FROM cabinets WHERE id=?", (cabinet_id,))
    if not row:
        await callback.answer("Кабинет не найден.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete:{cabinet_id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data="cabinets"),
            ]
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Удалить кабинет <b>{row['seller_name']}</b> и его историю мониторинга?",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(callback: CallbackQuery):
    if not await guard_callback(callback):
        return
    cabinet_id = int(callback.data.split(":", 1)[1])
    row = await db_fetchone("SELECT seller_name FROM cabinets WHERE id=?", (cabinet_id,))
    if row:
        await db_execute("DELETE FROM campaigns WHERE cabinet_id=?", (cabinet_id,))
        await db_execute("DELETE FROM cabinets WHERE id=?", (cabinet_id,))
    await callback.answer("Удалено")
    if callback.message:
        await callback.message.edit_text(
            f"🗑 Кабинет <b>{row['seller_name'] if row else ''}</b> удалён.",
            reply_markup=menu_keyboard(),
        )


@router.callback_query(F.data == "interval")
async def cb_interval(callback: CallbackQuery, state: FSMContext):
    if not await guard_callback(callback):
        return
    current = await get_setting("check_interval", str(DEFAULT_CHECK_INTERVAL))
    await state.set_state(SetInterval.waiting_minutes)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "⏱ <b>Интервал проверки</b>\n"
            f"Сейчас: <b>{current} мин.</b>\n\n"
            "Введите новое значение в минутах (от 1 до 1440).\n"
            "Рекомендуемое значение — 15.",
            reply_markup=back_keyboard(),
        )


@router.message(SetInterval.waiting_minutes)
async def receive_interval(message: Message, state: FSMContext):
    if not await guard_message(message):
        return
    try:
        minutes = int((message.text or "").strip())
        if not 1 <= minutes <= 1440:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 1440.")
        return

    await set_setting("check_interval", str(minutes))
    await state.clear()
    poll_wakeup.set()
    await message.answer(
        f"✅ Интервал изменён: проверка каждые <b>{minutes} мин.</b>",
        reply_markup=menu_keyboard(),
    )


@router.callback_query(F.data == "events")
async def cb_events(callback: CallbackQuery):
    if not await guard_callback(callback):
        return
    await callback.answer()
    raw = await get_setting("events", "[]")
    try:
        events = __import__("json").loads(raw or "[]")
    except Exception:
        events = []

    if not events:
        text = "📋 Событий пока нет."
    else:
        lines = ["📋 <b>Последние события</b>"]
        for item in events[:10]:
            short = str(item.get("text", "")).replace("\n", " | ")
            if len(short) > 180:
                short = short[:177] + "…"
            lines.append(f"\n<b>{item.get('at', '')}</b> — {short}")
        text = "\n".join(lines)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())


async def setup_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="status", description="Количество кампаний"),
        BotCommand(command="bind", description="Привязать владельца"),
    ])


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    if not BIND_CODE:
        raise RuntimeError("Не задан BIND_CODE в .env")
    if not APP_SECRET or len(APP_SECRET) < 16:
        raise RuntimeError("Задайте APP_SECRET длиной минимум 16 символов")

    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await setup_commands(bot)
    background = asyncio.create_task(polling_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        background.cancel()
        try:
            await background
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
