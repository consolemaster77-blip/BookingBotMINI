import os
import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _int_env(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} в .env указан некорректно: '{raw}' (нужно целое число)")


# Missing/malformed .env values fail loudly in main.py at startup, not here — importing config.py
# (which every test does, indirectly, via handlers.py) must never require a real .env to exist.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

try:
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
except ValueError as e:
    raise RuntimeError(
        f"ADMIN_IDS в .env указан некорректно ({e}). Нужны числовые Telegram ID через запятую, "
        f"например: 123456789,987654321") from e

DB_PATH = os.getenv("DB_PATH", "bot.db")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
try:
    TZ = ZoneInfo(TIMEZONE)
except Exception as e:
    raise RuntimeError(f"TIMEZONE в .env указан некорректно: '{TIMEZONE}' ({e}).") from e

PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_INTERVAL_HOURS = _int_env("BACKUP_INTERVAL_HOURS", 24)
BACKUP_KEEP_COUNT = _int_env("BACKUP_KEEP_COUNT", 14)


def now_local() -> datetime.datetime:
    """Naive local datetime in the configured business TIMEZONE — the server may run in any TZ
    (e.g. UTC), so bare datetime.datetime.now() would be wrong for reminders/schedules."""
    return datetime.datetime.now(TZ).replace(tzinfo=None)


def today_local() -> datetime.date:
    return now_local().date()


WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря"]
