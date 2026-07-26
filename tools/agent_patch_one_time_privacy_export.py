from __future__ import annotations

import subprocess
from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} occurrences, found {actual}: {old[:100]!r}")
    target.write_text(source.replace(old, new, count), encoding="utf-8")


replace_exact(
    "services/privacy_export_links.py",
    """def privacy_export_http_enabled() -> bool:\n    return bool(privacy_export_public_base_url())\n""",
    """def privacy_export_http_enabled() -> bool:\n    raw = os.getenv(\"PRIVACY_EXPORT_HTTP_ENABLED\")\n    enabled = str(raw or \"\").strip().lower() in {\"1\", \"true\", \"yes\", \"on\"}\n    if not enabled:\n        return False\n    if not privacy_export_public_base_url():\n        raise RuntimeError(\"PRIVACY_EXPORT_HTTP_ENABLED requires a valid public HTTPS base URL\")\n    return True\n""",
)
replace_exact(
    "services/privacy_export_links.py",
    """def issue_privacy_export_url(user_id: int, *, platform: str) -> str:\n    base = privacy_export_public_base_url()\n    if not base:\n        return \"\"\n""",
    """def issue_privacy_export_url(user_id: int, *, platform: str) -> str:\n    if not privacy_export_http_enabled():\n        return \"\"\n    base = privacy_export_public_base_url()\n""",
)

replace_exact(
    "services/migrations/__init__.py",
    "from services.migrations.user_audio_access_tokens_v1 import apply as _apply_audio_access_tokens\n",
    "from services.migrations.user_audio_access_tokens_v1 import apply as _apply_audio_access_tokens\n"
    "from services.migrations.user_privacy_export_tokens_v1 import apply as _apply_privacy_export_tokens\n",
)
replace_exact(
    "services/migrations/__init__.py",
    "    _apply_audio_access_tokens(conn)\n",
    "    _apply_audio_access_tokens(conn)\n    _apply_privacy_export_tokens(conn)\n",
)

replace_exact(
    "services/privacy_manifest.py",
    'MANIFEST_VERSION = "2026-07-21.v4"',
    'MANIFEST_VERSION = "2026-07-26.v5"',
)
replace_exact(
    "services/privacy_manifest.py",
    '    ("user_audio_access_tokens", ("user_id",), "expiring media access capability"),\n',
    '    ("user_audio_access_tokens", ("user_id",), "expiring media access capability"),\n'
    '    ("user_privacy_export_tokens", ("user_id",), "one-time privacy export capability"),\n',
)

replace_exact(
    "runtime/ingress_flags.py",
    "from config.settings import settings\n",
    "from config.settings import settings\nfrom services.privacy_export_links import privacy_export_http_enabled\n",
)
replace_exact(
    "runtime/ingress_flags.py",
    "def http_ingress_enabled() -> bool:\n    return bool(payment_http_enabled() or max_webhook_enabled() or vk_webhook_enabled())\n",
    "def http_ingress_enabled() -> bool:\n    return bool(\n        payment_http_enabled()\n        or max_webhook_enabled()\n        or vk_webhook_enabled()\n        or privacy_export_http_enabled()\n    )\n",
)

replace_exact(
    "runtime/messenger_webhooks.py",
    "from runtime.payment_http import payment_terms_web, pay_yookassa_web, yookassa_reconciliation_webhook\n",
    "from runtime.payment_http import payment_terms_web, pay_yookassa_web, yookassa_reconciliation_webhook\n"
    "from runtime.privacy_export_http import privacy_export_download, privacy_export_landing\n",
)
replace_exact(
    "runtime/messenger_webhooks.py",
    "from services.messenger.audio_links import AUDIO_ACCESS_PREFIX, AUDIO_MEDIA_PREFIX\n",
    "from services.messenger.audio_links import AUDIO_ACCESS_PREFIX, AUDIO_MEDIA_PREFIX\n"
    "from services.privacy_export_links import PRIVACY_EXPORT_PREFIX, privacy_export_http_enabled\n",
)
replace_exact(
    "runtime/messenger_webhooks.py",
    """def _register_payment_routes(app: web.Application) -> None:\n    app.router.add_get(\"/terms\", payment_terms_web)\n    app.router.add_get(\"/pay/yookassa\", pay_yookassa_web)\n    app.router.add_post(\"/pay/yookassa/webhook\", yookassa_reconciliation_webhook)\n\n\n""",
    """def _register_payment_routes(app: web.Application) -> None:\n    app.router.add_get(\"/terms\", payment_terms_web)\n    app.router.add_get(\"/pay/yookassa\", pay_yookassa_web)\n    app.router.add_post(\"/pay/yookassa/webhook\", yookassa_reconciliation_webhook)\n\n\ndef _register_privacy_export_routes(app: web.Application) -> None:\n    app.router.add_get(f\"{PRIVACY_EXPORT_PREFIX}{{token}}\", privacy_export_landing)\n    app.router.add_post(f\"{PRIVACY_EXPORT_PREFIX}{{token}}\", privacy_export_download)\n\n\n""",
)
replace_exact(
    "runtime/messenger_webhooks.py",
    """    payment_enabled = payment_http_enabled()\n    max_enabled = max_webhook_enabled()\n    vk_enabled = vk_webhook_enabled()\n    ingress_enabled = http_ingress_enabled()\n""",
    """    payment_enabled = payment_http_enabled()\n    privacy_export_enabled = privacy_export_http_enabled()\n    max_enabled = max_webhook_enabled()\n    vk_enabled = vk_webhook_enabled()\n    ingress_enabled = http_ingress_enabled()\n""",
)
replace_exact(
    "runtime/messenger_webhooks.py",
    """    if payment_enabled:\n        _register_payment_routes(app)\n    if max_enabled:\n""",
    """    if payment_enabled:\n        _register_payment_routes(app)\n    if privacy_export_enabled:\n        _register_privacy_export_routes(app)\n    if max_enabled:\n""",
)
replace_exact(
    "runtime/messenger_webhooks.py",
    '            "HTTP ingress started on %s:%s payment=%s max=%s vk=%s durable_delivery=%s",\n',
    '            "HTTP ingress started on %s:%s payment=%s privacy_export=%s max=%s vk=%s durable_delivery=%s",\n',
)
replace_exact(
    "runtime/messenger_webhooks.py",
    """            payment_enabled,\n            max_enabled,\n""",
    """            payment_enabled,\n            privacy_export_enabled,\n            max_enabled,\n""",
)

replace_exact(
    "handlers/info.py",
    """import asyncio\nimport logging\nimport sqlite3\nimport tempfile\nfrom pathlib import Path\n\nfrom aiogram import Router\nfrom aiogram.enums import ChatType\nfrom aiogram.filters import Command\nfrom aiogram.types import CallbackQuery, FSInputFile, Message\n""",
    """import asyncio\nimport logging\nimport sqlite3\n\nfrom aiogram import Router\nfrom aiogram.enums import ChatType\nfrom aiogram.filters import Command\nfrom aiogram.types import CallbackQuery, Message\n""",
)
replace_exact(
    "handlers/info.py",
    "from services.privacy_controls import erase_user_behavioral_data, write_user_data_export_gzip\n",
    "from services.privacy_controls import erase_user_behavioral_data\n"
    "from services.privacy_export_links import issue_privacy_export_url, privacy_export_ttl_minutes\n",
)
replace_exact(
    "handlers/info.py",
    """def _new_export_path() -> Path:\n    handle = tempfile.NamedTemporaryFile(\n        prefix=\"metrotherapy-user-data-\",\n        suffix=\".json.gz\",\n        delete=False,\n    )\n    path = Path(handle.name)\n    handle.close()\n    return path\n\n\ndef _remove_export_path(path: Path) -> None:\n    try:\n        path.unlink(missing_ok=True)\n    except OSError:\n        log.exception(\"Temporary privacy export cleanup failed: path=%s\", path)\n\n\n""",
    "",
)
replace_exact(
    "handlers/info.py",
    """async def _answer_export_failure(message: Message, user_id: int) -> None:\n    log.exception(\"User data export failed: user_id=%s\", user_id)\n    await message.answer(\n""",
    """async def _answer_export_failure(message: Message) -> None:\n    await message.answer(\n""",
)
replace_exact(
    "handlers/info.py",
    """            \"⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. \"\n            \"Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\\n\\n\"\n""",
    """            \"⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. \"\n            \"После подтверждения бот создаст одноразовую HTTPS-ссылку; предпросмотр ссылки не запускает скачивание.\\n\\n\"\n""",
)
replace_exact(
    "handlers/info.py",
    """    export_path: Path | None = None\n    try:\n        export_path = _new_export_path()\n        result = await asyncio.to_thread(\n            write_user_data_export_gzip,\n            user_id,\n            export_path,\n        )\n        document = FSInputFile(\n            result.path,\n            filename=\"metrotherapy-user-data.json.gz\",\n        )\n        await message.answer_document(\n            document,\n            caption=(\n                \"🔐 Это сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. \"\n                f\"Записей: {result.total_rows}. \"\n                \"Архив не зашифрован. Сохраните его только в защищённом месте и удалите это сообщение, \"\n                \"когда файл больше не нужен в истории чата.\"\n            ),\n        )\n    except sqlite3.Error:\n        await _answer_export_failure(message, user_id)\n    except RuntimeError:\n        await _answer_export_failure(message, user_id)\n    except OSError:\n        await _answer_export_failure(message, user_id)\n    except ValueError:\n        await _answer_export_failure(message, user_id)\n    except TypeError:\n        await _answer_export_failure(message, user_id)\n    finally:\n        if export_path is not None:\n            await asyncio.to_thread(_remove_export_path, export_path)\n""",
    """    try:\n        url = await asyncio.to_thread(\n            issue_privacy_export_url,\n            user_id,\n            platform=\"telegram\",\n        )\n    except (sqlite3.Error, RuntimeError, OSError, ValueError, TypeError):\n        log.exception(\"One-time user data export link failed: user_id=%s\", user_id)\n        await _answer_export_failure(message)\n        return\n    if not url:\n        log.error(\"One-time user data export link is unavailable: user_id=%s\", user_id)\n        await _answer_export_failure(message)\n        return\n\n    ttl = privacy_export_ttl_minutes()\n    await message.answer(\n        \"🔐 Одноразовая ссылка на экспорт Ваших данных:\\n\"\n        f\"{url}\\n\\n\"\n        f\"Ссылка действует не более {ttl} минут и позволяет скачать архив один раз. \"\n        \"Сначала откроется страница подтверждения; предпросмотр мессенджера не расходует ссылку. \"\n        \"Архив сжат, но не зашифрован — храните его в защищённом месте.\"\n    )\n""",
)

replace_exact(
    "services/messenger/reply_dispatcher.py",
    """import asyncio\nimport logging\nimport shutil\nimport sqlite3\nimport tempfile\nfrom pathlib import Path\n""",
    """import asyncio\nimport logging\nfrom pathlib import Path\n""",
)
replace_exact(
    "services/messenger/reply_dispatcher.py",
    "from services.privacy_controls import write_user_data_export_gzip\n",
    "from services.privacy_export_links import issue_privacy_export_url, privacy_export_ttl_minutes\n",
)
replace_exact(
    "services/messenger/reply_dispatcher.py",
    """def _privacy_export_paths(user_id: int) -> tuple[Path, Path]:\n    root = Path(tempfile.mkdtemp(prefix=\"metrotherapy_privacy_export_\"))\n    return root, root / \"metrotherapy-user-data.json.gz\"\n\n\ndef _remove_privacy_export_root(root: Path) -> None:\n    shutil.rmtree(root, ignore_errors=True)\n\n\nasync def _send_privacy_export(\n    *,\n    platform: str,\n    sender: Any,\n    external_user_id: str,\n    canonical_user_id: int,\n) -> None:\n    root, export_path = await asyncio.to_thread(_privacy_export_paths, canonical_user_id)\n    try:\n        try:\n            result = await asyncio.to_thread(\n                write_user_data_export_gzip,\n                canonical_user_id,\n                export_path,\n            )\n        except (sqlite3.Error, RuntimeError, OSError):\n            log.exception(\"%s privacy export generation failed\", platform.upper())\n            await sender.send_text(\n                external_user_id,\n                \"⚠️ Не удалось подготовить экспорт данных. Повторите позже или обратитесь в поддержку.\",\n                **_vk_kwargs(platform, {}, canonical_user_id),\n            )\n            return\n        except (TypeError, ValueError):\n            log.exception(\"%s privacy export data rejected\", platform.upper())\n            await sender.send_text(\n                external_user_id,\n                \"⚠️ Не удалось подготовить экспорт данных. Повторите позже или обратитесь в поддержку.\",\n                **_vk_kwargs(platform, {}, canonical_user_id),\n            )\n            return\n\n        send_document = getattr(sender, \"send_document_file\", None)\n        if not callable(send_document):\n            raise UnsupportedMessengerDelivery(\n                f\"No document sender for privacy export on platform={platform}\"\n            )\n        caption = (\n            \"🔐 Сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. \"\n            f\"Записей: {result.total_rows}. \"\n            \"Архив не зашифрован. Сохраните его только в защищённом месте и удалите сообщение, \"\n            \"когда файл больше не нужен в истории чата.\"\n        )\n        await send_document(\n            external_user_id,\n            result.path,\n            caption=caption,\n            **_vk_kwargs(platform, {}, canonical_user_id),\n        )\n    finally:\n        await asyncio.to_thread(_remove_privacy_export_root, root)\n\n\n""",
    """async def _send_privacy_export(\n    *,\n    platform: str,\n    sender: Any,\n    external_user_id: str,\n    canonical_user_id: int,\n) -> None:\n    try:\n        url = await asyncio.to_thread(\n            issue_privacy_export_url,\n            canonical_user_id,\n            platform=platform,\n        )\n    except (RuntimeError, OSError, TypeError, ValueError):\n        log.exception(\"%s one-time privacy export link failed\", platform.upper())\n        url = \"\"\n    if not url:\n        await sender.send_text(\n            external_user_id,\n            \"⚠️ Безопасная выдача экспорта сейчас недоступна. Повторите позже или обратитесь в поддержку.\",\n            **_vk_kwargs(platform, {}, canonical_user_id),\n        )\n        return\n\n    ttl = privacy_export_ttl_minutes()\n    text = (\n        \"🔐 Одноразовая ссылка на экспорт Ваших данных:\\n\"\n        f\"{url}\\n\\n\"\n        f\"Ссылка действует не более {ttl} минут и позволяет скачать архив один раз. \"\n        \"Сначала откроется страница подтверждения; предпросмотр мессенджера не расходует ссылку. \"\n        \"Архив сжат, но не зашифрован — храните его в защищённом месте.\"\n    )\n    await sender.send_text(\n        external_user_id,\n        text,\n        **_vk_kwargs(platform, {}, canonical_user_id, text=text),\n    )\n\n\n""",
)

replace_exact(
    "services/messenger/text_ui_router.py",
    """            \"⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. \"\n            \"Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\\n\\n\"\n""",
    """            \"⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. \"\n            \"После подтверждения бот создаст одноразовую HTTPS-ссылку; предпросмотр ссылки не запускает скачивание.\\n\\n\"\n""",
)

replace_exact(
    "deploy/metrotherapy.env.example",
    """PAYMENT_HTTP_ENABLED=1\nMAX_WEBHOOK_ENABLED=0\nVK_WEBHOOK_ENABLED=0\n""",
    """PAYMENT_HTTP_ENABLED=1\n# One-time privacy export links. The public URL must be HTTPS in prod.\nPRIVACY_EXPORT_HTTP_ENABLED=1\nPRIVACY_EXPORT_PUBLIC_BASE_URL=https://metrotherapy-bot.metrotherapy.ru\nPRIVACY_EXPORT_TOKEN_TTL_MINUTES=10\nMAX_WEBHOOK_ENABLED=0\nVK_WEBHOOK_ENABLED=0\n""",
)

Path("tests/test_privacy_user_commands.py").write_text(
    '''from __future__ import annotations\n\nfrom types import SimpleNamespace\n\nimport pytest\n\nfrom handlers import info\n\n\nclass FakeMessage:\n    def __init__(self, user_id: int, text: str = "", *, chat_type: str = "private") -> None:\n        self.from_user = SimpleNamespace(id=user_id)\n        self.chat = SimpleNamespace(type=chat_type)\n        self.text = text\n        self.answers: list[str] = []\n\n    async def answer(self, text: str, **_kwargs) -> None:\n        self.answers.append(text)\n\n\ndef test_delete_confirmation_is_exact() -> None:\n    assert info._delete_confirmed("/deletemydata CONFIRM") is True\n    assert info._delete_confirmed("/deletemydata confirm") is True\n    assert info._delete_confirmed("/deletemydata") is False\n    assert info._delete_confirmed("/deletemydata YES") is False\n    assert info._delete_confirmed("/deletemydata CONFIRM extra") is False\n\n\ndef test_export_confirmation_is_exact() -> None:\n    assert info._export_confirmed("/mydata CONFIRM") is True\n    assert info._export_confirmed("/mydata@metrotherapybot confirm") is True\n    assert info._export_confirmed("/mydata") is False\n    assert info._export_confirmed("mydata CONFIRM") is False\n    assert info._export_confirmed("/mydata YES") is False\n    assert info._export_confirmed("/mydata CONFIRM extra") is False\n\n\n@pytest.mark.asyncio\nasync def test_export_issues_authenticated_one_time_link(monkeypatch) -> None:\n    seen: list[tuple[int, str]] = []\n\n    def issue(user_id: int, *, platform: str) -> str:\n        seen.append((user_id, platform))\n        return "https://example.test/privacy/export/random-token"\n\n    monkeypatch.setattr(info, "issue_privacy_export_url", issue)\n    monkeypatch.setattr(info, "privacy_export_ttl_minutes", lambda: 10)\n    message = FakeMessage(91001, "/mydata CONFIRM")\n\n    await info.cmd_my_data(message)\n\n    assert seen == [(91001, "telegram")]\n    assert len(message.answers) == 1\n    assert "https://example.test/privacy/export/random-token" in message.answers[0]\n    assert "одноразовая" in message.answers[0].casefold()\n    assert "предпросмотр" in message.answers[0].casefold()\n\n\n@pytest.mark.asyncio\nasync def test_export_fails_closed_without_secure_public_link(monkeypatch) -> None:\n    monkeypatch.setattr(info, "issue_privacy_export_url", lambda *_args, **_kwargs: "")\n    message = FakeMessage(91004, "/mydata CONFIRM")\n\n    await info.cmd_my_data(message)\n\n    assert message.answers\n    assert "Не удалось подготовить экспорт" in message.answers[-1]\n\n\n@pytest.mark.asyncio\nasync def test_export_requires_confirmation_and_private_chat(monkeypatch) -> None:\n    called = False\n\n    def issue(*_args, **_kwargs):\n        nonlocal called\n        called = True\n        raise AssertionError("must not issue without confirmation or from a group")\n\n    monkeypatch.setattr(info, "issue_privacy_export_url", issue)\n\n    unconfirmed = FakeMessage(91005, "/mydata")\n    await info.cmd_my_data(unconfirmed)\n    assert called is False\n    assert "/mydata CONFIRM" in unconfirmed.answers[-1]\n    assert "одноразовую HTTPS-ссылку" in unconfirmed.answers[-1]\n\n    group = FakeMessage(91005, "/mydata CONFIRM", chat_type="group")\n    await info.cmd_my_data(group)\n    assert called is False\n    assert "только в личном чате" in group.answers[-1]\n\n\n@pytest.mark.asyncio\nasync def test_delete_without_confirmation_does_not_mutate(monkeypatch) -> None:\n    called = False\n\n    def fake_erase(*_args, **_kwargs):\n        nonlocal called\n        called = True\n        raise AssertionError("must not erase without confirmation")\n\n    monkeypatch.setattr(info, "erase_user_behavioral_data", fake_erase)\n    message = FakeMessage(91002, "/deletemydata")\n\n    await info.cmd_delete_my_data(message)\n\n    assert called is False\n    assert message.answers\n    assert "/deletemydata CONFIRM" in message.answers[0]\n    assert "Технический идентификатор канала" in message.answers[0]\n    assert "обезличит профиль" not in message.answers[0]\n\n\n@pytest.mark.asyncio\nasync def test_confirmed_delete_uses_authenticated_message_user(monkeypatch) -> None:\n    seen: list[tuple[int, str]] = []\n\n    def fake_erase(user_id: int, *, reason: str):\n        seen.append((user_id, reason))\n        return SimpleNamespace(deleted_tables={"events": 3, "jobs": 2})\n\n    monkeypatch.setattr(info, "erase_user_behavioral_data", fake_erase)\n    message = FakeMessage(91003, "/deletemydata CONFIRM")\n\n    await info.cmd_delete_my_data(message)\n\n    assert seen == [(91003, "telegram_user_request")]\n    assert message.answers\n    assert "Удалено записей: 5" in message.answers[-1]\n    assert "Технический идентификатор канала" in message.answers[-1]\n    assert "профиль обезличен" not in message.answers[-1]\n''',
    encoding="utf-8",
)

replace_exact(
    "tests/test_privacy_payment_runtime_followup.py",
    '    "PUBLIC_BASE_URL",\n',
    '    "PUBLIC_BASE_URL",\n    "PRIVACY_EXPORT_HTTP_ENABLED",\n    "PRIVACY_EXPORT_PUBLIC_BASE_URL",\n',
)
replace_exact(
    "tests/test_privacy_payment_runtime_followup.py",
    '''@pytest.mark.asyncio\nasync def test_privacy_export_is_sent_and_temp_files_are_removed(\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    observed: dict[str, Any] = {}\n\n    def write_export(user_id: int, output_path: Path) -> Any:\n        output_path.write_bytes(b"privacy-export")\n        observed["generated_path"] = output_path\n        return SimpleNamespace(path=output_path, total_rows=4)\n\n    class Sender:\n        async def send_document_file(\n            self,\n            external_user_id: str,\n            file_path: Path,\n            *,\n            caption: str,\n            **kwargs: Any,\n        ) -> None:\n            observed["external_user_id"] = external_user_id\n            observed["bytes"] = file_path.read_bytes()\n            observed["caption"] = caption\n            observed["kwargs"] = kwargs\n\n        async def send_text(self, *_args: Any, **_kwargs: Any) -> None:\n            raise AssertionError("failure fallback must not be used")\n\n    monkeypatch.setattr(reply_dispatcher, "write_user_data_export_gzip", write_export)\n    await reply_dispatcher._send_privacy_export(\n        platform="vk",\n        sender=Sender(),\n        external_user_id="vk-77",\n        canonical_user_id=77,\n    )\n\n    assert observed["external_user_id"] == "vk-77"\n    assert observed["bytes"] == b"privacy-export"\n    assert observed["generated_path"].name == "metrotherapy-user-data.json.gz"\n    assert "Записей: 4" in observed["caption"]\n    assert "не зашифрован" in observed["caption"]\n    assert not observed["generated_path"].exists()\n    assert not observed["generated_path"].parent.exists()\n\n\n''',
    '''@pytest.mark.asyncio\nasync def test_privacy_export_is_sent_as_one_time_link(\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    observed: dict[str, Any] = {}\n\n    class Sender:\n        async def send_document_file(self, *_args: Any, **_kwargs: Any) -> None:\n            raise AssertionError("privacy export must not be uploaded into messenger history")\n\n        async def send_text(self, external_user_id: str, text: str, **kwargs: Any) -> None:\n            observed["external_user_id"] = external_user_id\n            observed["text"] = text\n            observed["kwargs"] = kwargs\n\n    monkeypatch.setattr(\n        reply_dispatcher,\n        "issue_privacy_export_url",\n        lambda user_id, *, platform: f"https://example.test/privacy/export/{platform}-{user_id}",\n    )\n    monkeypatch.setattr(reply_dispatcher, "privacy_export_ttl_minutes", lambda: 10)\n    await reply_dispatcher._send_privacy_export(\n        platform="vk",\n        sender=Sender(),\n        external_user_id="vk-77",\n        canonical_user_id=77,\n    )\n\n    assert observed["external_user_id"] == "vk-77"\n    assert "https://example.test/privacy/export/vk-77" in observed["text"]\n    assert "одноразовая" in observed["text"].casefold()\n    assert "предпросмотр" in observed["text"].casefold()\n\n\n''',
)

Path("tests/test_privacy_export_download.py").write_text(
    '''from __future__ import annotations\n\nfrom datetime import datetime, timedelta, timezone\nfrom types import SimpleNamespace\nfrom urllib.parse import urlsplit\n\nimport pytest\nfrom aiohttp import web\nfrom aiohttp.test_utils import TestClient, TestServer\n\nfrom runtime import privacy_export_http\nfrom services import privacy_export_links\n\n\ndef test_privacy_export_ttl_has_safe_bounds(monkeypatch: pytest.MonkeyPatch) -> None:\n    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "0")\n    assert privacy_export_links.privacy_export_ttl_minutes() == 2\n\n    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "999")\n    assert privacy_export_links.privacy_export_ttl_minutes() == 30\n\n    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "bad")\n    assert privacy_export_links.privacy_export_ttl_minutes() == 10\n\n\ndef test_privacy_export_token_expiry_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:\n    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "10")\n    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)\n    assert privacy_export_links._grant_expired((now - timedelta(minutes=11)).isoformat(), now=now)\n    assert not privacy_export_links._grant_expired((now - timedelta(minutes=9)).isoformat(), now=now)\n    assert privacy_export_links._grant_expired("not-a-date", now=now)\n\n\ndef test_privacy_export_requires_explicit_https_ingress_in_prod(\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    monkeypatch.setenv("APP_ENV", "prod")\n    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")\n    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "http://example.test")\n    with pytest.raises(RuntimeError, match="valid public HTTPS"):\n        privacy_export_links.privacy_export_http_enabled()\n\n    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")\n    assert privacy_export_links.privacy_export_http_enabled() is True\n\n\ndef test_privacy_export_token_is_single_use(monkeypatch: pytest.MonkeyPatch) -> None:\n    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")\n    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")\n    url = privacy_export_links.issue_privacy_export_url(991001, platform="telegram")\n    token = urlsplit(url).path.rsplit("/", 1)[-1]\n\n    grant = privacy_export_links.get_privacy_export_grant(token)\n    assert grant is not None and grant.user_id == 991001\n    claimed = privacy_export_links.claim_privacy_export_grant(token)\n    assert claimed is not None and claimed.consumed_at is not None\n    assert privacy_export_links.get_privacy_export_grant(token) is None\n    assert privacy_export_links.claim_privacy_export_grant(token) is None\n\n\n@pytest.mark.asyncio\nasync def test_preview_get_does_not_consume_and_post_streams_once(\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")\n    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")\n    url = privacy_export_links.issue_privacy_export_url(991002, platform="vk")\n    path = urlsplit(url).path\n    token = path.rsplit("/", 1)[-1]\n    generated_paths = []\n\n    def write_export(user_id: int, output_path):\n        assert user_id == 991002\n        output_path.write_bytes(b"privacy-export")\n        generated_paths.append(output_path)\n        return SimpleNamespace(path=output_path, compressed_size_bytes=14, total_rows=3)\n\n    monkeypatch.setattr(privacy_export_http, "write_user_data_export_gzip", write_export)\n    app = web.Application()\n    app.router.add_get(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_landing)\n    app.router.add_post(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_download)\n    client = TestClient(TestServer(app))\n    await client.start_server()\n    try:\n        preview = await client.get(path)\n        assert preview.status == 200\n        assert "Скачать архив" in await preview.text()\n        assert privacy_export_links.get_privacy_export_grant(token) is not None\n\n        download = await client.post(path)\n        assert download.status == 200\n        assert await download.read() == b"privacy-export"\n        assert download.headers["Cache-Control"].startswith("private, no-store")\n        assert privacy_export_links.get_privacy_export_grant(token) is None\n\n        replay = await client.post(path)\n        assert replay.status == 404\n    finally:\n        await client.close()\n\n    assert generated_paths\n    assert all(not item.exists() and not item.parent.exists() for item in generated_paths)\n\n\n@pytest.mark.asyncio\nasync def test_generation_failure_does_not_consume_link(monkeypatch: pytest.MonkeyPatch) -> None:\n    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")\n    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")\n    url = privacy_export_links.issue_privacy_export_url(991003, platform="max")\n    path = urlsplit(url).path\n    token = path.rsplit("/", 1)[-1]\n\n    def fail_export(*_args, **_kwargs):\n        raise RuntimeError("synthetic export failure")\n\n    monkeypatch.setattr(privacy_export_http, "write_user_data_export_gzip", fail_export)\n    app = web.Application()\n    app.router.add_post(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_download)\n    client = TestClient(TestServer(app))\n    await client.start_server()\n    try:\n        response = await client.post(path)\n        assert response.status == 500\n    finally:\n        await client.close()\n\n    assert privacy_export_links.get_privacy_export_grant(token) is not None\n''',
    encoding="utf-8",
)

subprocess.run(
    [
        "python",
        "-m",
        "py_compile",
        "services/privacy_export_links.py",
        "runtime/privacy_export_http.py",
        "handlers/info.py",
        "services/messenger/reply_dispatcher.py",
        "services/messenger/text_ui_router.py",
        "runtime/messenger_webhooks.py",
        "runtime/ingress_flags.py",
    ],
    check=True,
)
subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_privacy_export_download.py",
        "tests/test_privacy_user_commands.py",
        "tests/test_privacy_payment_runtime_followup.py",
        "tests/test_privacy_manifest.py",
        "tests/test_privacy_streaming_export.py",
    ],
    check=True,
)
