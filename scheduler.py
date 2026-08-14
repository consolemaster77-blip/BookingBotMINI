from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backup import run_backup_job
from config import BACKUP_INTERVAL_HOURS
from database import Database
from handlers import check_reminders, check_reviews_noop


def setup_scheduler(bot: Bot, db: Database) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=1, args=(bot, db), id="reminders")
    scheduler.add_job(check_reviews_noop, "interval", minutes=5, args=(bot, db), id="complete_past")
    scheduler.add_job(run_backup_job, "interval", hours=BACKUP_INTERVAL_HOURS, args=(bot, db), id="backup")
    return scheduler
