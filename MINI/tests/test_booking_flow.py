"""Golden-path booking, the concurrency race guard, single-resource capacity, and the core
FSM escape hatch — the same load-bearing behaviors the full bot's suite guards, scoped to
MINI's simpler single-service-per-client-tap flow."""
import asyncio

import handlers


async def _seed_service(db, duration=60, price=1000):
    return await db.add_service("Стрижка", price, duration)


async def _get_valid_date_and_slot(db, duration=60):
    dates = await handlers.get_allowed_dates(db)
    assert dates, "test schedule produced no bookable dates"
    for date_iso in dates:
        slots = await handlers.get_available_slots(db, date_iso, duration)
        if slots:
            return date_iso, slots[0]
    raise AssertionError("no bookable slot found")


async def _advance_to_confirm_screen(feed_update, make_callback, make_message, user_id, service_id, date_iso, time_str):
    await feed_update(make_callback(user_id, "book_start"))
    await feed_update(make_callback(user_id, f"svc:{service_id}"))
    await feed_update(make_callback(user_id, "calendar_show_full"))
    await feed_update(make_callback(user_id, f"day:{date_iso}"))
    await feed_update(make_callback(user_id, f"time:{time_str}"))
    await feed_update(make_message(user_id, "пропустить"))


async def test_golden_path_booking_creates_active_appointment(feed_update, make_callback, make_message, db):
    service_id = await _seed_service(db)
    date_iso, time_str = await _get_valid_date_and_slot(db)
    uid = 4001

    await feed_update(make_message(uid, "/start"))
    await _advance_to_confirm_screen(feed_update, make_callback, make_message, uid, service_id, date_iso, time_str)
    await feed_update(make_callback(uid, "confirm_book"))

    appts = await db.get_user_appointments(uid)
    assert len(appts) == 1
    assert appts[0]["date"] == date_iso
    assert appts[0]["time"] == time_str
    # MINI has no admin-approval gate — a booking is active the moment it's confirmed.
    assert appts[0]["status"] == "active"


async def test_concurrent_double_booking_creates_only_one_appointment(feed_update, make_callback, make_message, db):
    service_id = await _seed_service(db)
    date_iso, time_str = await _get_valid_date_and_slot(db)
    client_a, client_b = 4002, 4003

    for uid in (client_a, client_b):
        await feed_update(make_message(uid, "/start"))
        await _advance_to_confirm_screen(feed_update, make_callback, make_message, uid, service_id, date_iso, time_str)

    await asyncio.gather(
        feed_update(make_callback(client_a, "confirm_book")),
        feed_update(make_callback(client_b, "confirm_book")),
    )

    total = len(await db.get_user_appointments(client_a)) + len(await db.get_user_appointments(client_b))
    assert total == 1, "the booking lock must serialize the check-then-write, or both clients can win the race"


async def test_single_resource_means_one_booking_closes_the_slot(feed_update, make_callback, make_message, db):
    """MINI has no multi-specialist capacity model on purpose — one booking must fully occupy
    the slot for every other client, unlike the full bot's parallel-places math."""
    service_id = await _seed_service(db)
    date_iso, time_str = await _get_valid_date_and_slot(db)
    await db.create_appointment(9999, service_id, date_iso, time_str, 60, 1000, status="active")

    assert time_str not in await handlers.get_available_slots(db, date_iso, 60)


async def test_cancel_command_clears_stuck_state(feed_update, make_callback, make_message, db):
    service_id = await _seed_service(db)
    date_iso, time_str = await _get_valid_date_and_slot(db)
    uid = 4004
    await feed_update(make_message(uid, "/start"))
    await feed_update(make_callback(uid, "book_start"))
    await feed_update(make_callback(uid, f"svc:{service_id}"))
    await feed_update(make_callback(uid, "calendar_show_full"))
    await feed_update(make_callback(uid, f"day:{date_iso}"))
    await feed_update(make_callback(uid, f"time:{time_str}"))  # now waiting in Form.booking_phone

    await feed_update(make_message(uid, "/cancel"))
    await feed_update(make_message(uid, "📅 Записаться"))  # must work again, not be swallowed as a phone number

    appts = await db.get_user_appointments(uid)
    assert not appts, "no accidental booking should have been created via the stuck state"
