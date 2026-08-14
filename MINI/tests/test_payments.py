"""Payment gating and amount computation — mirrors the full bot's coverage since the module
was copied with the same design (inert without both a setting AND a provider token)."""
import pytest

import payments


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_PROVIDER_TOKEN", "TEST:PROVIDER-TOKEN")


async def test_payments_off_without_provider_token(db, monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_PROVIDER_TOKEN", "")
    await db.set_setting("payment_enabled", "1")
    assert await payments.payments_active(db) is False


async def test_payments_active_when_both_configured(db, with_token):
    await db.set_setting("payment_enabled", "1")
    assert await payments.payments_active(db) is True


async def test_deposit_amount_is_percentage_in_minor_units(db):
    await db.set_setting("payment_mode", "deposit")
    await db.set_setting("payment_deposit_percent", "20")
    assert await payments.compute_amount(db, 1500) == 30000


async def test_full_mode_charges_whole_price(db):
    await db.set_setting("payment_mode", "full")
    assert await payments.compute_amount(db, 1500) == 150000


async def test_invoice_failure_does_not_raise(db, bot, with_token):
    await db.set_setting("payment_enabled", "1")
    assert await payments.send_invoice_for_appointment(bot, db, 555, 1, "Стрижка", 1000) in (True, False)
