from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_ROOT = Path(tempfile.gettempdir()) / "clientplatform_pytest"
TEST_ROOT.mkdir(parents=True, exist_ok=True)

# Pytest must never inherit production DB/messenger/webhook state from systemd/.env.
os.environ["APP_ENV"] = "test"
os.environ["LOAD_DOTENV"] = "0"
os.environ["CLIENTPLATFORM_DB_ENGINE"] = "sqlite"
os.environ["DATABASE_URL"] = ""
TEST_DB_PATH = TEST_ROOT / f"pytest_{os.getpid()}.db"
for suffix in ("", "-wal", "-shm"):
    (TEST_ROOT / f"{TEST_DB_PATH.name}{suffix}").unlink(missing_ok=True)
os.environ["CLIENTPLATFORM_DB_PATH"] = str(TEST_DB_PATH)

os.environ.setdefault("BOT_TOKEN", "000000:TEST")
os.environ.setdefault("PAY_PROVIDER_TOKEN", "000000:TEST")
os.environ.setdefault("PUBLIC_BASE_URL", "https" + "://" + "clientplatform.ru")

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
    """Give every test a fresh canonical database snapshot and DB module target.

    Some isolation tests intentionally reload ``core.paths`` / ``services.db.core``
    while a temporary CLIENTPLATFORM_DB_PATH is active. ``monkeypatch`` restores
    the environment afterwards, but a module reload also rewrites module globals;
    without resetting those globals, later money/privacy tests can silently keep
    using the previous test's SQLite file. Re-pin the shared DB modules before
    every test, then restore the schema-only snapshot.
    """

    os.environ["CLIENTPLATFORM_DB_ENGINE"] = "sqlite"
    os.environ["CLIENTPLATFORM_DB_PATH"] = str(TEST_DB_PATH)
    os.environ["DATABASE_URL"] = ""

    import core.paths as core_paths
    import services.db as db_package
    import services.db.core as db_core
    import services.db.runtime as db_runtime

    core_paths.DB_ENGINE = "sqlite"
    core_paths.DB_PATH = TEST_DB_PATH
    core_paths.DATABASE_URL = ""
    db_core.DB_PATH = TEST_DB_PATH
    db_core.DATABASE_URL = ""
    db_package.DB_PATH = TEST_DB_PATH
    canonical_config = db_runtime.DbRuntimeConfig(
        engine="sqlite",
        db_path=TEST_DB_PATH,
        database_url="",
    )
    db_runtime.CONFIG = canonical_config
    db_core.CONFIG = canonical_config

    for suffix in ("", "-wal", "-shm"):
        Path(f"{TEST_DB_PATH}{suffix}").unlink(missing_ok=True)
    shutil.copy2(SCHEMA_TEMPLATE_PATH, TEST_DB_PATH)
    yield
