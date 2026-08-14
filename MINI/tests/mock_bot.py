"""Mock aiogram BaseSession so tests never touch the real Telegram API.

Records every outgoing Bot API call (as the TelegramMethod object aiogram built) so
assertions can inspect exactly what a handler tried to send, and returns plausible
default responses so handler code that reads the result (e.g. bot.get_me().username)
keeps working. Per-method responses can be overridden for a single test via `responses`.
"""
import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, ChatMemberMember, Message, User

FAKE_BOT_ID = 111111111
FAKE_BOT_TOKEN = f"{FAKE_BOT_ID}:TEST-TOKEN-FOR-PYTEST-ONLY-NOT-REAL"


class MockSession(BaseSession):
    def __init__(self, responses: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.sent: List[Any] = []
        self.responses = responses or {}
        self._next_message_id = 1000

    async def close(self) -> None:
        pass

    async def stream_content(
        self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536, raise_for_status: bool = True
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        name = type(method).__name__

        override = self.responses.get(name)
        if override is not None:
            if isinstance(override, Exception):
                raise override
            if callable(override):
                return override(method)
            return override

        if name == "GetMe":
            return User(id=FAKE_BOT_ID, is_bot=True, first_name="TestBot", username="test_bot")
        if name == "GetChatMember":
            return ChatMemberMember(user=User(id=method.user_id, is_bot=False, first_name="U"))
        if name in ("AnswerCallbackQuery", "DeleteWebhook", "DeleteMessage"):
            return True
        if name in ("EditMessageText", "EditMessageCaption", "EditMessageReplyMarkup", "EditMessageMedia"):
            return True
        if name in ("SendMessage", "SendPhoto", "SendVideo", "SendDocument", "CopyMessage"):
            self._next_message_id += 1
            chat_id = getattr(method, "chat_id", 0)
            return Message.model_construct(
                message_id=self._next_message_id,
                date=datetime.datetime.now(),
                chat=Chat(id=chat_id, type="private"),
            )
        return True

    def calls_named(self, name: str) -> List[Any]:
        return [m for m in self.sent if type(m).__name__ == name]
