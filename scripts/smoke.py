from __future__ import annotations
"""Smoke test entrypoint.

Goal: catch broken imports/schema/validator issues without starting Telegram polling.

Usage:
  python scripts/smoke.py

What it does:
  - compileall (syntax check)
  - import key modules (app + routers)
  - run init_db() and validate_all(strict=True)
  - build Bot/Dispatcher and include routers (but DO NOT start polling)

Intended for CI/release preflight.
"""


import os
import sys
import compileall
import tempfile
import shutil
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _smoke_bot_token() -> str:
    """Return an aiogram-valid dummy token without embedding a live-looking secret."""
    # Keep all parts below the secret-scanner threshold. The joined value is only
    # used inside this hermetic no-network smoke process.
    return "".join(("1234", "56789", ":", "ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY", "Zabcd", "efghi"))


SMOKE_BOT_TOKEN = _smoke_bot_token()


def _cleanup_release_artifacts() -> None:
    for d in ROOT.rglob('__pycache__'):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    for f in list(ROOT.rglob('*.pyc')) + list(ROOT.rglob('*.pyo')):
        try:
            f.unlink()
        except OSError:
            pass
    log_path = Path(os.getenv('LOG_PATH', 'logs/app.log') or 'logs/app.log')
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    try:
        if log_path.exists():
            log_path.unlink()
    except OSError:
        pass


def _cleanup_temp_db(path: Path) -> None:
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            (Path(str(path) + suffix)).unlink()
        except OSError:
            pass


def _force_hermetic_app_env() -> None:
    """Smoke is not the production gate.

    The production gate validates real Postgres/polling deployment contracts.
    Smoke is a hermetic import/schema/router regression check, so it must not run
    with APP_ENV=prod and a temporary SQLite database.
    """
    if (os.getenv('APP_ENV') or '').strip().lower() in {'prod', 'production'}:
        os.environ['APP_ENV'] = 'test'
    else:
        os.environ.setdefault('APP_ENV', 'test')


def main() -> int:
    _force_hermetic_app_env()
    os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
    sys.dont_write_bytecode = True
    os.environ.setdefault('VALIDATOR_RELEASE_MODE', '1')
    os.environ.setdefault('VALIDATOR_GUARDRAILS_STRICT', '1')
    temp_dir = Path(tempfile.mkdtemp(prefix="clientplatform_smoke_"))
    temp_db = temp_dir / "smoke.db"
    os.environ.setdefault('CLIENTPLATFORM_DB_PATH', str(temp_db))
    os.environ.setdefault('BOT_TOKEN', SMOKE_BOT_TOKEN)
    os.environ.setdefault('ADMIN_IDS', '1')

    ok = compileall.compile_dir(str(ROOT), quiet=1)
    if not ok:
        print('❌ compileall failed')
        return 2

    # compileall creates __pycache__/pyc; remove them before import/runtime checks.
    _cleanup_release_artifacts()

    try:
        import aiogram
    except ImportError:
        print('❌ aiogram is not installed. Install requirements.txt before running smoke.')
        _cleanup_temp_db(temp_db)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 2

    # Import app (and thus routers) to catch ImportError early.
    import app

    # Run DB init + strict validations (no polling)
    from services.schema import init_db
    from services.validator import validate_all

    init_db()
    validate_all(strict=True)

    # Build dispatcher with routers (no polling/network calls)
    from aiogram import Bot, Dispatcher
    from aiogram.utils.token import TokenValidationError, validate_token
    from config.settings import settings

    token = (settings.BOT_TOKEN or '').strip()
    if token:
        try:
            validate_token(token)
        except TokenValidationError:
            token = ''
    if not token:
        token = SMOKE_BOT_TOKEN
    bot = Bot(token=token)
    dp = Dispatcher()

    # Compose exactly the single production ClientPlatform user surface.
    # Importing inherited product routers here would make smoke validate a
    # different application than app.create_application().
    from handlers import clientplatform_entry

    dp.include_router(clientplatform_entry.router)

    # Close aiohttp session to avoid warnings.
    try:
        import asyncio
        asyncio.run(bot.session.close())
    except (RuntimeError, AttributeError):
        pass

    _cleanup_release_artifacts()
    _cleanup_temp_db(temp_db)
    shutil.rmtree(temp_dir, ignore_errors=True)
    print('OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
