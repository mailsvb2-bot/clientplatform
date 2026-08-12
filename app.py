import logging
import asyncio
import os
from pathlib import Path

# Matplotlib: keep cache inside project to avoid repeated heavy font scans
_MPLCFG = (Path(__file__).resolve().parent / "data" / "mplcache").resolve()
os.environ.setdefault("MPLCONFIGDIR", str(_MPLCFG))

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from core.logging import setup_logging
setup_logging()

# Silence noisy matplotlib internals; real errors are still logged via exception handlers
logging.getLogger('matplotlib').setLevel(logging.WARNING)
logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
logging.getLogger('matplotlib.category').setLevel(logging.WARNING)

from core.shutdown import ShutdownStep, run_shutdown_steps
from core.task_manager import TaskManager
from core.telegram_bot import build_bot

log = logging.getLogger(__name__)


def _runtime_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("Out-of-range %s=%r; using %s", name, raw, default)
        return default
    return value


def _runtime_float(
    name: str,
    default: float,
    *,
    fallback_name: str = "",
    minimum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw in (None, "") and fallback_name:
        raw = os.getenv(fallback_name)
    raw = str(default) if raw in (None, "") else str(raw).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("Out-of-range %s=%r; using %s", name, raw, default)
        return default
    return value


async def _rollback_partial_startup(
    *,
    webhook_runtime,
    health_runtime,
    scheduler_started: bool,
    db_writer_started: bool,
) -> None:
    """Best-effort reverse-order rollback without masking startup failure."""
    if health_runtime is not None:
        try:
            await health_runtime.stop()
        except Exception:  # validator: allow-wide-except
            log.exception("Partial-startup health rollback failed")
    if webhook_runtime is not None:
        try:
            await webhook_runtime.stop()
        except Exception:  # validator: allow-wide-except
            log.exception("Partial-startup webhook rollback failed")
    if scheduler_started:
        try:
            await stop_scheduler()
        except Exception:  # validator: allow-wide-except
            log.exception("Partial-startup scheduler rollback failed")
    if db_writer_started:
        try:
            await stop_db_writer(drain=False)
        except Exception:  # validator: allow-wide-except
            log.exception("Partial-startup DB writer rollback failed")


async def _safe_answer_callback(cb, *args, **kwargs) -> None:
    """Best-effort callback acknowledgement.

    Telegram callback queries must be answered quickly. Business logic can fail,
    but the UI button must not look frozen for the user.
    """
    try:
        await cb.answer(*args, **kwargs)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "callback answer skipped",
            extra={
                "err": str(exc),
                "callback_data": getattr(cb, "data", None),
                "from_user": getattr(getattr(cb, "from_user", None), "id", None),
            },
        )



from core.environment import is_production_env, normalize_app_env
from config.settings import settings

from core.ai.decision_core import DecisionCore

from services.schema import init_db
from services.validator import validate_all
from services.validators.prod import validate_prod_guardrails
from services.scheduler import start_scheduler
from services.prewarm import prewarm_audio_cache, prewarm_matplotlib_cache
from services.scheduler import stop_scheduler
from services.db_writer import start_db_writer, stop_db_writer
from services.bg import bind_task_manager, register_task_manager
from runtime.messenger_webhooks import start_messenger_webhook_runtime
from runtime.telegram_transport import telegram_transport
from runtime.health_server import start_health_runtime
from services.messenger.setup import build_setup_status
from clientplatform.runtime.control_bot import bind_control_bot_secret

from core.startup_checks import run_startup_checks

from core.middlewares import (
    SoftRateLimitMiddleware,
    StateLogMiddleware,
    InteractionAnalyticsMiddleware,
    TimeInputTraceMiddleware,
    SlowHandlerLogMiddleware,
    QuickAckCallbackMiddleware,
)

# Важно: не называем модуль handlers.settings как `settings`, чтобы не затереть config.settings.settings
from handlers import (
    clientplatform_control,
    start,
    menu,
    text_input,
    payments,
    demo,
    audio,
    admin,
    admin_release,
    admin_stats,
    admin_inline,
    share,
    weather,
    info,
    micro,
    settings as settings_router,
    mood,
    diagnostics,
    gift_flow,
    kb_debug,
    messenger_audio,
)


async def create_application():
    webhook_runtime = None
    health_runtime = None
    scheduler_started = False
    db_writer_started = False

    async def _on_startup(bot: Bot):
        nonlocal webhook_runtime, health_runtime, scheduler_started, db_writer_started
        app_env = normalize_app_env()
        production = is_production_env(app_env)
        strict_env = os.getenv("VALIDATOR_STRICT")
        strict = production if strict_env is None else (strict_env.strip().lower() in {"1", "true", "yes", "on"})

        # Environment-only guardrail must run before schema/migrations mutate persistent state.
        validate_prod_guardrails(strict=True)

        # v15.2: fail fast if critical files/folders are missing
        run_startup_checks(Path(__file__).resolve().parent)
        init_db()
        # Full validators run after schema/migrations exist.
        validate_all(strict=strict)
        messenger_setup = build_setup_status()
        if messenger_setup.missing:
            log.warning('Messenger setup incomplete: %s', ', '.join(messenger_setup.missing))
        if messenger_setup.warnings:
            log.warning('Messenger setup warnings: %s', ' | '.join(messenger_setup.warnings))
        try:
            # Register the canonical manager before DB writer and scheduler call services.bg.tm().
            # ClientPlatform owners are started separately after all fatal startup steps succeed.
            register_task_manager(tm)
            start_db_writer()
            db_writer_started = True
            start_scheduler(bot)
            scheduler_started = True
            try:
                webhook_runtime = await start_messenger_webhook_runtime(bot=bot, dispatcher=dp)
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):  # validator: allow-wide-except
                webhook_runtime = None
                selected_transport = telegram_transport()
                log.exception('Messenger/Telegram webhook runtime failed to start')
                if selected_transport == 'webhook' or production:
                    raise
                log.warning('Continuing without optional messenger webhook runtime in non-prod polling mode')

            try:
                health_runtime = await start_health_runtime()
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):  # validator: allow-wide-except
                health_runtime = None
                log.exception('Health runtime failed to start')
                if production:
                    raise
                log.warning('Continuing without health endpoint in non-prod mode')

            try:
                await prewarm_audio_cache(bot)
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):  # validator: allow-wide-except
                log.exception("Prewarm audio cache failed")

            try:
                await prewarm_matplotlib_cache()
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):  # validator: allow-wide-except
                log.exception("Prewarm matplotlib cache failed")

            # Aiogram can run startup again after a polling transport failure.
            # Rebind process-owned ClientPlatform tasks on every successful startup
            # because the matching shutdown cancels them through the TaskManager.
            bind_task_manager(tm)
            # Sales AI is an optional advisory worker owned by the canonical TaskManager.
            # Its configuration/provider failure must never take down core ClientPlatform.
            try:
                from clientplatform.runtime.sales_ai import bind_sales_ai_worker

                bind_sales_ai_worker(tm)
            except Exception:  # validator: allow-wide-except
                log.exception("Optional Sales AI worker failed to start; core runtime continues")
        except BaseException:  # validator: allow-wide-except
            await _rollback_partial_startup(
                webhook_runtime=webhook_runtime,
                health_runtime=health_runtime,
                scheduler_started=scheduler_started,
                db_writer_started=db_writer_started,
            )
            webhook_runtime = None
            health_runtime = None
            scheduler_started = False
            db_writer_started = False
            raise

    async def _on_shutdown(bot: Bot):
        del bot
        nonlocal webhook_runtime, health_runtime, scheduler_started, db_writer_started

        async def stop_webhook_runtime() -> None:
            nonlocal webhook_runtime
            try:
                if webhook_runtime is not None:
                    await webhook_runtime.stop()
            finally:
                webhook_runtime = None

        async def stop_health_runtime() -> None:
            nonlocal health_runtime
            try:
                if health_runtime is not None:
                    await health_runtime.stop()
            finally:
                health_runtime = None

        async def stop_scheduler_runtime() -> None:
            nonlocal scheduler_started
            try:
                if scheduler_started:
                    await stop_scheduler()
            finally:
                scheduler_started = False

        async def stop_db_writer_runtime() -> None:
            nonlocal db_writer_started
            try:
                if db_writer_started:
                    await stop_db_writer(drain=True)
            finally:
                db_writer_started = False

        await run_shutdown_steps(
            (
                ShutdownStep("messenger webhook runtime", stop_webhook_runtime),
                ShutdownStep("health runtime", stop_health_runtime),
                ShutdownStep("scheduler", stop_scheduler_runtime),
                ShutdownStep("database writer", stop_db_writer_runtime),
                ShutdownStep("task manager", tm.shutdown),
            )
        )

    token = (settings.BOT_TOKEN or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is empty. Put it into .env (see .env.example)")

    bind_control_bot_secret(token)

    tm = TaskManager()

    # Sovereignty (optional): initialize DecisionCore singleton (SelfHealingEngine.tick runs via services.scheduler)
    sovereign_enabled = os.getenv('SOVEREIGN_ENABLED', '0').strip() in {'1','true','yes','on'}
    if sovereign_enabled:
        DecisionCore.instance()

    bot = build_bot(token)
    # Prewarm caches on startup hook (no TaskManager.create in v16.1)
    dp = Dispatcher()
    dp.workflow_data["task_manager"] = tm

    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    # ✅ Лёгкие middleware без изменения UX:
    # 1) мягкий антиспам (не банит, просто просит "секунду")
    # 2) журнал состояния пользователя (диагностика/аналитика)
    # 0.2 сек — достаточно, чтобы убрать "дубль-клики", но не создаёт ощущение "тормозов".
    # Perf diagnostics (does not change UX): logs slow updates.
    thr_ms = _runtime_int("SLOW_HANDLER_MS", 700, minimum=1)
    dp.update.middleware(SlowHandlerLogMiddleware(threshold_ms=thr_ms))

    # Make inline buttons feel instant (spinner disappears immediately).
    # Aiogram may pass the raw CallbackQuery through the callback_query observer
    # after the top-level Update middleware chain, so register the quick-ack guard
    # on both contours. The middleware itself makes duplicate cb.answer() calls
    # a no-op inside the same callback lifecycle.
    quick_ack = QuickAckCallbackMiddleware()
    dp.update.middleware(quick_ack)
    dp.callback_query.middleware(quick_ack)

    callback_interval_sec = _runtime_float(
        "SOFT_CALLBACK_RATE_LIMIT_SEC", 0.05, fallback_name="SOFT_RATE_LIMIT_SEC", minimum=0.0
    )
    message_interval_sec = _runtime_float(
        "SOFT_MESSAGE_RATE_LIMIT_SEC", 0.05, fallback_name="SOFT_RATE_LIMIT_SEC", minimum=0.0
    )
    dp.update.middleware(
        SoftRateLimitMiddleware(
            callback_interval_sec=callback_interval_sec,
            message_interval_sec=message_interval_sec,
        )
    )
    # Диагностика: логируем, какой хендлер "перехватил" ввод HH:MM.
    dp.update.middleware(TimeInputTraceMiddleware())
    dp.update.middleware(StateLogMiddleware())
    dp.update.middleware(InteractionAnalyticsMiddleware())

    dp.include_router(clientplatform_control.router)
    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(text_input.router)
    dp.include_router(demo.router)
    dp.include_router(audio.router)
    dp.include_router(payments.router)
    dp.include_router(admin.router)
    dp.include_router(admin_release.router)
    dp.include_router(admin_stats.router)
    dp.include_router(admin_inline.router)
    dp.include_router(share.router)
    dp.include_router(weather.router)
    dp.include_router(info.router)
    dp.include_router(micro.router)
    dp.include_router(settings_router.router)
    dp.include_router(mood.router)
    dp.include_router(diagnostics.router)
    dp.include_router(gift_flow.router)
    dp.include_router(kb_debug.router)
    dp.include_router(messenger_audio.router)

    transport = telegram_transport()
    if transport == 'webhook':
        log.info('Telegram transport selected: webhook')
        try:
            await _on_startup(bot)
            await asyncio.Event().wait()
        finally:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except TelegramNetworkError:
                log.warning('Failed to clear Telegram webhook during webhook shutdown', exc_info=True)
            await _on_shutdown(bot)
            try:
                await bot.session.close()
            except (RuntimeError, AttributeError):
                log.debug('bot.session.close final close failed', exc_info=True)
        return

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramNetworkError:
        log.warning('Failed to clear Telegram webhook before polling start; continuing with polling', exc_info=True)

    # Telegram API connectivity can be temporarily unavailable (DNS issues, network hiccups,
    # captive portal, ISP blocks, etc.). aiogram may raise TelegramNetworkError during the
    # initial getMe() call while starting polling. We retry with backoff, but not forever.
    backoff = 2
    max_retries = _runtime_int("STARTUP_NETWORK_RETRIES", 5, minimum=1)
    attempt = 0
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except TelegramConflictError as e:
                log.exception('Telegram polling conflict: another bot instance is already consuming updates')
                raise SystemExit(
                    'Telegram polling conflict: another instance is already running. '
                    'Stop the old systemd/local process or switch this deployment to webhook mode.'
                ) from e
            except TelegramNetworkError as e:
                attempt += 1
                msg = str(e)
                low = msg.lower()
                if 'getaddrinfo failed' in low or 'name or service not known' in low or 'gaierror' in low:
                    log.error(
                        'Проблема DNS/интернет, а не код. Подсказки: проверь интернет, DNS (1.1.1.1/8.8.8.8), ' +
                        'ipconfig /flushdns и доступ к api.telegram.org (nslookup). При блокировках нужен VPN/Proxy.'
                    )

                log.exception('TelegramNetworkError while starting polling (attempt %s/%s).', attempt, max_retries)
                try:
                    await bot.session.close()
                except (RuntimeError, AttributeError):
                    log.debug('bot.session.close failed', exc_info=True)
                if attempt >= max_retries:
                    raise SystemExit(
                        f'Не удалось подключиться к Telegram после {attempt} попыток. '
                        f'Проверь сеть/DNS/прокси или увеличь STARTUP_NETWORK_RETRIES.'
                    ) from e
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        try:
            await bot.session.close()
        except (RuntimeError, AttributeError):
            log.debug('bot.session.close final close failed', exc_info=True)
