import json
import datetime
import aiosqlite

from config import now_local, today_local

DEFAULT_SETTINGS = {
    "business_name": "Моя запись",
    "business_address": "",
    "business_phone": "",
    "business_map_link": "",
    "currency": "₽",
    "text_welcome": "👋 Здравствуйте! Это бот записи {business_name}.\n\nВыберите время и записывайтесь — без звонков.",
    "text_rules": "ℹ️ Пожалуйста, приходите вовремя. Если планы изменились — отмените запись заранее, чтобы время могло достаться кому-то ещё.",
    "text_confirm": "✅ Готово! Вы записаны, ждём вас.",
    "text_cancelled": "❌ Запись отменена. Будем рады видеть вас в другой раз!",
    "text_reminder": "🔔 Напоминание: {user_name}, у вас запись {date} в {time} — {service_name} ({price}{currency}).",
    "text_payment_invite": "💳 Чтобы закрепить время, внесите предоплату:",
    "text_payment_thanks": "✅ Оплата получена, спасибо! Ваша запись закреплена.",
    "text_payment_title": "Предоплата за запись",
    "collect_phone_in_booking": "1",
    "slot_step": "30",
    "buffer_time": "0",
    "booking_horizon_days": "30",
    "cancel_hours": "2",
    "reminder_hours": "24",
    "payment_enabled": "0",
    "payment_mode": "deposit",
    "payment_deposit_percent": "20",
    "payment_currency_code": "RUB",
    "setup_done": "0",
}

# Same floor-not-just-type-check rule as the full bot: a bad value here drives a loop bound or
# step size directly, so an admin typo must not be able to freeze the event loop.
SETTINGS_INT_BOUNDS = {
    "slot_step": (1, 1440),
    "buffer_time": (0, 1440),
    "booking_horizon_days": (1, 365),
    "payment_deposit_percent": (1, 100),
}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._settings_cache: dict | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.init_db()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def init_db(self):
        c = self.conn
        await c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        await c.execute("""CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, phone TEXT DEFAULT '',
            created_at TEXT, visits_count INTEGER DEFAULT 0)""")
        await c.execute("""CREATE TABLE IF NOT EXISTS services(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL, duration_min INTEGER,
            description TEXT DEFAULT '', is_active INTEGER DEFAULT 1)""")
        await c.execute("""CREATE TABLE IF NOT EXISTS schedule(
            weekday INTEGER PRIMARY KEY, is_working INTEGER DEFAULT 1,
            start_time TEXT DEFAULT '09:00', end_time TEXT DEFAULT '18:00',
            break_start TEXT DEFAULT '', break_end TEXT DEFAULT '')""")
        await c.execute("""CREATE TABLE IF NOT EXISTS blocked_slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time_start TEXT, time_end TEXT,
            reason TEXT DEFAULT '')""")
        await c.execute("""CREATE TABLE IF NOT EXISTS appointments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_id INTEGER,
            date TEXT, time TEXT, duration_min INTEGER, price REAL, created_at TEXT,
            status TEXT DEFAULT 'active', client_name TEXT DEFAULT '', client_phone TEXT DEFAULT '',
            cancel_reason TEXT DEFAULT '', no_show INTEGER DEFAULT 0,
            paid_amount REAL DEFAULT 0, payment_charge_id TEXT DEFAULT '',
            reminder_sent INTEGER DEFAULT 0)""")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(date, status)")
        await c.execute("CREATE INDEX IF NOT EXISTS idx_appt_user ON appointments(user_id, status)")
        await c.commit()

        for weekday in range(7):
            await c.execute(
                "INSERT OR IGNORE INTO schedule(weekday, is_working, start_time, end_time) VALUES (?,?,?,?)",
                (weekday, 1 if weekday < 5 else 0, "09:00", "18:00"))
        await c.commit()

        row = await (await c.execute("SELECT COUNT(*) c FROM settings")).fetchone()
        if row["c"] == 0:
            for k, v in DEFAULT_SETTINGS.items():
                await c.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (k, v))
            await c.commit()
        else:
            existing = {r["key"] for r in await (await c.execute("SELECT key FROM settings")).fetchall()}
            for k, v in DEFAULT_SETTINGS.items():
                if k not in existing:
                    await c.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (k, v))
            await c.commit()

    # ---------- settings (cached, same pattern as the full bot) ----------
    async def get_all_settings(self):
        if self._settings_cache is None:
            rows = await (await self.conn.execute("SELECT key, value FROM settings")).fetchall()
            self._settings_cache = {r["key"]: r["value"] for r in rows}
        return self._settings_cache

    async def get_setting(self, key, default=None):
        settings = await self.get_all_settings()
        return settings.get(key, default)

    async def set_setting(self, key, value):
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        await self.conn.commit()
        if self._settings_cache is not None:
            self._settings_cache[key] = str(value)

    # ---------- users ----------
    async def upsert_user(self, tg_id, username, full_name):
        await self.conn.execute(
            "INSERT INTO users(tg_id, username, full_name, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
            (tg_id, username or "", full_name or "", now_local().isoformat()))
        await self.conn.commit()

    async def get_user(self, tg_id):
        return await (await self.conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))).fetchone()

    async def set_phone(self, tg_id, phone):
        await self.conn.execute("UPDATE users SET phone=? WHERE tg_id=?", (phone, tg_id))
        await self.conn.commit()

    async def increment_visits(self, tg_id):
        await self.conn.execute("UPDATE users SET visits_count = visits_count + 1 WHERE tg_id=?", (tg_id,))
        await self.conn.commit()

    # ---------- services ----------
    async def add_service(self, name, price, duration_min, description=""):
        cur = await self.conn.execute(
            "INSERT INTO services(name, price, duration_min, description) VALUES (?,?,?,?)",
            (name, price, duration_min, description))
        await self.conn.commit()
        return cur.lastrowid

    async def get_services(self, active_only=True):
        q = "SELECT * FROM services" + (" WHERE is_active=1" if active_only else "") + " ORDER BY id"
        return await (await self.conn.execute(q)).fetchall()

    async def get_service(self, service_id):
        return await (await self.conn.execute("SELECT * FROM services WHERE id=?", (service_id,))).fetchone()

    async def update_service(self, service_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE services SET {cols} WHERE id=?", (*fields.values(), service_id))
        await self.conn.commit()

    async def toggle_service(self, service_id):
        await self.conn.execute("UPDATE services SET is_active = 1 - is_active WHERE id=?", (service_id,))
        await self.conn.commit()

    async def delete_service(self, service_id):
        await self.conn.execute("DELETE FROM services WHERE id=?", (service_id,))
        await self.conn.commit()

    # ---------- schedule ----------
    async def get_schedule(self):
        return await (await self.conn.execute("SELECT * FROM schedule ORDER BY weekday")).fetchall()

    async def get_schedule_day(self, weekday):
        return await (await self.conn.execute("SELECT * FROM schedule WHERE weekday=?", (weekday,))).fetchone()

    async def update_schedule_day(self, weekday, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE schedule SET {cols} WHERE weekday=?", (*fields.values(), weekday))
        await self.conn.commit()

    # ---------- blocked slots (whole-day, used for vacations) ----------
    async def add_blocked_slot(self, date, time_start, time_end, reason=""):
        cur = await self.conn.execute(
            "INSERT INTO blocked_slots(date, time_start, time_end, reason) VALUES (?,?,?,?)",
            (date, time_start, time_end, reason))
        await self.conn.commit()
        return cur.lastrowid

    async def get_blocked_slots(self, date):
        return await (await self.conn.execute("SELECT * FROM blocked_slots WHERE date=?", (date,))).fetchall()

    async def get_upcoming_blocked_slots(self):
        today = today_local().isoformat()
        return await (await self.conn.execute(
            "SELECT * FROM blocked_slots WHERE date>=? ORDER BY date", (today,))).fetchall()

    async def delete_full_day_blocks_in_range(self, date_from, date_to):
        cur = await self.conn.execute(
            "DELETE FROM blocked_slots WHERE date>=? AND date<=? AND time_start='00:00' AND time_end='23:59'",
            (date_from, date_to))
        await self.conn.commit()
        return cur.rowcount

    # ---------- appointments ----------
    async def create_appointment(self, user_id, service_id, date, time, duration_min, price,
                                  status="active", client_name="", client_phone=""):
        cur = await self.conn.execute(
            "INSERT INTO appointments(user_id, service_id, date, time, duration_min, price, created_at, "
            "status, client_name, client_phone) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, service_id, date, time, duration_min, price, now_local().isoformat(),
             status, client_name, client_phone))
        await self.conn.commit()
        return cur.lastrowid

    async def get_appointment(self, appointment_id):
        return await (await self.conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,))).fetchone()

    async def get_occupied_appointments_by_date(self, date, exclude_id=None):
        """MINI has no multi-specialist capacity model — one resource, so 'occupied' is simply
        every active booking that day, exactly like the full bot's solo-master case."""
        q = "SELECT * FROM appointments WHERE date=? AND status='active'"
        params = [date]
        if exclude_id is not None:
            q += " AND id!=?"
            params.append(exclude_id)
        return await (await self.conn.execute(q, params)).fetchall()

    async def get_user_appointments(self, user_id, status="active"):
        return await (await self.conn.execute(
            "SELECT * FROM appointments WHERE user_id=? AND status=? ORDER BY date, time",
            (user_id, status))).fetchall()

    async def cancel_appointment(self, appointment_id, reason=""):
        await self.conn.execute("UPDATE appointments SET status='cancelled', cancel_reason=? WHERE id=?",
                                 (reason, appointment_id))
        await self.conn.commit()

    async def complete_appointment(self, appointment_id):
        await self.conn.execute("UPDATE appointments SET status='done' WHERE id=?", (appointment_id,))
        await self.conn.commit()

    async def toggle_no_show(self, appointment_id):
        a = await self.get_appointment(appointment_id)
        await self.conn.execute("UPDATE appointments SET no_show=? WHERE id=?", (1 - a["no_show"], appointment_id))
        await self.conn.commit()

    async def reschedule_appointment(self, appointment_id, date, time):
        await self.conn.execute("UPDATE appointments SET date=?, time=? WHERE id=?", (date, time, appointment_id))
        await self.conn.commit()

    async def mark_reminder_sent(self, appointment_id):
        await self.conn.execute("UPDATE appointments SET reminder_sent=1 WHERE id=?", (appointment_id,))
        await self.conn.commit()

    async def get_all_active_future_appointments(self):
        today = today_local().isoformat()
        return await (await self.conn.execute(
            "SELECT * FROM appointments WHERE status='active' AND date>=? ORDER BY date, time", (today,))).fetchall()

    async def get_past_active_appointments(self):
        today = today_local().isoformat()
        return await (await self.conn.execute(
            "SELECT * FROM appointments WHERE status='active' AND date<=? ORDER BY date, time", (today,))).fetchall()

    async def get_appointments_for_date_detailed(self, date):
        return await (await self.conn.execute(
            """SELECT a.*, s.name AS service_name,
                      COALESCE(NULLIF(a.client_name, ''), u.full_name, u.username, CAST(a.user_id AS TEXT)) AS display_name
               FROM appointments a
               LEFT JOIN services s ON s.id = a.service_id
               LEFT JOIN users u ON u.tg_id = a.user_id
               WHERE a.date=? AND a.status IN ('active','done')
               ORDER BY a.time""", (date,))).fetchall()

    async def get_day_counts(self, date_from, date_to):
        rows = await (await self.conn.execute(
            """SELECT date, COUNT(*) cnt FROM appointments
               WHERE date>=? AND date<=? AND status IN ('active','done') GROUP BY date""",
            (date_from, date_to))).fetchall()
        return {r["date"]: r["cnt"] for r in rows}

    async def mark_appointment_paid(self, appointment_id, amount, charge_id=""):
        await self.conn.execute("UPDATE appointments SET paid_amount=?, payment_charge_id=? WHERE id=?",
                                 (amount, charge_id, appointment_id))
        await self.conn.commit()
