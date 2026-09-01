from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot
else:
    Bot = Any


class TelegramBotSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_text(self, external_user_id: str, text: str, **kwargs: Any):
        return await self.bot.send_message(int(external_user_id), text, **kwargs)

    async def send_audio_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ):
        from aiogram.types import FSInputFile

        return await self.bot.send_audio(
            int(external_user_id),
            audio=FSInputFile(file_path),
            caption=caption or None,
            **kwargs,
        )
