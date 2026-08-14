import itertools
import os
import tempfile
import time

# Set before any project module is imported: the test suite must be self-contained and never
# depend on a real .env existing (this repo is meant to be handed to a buyer standalone).
os.environ.setdefault("BOT_TOKEN", "111111111:TEST-TOKEN-FOR-PYTEST-ONLY")
os.environ.setdefault("ADMIN_IDS", "5555555555")

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from database import Database
from handlers import admin_router, client_router
from tests.mock_bot import FAKE_BOT_TOKEN, MockSession
from config import ADMIN_IDS

ADMIN = ADMIN_IDS[0]

_update_id_counter = itertools.count(1)


@pytest_asyncio.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = Database(path)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
        for ext in ("", "-shm", "-wal"):
            try:
                os.remove(path + ext)
            except OSError:
                pass


@pytest.fixture(scope="session")
def admin_id():
    return ADMIN


@pytest.fixture(scope="session")
def session():
    return MockSession()


@pytest.fixture(scope="session")
def bot(session):
    return Bot(token=FAKE_BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@pytest.fixture(scope="session")
def dp():
    # Session-scoped for the same reason as the full bot's suite: aiogram routers are
    # module-level singletons and can only attach to one Dispatcher ever.
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(admin_router)
    dispatcher.include_router(client_router)
    return dispatcher


@pytest.fixture(autouse=True)
def _reset_session_log(session):
    session.sent.clear()
    session.responses.clear()


def next_update_id() -> int:
    return next(_update_id_counter)


def message_update(user_id: int, text: str = None, contact=None, chat_id: int = None) -> dict:
    chat_id = chat_id if chat_id is not None else user_id
    msg: dict = {
        "message_id": next_update_id(),
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test", "username": f"user{user_id}"},
    }
    if text is not None:
        msg["text"] = text
    if contact is not None:
        msg["contact"] = contact
    return {"update_id": next_update_id(), "message": msg}


def callback_update(user_id: int, data: str, message_text: str = "prev", message_id: int = None, chat_id: int = None) -> dict:
    chat_id = chat_id if chat_id is not None else user_id
    message_id = message_id if message_id is not None else next_update_id()
    return {
        "update_id": next_update_id(),
        "callback_query": {
            "id": str(next_update_id()),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test", "username": f"user{user_id}"},
            "chat_instance": "test-chat-instance",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private"},
                "text": message_text,
            },
        },
    }


async def feed(dispatcher: Dispatcher, bot: Bot, raw: dict, db: Database):
    update = Update.model_validate(raw, context={"bot": bot})
    return await dispatcher.feed_update(bot, update, db=db)


@pytest.fixture
def make_message():
    return message_update


@pytest.fixture
def make_callback():
    return callback_update


async def bookable_date_iso(db, duration=60):
    """First allowed date that actually has an available slot for `duration`.

    Unlike `get_allowed_dates(db)[0]`, this never lands on "today" once its working
    hours have already passed — `get_allowed_dates` doesn't know about durations and
    still lists today even with zero bookable slots left, which made this flaky
    depending on what time of day the suite happened to run.
    """
    import handlers
    for date_iso in await handlers.get_allowed_dates(db):
        if await handlers.get_available_slots(db, date_iso, duration):
            return date_iso
    raise AssertionError("no bookable date found — test schedule/defaults changed")


@pytest_asyncio.fixture
async def feed_update(dp, bot, db):
    async def _feed(raw: dict):
        return await feed(dp, bot, raw, db)
    return _feed
