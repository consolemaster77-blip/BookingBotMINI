"""Telegram Payments: optional prepayment / full payment for a booking. Disabled unless BOTH
`payment_enabled` is on AND PAYMENT_PROVIDER_TOKEN is set in .env."""
import logging

from aiogram import Bot
from aiogram.types import LabeledPrice

from config import PAYMENT_PROVIDER_TOKEN
from database import Database

logger = logging.getLogger("bot.payments")

_MINOR_UNITS = 100


async def payments_active(db: Database) -> bool:
    return bool(PAYMENT_PROVIDER_TOKEN) and await db.get_setting("payment_enabled", "0") == "1"


async def compute_amount(db: Database, price: float) -> int:
    settings = await db.get_all_settings()
    if settings.get("payment_mode") == "full":
        share = 1.0
    else:
        try:
            share = max(0.0, min(100.0, float(settings.get("payment_deposit_percent", "20")))) / 100
        except ValueError:
            share = 0.2
    return int(round(price * share * _MINOR_UNITS))


async def send_invoice_for_appointment(bot: Bot, db: Database, chat_id: int, appointment_id: int,
                                        service_name: str, price: float) -> bool:
    if not await payments_active(db):
        return False
    amount = await compute_amount(db, price)
    if amount <= 0:
        return False
    settings = await db.get_all_settings()
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title=(settings.get("text_payment_title") or "Предоплата за запись")[:32],
            description=f"{service_name}"[:255],
            payload=f"appt:{appointment_id}",
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=settings.get("payment_currency_code", "RUB"),
            prices=[LabeledPrice(label=service_name[:32] or "Запись", amount=amount)],
        )
        return True
    except Exception:
        logger.warning("Failed to send invoice for appointment %s", appointment_id, exc_info=True)
        return False
