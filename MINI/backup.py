import glob
import logging
import os
import time

from aiogram import Bot
from aiogram.types import FSInputFile

from config import ADMIN_IDS, BACKUP_DIR, BACKUP_KEEP_COUNT, DB_PATH
from database import Database

logger = logging.getLogger("bot.backup")

_DB_STEM = os.path.splitext(os.path.basename(DB_PATH))[0]


async def create_backup(db: Database) -> str:
    """Write a consistent snapshot of the live database to BACKUP_DIR and return its path.

    Uses SQLite's VACUUM INTO rather than a plain file copy: the source database is in
    WAL mode and stays open for writes the whole time, so copying the file bytes directly
    could grab it mid-write. VACUUM INTO produces a complete, valid database file without
    requiring exclusive access to the source.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"{_DB_STEM}_{timestamp}.db")
    await db.conn.execute("VACUUM INTO ?", (path,))
    return path


def prune_old_backups() -> None:
    """Keep only the BACKUP_KEEP_COUNT most recent backup files."""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{_DB_STEM}_*.db")))
    excess = len(files) - BACKUP_KEEP_COUNT
    for stale in files[:max(excess, 0)]:
        try:
            os.remove(stale)
        except OSError:
            logger.warning("Failed to remove old backup %s", stale, exc_info=True)


async def run_backup_job(bot: Bot, db: Database) -> None:
    try:
        path = await create_backup(db)
    except Exception:
        logger.error("Database backup failed", exc_info=True)
        return

    logger.info("Database backup created: %s", path)
    prune_old_backups()

    caption = f"💾 Резервная копия базы данных ({time.strftime('%Y-%m-%d %H:%M')})"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_document(admin_id, FSInputFile(path), caption=caption)
        except Exception:
            logger.warning("Failed to send backup to admin %s", admin_id, exc_info=True)
