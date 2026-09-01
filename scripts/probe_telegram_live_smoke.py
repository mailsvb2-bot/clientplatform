from __future__ import annotations

"""Live Telegram Bot API smoke probe via aiogram.

Checks real Telegram reachability without payments:
- get_me confirms token/network;
- get_webhook_info is compared with configured polling/webhook transport;
- optionally sends and deletes a harmless smoke message to a configured test chat.

Bot API cannot impersonate a human user or press inline buttons. This probe covers
live transport reachability; synthetic probes cover business handler contracts.
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/clientplatform/clientplatform.env")
PROBE_TYPE = "telegram_live_smoke_probe"
TELEGRAM_NETWORK_ATTEMPTS = 3
TELEGRAM_RETRY_BASE_DELAY_SECONDS = 1.0

T = TypeVar("T")


@dataclass(frozen=True)
class TelegramLiveSmokeResult:
    ok: bool
    run_id: str
    bot_id: int | None
    bot_username: str
    transport: str
    webhook_url_present: bool
    pending_update_count: int | None
    send_checked: bool
    sent_message_id: int | None
    deleted_message: bool
    cleanup_status: str
    problems: list[str]


def _load_env_file(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parts = shlex.split(value, posix=True)
            loaded[key] = parts[0] if len(parts) == 1 else value
        except ValueError:
            loaded[key] = value.strip('"').strip("'")
    return loaded


def _apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        os.environ.setdefault(str(key), str(value))


def _token() -> str:
    return (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _chat_id(explicit: str | None) -> str:
    return (explicit or os.getenv("TELEGRAM_LIVE_SMOKE_CHAT_ID") or os.getenv("TEST_CHAT_ID") or "").strip()


def _record_probe(result: TelegramLiveSmokeResult) -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services.probe_ledger import finish_probe_run, start_probe_run

    start_probe_run(
        probe_type=PROBE_TYPE,
        user_id=None,
        run_id=result.run_id,
        evidence={"transport": result.transport, "send_checked": result.send_checked},
    )
    finish_probe_run(
        run_id=result.run_id,
        status="ok" if result.ok else "failed",
        cleanup_status=result.cleanup_status,
        rows_touched=1 if result.send_checked else 0,
        error=";".join(result.problems) or None,
        evidence=asdict(result),
    )


async def _retry_telegram_network(
    label: str,
    call: Callable[[], Awaitable[T]],
    problems: list[str],
    *,
    attempts: int = TELEGRAM_NETWORK_ATTEMPTS,
) -> T | None:
    last_error = ""
    for attempt in range(1, int(attempts) + 1):
        try:
            return await call()
        except TelegramNetworkError as exc:
            last_error = str(exc)
            if attempt >= int(attempts):
                break
            await asyncio.sleep(float(TELEGRAM_RETRY_BASE_DELAY_SECONDS) * attempt)
    problems.append(f"telegram_network_error:{label}:{last_error}")
    return None


async def run_probe(*, chat_id: str | None = None, allow_send: bool = False, keep_message: bool = False) -> TelegramLiveSmokeResult:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from runtime.telegram_transport import telegram_transport

    run_id = uuid.uuid4().hex
    problems: list[str] = []
    token = _token()
    transport = telegram_transport()
    bot_id: int | None = None
    bot_username = ""
    webhook_url_present = False
    pending_update_count: int | None = None
    send_checked = False
    sent_message_id: int | None = None
    deleted_message = False
    cleanup_status = "clean"

    if not token:
        problems.append("bot_token_missing")
        result = TelegramLiveSmokeResult(
            ok=False,
            run_id=run_id,
            bot_id=None,
            bot_username="",
            transport=transport,
            webhook_url_present=False,
            pending_update_count=None,
            send_checked=False,
            sent_message_id=None,
            deleted_message=False,
            cleanup_status="failed",
            problems=problems,
        )
        _record_probe(result)
        return result

    bot = Bot(token)
    try:
        me = await _retry_telegram_network("get_me", lambda: bot.get_me(), problems)
        if me is not None:
            if me.is_bot is not True:
                problems.append("get_me_not_bot")
            bot_id = int(me.id)
            bot_username = str(me.username or "")

            webhook = await _retry_telegram_network("get_webhook_info", lambda: bot.get_webhook_info(), problems)
            if webhook is not None:
                webhook_url_present = bool(str(webhook.url or "").strip())
                pending_update_count = int(webhook.pending_update_count or 0)
                if transport == "polling" and webhook_url_present:
                    problems.append("polling_selected_but_webhook_url_present")
                if transport == "webhook" and not webhook_url_present:
                    problems.append("webhook_selected_but_webhook_url_missing")

            target_chat_id = _chat_id(chat_id)
            if allow_send:
                if not target_chat_id:
                    problems.append("allow_send_without_chat_id")
                else:
                    send_checked = True
                    text = f"🧪 ClientPlatform live Telegram smoke OK\nrun={run_id[:12]}\nБез платежей."
                    sent = await _retry_telegram_network(
                        "send_message",
                        lambda: bot.send_message(chat_id=target_chat_id, text=text, disable_notification=True),
                        problems,
                    )
                    if sent is not None:
                        sent_message_id = int(sent.message_id)
                        if sent_message_id <= 0:
                            problems.append("send_message_missing_message_id")
                        if sent_message_id and not keep_message:
                            deleted = await _retry_telegram_network(
                                "delete_message",
                                lambda: bot.delete_message(chat_id=target_chat_id, message_id=sent_message_id),
                                problems,
                            )
                            deleted_message = bool(deleted)
                            if not deleted_message:
                                problems.append("delete_message_not_true")
                        cleanup_status = "kept" if keep_message and sent_message_id else "clean"
    except TelegramAPIError as exc:
        problems.append(f"telegram_api_error:{exc}")
    finally:
        await bot.session.close()

    result = TelegramLiveSmokeResult(
        ok=not problems,
        run_id=run_id,
        bot_id=bot_id,
        bot_username=bot_username,
        transport=transport,
        webhook_url_present=webhook_url_present,
        pending_update_count=pending_update_count,
        send_checked=send_checked,
        sent_message_id=sent_message_id,
        deleted_message=deleted_message,
        cleanup_status=cleanup_status if not problems else "failed",
        problems=problems,
    )
    _record_probe(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live Telegram transport smoke through aiogram")
    parser.add_argument("--env-file", default=os.getenv("CLIENTPLATFORM_ENV_FILE", str(DEFAULT_ENV_FILE)))
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--allow-send", action="store_true")
    parser.add_argument("--keep-message", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _apply_env(_load_env_file(args.env_file))
    result = asyncio.run(run_probe(chat_id=args.chat_id, allow_send=bool(args.allow_send), keep_message=bool(args.keep_message)))
    payload = asdict(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "TELEGRAM_LIVE_SMOKE "
            f"ok={result.ok} bot=@{result.bot_username or '-'} transport={result.transport} "
            f"webhook_url_present={result.webhook_url_present} send_checked={result.send_checked} "
            f"cleanup={result.cleanup_status}"
        )
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
