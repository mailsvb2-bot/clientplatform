        return YandexArtProvider(cfg)
    if normalized == "yandexart_motion":
        return YandexArtMotionVideoProvider(cfg)
    if normalized == "gigachat":
        return GigaChatImageProvider(cfg)
    if normalized == "openai":
        return OpenAIVisualProvider(cfg)
    if normalized == "runway":
        return RunwayVisualProvider(cfg)
    return SelfHostedVisualProvider(cfg)


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def _country_order(kind: str, country: str) -> tuple[str, ...]:
    token = re.sub(r"[^A-Z0-9]", "", str(country or "").upper())
    if not token:
        return ()
    raw = _env(f"VISUAL_{token}_{kind.upper()}_ORDER", "").strip()
    return _csv(f"VISUAL_{token}_{kind.upper()}_ORDER", "") if raw else ()


def _policy_order(kind: str, country_code: str = "") -> tuple[str, ...]:
    env_explicit = _env("VISUAL_IMAGE_PROVIDER" if kind == "image" else "VISUAL_VIDEO_PROVIDER", "auto").strip().lower()
    if env_explicit and env_explicit != "auto":
        # Operator-level deployment configuration is authoritative. Request-level
        # overrides below are still constrained by the resulting deployment policy.
        return (env_explicit,)
    country = str(country_code or _env("VISUAL_DEPLOYMENT_COUNTRY", "RU")).strip().upper()
    country_specific = _country_order(kind, country)
    if country_specific:
        return country_specific
    if country in _RU_COUNTRIES:
        if kind == "image":
            order = _csv("VISUAL_RU_IMAGE_ORDER", "yandexart,gigachat,selfhosted")
        else:
            order = _csv("VISUAL_RU_VIDEO_ORDER", "selfhosted,selfhosted_backup,yandexart_motion")
        if _truthy("VISUAL_ALLOW_GLOBAL_PROVIDERS_IN_RU", "0"):
            global_order = _csv(
                "VISUAL_GLOBAL_IMAGE_ORDER" if kind == "image" else "VISUAL_GLOBAL_VIDEO_ORDER",
                "openai,runway,selfhosted" if kind == "image" else "runway,selfhosted,openai",
            )
            order = tuple(dict.fromkeys((*order, *global_order)))
        return order
    return _csv(
        "VISUAL_GLOBAL_IMAGE_ORDER" if kind == "image" else "VISUAL_GLOBAL_VIDEO_ORDER",
        "openai,runway,selfhosted" if kind == "image" else "runway,selfhosted,openai",
    )


def _video_provider_mode(name: str) -> str:
    """Classify video providers without exposing provider implementation details.

    Native providers synthesize moving frames. yandexart_motion is deliberately
    classified as a motion fallback because it animates one generated keyframe with
    ffmpeg rather than generating a scene over time.
    """

    return "motion" if str(name or "").strip().lower() == "yandexart_motion" else "native"


def _prefer_native_video_order(order: tuple[str, ...]) -> tuple[str, ...]:
    native = tuple(name for name in order if _video_provider_mode(name) == "native")
    motion = tuple(name for name in order if _video_provider_mode(name) == "motion")
    if _truthy("VISUAL_VIDEO_MOTION_PRIMARY", "0"):
        return tuple(dict.fromkeys((*motion, *native)))
    return tuple(dict.fromkeys((*native, *motion)))


def provider_order(kind: str, country_code: str = "", preferred_provider: str = "") -> tuple[str, ...]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    policy = _policy_order(normalized_kind, country_code)
    if normalized_kind == "video":
        policy = _prefer_native_video_order(policy)
    explicit = str(preferred_provider or "").strip().lower()
    if not explicit or explicit == "auto":
        return policy
    if not _truthy("VISUAL_ALLOW_REQUEST_PROVIDER_OVERRIDE", "0"):
        raise ValueError("visual_provider_override_disabled")
    if explicit not in policy:
        raise ValueError("visual_provider_not_allowed_by_country_policy")
    return (explicit,)


def configured_providers(kind: str, country_code: str = "") -> tuple[str, ...]:
    names: list[str] = []