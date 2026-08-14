"""MINI — a deliberately small booking bot for a solo specialist. One resource (no multi-master
capacity math), booking is active immediately (no admin-approval gate), and the feature surface
is limited to what a one-person business actually uses day to day. Kept as a single file on
purpose — the whole point of MINI is that a buyer can read it end to end in one sitting."""

import asyncio
import datetime
import json
import logging

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardRemove, PreCheckoutQuery,
                            BufferedInputFile, InlineKeyboardButton)

import keyboards as kb
import payments
from database import Database
from config import ADMIN_IDS, PAYMENT_PROVIDER_TOKEN, TZ, now_local, today_local

logger = logging.getLogger("bot.actions")

client_router = Router()
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

# Serializes "check the slot is free" + "write the appointment" so two clients racing for the
# same slot can't both pass the check before either commits — same fix as the full bot's, because
# the race is a property of SQLite + asyncio, not of how many features are built on top of it.
_booking_lock = asyncio.Lock()


class Form(StatesGroup):
    admin_text_input = State()
    booking_phone = State()
    setup_wizard = State()


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def render_template(template: str, **kwargs) -> str:
    return (template or "").format_map(_SafeDict(**kwargs))


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "")


def t2m(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def m2t(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _parse_hhmm(text: str):
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


_SETTINGS_EDIT_TARGET = {
    "payment_deposit_percent": "adm:payments",
}


def _settings_edit_back_target(key: str) -> str:
    return _SETTINGS_EDIT_TARGET.get(key, "adm:settings")


async def _with_business_vars(text: str, db: Database, **extra) -> str:
    settings = await db.get_all_settings()
    return render_template(text, business_name=settings.get("business_name") or "",
                            business_address=settings.get("business_address") or "",
                            business_phone=settings.get("business_phone") or "", **extra)


async def send_media_text(target, text, reply_markup=None):
    try:
        await target.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await target.answer(_strip_html(text), reply_markup=reply_markup)


# ================= slot math (single resource — no multi-master capacity model) =================

async def get_allowed_dates(db: Database):
    settings = await db.get_all_settings()
    horizon = int(settings.get("booking_horizon_days", 30))
    schedule = await db.get_schedule()
    working_days = {r["weekday"] for r in schedule if r["is_working"]}
    today = today_local()
    return [(today + datetime.timedelta(days=i)).isoformat()
            for i in range(horizon + 1) if (today + datetime.timedelta(days=i)).weekday() in working_days]


async def get_available_slots(db: Database, date_iso: str, duration_needed: int, exclude_appointment_id=None):
    settings = await db.get_all_settings()
    step = int(settings.get("slot_step", 30))
    buffer_t = int(settings.get("buffer_time", 0))
    weekday = datetime.date.fromisoformat(date_iso).weekday()
    sched = await db.get_schedule_day(weekday)
    if not sched or not sched["is_working"]:
        return []
    start, end = t2m(sched["start_time"]), t2m(sched["end_time"])
    break_start = t2m(sched["break_start"]) if sched["break_start"] else None
    break_end = t2m(sched["break_end"]) if sched["break_end"] else None
    occupied = await db.get_occupied_appointments_by_date(date_iso, exclude_id=exclude_appointment_id)
    ranges = [(t2m(a["time"]), t2m(a["time"]) + a["duration_min"] + buffer_t) for a in occupied]
    blocked = await db.get_blocked_slots(date_iso)
    ranges += [(t2m(b["time_start"]), t2m(b["time_end"])) for b in blocked]
    now = now_local()
    is_today = datetime.date.fromisoformat(date_iso) == now.date()
    slots = []
    t = start
    while t + duration_needed <= end:
        end_t = t + duration_needed
        blocked_by_break = break_start is not None and t < break_end and end_t > break_start
        conflict = any(t < be and end_t > bs for bs, be in ranges)
        if not blocked_by_break and not conflict:
            if not is_today or t > now.hour * 60 + now.minute:
                slots.append(m2t(t))
        t += step
    return slots


async def _nearest_slots(db: Database, duration_min: int, limit: int = 6):
    results = []
    for date_iso in await get_allowed_dates(db):
        for t in await get_available_slots(db, date_iso, duration_min):
            results.append((date_iso, t))
            if len(results) >= limit:
                return results
    return results


# ================= client: entry / menu =================

async def send_welcome(message: Message, db: Database):
    settings = await db.get_all_settings()
    text = await _with_business_vars(settings.get("text_welcome") or "", db)
    await send_media_text(message, text, reply_markup=kb.client_main_kb())


@client_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await send_welcome(message, db)


@client_router.message(Command("cancel"))
async def client_cancel_cmd(message: Message, state: FSMContext, db: Database):
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.client_main_kb())


# ================= client: booking =================

async def start_booking(message: Message, state: FSMContext, db: Database, user_id=None):
    user_id = user_id or message.from_user.id
    services = await db.get_services()
    if not services:
        await message.answer("Пока нет доступных услуг для записи.")
        return
    await state.clear()
    if len(services) == 1:
        await enter_booking_for_service(message, state, db, services[0]["id"])
        return
    currency = await db.get_setting("currency", "₽")
    await message.answer("Выберите услугу:", reply_markup=kb.services_kb(services, currency))


async def enter_booking_for_service(target, state: FSMContext, db: Database, service_id, user_id=None):
    service = await db.get_service(service_id)
    if not service or not service["is_active"]:
        return
    await state.update_data(service_id=service_id)
    await proceed_to_calendar(target, state, db, user_id=user_id)


async def proceed_to_calendar(target, state: FSMContext, db: Database, user_id=None):
    data = await state.get_data()
    service = await db.get_service(data["service_id"])

    async def render(text, markup):
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)

    if not data.get("show_full_calendar"):
        slots = await _nearest_slots(db, service["duration_min"])
        if slots:
            await render("Ближайшее свободное время:", kb.quick_slots_kb(slots))
            return
    dates = await get_allowed_dates(db)
    # `get_allowed_dates` doesn't know about durations, so it still lists today even once
    # every slot in its working hours is in the past — tapping it then just shows "no slots".
    if dates and dates[0] == today_local().isoformat() and not await get_available_slots(db, dates[0], service["duration_min"]):
        dates = dates[1:]
    if not dates:
        await render("Нет доступных дат для записи.", None)
        return
    await render("Выберите дату:", kb.calendar_kb(dates))


@client_router.callback_query(F.data == "book_start")
async def book_start_cb(callback: CallbackQuery, state: FSMContext, db: Database):
    await start_booking(callback.message, state, db, callback.from_user.id)
    await callback.answer()


@client_router.message(F.text == "📅 Записаться")
async def book_start_reply(message: Message, state: FSMContext, db: Database):
    await start_booking(message, state, db)


@client_router.callback_query(F.data.startswith("svc:"))
async def choose_service(callback: CallbackQuery, state: FSMContext, db: Database):
    service_id = int(callback.data.split(":")[1])
    await enter_booking_for_service(callback, state, db, service_id, user_id=callback.from_user.id)
    await callback.answer()


@client_router.callback_query(F.data == "calendar_show_full")
async def show_full_calendar(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.update_data(show_full_calendar=True)
    await proceed_to_calendar(callback, state, db, user_id=callback.from_user.id)
    await callback.answer()


@client_router.callback_query(F.data.startswith("day:"))
async def choose_day(callback: CallbackQuery, state: FSMContext, db: Database):
    date_iso = callback.data.split(":", 1)[1]
    data = await state.get_data()
    service = await db.get_service(data["service_id"])
    slots = await get_available_slots(db, date_iso, service["duration_min"])
    await state.update_data(date=date_iso)
    if not slots:
        await callback.message.edit_text(f"На {kb.fmt_date_short(date_iso)} нет свободных слотов.",
                                          reply_markup=kb.back_kb("calendar_show_full"))
        await callback.answer()
        return
    await callback.message.edit_text("Выберите время:", reply_markup=kb.time_slots_kb(slots, date_iso))
    await callback.answer()


@client_router.callback_query(F.data.startswith("time:"))
async def choose_time(callback: CallbackQuery, state: FSMContext, db: Database):
    time_str = callback.data.split(":", 1)[1]
    await _apply_time_choice(callback, state, db, time_str)


@client_router.callback_query(F.data.startswith("qtime:"))
async def choose_quick_time(callback: CallbackQuery, state: FSMContext, db: Database):
    _, date_iso, time_str = callback.data.split(":", 2)
    await state.update_data(date=date_iso)
    await _apply_time_choice(callback, state, db, time_str)


async def _apply_time_choice(callback: CallbackQuery, state: FSMContext, db: Database, time_str: str):
    data = await state.get_data()
    if data.get("reschedule_id"):
        await _resched_apply(callback, state, db, time_str)
        return
    await state.update_data(time=time_str)
    collect_phone = await db.get_setting("collect_phone_in_booking", "1") == "1"
    user = await db.get_user(callback.from_user.id)
    if collect_phone and not (user and user["phone"]):
        await state.set_state(Form.booking_phone)
        await callback.message.answer(
            "📞 Оставьте номер телефона, чтобы можно было связаться по записи (или «Пропустить»).\n\n"
            "<i>Передумали? Отправьте /cancel</i>", reply_markup=kb.phone_request_kb())
        await callback.answer()
        return
    await show_booking_summary(callback, state, db)


@client_router.message(Form.booking_phone, F.contact)
async def save_phone_contact(message: Message, state: FSMContext, db: Database):
    await db.set_phone(message.from_user.id, message.contact.phone_number)
    await message.answer("✅ Спасибо!", reply_markup=ReplyKeyboardRemove())
    await show_booking_summary(message, state, db)


@client_router.message(Form.booking_phone)
async def save_phone_or_skip(message: Message, state: FSMContext, db: Database):
    if message.text and message.text.strip() not in ("⏭ Пропустить", "/skip"):
        await db.set_phone(message.from_user.id, message.text.strip())
    await message.answer("Хорошо, продолжим.", reply_markup=ReplyKeyboardRemove())
    await show_booking_summary(message, state, db)


async def show_booking_summary(target, state: FSMContext, db: Database):
    data = await state.get_data()
    service = await db.get_service(data["service_id"])
    currency = await db.get_setting("currency", "₽")
    text = (f"<b>Проверьте запись:</b>\n\nУслуга: {service['name']}\n"
            f"Дата: {kb.fmt_date_full(data['date'])}\nВремя: {data['time']}\n"
            f"Стоимость: {service['price']}{currency}")
    await state.set_state(None)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=kb.confirm_booking_kb())
    else:
        await target.answer(text, reply_markup=kb.confirm_booking_kb())


@client_router.callback_query(F.data == "booking_cancel")
async def booking_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Запись отменена.")
    await callback.answer()


@client_router.callback_query(F.data == "confirm_book")
async def confirm_booking(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    service = await db.get_service(data["service_id"])
    async with _booking_lock:
        slots = await get_available_slots(db, data["date"], service["duration_min"])
        if data["time"] not in slots:
            await callback.answer("Увы, это время уже заняли. Выберите другое.", show_alert=True)
            await state.clear()
            return
        appointment_id = await db.create_appointment(
            callback.from_user.id, data["service_id"], data["date"], data["time"],
            service["duration_min"], service["price"], status="active")
    await db.increment_visits(callback.from_user.id)
    await state.clear()
    logger.info("Appointment %s created by user %s (%s %s)", appointment_id, callback.from_user.id, data["date"], data["time"])
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(
                admin_id, f"🆕 Новая запись: {service['name']} — {kb.fmt_date_full(data['date'])} в {data['time']}")
        except Exception:
            logger.warning("Failed to notify admin %s about new booking", admin_id, exc_info=True)
    settings = await db.get_all_settings()
    text = await _with_business_vars(settings.get("text_confirm") or "", db)
    await callback.message.edit_text(text)
    await callback.message.answer("👇 Что дальше:", reply_markup=kb.client_main_kb())
    if not PAYMENT_PROVIDER_TOKEN or await db.get_setting("payment_enabled", "0") != "1":
        pass
    elif await payments.payments_active(db):
        invite = await db.get_setting("text_payment_invite")
        if await payments.send_invoice_for_appointment(callback.bot, db, callback.from_user.id,
                                                        appointment_id, service["name"], service["price"]):
            try:
                await callback.bot.send_message(callback.from_user.id, invite)
            except Exception:
                logger.warning("Failed to send payment invite to %s", callback.from_user.id, exc_info=True)
    await callback.answer()


@client_router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@client_router.message(F.successful_payment)
async def process_successful_payment(message: Message, db: Database):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    appointment_id = None
    if payload.startswith("appt:"):
        try:
            appointment_id = int(payload.split(":", 1)[1])
        except ValueError:
            pass
    amount = sp.total_amount / 100
    if appointment_id:
        await db.mark_appointment_paid(appointment_id, amount, sp.provider_payment_charge_id or "")
    await message.answer(await db.get_setting("text_payment_thanks"))
    currency = await db.get_setting("currency", "₽")
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, f"💳 Оплата получена: {amount}{currency}")
        except Exception:
            logger.warning("Failed to notify admin %s about payment", admin_id, exc_info=True)


# ================= client: my appointments =================

async def my_appointments(message: Message, db: Database, user_id=None):
    user_id = user_id or message.from_user.id
    appointments = await db.get_user_appointments(user_id)
    if not appointments:
        await message.answer("У вас нет активных записей.")
        return
    currency = await db.get_setting("currency", "₽")
    lines, rows_data = [], []
    for a in appointments:
        service = await db.get_service(a["service_id"])
        service_name = service["name"] if service else "?"
        lines.append(f"📅 {kb.fmt_date_short(a['date'])} {a['time']} — {service_name} ({a['price']}{currency})")
        rows_data.append((a["id"], f"{kb.fmt_date_short(a['date'])} {a['time']} — {service_name}"))
    text = "\n".join(lines) + "\n\nВыберите запись, чтобы перенести или отменить её:"
    await message.answer(text, reply_markup=kb.my_appointments_list_kb(rows_data))


@client_router.callback_query(F.data == "my_appointments_start")
async def my_appointments_start_cb(callback: CallbackQuery, db: Database):
    await my_appointments(callback.message, db, callback.from_user.id)
    await callback.answer()


@client_router.message(F.text == "🗓 Мои записи")
async def my_appointments_reply(message: Message, db: Database):
    await my_appointments(message, db)


@client_router.callback_query(F.data.startswith("myapp_detail:"))
async def myapp_detail(callback: CallbackQuery, db: Database):
    appointment_id = int(callback.data.split(":")[1])
    a = await db.get_appointment(appointment_id)
    if not a or a["user_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    service = await db.get_service(a["service_id"])
    currency = await db.get_setting("currency", "₽")
    text = (f"📅 <b>{kb.fmt_date_full(a['date'])}, {a['time']}</b>\n"
            f"Услуга: {service['name'] if service else '?'}\nСумма: {a['price']}{currency}")
    await callback.message.edit_text(text, reply_markup=kb.my_appointment_actions_kb(appointment_id, a["status"] == "active"))
    await callback.answer()


async def _check_cancel_eligible(db: Database, user_id: int, appointment_id: int):
    a = await db.get_appointment(appointment_id)
    if not a or a["user_id"] != user_id:
        return None, "Запись не найдена."
    if a["status"] != "active":
        return None, "Эта запись уже неактивна."
    cancel_hours = float(await db.get_setting("cancel_hours", 2))
    dt = datetime.datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
    if (dt - now_local()).total_seconds() < cancel_hours * 3600:
        return None, f"Отмена возможна не позднее чем за {cancel_hours} ч. до визита."
    return a, None


@client_router.callback_query(F.data.startswith("myapp_cancel:"))
async def do_cancel_prompt(callback: CallbackQuery, db: Database):
    appointment_id = int(callback.data.split(":")[1])
    _, err = await _check_cancel_eligible(db, callback.from_user.id, appointment_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.message.edit_text("Почему отменяете?",
                                      reply_markup=kb.cancel_reason_kb(appointment_id, f"myapp_detail:{appointment_id}"))
    await callback.answer()


@client_router.callback_query(F.data.startswith("cancel_reason:"))
async def pick_cancel_reason(callback: CallbackQuery, db: Database):
    _, appointment_id_s, code = callback.data.split(":")
    appointment_id = int(appointment_id_s)
    a, err = await _check_cancel_eligible(db, callback.from_user.id, appointment_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    label = dict(kb.CANCEL_REASONS).get(code, code)
    await db.cancel_appointment(appointment_id, reason=label)
    logger.info("Appointment %s cancelled by client %s (reason=%s)", appointment_id, callback.from_user.id, label)
    settings = await db.get_all_settings()
    text = await _with_business_vars(settings.get("text_cancelled") or "", db)
    await callback.message.edit_text(text)
    await callback.answer("Запись отменена")


@client_router.callback_query(F.data.startswith("resched_pick:"))
async def resched_pick(callback: CallbackQuery, state: FSMContext, db: Database):
    appointment_id = int(callback.data.split(":")[1])
    a, err = await _check_cancel_eligible(db, callback.from_user.id, appointment_id)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await state.clear()
    await state.update_data(reschedule_id=appointment_id, service_id=a["service_id"])
    await proceed_to_calendar(callback, state, db, user_id=callback.from_user.id)
    await callback.answer()


async def _resched_apply(callback: CallbackQuery, state: FSMContext, db: Database, time_str: str):
    data = await state.get_data()
    appointment_id = data["reschedule_id"]
    a, err = await _check_cancel_eligible(db, callback.from_user.id, appointment_id)
    if err:
        await callback.answer(err, show_alert=True)
        await state.clear()
        return
    async with _booking_lock:
        a = await db.get_appointment(appointment_id)
        if not a or a["status"] != "active":
            await callback.answer("Эта запись уже недоступна для переноса.", show_alert=True)
            await state.clear()
            return
        slots = await get_available_slots(db, data["date"], a["duration_min"], exclude_appointment_id=appointment_id)
        if time_str not in slots:
            await callback.answer("Увы, это время уже заняли. Выберите другое.", show_alert=True)
            return
        await db.reschedule_appointment(appointment_id, data["date"], time_str)
    await state.clear()
    logger.info("Appointment %s rescheduled by client %s to %s %s", appointment_id, callback.from_user.id, data["date"], time_str)
    await callback.message.edit_text(f"✅ Запись перенесена на {kb.fmt_date_full(data['date'])} {time_str}.")
    await callback.answer()


# ================= reminders (scheduler entry point) =================

async def check_reminders(bot: Bot, db: Database):
    settings = await db.get_all_settings()
    threshold = float(settings.get("reminder_hours", 24))
    currency = settings.get("currency", "₽")
    tmpl = settings.get("text_reminder")
    now = now_local()
    for a in await db.get_all_active_future_appointments():
        if not a["user_id"] or a["reminder_sent"]:
            continue
        try:
            dt = datetime.datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        remaining_hours = (dt - now).total_seconds() / 3600
        if remaining_hours > threshold or remaining_hours < 0:
            continue
        service = await db.get_service(a["service_id"])
        user = await db.get_user(a["user_id"])
        text = render_template(tmpl, user_name=(user["full_name"] if user else "") or "",
                                date=a["date"], time=a["time"],
                                service_name=service["name"] if service else "", price=a["price"], currency=currency)
        try:
            await bot.send_message(a["user_id"], text)
        except Exception as e:
            logger.warning("Failed to send reminder to %s: %s", a["user_id"], e)
        await db.mark_reminder_sent(a["id"])


async def check_reviews_noop(bot: Bot, db: Database):
    """Closes out past active appointments as done — MINI has no review-collection feature,
    but analytics/no-show marking still need a 'this visit happened' signal."""
    today = today_local().isoformat()
    for a in await db.get_past_active_appointments():
        dt_end = datetime.datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M") + \
            datetime.timedelta(minutes=a["duration_min"] or 0)
        if now_local() >= dt_end:
            await db.complete_appointment(a["id"])


# ================= admin: setup wizard =================

async def needs_setup(db: Database) -> bool:
    if await db.get_setting("setup_done", "0") == "1":
        return False
    return not await db.get_services(active_only=False)


async def start_setup_wizard(message: Message, state: FSMContext, db: Database):
    await state.set_state(Form.setup_wizard)
    await state.update_data(step="business_name")
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\nНастроим бота за 4 шага — займёт минуту.\n"
        "В любой момент можно выйти командой /cancel.\n\n"
        "<b>Шаг 1 из 4.</b> Как вас представить клиентам? (имя или название)")


@admin_router.message(Form.setup_wizard)
async def setup_wizard_handler(message: Message, state: FSMContext, db: Database):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите ответ текстом.")
        return
    data = await state.get_data()
    step = data.get("step")

    if step == "business_name":
        await db.set_setting("business_name", text)
        await state.update_data(step="service_name")
        await message.answer("<b>Шаг 2 из 4.</b> Назовите вашу услугу (например, «Стрижка»):")
        return
    if step == "service_name":
        await state.update_data(step="service_price", wiz_service=text)
        await message.answer("<b>Шаг 3 из 4.</b> Сколько она стоит? Введите число:")
        return
    if step == "service_price":
        try:
            price = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Введите число, например 1500.")
            return
        if price < 0:
            await message.answer("Цена не может быть отрицательной.")
            return
        await state.update_data(step="service_duration", wiz_price=price)
        await message.answer("<b>Шаг 4 из 4.</b> Сколько минут она занимает?")
        return
    if step == "service_duration":
        try:
            duration = int(text)
        except ValueError:
            await message.answer("Введите целое число минут, например 60.")
            return
        if not (1 <= duration <= 1440):
            await message.answer("Длительность должна быть от 1 до 1440 минут.")
            return
        await db.add_service(data["wiz_service"], data["wiz_price"], duration)
        await db.set_setting("setup_done", "1")
        await state.clear()
        await message.answer(
            "✅ <b>Готово! Бот настроен и принимает записи.</b>\n\n"
            "Часы работы по умолчанию: 09:00–18:00, будни. Поменять — «⏰ Время работы».",
            reply_markup=kb.setup_done_kb())
        return
    await state.clear()
    await message.answer("Настройка прервана.", reply_markup=kb.admin_main_kb())


# ================= admin: day view / manual booking =================

async def _render_day(target, db: Database, offset: int):
    date = today_local() + datetime.timedelta(days=offset)
    date_iso = date.isoformat()
    appts = await db.get_appointments_for_date_detailed(date_iso)
    monday = date - datetime.timedelta(days=date.weekday())
    counts = await db.get_day_counts(monday.isoformat(), (monday + datetime.timedelta(days=6)).isoformat())
    currency = await db.get_setting("currency", "₽")
    revenue = sum(a["price"] for a in appts if a["status"] in ("active", "done"))
    when = "Сегодня" if offset == 0 else ("Завтра" if offset == 1 else "")
    header = f"📅 <b>{kb.fmt_date_full(date_iso)}</b>" + (f" — {when}" if when else "")
    text = f"{header}\n\nЗаписей: {len(appts)}   Сумма: {revenue}{currency}"
    markup = kb.day_view_kb(date_iso, appts, counts, offset)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext, db: Database):
    await state.clear()
    if await needs_setup(db):
        await start_setup_wizard(message, state, db)
        return
    await _render_day(message, db, 0)


@admin_router.message(Command("cancel"))
async def admin_cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.admin_main_kb())


@admin_router.callback_query(F.data == "adm:menu")
async def adm_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=kb.admin_main_kb())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:day")
async def adm_day_home(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    await _render_day(callback, db, 0)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_day:"))
async def adm_day_offset(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    await _render_day(callback, db, int(callback.data.split(":")[1]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_day_iso:"))
async def adm_day_iso(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    date = datetime.date.fromisoformat(callback.data.split(":", 1)[1])
    await _render_day(callback, db, (date - today_local()).days)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_appt:"))
async def adm_appt_detail(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    appointment_id = int(callback.data.split(":")[1])
    a = await db.get_appointment(appointment_id)
    if not a:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    service = await db.get_service(a["service_id"])
    currency = await db.get_setting("currency", "₽")
    who = a["client_name"] or a["display_name"]
    text = (f"📅 <b>{kb.fmt_date_full(a['date'])}, {a['time']}</b>\n"
            f"Клиент: {who}\nУслуга: {service['name'] if service else '?'}\nСумма: {a['price']}{currency}")
    if a["client_phone"]:
        text += f"\nТелефон: {a['client_phone']}"
    if a["status"] == "cancelled" and a["cancel_reason"]:
        text += f"\n\n❌ Причина отмены: {a['cancel_reason']}"
    if a["no_show"]:
        text += "\n\n👻 Отмечена неявка"
    started = f"{a['date']} {a['time']}" <= now_local().strftime("%Y-%m-%d %H:%M")
    await callback.message.edit_text(text, reply_markup=kb.appointment_detail_admin_kb(
        appointment_id, f"adm_day_iso:{a['date']}", started, bool(a["no_show"])))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_appt_cancel:"))
async def adm_appt_cancel(callback: CallbackQuery, db: Database):
    appointment_id = int(callback.data.split(":")[1])
    a = await db.get_appointment(appointment_id)
    if not a or a["status"] != "active":
        await callback.answer("Эта запись уже неактивна.", show_alert=True)
        return
    await db.cancel_appointment(appointment_id, reason="Отменено администратором")
    logger.info("Appointment %s cancelled by admin %s", appointment_id, callback.from_user.id)
    if a["user_id"]:
        try:
            settings = await db.get_all_settings()
            text = await _with_business_vars(settings.get("text_cancelled") or "", db)
            await callback.bot.send_message(a["user_id"], f"{text}\n\n(отменено администратором)")
        except Exception:
            logger.warning("Failed to notify client %s of admin cancellation", a["user_id"], exc_info=True)
    await _render_day(callback, db, (datetime.date.fromisoformat(a["date"]) - today_local()).days)
    await callback.answer("Отменено")


@admin_router.callback_query(F.data.startswith("adm_appt_noshow:"))
async def adm_appt_noshow(callback: CallbackQuery, db: Database):
    appointment_id = int(callback.data.split(":")[1])
    a = await db.get_appointment(appointment_id)
    if not a:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    await db.toggle_no_show(appointment_id)
    if not a["no_show"] and a["status"] == "active":
        await db.complete_appointment(appointment_id)
    await callback.answer()
    fresh = await db.get_appointment(appointment_id)
    service = await db.get_service(fresh["service_id"])
    currency = await db.get_setting("currency", "₽")
    text = (f"📅 <b>{kb.fmt_date_full(fresh['date'])}, {fresh['time']}</b>\n"
            f"Услуга: {service['name'] if service else '?'}\nСумма: {fresh['price']}{currency}")
    if fresh["no_show"]:
        text += "\n\n👻 Отмечена неявка"
    await callback.message.edit_text(text, reply_markup=kb.appointment_detail_admin_kb(
        appointment_id, f"adm_day_iso:{fresh['date']}", True, bool(fresh["no_show"])))


@admin_router.callback_query(F.data.startswith("adm_day_cancel:"))
async def adm_day_cancel_confirm(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    date_iso = callback.data.split(":", 1)[1]
    appts = await db.get_occupied_appointments_by_date(date_iso)
    if not appts:
        await callback.answer("На этот день записей уже нет.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🚫 Отменить все записи на {kb.fmt_date_full(date_iso)} ({len(appts)})?\n\nКлиенты получат уведомление.",
        reply_markup=kb.ikb([[InlineKeyboardButton(text="✅ Да", callback_data=f"adm_day_cancel_do:{date_iso}"),
                              InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_day_iso:{date_iso}")]]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_day_cancel_do:"))
async def adm_day_cancel_do(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    date_iso = callback.data.split(":", 1)[1]
    appts = await db.get_occupied_appointments_by_date(date_iso)
    settings = await db.get_all_settings()
    text_cancelled = await _with_business_vars(settings.get("text_cancelled") or "", db)
    for a in appts:
        await db.cancel_appointment(a["id"], reason="Отменено администратором (весь день)")
        if a["user_id"]:
            try:
                await callback.bot.send_message(a["user_id"], f"{text_cancelled}\n\n(день отменён администратором)")
            except Exception:
                logger.warning("Failed to notify client %s of whole-day cancellation", a["user_id"], exc_info=True)
    logger.info("Whole day %s cancelled by admin %s: %d appointments", date_iso, callback.from_user.id, len(appts))
    await _render_day(callback, db, (datetime.date.fromisoformat(date_iso) - today_local()).days)
    await callback.answer(f"Отменено записей: {len(appts)}")


@admin_router.callback_query(F.data.startswith("aab_start:"))
async def aab_start(callback: CallbackQuery, state: FSMContext, db: Database):
    date_iso = callback.data.split(":", 1)[1]
    services = await db.get_services()
    if not services:
        await callback.answer("Сначала добавьте хотя бы одну услугу.", show_alert=True)
        return
    await state.clear()
    await state.update_data(aab_date=date_iso)
    currency = await db.get_setting("currency", "₽")
    await callback.message.edit_text(f"➕ <b>Запись на {kb.fmt_date_full(date_iso)}</b>\n\nВыберите услугу:",
                                      reply_markup=kb.add_booking_service_kb(services, date_iso, currency))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("aab_svc:"))
async def aab_service(callback: CallbackQuery, state: FSMContext, db: Database):
    service_id = int(callback.data.split(":")[1])
    await state.update_data(aab_service_id=service_id)
    data = await state.get_data()
    service = await db.get_service(service_id)
    slots = await get_available_slots(db, data["aab_date"], service["duration_min"])
    await callback.message.edit_text(
        f"«{service['name']}» на {kb.fmt_date_full(data['aab_date'])}.\n\nВыберите время:",
        reply_markup=kb.add_booking_time_kb(slots, data["aab_date"]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("aab_time:"))
async def aab_time(callback: CallbackQuery, state: FSMContext, db: Database):
    time_str = callback.data.split(":", 1)[1]
    date_iso = (await state.get_data())["aab_date"]
    await state.update_data(aab_time=time_str)
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="aab_client_name")
    await callback.message.edit_text(f"Время {time_str}. Введите имя клиента:", reply_markup=kb.back_kb(f"adm_day_iso:{date_iso}"))
    await callback.answer()


async def _aab_create(message: Message, state: FSMContext, db: Database, name: str, phone: str):
    data = await state.get_data()
    service = await db.get_service(data["aab_service_id"])
    if not service:
        await state.clear()
        await message.answer("Услуга больше недоступна. Запись не создана.", reply_markup=kb.back_kb("adm:day"))
        return
    async with _booking_lock:
        slots = await get_available_slots(db, data["aab_date"], service["duration_min"])
        if data["aab_time"] not in slots:
            await state.clear()
            await message.answer("⚠️ Это время только что заняли. Запись не создана.", reply_markup=kb.back_kb("adm:day"))
            return
        appointment_id = await db.create_appointment(
            0, data["aab_service_id"], data["aab_date"], data["aab_time"], service["duration_min"],
            service["price"], status="active", client_name=name, client_phone=phone)
    await state.clear()
    logger.info("Manual appointment %s created by admin %s for %s (%s %s)",
                appointment_id, message.chat.id, name, data["aab_date"], data["aab_time"])
    await message.answer(f"✅ Записан: <b>{name}</b>\n{kb.fmt_date_full(data['aab_date'])} в {data['aab_time']}\n"
                          f"Услуга: {service['name']}", reply_markup=kb.back_kb("adm:day"))


# ================= admin: services =================

@admin_router.callback_query(F.data == "adm:services")
async def adm_services(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    services = await db.get_services(active_only=False)
    await callback.message.edit_text("🧰 <b>Услуги</b>", reply_markup=kb.services_admin_kb(services))
    await callback.answer()


@admin_router.callback_query(F.data == "svc_add")
async def svc_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="svc_add_name")
    await callback.message.edit_text("Введите название новой услуги:", reply_markup=kb.back_kb("adm:services"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_open:"))
async def svc_open(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    service_id = int(callback.data.split(":")[1])
    service = await db.get_service(service_id)
    if not service:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return
    text = f"<b>{service['name']}</b>\nЦена: {service['price']}\nДлительность: {service['duration_min']} мин"
    await callback.message.edit_text(text, reply_markup=kb.service_detail_kb(service))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_edit_name:"))
async def svc_edit_name(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split(":")[1])
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="svc_edit_name", target_id=service_id)
    await callback.message.edit_text("Введите новое название:", reply_markup=kb.back_kb(f"svc_open:{service_id}"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_edit_price:"))
async def svc_edit_price(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split(":")[1])
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="svc_edit_price", target_id=service_id)
    await callback.message.edit_text("Введите новую цену (число):", reply_markup=kb.back_kb(f"svc_open:{service_id}"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_edit_dur:"))
async def svc_edit_dur(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split(":")[1])
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="svc_edit_dur", target_id=service_id)
    await callback.message.edit_text("Введите новую длительность в минутах:", reply_markup=kb.back_kb(f"svc_open:{service_id}"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_toggle:"))
async def svc_toggle(callback: CallbackQuery, db: Database):
    service_id = int(callback.data.split(":")[1])
    await db.toggle_service(service_id)
    service = await db.get_service(service_id)
    await callback.message.edit_reply_markup(reply_markup=kb.service_detail_kb(service))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("svc_del:"))
async def svc_del(callback: CallbackQuery, db: Database):
    await db.delete_service(int(callback.data.split(":")[1]))
    services = await db.get_services(active_only=False)
    await callback.message.edit_text("🧰 <b>Услуги</b>", reply_markup=kb.services_admin_kb(services))
    await callback.answer("Удалено")


# ================= admin: schedule / vacations =================

@admin_router.callback_query(F.data == "adm:schedule")
async def adm_schedule(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    schedule = await db.get_schedule()
    await callback.message.edit_text("⏰ <b>Время работы</b>", reply_markup=kb.schedule_admin_kb(schedule))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sched_day:"))
async def sched_day(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    weekday = int(callback.data.split(":")[1])
    day = await db.get_schedule_day(weekday)
    text = f"<b>{['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'][weekday]}</b>\n"
    text += f"Рабочее время: {day['start_time']}-{day['end_time']}" if day["is_working"] else "Выходной"
    await callback.message.edit_text(text, reply_markup=kb.schedule_day_kb(weekday, day["is_working"]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sched_toggle:"))
async def sched_toggle(callback: CallbackQuery, db: Database):
    weekday = int(callback.data.split(":")[1])
    day = await db.get_schedule_day(weekday)
    await db.update_schedule_day(weekday, is_working=0 if day["is_working"] else 1)
    day = await db.get_schedule_day(weekday)
    await callback.message.edit_reply_markup(reply_markup=kb.schedule_day_kb(weekday, day["is_working"]))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sched_edit_start:"))
async def sched_edit_start(callback: CallbackQuery, state: FSMContext):
    weekday = int(callback.data.split(":")[1])
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="sched_start", target_id=weekday)
    await callback.message.edit_text("Введите время начала работы (ЧЧ:ММ):", reply_markup=kb.back_kb(f"sched_day:{weekday}"))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("sched_edit_end:"))
async def sched_edit_end(callback: CallbackQuery, state: FSMContext):
    weekday = int(callback.data.split(":")[1])
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="sched_end", target_id=weekday)
    await callback.message.edit_text("Введите время окончания работы (ЧЧ:ММ):", reply_markup=kb.back_kb(f"sched_day:{weekday}"))
    await callback.answer()


@admin_router.callback_query(F.data == "vacation_add")
async def vacation_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="vacation_from")
    await callback.message.edit_text("🏖 Введите первый день отпуска (ГГГГ-ММ-ДД):", reply_markup=kb.back_kb("adm:schedule"))
    await callback.answer()


# ================= admin: settings =================

@admin_router.callback_query(F.data == "adm:settings")
async def adm_settings(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    settings = await db.get_all_settings()
    await callback.message.edit_text("⚙️ <b>Настройки</b>", reply_markup=kb.settings_admin_kb(settings))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("settings_edit:"))
async def settings_edit(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    await state.set_state(Form.admin_text_input)
    await state.update_data(action="settings_edit", target_key=key)
    await callback.message.edit_text("Введите новое значение:", reply_markup=kb.back_kb(_settings_edit_back_target(key)))
    await callback.answer()


@admin_router.callback_query(F.data == "adm:payments")
async def adm_payments(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    settings = await db.get_all_settings()
    text = ("💳 <b>Оплата и предоплата</b>\n\nЧтобы включить, нужен токен провайдера: @BotFather → ваш бот → "
            "Payments → выберите провайдера → впишите токен в .env как PAYMENT_PROVIDER_TOKEN.")
    await callback.message.edit_text(text, reply_markup=kb.payments_admin_kb(settings, bool(PAYMENT_PROVIDER_TOKEN)))
    await callback.answer()


@admin_router.callback_query(F.data == "pay_toggle_enabled")
async def pay_toggle_enabled(callback: CallbackQuery, db: Database):
    current = await db.get_setting("payment_enabled", "0")
    await db.set_setting("payment_enabled", "0" if current == "1" else "1")
    settings = await db.get_all_settings()
    await callback.message.edit_reply_markup(reply_markup=kb.payments_admin_kb(settings, bool(PAYMENT_PROVIDER_TOKEN)))
    await callback.answer()


@admin_router.callback_query(F.data == "pay_toggle_mode")
async def pay_toggle_mode(callback: CallbackQuery, db: Database):
    current = await db.get_setting("payment_mode", "deposit")
    await db.set_setting("payment_mode", "deposit" if current == "full" else "full")
    settings = await db.get_all_settings()
    await callback.message.edit_reply_markup(reply_markup=kb.payments_admin_kb(settings, bool(PAYMENT_PROVIDER_TOKEN)))
    await callback.answer()


@admin_router.callback_query(F.data == "ignore")
async def ignore_cb(callback: CallbackQuery):
    await callback.answer()


# ================= admin: generic text-input dispatcher =================

_TEXT_ACTIONS = {}


def text_action(name):
    def register(fn):
        _TEXT_ACTIONS[name] = fn
        return fn
    return register


@text_action("svc_add_name")
async def _ta_svc_add_name(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    await state.update_data(action="svc_add_price", new_name=text)
    await message.answer("Введите цену:", reply_markup=kb.back_kb("adm:services"))


@text_action("svc_add_price")
async def _ta_svc_add_price(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        price = float(text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    if price < 0:
        await message.answer("Цена не может быть отрицательной.")
        return
    await state.update_data(action="svc_add_dur", new_price=price)
    await message.answer("Введите длительность в минутах:", reply_markup=kb.back_kb("adm:services"))


@text_action("svc_add_dur")
async def _ta_svc_add_dur(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        dur = int(text)
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if dur < 1:
        await message.answer("Длительность должна быть не менее 1 минуты.")
        return
    await db.add_service(data["new_name"], data["new_price"], dur)
    await state.clear()
    services = await db.get_services(active_only=False)
    await message.answer("✅ Услуга добавлена.", reply_markup=kb.services_admin_kb(services))


@text_action("svc_edit_name")
async def _ta_svc_edit_name(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    await db.update_service(data["target_id"], name=text)
    await state.clear()
    await message.answer("✅ Название обновлено.", reply_markup=kb.back_kb(f"svc_open:{data['target_id']}"))


@text_action("svc_edit_price")
async def _ta_svc_edit_price(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        price = float(text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число.")
        return
    if price < 0:
        await message.answer("Цена не может быть отрицательной.")
        return
    await db.update_service(data["target_id"], price=price)
    await state.clear()
    await message.answer("✅ Цена обновлена.", reply_markup=kb.back_kb(f"svc_open:{data['target_id']}"))


@text_action("svc_edit_dur")
async def _ta_svc_edit_dur(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        dur = int(text)
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if dur < 1:
        await message.answer("Длительность должна быть не менее 1 минуты.")
        return
    await db.update_service(data["target_id"], duration_min=dur)
    await state.clear()
    await message.answer("✅ Длительность обновлена.", reply_markup=kb.back_kb(f"svc_open:{data['target_id']}"))


@text_action("sched_start")
async def _ta_sched_start(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    parsed = _parse_hhmm(text)
    if not parsed:
        await message.answer("Формат ЧЧ:ММ, например 09:00.")
        return
    day = await db.get_schedule_day(data["target_id"])
    if day and day["end_time"] and parsed >= day["end_time"]:
        await message.answer(f"Время начала должно быть раньше окончания ({day['end_time']}).")
        return
    await db.update_schedule_day(data["target_id"], start_time=parsed)
    await state.clear()
    await message.answer("✅ Время начала обновлено.", reply_markup=kb.back_kb(f"sched_day:{data['target_id']}"))


@text_action("sched_end")
async def _ta_sched_end(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    parsed = _parse_hhmm(text)
    if not parsed:
        await message.answer("Формат ЧЧ:ММ, например 18:00.")
        return
    day = await db.get_schedule_day(data["target_id"])
    if day and day["start_time"] and parsed <= day["start_time"]:
        await message.answer(f"Время окончания должно быть позже начала ({day['start_time']}).")
        return
    await db.update_schedule_day(data["target_id"], end_time=parsed)
    await state.clear()
    await message.answer("✅ Время окончания обновлено.", reply_markup=kb.back_kb(f"sched_day:{data['target_id']}"))


@text_action("vacation_from")
async def _ta_vacation_from(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        await message.answer("Формат ГГГГ-ММ-ДД. Введите первый день:")
        return
    await state.update_data(action="vacation_to", vac_from=text)
    await message.answer("Введите последний день периода (ГГГГ-ММ-ДД):", reply_markup=kb.back_kb("adm:schedule"))


@text_action("vacation_to")
async def _ta_vacation_to(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    try:
        end = datetime.date.fromisoformat(text)
    except ValueError:
        await message.answer("Формат ГГГГ-ММ-ДД. Введите последний день периода:")
        return
    start = datetime.date.fromisoformat(data["vac_from"])
    if end < start:
        await message.answer("Последний день раньше первого. Введите последний день периода:")
        return
    if (end - start).days > 365:
        await message.answer("Период не может быть длиннее года.")
        return
    day, created = start, 0
    while day <= end:
        await db.add_blocked_slot(day.isoformat(), "00:00", "23:59", reason="Отпуск")
        created += 1
        day += datetime.timedelta(days=1)
    await state.clear()
    await message.answer(f"✅ Заблокировано дней: {created}.", reply_markup=kb.back_kb("adm:schedule"))


@text_action("aab_client_name")
async def _ta_aab_client_name(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    await state.update_data(action="aab_client_phone", aab_name=text)
    await message.answer("Введите телефон клиента (или «-», если не нужен):")


@text_action("aab_client_phone")
async def _ta_aab_client_phone(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    phone = "" if text == "-" else text
    await _aab_create(message, state, db, data["aab_name"], phone)


@text_action("settings_edit")
async def _ta_settings_edit(message: Message, state: FSMContext, db: Database, data: dict, text: str):
    from database import SETTINGS_INT_BOUNDS
    key = data["target_key"]
    if key in SETTINGS_INT_BOUNDS:
        lo, hi = SETTINGS_INT_BOUNDS[key]
        try:
            value = int(text)
        except ValueError:
            await message.answer("Введите целое число.")
            return
        if not (lo <= value <= hi):
            await message.answer(f"Введите число от {lo} до {hi}.")
            return
        text = str(value)
    elif key == "cancel_hours" or key == "reminder_hours":
        try:
            v = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Введите число.")
            return
        if v < 0:
            await message.answer("Число не может быть отрицательным.")
            return
    await db.set_setting(key, text)
    await state.clear()
    await message.answer("✅ Настройка обновлена.", reply_markup=kb.back_kb(_settings_edit_back_target(key)))


@admin_router.message(Form.admin_text_input)
async def admin_text_input_handler(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    handler = _TEXT_ACTIONS.get(data.get("action"))
    if handler is None:
        await state.clear()
        await message.answer("Не понял, к чему относится этот ответ.", reply_markup=kb.back_kb("adm:menu"))
        return
    if not message.text:
        await message.answer("Здесь нужен текст. Отправьте ответ сообщением или выйдите командой /cancel.")
        return
    await handler(message, state, db, data, message.text.strip())
