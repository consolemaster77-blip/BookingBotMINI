"""Cancel-with-reason, reschedule, and the quick-slots screen from the client side."""
import handlers
from tests.conftest import bookable_date_iso


async def _seed_and_book(db, user_id, date_iso=None):
    service_id = await db.add_service("Стрижка", 1000, 60)
    if date_iso is None:
        date_iso = await bookable_date_iso(db)
    slots = await handlers.get_available_slots(db, date_iso, 60)
    appointment_id = await db.create_appointment(user_id, service_id, date_iso, slots[0], 60, 1000, status="active")
    return service_id, appointment_id, date_iso, slots[0]


async def test_single_service_shows_quick_slots_not_service_choice(feed_update, make_callback, db, session):
    await db.add_service("Стрижка", 1000, 60)
    uid = 6001
    await feed_update(make_callback(uid, "book_start"))

    msgs = session.calls_named("SendMessage") + session.calls_named("EditMessageText")
    assert any("Ближайшее свободное время" in (m.text or "") for m in msgs)
    assert not any((m.text or "").startswith("Выберите услугу") for m in msgs)


async def test_two_services_show_the_choice_screen(feed_update, make_callback, db, session):
    await db.add_service("Стрижка", 1000, 60)
    await db.add_service("Окрашивание", 3000, 90)
    uid = 6002
    await feed_update(make_callback(uid, "book_start"))

    msgs = session.calls_named("SendMessage")
    assert any((m.text or "").startswith("Выберите услугу") for m in msgs)


async def test_cancel_asks_for_a_reason_before_writing_it(feed_update, make_callback, db):
    uid = 6003
    _, appointment_id, _, _ = await _seed_and_book(db, uid)

    await feed_update(make_callback(uid, f"myapp_cancel:{appointment_id}"))
    assert (await db.get_appointment(appointment_id))["status"] == "active"

    await feed_update(make_callback(uid, f"cancel_reason:{appointment_id}:sick"))
    a = await db.get_appointment(appointment_id)
    assert a["status"] == "cancelled"
    assert a["cancel_reason"] == "🤒 Заболел(а)"


async def test_reschedule_moves_the_appointment_to_a_new_slot(feed_update, make_callback, db):
    uid = 6004
    service_id, appointment_id, old_date, old_time = await _seed_and_book(db, uid)

    await feed_update(make_callback(uid, f"resched_pick:{appointment_id}"))
    await feed_update(make_callback(uid, "calendar_show_full"))
    new_date = None
    for candidate in await handlers.get_allowed_dates(db):
        if candidate != old_date and await handlers.get_available_slots(db, candidate, 60):
            new_date = candidate
            break
    assert new_date, "no other bookable date found — test schedule/defaults changed"
    await feed_update(make_callback(uid, f"day:{new_date}"))
    slots = await handlers.get_available_slots(db, new_date, 60)
    await feed_update(make_callback(uid, f"time:{slots[0]}"))

    a = await db.get_appointment(appointment_id)
    assert a["date"] == new_date
    assert a["time"] == slots[0]
    assert a["status"] == "active"


async def test_my_appointments_lists_bookings_with_detail_links(feed_update, make_callback, make_message, db, session):
    uid = 6005
    await db.upsert_user(uid, "u", "Test User")
    _, appointment_id, _, _ = await _seed_and_book(db, uid)

    await feed_update(make_message(uid, "🗓 Мои записи"))

    msgs = [m for m in session.calls_named("SendMessage") if m.reply_markup is not None]
    buttons = [b for row in msgs[-1].reply_markup.inline_keyboard for b in row]
    assert any(b.callback_data == f"myapp_detail:{appointment_id}" for b in buttons)


async def test_empty_my_appointments_does_not_crash(feed_update, make_message, db, session):
    uid = 6006
    await feed_update(make_message(uid, "🗓 Мои записи"))
    texts = [m.text for m in session.calls_named("SendMessage")]
    assert any("нет активных записей" in (t or "") for t in texts)
