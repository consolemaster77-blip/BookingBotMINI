"""Admin day view, manual booking, whole-day cancel, no-show, first-run wizard."""
import datetime

import handlers
from config import today_local


async def _seed(db):
    service_id = await db.add_service("Стрижка", 1000, 60)
    await db.set_setting("setup_done", "1")
    return service_id


async def _first_bookable(db, duration=60):
    for date_iso in await handlers.get_allowed_dates(db):
        slots = await handlers.get_available_slots(db, date_iso, duration)
        if slots:
            return date_iso, slots
    raise AssertionError("no bookable date found")


async def test_admin_command_opens_day_view(feed_update, make_message, db, session, admin_id):
    await _seed(db)
    await feed_update(make_message(admin_id, "/admin"))
    texts = [m.text for m in session.calls_named("SendMessage")]
    assert any("Записей:" in (t or "") for t in texts)


async def test_first_run_wizard_configures_a_bookable_bot(feed_update, make_message, db, admin_id):
    assert not await db.get_services(active_only=False)
    await feed_update(make_message(admin_id, "/admin"))
    await feed_update(make_message(admin_id, "Мастер Аня"))
    await feed_update(make_message(admin_id, "Стрижка"))
    await feed_update(make_message(admin_id, "1500"))
    await feed_update(make_message(admin_id, "60"))

    assert await db.get_setting("business_name") == "Мастер Аня"
    assert await db.get_setting("setup_done") == "1"
    services = await db.get_services()
    assert len(services) == 1 and services[0]["price"] == 1500
    assert not await handlers.needs_setup(db)


async def test_manual_booking_creates_appointment_without_telegram_user(feed_update, make_callback, make_message, db, admin_id):
    service_id = await _seed(db)
    date_iso, slots = await _first_bookable(db)

    await feed_update(make_callback(admin_id, f"aab_start:{date_iso}"))
    await feed_update(make_callback(admin_id, f"aab_svc:{service_id}"))
    await feed_update(make_callback(admin_id, f"aab_time:{slots[0]}"))
    await feed_update(make_message(admin_id, "Мария Петрова"))
    await feed_update(make_message(admin_id, "+7 900 000-00-00"))

    appts = await db.get_appointments_for_date_detailed(date_iso)
    manual = [a for a in appts if a["client_name"] == "Мария Петрова"]
    assert len(manual) == 1
    assert manual[0]["user_id"] == 0
    assert manual[0]["client_phone"] == "+7 900 000-00-00"


async def test_manual_booking_occupies_the_slot(feed_update, make_callback, make_message, db, admin_id):
    service_id = await _seed(db)
    date_iso, slots = await _first_bookable(db)
    target = slots[0]

    await feed_update(make_callback(admin_id, f"aab_start:{date_iso}"))
    await feed_update(make_callback(admin_id, f"aab_svc:{service_id}"))
    await feed_update(make_callback(admin_id, f"aab_time:{target}"))
    await feed_update(make_message(admin_id, "Телефонный клиент"))
    await feed_update(make_message(admin_id, "-"))

    assert target not in await handlers.get_available_slots(db, date_iso, 60)


async def test_no_show_toggle_marks_and_completes(feed_update, make_callback, db, admin_id):
    service_id = await _seed(db)
    appointment_id = await db.create_appointment(0, service_id, "2026-01-05", "10:00", 60, 1000,
                                                  status="active", client_name="Клиент")
    await feed_update(make_callback(admin_id, f"adm_appt_noshow:{appointment_id}"))
    a = await db.get_appointment(appointment_id)
    assert a["no_show"] == 1
    assert a["status"] == "done"


async def test_whole_day_cancel_cancels_every_active_booking(feed_update, make_callback, db, admin_id):
    service_id = await _seed(db)
    date_iso = "2026-09-20"
    a1 = await db.create_appointment(5001, service_id, date_iso, "10:00", 60, 1000, status="active")
    a2 = await db.create_appointment(0, service_id, date_iso, "12:00", 60, 1000, status="active", client_name="Walk-in")

    await feed_update(make_callback(admin_id, f"adm_day_cancel:{date_iso}"))
    await feed_update(make_callback(admin_id, f"adm_day_cancel_do:{date_iso}"))

    assert (await db.get_appointment(a1))["status"] == "cancelled"
    assert (await db.get_appointment(a2))["status"] == "cancelled"


async def test_service_crud_via_admin_panel(feed_update, make_callback, make_message, db, admin_id):
    await db.set_setting("setup_done", "1")
    await feed_update(make_callback(admin_id, "adm:services"))
    await feed_update(make_callback(admin_id, "svc_add"))
    await feed_update(make_message(admin_id, "Маникюр"))
    await feed_update(make_message(admin_id, "1200"))
    await feed_update(make_message(admin_id, "90"))

    services = await db.get_services()
    assert len(services) == 1
    assert services[0]["name"] == "Маникюр" and services[0]["duration_min"] == 90


async def test_slot_step_zero_is_rejected_not_silently_frozen(feed_update, make_callback, make_message, db, admin_id):
    """Same production-outage class the full bot guards against: an admin typo in a setting
    that drives a while-loop step must not be able to hang the event loop forever."""
    await feed_update(make_callback(admin_id, "settings_edit:slot_step"))
    await feed_update(make_message(admin_id, "0"))
    assert await db.get_setting("slot_step") == "30"  # unchanged — rejected, not applied


async def test_vacation_blocks_every_day_in_range(feed_update, make_callback, make_message, db, admin_id):
    await _seed(db)
    start = today_local() + datetime.timedelta(days=5)
    end = start + datetime.timedelta(days=3)

    await feed_update(make_callback(admin_id, "vacation_add"))
    await feed_update(make_message(admin_id, start.isoformat()))
    await feed_update(make_message(admin_id, end.isoformat()))

    blocked_dates = {b["date"] for b in await db.get_upcoming_blocked_slots()}
    assert {(start + datetime.timedelta(days=i)).isoformat() for i in range(4)} <= blocked_dates
    assert not await handlers.get_available_slots(db, start.isoformat(), 60)
