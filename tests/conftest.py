from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_ROOT = Path(tempfile.gettempdir()) / "metrotherapy_pytest"
TEST_ROOT.mkdir(parents=True, exist_ok=True)

# Pytest must never inherit production DB/messenger/webhook state from systemd/.env.
os.environ["APP_ENV"] = "test"
os.environ["LOAD_DOTENV"] = "0"
os.environ["CLIENTPLATFORM_DB_ENGINE"] = "sqlite"
os.environ["METRO_DB_ENGINE"] = "sqlite"
os.environ["DATABASE_URL"] = ""
TEST_DB_PATH = TEST_ROOT / f"pytest_{os.getpid()}.db"
for suffix in ("", "-wal", "-shm"):
    (TEST_ROOT / f"{TEST_DB_PATH.name}{suffix}").unlink(missing_ok=True)
os.environ["CLIENTPLATFORM_DB_PATH"] = str(TEST_DB_PATH)
os.environ["METRO_DB_PATH"] = str(TEST_DB_PATH)

os.environ.setdefault("BOT_TOKEN", "000000:TEST")
os.environ.setdefault("PAY_PROVIDER_TOKEN", "000000:TEST")
os.environ.setdefault("PUBLIC_BASE_URL", "https" + "://" + "metrotherapy.ru")

# Messenger/webhook/runtime defaults for deterministic unit tests. Dedicated
# ClientPlatform default-on tests clear these variables explicitly before asserting
# production defaults, while unrelated tests never start real background workers.
os.environ["TELEGRAM_TRANSPORT"] = "polling"
os.environ["TELEGRAM_WEBHOOK_ENABLED"] = "0"
os.environ["MESSENGER_WEBHOOK_ENABLED"] = "0"
os.environ["CLIENTPLATFORM_CONTROL_BOT_ENABLED"] = "0"
os.environ["CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED"] = "0"

# Prevent real server integrations leaking into tests.
for name in (
    "MAX_BOT_TOKEN",
    "MAX_BOT_NAME",
    "MAX_BOT_LINK_BASE",
    "VK_GROUP_TOKEN",
    "VK_CONFIRMATION_TOKEN",
    "VK_SECRET",
    "VK_GROUP_ID",
    "MESSENGER_PUBLIC_BASE_URL",
    "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL",
    "TELEGRAM_WEBHOOK_SECRET_TOKEN",
):
    os.environ.pop(name, None)

# Tests that exercise messenger text entrypoints must use the same canonical
# schema bootstrap as application startup. Otherwise a fresh isolated pytest DB
# exists but has no users/events tables, and button-parity tests fail before
# reaching the messenger behavior under test.
from services.schema import init_db

init_db()
SCHEMA_TEMPLATE_PATH = TEST_ROOT / f"pytest_schema_{os.getpid()}.db"
shutil.copy2(TEST_DB_PATH, SCHEMA_TEMPLATE_PATH)

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_database():
    """Give every test a fresh canonical database snapshot.

    Application services open independent connections, so transaction rollback
    cannot isolate them. Copying a schema-only SQLite snapshot is both honest
    and fast: state can never leak between tests, while migrations are not rerun
    hundreds of times.
    """

    for suffix in ("", "-wal", "-shm"):
        Path(f"{TEST_DB_PATH}{suffix}").unlink(missing_ok=True)
    shutil.copy2(SCHEMA_TEMPLATE_PATH, TEST_DB_PATH)
    yield


@pytest.fixture(autouse=True)
def _phase2_stars_invoice_transport_bridge(request, monkeypatch):
    """Keep the focused phase-2 test aligned with the installed transport owner.

    ``telegram_stars.send_stars_invoice`` is intentionally replaced at startup by
    ``stars_invoice_transport.send_stars_invoice``. The focused test changes the
    public module's package/flag doubles between assertions; this bridge forwards
    only those doubles to the actual owner and is inactive for every other module.
    """

    if Path(str(request.fspath)).name != "test_telegram_stars_critical_paths_phase2.py":
        yield
        return

    from services.payments import stars_invoice_transport as transport
    from services.payments import telegram_stars as stars

    monkeypatch.setattr(transport, "package_by_id", lambda package_id: stars.package_by_id(package_id))
    monkeypatch.setattr(transport, "telegram_stars_enabled", lambda: stars.telegram_stars_enabled())
    monkeypatch.setattr(
        transport,
        "create_gift_checkout_token",
        lambda **kwargs: stars.create_gift_checkout_token(**kwargs),
    )
    monkeypatch.setattr(transport, "log_event", lambda *args, **kwargs: stars.log_event(*args, **kwargs))
    yield
