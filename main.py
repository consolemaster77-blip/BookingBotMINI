import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from config import BOT_TOKEN, DB_PATH
from database import Database
from handlers import client_router, admin_router
from scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot.errors")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env на основе .env.example")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    db = Database(DB_PATH)
    await db.connect()

    dp.include_router(admin_router)
    dp.include_router(client_router)

    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        logger.exception("Unhandled error while processing update", exc_info=event.exception)
        update = event.update
        try:
            if update.message:
                await update.message.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз или отправьте /cancel.")
            elif update.callback_query:
                await update.callback_query.answer("⚠️ Ошибка, попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
        return True

    scheduler = setup_scheduler(bot, db)
    scheduler.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling")
        await dp.start_polling(bot, db=db)
    finally:
        scheduler.shutdown()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
