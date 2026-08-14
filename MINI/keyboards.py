import datetime

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from config import WEEKDAYS_SHORT_RU, MONTHS_RU_GEN


def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_date_short(date_iso):
    try:
        d = datetime.date.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return date_iso
    return f"{d.day} {MONTHS_RU_GEN[d.month - 1][:3]}, {WEEKDAYS_SHORT_RU[d.weekday()].lower()}"


def fmt_date_full(date_iso):
    try:
        d = datetime.date.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return date_iso
    return f"{d.day} {MONTHS_RU_GEN[d.month - 1]}"


# ---------- client ----------

def client_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="🗓 Мои записи")],
    ], resize_keyboard=True)


def phone_request_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📞 Отправить номер", request_contact=True)],
        [KeyboardButton(text="⏭ Пропустить")],
    ], resize_keyboard=True)


def services_kb(services, currency):
    rows = [[InlineKeyboardButton(text=f"{s['name']} — {s['price']}{currency}", callback_data=f"svc:{s['id']}")]
            for s in services]
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="booking_cancel")])
    return ikb(rows)


def quick_slots_kb(slots):
    rows, row = [], []
    for date_iso, t in slots:
        row.append(InlineKeyboardButton(text=f"{fmt_date_short(date_iso)} {t}", callback_data=f"qtime:{date_iso}:{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="📅 Другое время", callback_data="calendar_show_full")])
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="booking_cancel")])
    return ikb(rows)


def calendar_kb(dates):
    rows, row = [], []
    for d in dates:
        row.append(InlineKeyboardButton(text=fmt_date_short(d), callback_data=f"day:{d}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="booking_cancel")])
    return ikb(rows)


def time_slots_kb(slots, date_iso):
    rows, row = [], []
    for t in slots:
        row.append(InlineKeyboardButton(text=t, callback_data=f"time:{t}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 К датам", callback_data="calendar_show_full")])
    return ikb(rows)


def confirm_booking_kb():
    return ikb([[InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_book"),
                 InlineKeyboardButton(text="❌ Отмена", callback_data="booking_cancel")]])


def my_appointments_list_kb(rows_data):
    rows = [[InlineKeyboardButton(text=label, callback_data=f"myapp_detail:{appointment_id}")]
            for appointment_id, label in rows_data]
    return ikb(rows)


def my_appointment_actions_kb(appointment_id, can_reschedule):
    rows = []
    if can_reschedule:
        rows.append([InlineKeyboardButton(text="🔁 Перенести", callback_data=f"resched_pick:{appointment_id}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"myapp_cancel:{appointment_id}")])
    rows.append([InlineKeyboardButton(text="🔙 К списку записей", callback_data="my_appointments_start")])
    return ikb(rows)


CANCEL_REASONS = [
    ("sick", "🤒 Заболел(а)"), ("busy", "⏰ Не успеваю"),
    ("changed_mind", "🤷 Передумал(а)"), ("expensive", "💸 Дорого"), ("other", "Другое"),
]


def cancel_reason_kb(appointment_id, back_cb):
    rows = [[InlineKeyboardButton(text=label, callback_data=f"cancel_reason:{appointment_id}:{code}")]
            for code, label in CANCEL_REASONS]
    rows.append([InlineKeyboardButton(text="🔙 Не отменять", callback_data=back_cb)])
    return ikb(rows)


def back_kb(target):
    return ikb([[InlineKeyboardButton(text="🔙 Назад", callback_data=target)]])


# ---------- admin: day view ----------

def day_view_kb(date_iso, appointments, day_counts, offset):
    rows = []
    for a in appointments:
        mark = "👻" if a["no_show"] else ("✅" if a["status"] == "done" else "•")
        label = f"{mark} {a['time']} {a['display_name']} — {a['service_name'] or '?'}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"adm_appt:{a['id']}")])
    if not appointments:
        rows.append([InlineKeyboardButton(text="— записей нет —", callback_data="ignore")])
    rows.append([InlineKeyboardButton(text="➕ Записать клиента", callback_data=f"aab_start:{date_iso}")])
    cancellable = [a for a in appointments if a["status"] == "active"]
    if cancellable:
        rows.append([InlineKeyboardButton(text=f"🚫 Отменить весь день ({len(cancellable)})",
                                           callback_data=f"adm_day_cancel:{date_iso}")])
    rows.append([
        InlineKeyboardButton(text="⬅️", callback_data=f"adm_day:{offset - 1}"),
        InlineKeyboardButton(text="Сегодня", callback_data="adm_day:0"),
        InlineKeyboardButton(text="➡️", callback_data=f"adm_day:{offset + 1}"),
    ])
    week = []
    base = datetime.date.fromisoformat(date_iso)
    for i in range(-base.weekday(), 7 - base.weekday()):
        d = base + datetime.timedelta(days=i)
        cnt = day_counts.get(d.isoformat(), 0)
        marker = "▪️" if d == base else ""
        week.append(InlineKeyboardButton(text=f"{marker}{WEEKDAYS_SHORT_RU[d.weekday()]}\n{cnt or '·'}",
                                          callback_data=f"adm_day:{offset + i}"))
    rows.append(week[:4])
    rows.append(week[4:])
    rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings")])
    return ikb(rows)


def appointment_detail_admin_kb(appointment_id, back_target, is_past, no_show):
    rows = [[InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"adm_appt_cancel:{appointment_id}")]]
    if is_past:
        rows.append([InlineKeyboardButton(text="✅ Клиент был" if no_show else "👻 Клиент не пришёл",
                                           callback_data=f"adm_appt_noshow:{appointment_id}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_target)])
    return ikb(rows)


def add_booking_service_kb(services, date_iso, currency):
    rows = [[InlineKeyboardButton(text=f"{s['name']} — {s['price']}{currency}", callback_data=f"aab_svc:{s['id']}")]
            for s in services]
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data=f"adm_day_iso:{date_iso}")])
    return ikb(rows)


def add_booking_time_kb(slots, date_iso):
    rows, row = [], []
    for t in slots:
        row.append(InlineKeyboardButton(text=t, callback_data=f"aab_time:{t}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not slots:
        rows.append([InlineKeyboardButton(text="— свободного времени нет —", callback_data="ignore")])
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data=f"adm_day_iso:{date_iso}")])
    return ikb(rows)


# ---------- admin: services ----------

def services_admin_kb(services):
    rows = []
    for s in services:
        mark = "✅" if s["is_active"] else "🚫"
        rows.append([InlineKeyboardButton(text=f"{mark} {s['name']} — {s['price']}", callback_data=f"svc_open:{s['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="svc_add")])
    rows.append([InlineKeyboardButton(text="🔙 Меню", callback_data="adm:menu")])
    return ikb(rows)


def service_detail_kb(service):
    mark = "🚫 Выключить" if service["is_active"] else "✅ Включить"
    return ikb([
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"svc_edit_name:{service['id']}"),
         InlineKeyboardButton(text="✏️ Цена", callback_data=f"svc_edit_price:{service['id']}")],
        [InlineKeyboardButton(text="✏️ Длительность", callback_data=f"svc_edit_dur:{service['id']}")],
        [InlineKeyboardButton(text=mark, callback_data=f"svc_toggle:{service['id']}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"svc_del:{service['id']}")],
        [InlineKeyboardButton(text="🔙 К услугам", callback_data="adm:services")],
    ])


# ---------- admin: schedule / vacations ----------

def schedule_admin_kb(schedule):
    rows = []
    for day in schedule:
        mark = "✅" if day["is_working"] else "🚫"
        label = f"{mark} {WEEKDAYS_SHORT_RU[day['weekday']]} {day['start_time']}-{day['end_time']}" if day["is_working"] \
            else f"{mark} {WEEKDAYS_SHORT_RU[day['weekday']]} выходной"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"sched_day:{day['weekday']}")])
    rows.append([InlineKeyboardButton(text="🏖 Отпуск (период дат)", callback_data="vacation_add")])
    rows.append([InlineKeyboardButton(text="🔙 Меню", callback_data="adm:menu")])
    return ikb(rows)


def schedule_day_kb(weekday, is_working):
    mark = "🚫 Сделать выходным" if is_working else "✅ Сделать рабочим"
    rows = [[InlineKeyboardButton(text=mark, callback_data=f"sched_toggle:{weekday}")]]
    if is_working:
        rows.append([InlineKeyboardButton(text="🕐 Начало", callback_data=f"sched_edit_start:{weekday}"),
                     InlineKeyboardButton(text="🕐 Конец", callback_data=f"sched_edit_end:{weekday}")])
    rows.append([InlineKeyboardButton(text="🔙 К расписанию", callback_data="adm:schedule")])
    return ikb(rows)


# ---------- admin: settings ----------

def settings_admin_kb(settings):
    payment_on = settings.get("payment_enabled") == "1"
    return ikb([
        [InlineKeyboardButton(text=f"🏢 Название: {settings.get('business_name')}", callback_data="settings_edit:business_name")],
        [InlineKeyboardButton(text=f"📍 Адрес: {settings.get('business_address') or 'не указан'}", callback_data="settings_edit:business_address")],
        [InlineKeyboardButton(text=f"☎️ Телефон: {settings.get('business_phone') or 'не указан'}", callback_data="settings_edit:business_phone")],
        [InlineKeyboardButton(text=f"⏱ Шаг сетки: {settings.get('slot_step')} мин", callback_data="settings_edit:slot_step")],
        [InlineKeyboardButton(text=f"☕ Буфер между записями: {settings.get('buffer_time')} мин", callback_data="settings_edit:buffer_time")],
        [InlineKeyboardButton(text=f"📆 Горизонт записи: {settings.get('booking_horizon_days')} дн.", callback_data="settings_edit:booking_horizon_days")],
        [InlineKeyboardButton(text=f"🚫 Отмена не позднее чем за: {settings.get('cancel_hours')} ч.", callback_data="settings_edit:cancel_hours")],
        [InlineKeyboardButton(text=f"🔔 Напоминание за: {settings.get('reminder_hours')} ч.", callback_data="settings_edit:reminder_hours")],
        [InlineKeyboardButton(text=f"💰 Валюта: {settings.get('currency')}", callback_data="settings_edit:currency")],
        [InlineKeyboardButton(text=f"💳 Оплата: {'✅ вкл' if payment_on else '🚫 выкл'}", callback_data="adm:payments")],
        [InlineKeyboardButton(text="🔙 Меню", callback_data="adm:menu")],
    ])


def payments_admin_kb(settings, token_present):
    enabled = settings.get("payment_enabled") == "1"
    full = settings.get("payment_mode") == "full"
    rows = [[InlineKeyboardButton(text=f"{'✅ Включена' if enabled else '🚫 Выключена'} — нажмите для переключения",
                                   callback_data="pay_toggle_enabled")]]
    if not token_present:
        rows.append([InlineKeyboardButton(text="⚠️ Не задан PAYMENT_PROVIDER_TOKEN в .env", callback_data="ignore")])
    rows.append([InlineKeyboardButton(text=f"Списывать: {'всю сумму' if full else 'предоплату'}", callback_data="pay_toggle_mode")])
    if not full:
        rows.append([InlineKeyboardButton(text=f"Размер предоплаты: {settings.get('payment_deposit_percent')}%",
                                           callback_data="settings_edit:payment_deposit_percent")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:settings")])
    return ikb(rows)


def admin_main_kb():
    return ikb([
        [InlineKeyboardButton(text="📅 Мой день", callback_data="adm:day")],
        [InlineKeyboardButton(text="🧰 Услуги", callback_data="adm:services"),
         InlineKeyboardButton(text="⏰ Время работы", callback_data="adm:schedule")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:settings")],
    ])


def setup_done_kb():
    return ikb([[InlineKeyboardButton(text="📅 Мой день", callback_data="adm:day")]])
