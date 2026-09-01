from dataclasses import dataclass
import os


# ВАЖНО (prod-safe): НЕ подхватываем .env автоматически в продакшене.
# Иначе локальный .env рядом с кодом может неожиданно переопределить системные переменные окружения.
#
# По умолчанию .env читаем только в dev/stage.
# Для принудительного поведения можно выставить LOAD_DOTENV=1.
APP_ENV = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
if os.getenv("LOAD_DOTENV", "") == "1" or APP_ENV in {"dev", "stage"}:
    try:
        from dotenv import load_dotenv

        # override=False: не перетираем уже заданные переменные окружения
        load_dotenv(override=False)
    except ImportError:
        # dotenv не обязателен (особенно в минимальных прод-сборках)
        pass


class ConfigurationError(ValueError):
    """Raised when an environment value cannot satisfy the typed runtime contract."""


# --- Админ-доступ к аналитике ---
# ADMIN_IDS можно задать так: ADMIN_IDS="123,456" или ADMIN_ID="123"
def _parse_admin_ids_env() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if raw:
        out: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue
        return out
    one = os.getenv("ADMIN_ID", "").strip()
    if one:
        try:
            return [int(one)]
        except ValueError:
            return []
    return []


# ВАЖНО: это module-level переменная для удобного импорта в keyboards/handlers.
ADMIN_IDS: list[int] = _parse_admin_ids_env()


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _env_bool(name: str, default: str = "0") -> bool:
    """Parse boolean environment variables consistently across runtime settings.

    Accepts the common deployment values used in systemd/GitHub Actions/manual
    shells: 1/true/yes/on/webhook. This prevents a dangerous split where one
    runtime resolver treats "true" as enabled while Settings treats it as false.
    """
    raw = _env(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _env(name, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}, got {value}")
    return value


def _env_int_fallback(
    name: str,
    fallback_name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if os.getenv(name) not in (None, ""):
        return _env_int(name, default, minimum=minimum, maximum=maximum)
    return _env_int(fallback_name, default, minimum=minimum, maximum=maximum)


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on", "webhook"}


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _optional_feature_flag(name: str) -> bool | None:
    if name not in os.environ:
        return None
    return _truthy_env(name)


@dataclass
class Settings:
    BOT_TOKEN: str = _env("BOT_TOKEN", "")

    # --- Multi-messenger routing ---
    CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED: bool = _env_bool(
        "CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED", "1"
    )
    TELEGRAM_BOT_USERNAME: str = _env("TELEGRAM_BOT_USERNAME", "")
    CLIENTPLATFORM_PRODUCTION_BOT_USERNAME: str = _env(
        "CLIENTPLATFORM_PRODUCTION_BOT_USERNAME", ""
    )
    MAX_BOT_TOKEN: str = _env("MAX_BOT_TOKEN", "")
    MAX_WEBHOOK_SECRET: str = _env("MAX_WEBHOOK_SECRET", "")
    MAX_BOT_NAME: str = _env("MAX_BOT_NAME", "")
    MAX_BOT_LINK_BASE: str = _env("MAX_BOT_LINK_BASE", "")
    VK_GROUP_TOKEN: str = _env("VK_GROUP_TOKEN", "")
    VK_CONFIRMATION_TOKEN: str = _env("VK_CONFIRMATION_TOKEN", "")
    VK_SECRET: str = _env("VK_SECRET", "")
    VK_GROUP_ID: str = _env("VK_GROUP_ID", "")
    VK_API_VERSION: str = _env("VK_API_VERSION", "5.199")
    MESSENGER_WEBHOOK_ENABLED: bool = _env_bool("MESSENGER_WEBHOOK_ENABLED")
    MESSENGER_WEBHOOK_HOST: str = _env("MESSENGER_WEBHOOK_HOST", _env("WEBHOOK_HOST", "127.0.0.1"))
    MESSENGER_WEBHOOK_PORT: int = _env_int_fallback(
        "MESSENGER_WEBHOOK_PORT",
        "WEBHOOK_PORT",
        8081,
        minimum=1,
        maximum=65535,
    )
    TELEGRAM_TRANSPORT: str = (_env("TELEGRAM_TRANSPORT", _env("RUN_MODE", "polling")) or "polling").strip().lower()
    TELEGRAM_WEBHOOK_ENABLED: bool = _env_bool("TELEGRAM_WEBHOOK_ENABLED")
    TELEGRAM_WEBHOOK_HOST: str = _env("TELEGRAM_WEBHOOK_HOST", _env("WEBHOOK_HOST", "127.0.0.1"))
    TELEGRAM_WEBHOOK_PORT: int = _env_int_fallback(
        "TELEGRAM_WEBHOOK_PORT",
        "WEBHOOK_PORT",
        8081,
        minimum=1,
        maximum=65535,
    )
    TELEGRAM_WEBHOOK_PREFIX: str = _env("TELEGRAM_WEBHOOK_PREFIX", "/telegram-webhook")
    TELEGRAM_WEBHOOK_SECRET_TOKEN: str = _env("TELEGRAM_WEBHOOK_SECRET_TOKEN", "")
    TELEGRAM_WEBHOOK_PUBLIC_BASE_URL: str = _env("TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", _env("PUBLIC_BASE_URL", ""))
    TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES: bool = _env_bool("TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES")
    HEALTHCHECK_ENABLED: bool = _env_bool("HEALTHCHECK_ENABLED", "1")
    HEALTHCHECK_HOST: str = _env("HEALTHCHECK_HOST", "127.0.0.1")
    HEALTHCHECK_PORT: int = _env_int("HEALTHCHECK_PORT", 8082, minimum=1, maximum=65535)
    MESSENGER_PUBLIC_BASE_URL: str = _env("MESSENGER_PUBLIC_BASE_URL", "")
    MESSENGER_BRIDGE_TOKEN_TTL_HOURS: int = _env_int("MESSENGER_BRIDGE_TOKEN_TTL_HOURS", 72, minimum=1)

    # IANA timezone used by scheduling and business-local date rendering.
    TIMEZONE: str = _env("TIMEZONE", "Europe/Moscow")

    ADMIN_IDS: str = _env("ADMIN_IDS", "")
    # --- AI (OpenAI) ---
    # AI включён, если задан ключ. UX не меняем: AI помогает выбирать сценарии/тексты/цены.
    OPENAI_API_KEY: str = _env("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = _env("OPENAI_MODEL", "gpt-4.1-mini")
    OPENAI_BASE_URL: str = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # Для безопасного запуска: можно жёстко отключить AI, даже если ключ задан.
    AI_ENABLED: int = _env_int("AI_ENABLED", 1, minimum=0, maximum=1)

    @property
    def admin_id_list(self) -> list[int]:
        """Список ID админов/суперадмина.

        Исторически в проекте использовались переменные ADMIN_ID и ADMIN_IDS.
        Важно: поддерживаем оба варианта, иначе супер-админ может потеряться
        (например, если задан только ADMIN_ID).
        """
        # 1) Разбираем ADMIN_IDS/ADMIN_ID напрямую из окружения.
        parsed = _parse_admin_ids_env()
        if parsed:
            return parsed

        # 2) Фолбэк на строковое поле ADMIN_IDS из объекта Settings.
        raw = (self.ADMIN_IDS or "").strip()
        if not raw:
            return []
        out: list[int] = []
        for p in raw.replace(";", ",").split(","):
            p = p.strip()
            if not p:
                continue
            try:
                out.append(int(p))
            except ValueError:
                continue
        return out


settings = Settings()


def _fail_fast_prod_config() -> None:
    """Fail fast in prod if critical env vars are missing or inconsistent.

    In production we do not allow quiet defaults for secrets, webhook ingress,
    or partially configured channel ingress. This catches server mixups before the
    selected channel runtime starts accepting users from ads.
    """
    if APP_ENV not in {"prod", "production"}:
        return

    missing: list[str] = []
    telegram_runtime = bool(settings.CLIENTPLATFORM_TELEGRAM_RUNTIME_ENABLED)
    if telegram_runtime:
        if not (settings.BOT_TOKEN or '').strip():
            missing.append('BOT_TOKEN')
        if not (os.getenv('ADMIN_IDS') or os.getenv('ADMIN_ID') or '').strip():
            missing.append('ADMIN_IDS')

    legacy_messenger = bool(settings.MESSENGER_WEBHOOK_ENABLED)
    max_flag = _optional_feature_flag('MAX_WEBHOOK_ENABLED')
    vk_flag = _optional_feature_flag('VK_WEBHOOK_ENABLED')
    max_enabled = max_flag if max_flag is not None else bool(legacy_messenger and settings.MAX_BOT_TOKEN)
    vk_enabled = vk_flag if vk_flag is not None else bool(legacy_messenger and settings.VK_GROUP_TOKEN)

    telegram_webhook = telegram_runtime and (
        (settings.TELEGRAM_TRANSPORT == 'webhook')
        or bool(settings.TELEGRAM_WEBHOOK_ENABLED)
    )
    if telegram_webhook:
        if not (settings.TELEGRAM_WEBHOOK_PUBLIC_BASE_URL or '').strip():
            missing.append('TELEGRAM_WEBHOOK_PUBLIC_BASE_URL')
        if not (settings.TELEGRAM_WEBHOOK_SECRET_TOKEN or '').strip():
            missing.append('TELEGRAM_WEBHOOK_SECRET_TOKEN')
        if not (settings.TELEGRAM_WEBHOOK_PUBLIC_BASE_URL or '').strip().startswith('https://'):
            raise SystemExit('TELEGRAM_WEBHOOK_PUBLIC_BASE_URL must start with https:// in prod webhook mode')
        if not (settings.TELEGRAM_WEBHOOK_PREFIX or '').strip().startswith('/'):
            raise SystemExit('TELEGRAM_WEBHOOK_PREFIX must start with / in prod webhook mode')

    if max_enabled:
        if not (settings.MESSENGER_PUBLIC_BASE_URL or '').strip():
            missing.append('MESSENGER_PUBLIC_BASE_URL')
        if not (settings.MAX_BOT_TOKEN or '').strip():
            missing.append('MAX_BOT_TOKEN')
        if not (settings.MAX_BOT_LINK_BASE or '').strip():
            missing.append('MAX_BOT_LINK_BASE')
        if not (settings.MAX_WEBHOOK_SECRET or '').strip():
            missing.append('MAX_WEBHOOK_SECRET')

    if vk_enabled:
        if not (settings.MESSENGER_PUBLIC_BASE_URL or '').strip():
            missing.append('MESSENGER_PUBLIC_BASE_URL')
        if not (settings.VK_GROUP_TOKEN or '').strip():
            missing.append('VK_GROUP_TOKEN')
        if not (settings.VK_CONFIRMATION_TOKEN or '').strip():
            missing.append('VK_CONFIRMATION_TOKEN')
        if not (settings.VK_GROUP_ID or '').strip():
            missing.append('VK_GROUP_ID')
        if not (settings.VK_SECRET or '').strip():
            missing.append('VK_SECRET')

    if not bool(settings.HEALTHCHECK_ENABLED):
        raise SystemExit('HEALTHCHECK_ENABLED must be 1 in prod')

    if missing:
        raise SystemExit(
            "Missing required environment variables for prod: "
            + ", ".join(sorted(set(missing)))
            + "\nSet them as system environment variables (recommended). .env is loaded only in dev/stage (or LOAD_DOTENV=1)."
        )


_fail_fast_prod_config()
