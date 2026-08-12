"""Lazy ClientPlatform handler exports.

Production imports ``clientplatform_control`` from this package. Loading either
public ClientPlatform module first imports the entry router, which performs the
single idempotent router composition after all public modules are initialized.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def _load_clientplatform_modules() -> tuple[ModuleType, ModuleType]:
    entry = importlib.import_module(".clientplatform_entry", __name__)
    control = importlib.import_module(".clientplatform_control", __name__)
    globals()["clientplatform_entry"] = entry
    globals()["clientplatform_control"] = control

    admin = importlib.import_module(".clientplatform_admin", __name__)
    admin_callback_guard = importlib.import_module(
        ".clientplatform_admin_callback_guard",
        __name__,
    )
    admin_callback_guard.install_admin_callback_namespace_guard(admin, control)

    if not bool(getattr(entry, "_telegram_commands_startup_composed", False)):
        entry.router.startup.register(entry.register_clientplatform_bot_commands)
        entry._telegram_commands_startup_composed = True

    bot_setup = importlib.import_module(".clientplatform_bot_setup", __name__)
    globals()["clientplatform_bot_setup"] = bot_setup
    bot_setup.install_dashboard_button(control)

    managed_bot_onboarding = importlib.import_module(
        ".clientplatform_managed_bot_onboarding",
        __name__,
    )
    globals()["clientplatform_managed_bot_onboarding"] = managed_bot_onboarding
    managed_bot_onboarding.install_managed_bot_onboarding(bot_setup)
    if not bool(getattr(bot_setup, "_managed_bot_onboarding_composed", False)):
        bot_setup.router.include_router(managed_bot_onboarding.router)
        bot_setup._managed_bot_onboarding_composed = True

    existing_bot_onboarding = importlib.import_module(
        ".clientplatform_existing_bot_onboarding",
        __name__,
    )
    globals()["clientplatform_existing_bot_onboarding"] = existing_bot_onboarding
    existing_bot_onboarding.install_existing_bot_onboarding(bot_setup)
    if not bool(getattr(bot_setup, "_existing_bot_onboarding_composed", False)):
        bot_setup.router.include_router(existing_bot_onboarding.router)
        bot_setup._existing_bot_onboarding_composed = True

    bot_lifecycle = importlib.import_module(
        ".clientplatform_bot_lifecycle",
        __name__,
    )
    globals()["clientplatform_bot_lifecycle"] = bot_lifecycle
    bot_lifecycle.install_lifecycle_controls(bot_setup)
    if not bool(getattr(bot_setup, "_managed_bot_lifecycle_composed", False)):
        bot_setup.router.include_router(bot_lifecycle.router)
        bot_setup._managed_bot_lifecycle_composed = True

    if not bool(getattr(entry, "_managed_bot_setup_composed", False)):
        entry.router.include_router(bot_setup.router)
        entry._managed_bot_setup_composed = True

    admin_extension = importlib.import_module(
        ".clientplatform_admin_extension",
        __name__,
    )
    globals()["clientplatform_admin_extension"] = admin_extension
    admin_extension.install_admin_extension(entry.router, control)

    simple_experience = importlib.import_module(
        ".clientplatform_simple_experience",
        __name__,
    )
    globals()["clientplatform_simple_experience"] = simple_experience
    simple_experience.install_simple_experience(control)

    sales = importlib.import_module(".clientplatform_sales", __name__)
    sales_install = importlib.import_module(".clientplatform_sales_install", __name__)
    globals()["clientplatform_sales"] = sales
    globals()["clientplatform_sales_install"] = sales_install
    sales_install.install_sales_ui(simple_experience)
    if not bool(getattr(simple_experience, "_sales_ui_composed", False)):
        simple_experience.router.include_router(sales.router)
        simple_experience._sales_ui_composed = True

    owner_journey = importlib.import_module(
        ".clientplatform_owner_journey",
        __name__,
    )
    globals()["clientplatform_owner_journey"] = owner_journey
    owner_journey.install_owner_journey(entry, control, simple_experience)

    first_result = importlib.import_module(
        ".clientplatform_first_result",
        __name__,
    )
    globals()["clientplatform_first_result"] = first_result
    first_result.install_first_result(owner_journey)
    if not bool(getattr(simple_experience, "_first_result_composed", False)):
        simple_experience.router.include_router(first_result.router)
        simple_experience._first_result_composed = True

    public_storefront = importlib.import_module(
        ".clientplatform_public_storefront",
        __name__,
    )
    globals()["clientplatform_public_storefront"] = public_storefront
    public_storefront.install_public_storefront(owner_journey)

    promotion = importlib.import_module(
        ".clientplatform_promotion",
        __name__,
    )
    globals()["clientplatform_promotion"] = promotion
    promotion_install = importlib.import_module(
        ".clientplatform_promotion_install",
        __name__,
    )
    globals()["clientplatform_promotion_install"] = promotion_install
    promotion_install.install_promotion_engine(
        owner_module=owner_journey,
        simple_module=simple_experience,
    )

    yandex_screen_code = importlib.import_module(
        ".clientplatform_yandex_screen_code",
        __name__,
    )
    globals()["clientplatform_yandex_screen_code"] = yandex_screen_code

    yandex_analytics = importlib.import_module(
        ".clientplatform_yandex_analytics",
        __name__,
    )
    globals()["clientplatform_yandex_analytics"] = yandex_analytics
    if not bool(getattr(simple_experience, "_yandex_analytics_composed", False)):
        simple_experience.router.include_router(yandex_analytics.router)
        simple_experience._yandex_analytics_composed = True

    ad_connections = importlib.import_module(
        ".clientplatform_ad_connections",
        __name__,
    )
    globals()["clientplatform_ad_connections"] = ad_connections
    ad_disconnect = importlib.import_module(
        ".clientplatform_ad_disconnect",
        __name__,
    )
    globals()["clientplatform_ad_disconnect"] = ad_disconnect

    ad_spend = importlib.import_module(
        ".clientplatform_ad_spend",
        __name__,
    )
    globals()["clientplatform_ad_spend"] = ad_spend
    ad_spend.install_ad_spend_controls(control, simple_experience)

    one_click = importlib.import_module(
        ".clientplatform_one_click_experience",
        __name__,
    )
    globals()["clientplatform_one_click_experience"] = one_click
    canonical_owner_dashboard = owner_journey.send_owner_dashboard
    one_click.install_one_click_experience(
        owner_module=owner_journey,
        simple_module=simple_experience,
        control_module=control,
    )
    # Keep the canonical information-rich dashboard until the goal-first layer
    # is installed. One-click owns advertising orchestration; goal-first owns
    # the visible outcome and the minimum business questions.
    owner_journey.send_owner_dashboard = canonical_owner_dashboard
    simple_experience.send_simple_dashboard = canonical_owner_dashboard
    control._send_dashboard = canonical_owner_dashboard

    goal_first = importlib.import_module(
        ".clientplatform_goal_first_autopilot",
        __name__,
    )
    globals()["clientplatform_goal_first_autopilot"] = goal_first
    goal_dashboard = importlib.import_module(
        ".clientplatform_goal_dashboard",
        __name__,
    )
    globals()["clientplatform_goal_dashboard"] = goal_dashboard
    goal_first.send_goal_dashboard = goal_dashboard.send_goal_dashboard

    visual_brand = importlib.import_module(
        ".clientplatform_visual_brand",
        __name__,
    )
    globals()["clientplatform_visual_brand"] = visual_brand
    if not bool(getattr(simple_experience, "_visual_brand_composed", False)):
        simple_experience.router.include_router(visual_brand.router)
        simple_experience._visual_brand_composed = True

    goal_schedule = importlib.import_module(
        ".clientplatform_goal_schedule",
        __name__,
    )
    globals()["clientplatform_goal_schedule"] = goal_schedule
    # This router owns cpo:start first. If a slot already exists it delegates
    # to the canonical one-click handler; otherwise it creates the missing
    # business setup and then resumes that same handler automatically.
    if not bool(getattr(simple_experience, "_goal_schedule_composed", False)):
        simple_experience.router.include_router(goal_schedule.router)
        simple_experience._goal_schedule_composed = True
    if not bool(getattr(simple_experience, "_one_click_experience_composed", False)):
        simple_experience.router.include_router(one_click.router)
        simple_experience._one_click_experience_composed = True

    # Real paid launch has one owner. Compose it before goal-first presentation
    # callbacks so stale or duplicate launch actions cannot fall through.
    goal_launch = importlib.import_module(
        ".clientplatform_goal_launch",
        __name__,
    )
    globals()["clientplatform_goal_launch"] = goal_launch
    if not bool(getattr(simple_experience, "_goal_launch_composed", False)):
        simple_experience.router.include_router(goal_launch.router)
        simple_experience._goal_launch_composed = True

    goal_first.install_goal_first_autopilot(
        owner_module=owner_journey,
        simple_module=simple_experience,
        control_module=control,
    )

    goal_first_safety = importlib.import_module(
        ".clientplatform_goal_first_safety",
        __name__,
    )
    interaction_safety = importlib.import_module(
        ".clientplatform_interaction_safety",
        __name__,
    )
    goal_first_safety.install_goal_first_safety(interaction_safety)

    ad_media_monitor = importlib.import_module(
        "clientplatform.runtime.ad_media_monitor"
    )
    if not bool(getattr(entry, "_ad_media_monitor_composed", False)):
        entry.router.startup.register(ad_media_monitor.start_ad_media_monitor)
        entry.router.shutdown.register(ad_media_monitor.stop_ad_media_monitor)
        entry._ad_media_monitor_composed = True
    return entry, control


def __getattr__(name: str) -> ModuleType:
    if name == "clientplatform_control":
        _, control = _load_clientplatform_modules()
        return control
    if name == "clientplatform_entry":
        entry, _ = _load_clientplatform_modules()
        return entry
    if name == "clientplatform_bot_setup":
        _load_clientplatform_modules()
        return globals()["clientplatform_bot_setup"]
    if name == "clientplatform_managed_bot_onboarding":
        _load_clientplatform_modules()
        return globals()["clientplatform_managed_bot_onboarding"]
    if name == "clientplatform_existing_bot_onboarding":
        _load_clientplatform_modules()
        return globals()["clientplatform_existing_bot_onboarding"]
    if name == "clientplatform_bot_lifecycle":
        _load_clientplatform_modules()
        return globals()["clientplatform_bot_lifecycle"]
    if name == "clientplatform_admin_extension":
        _load_clientplatform_modules()
        return globals()["clientplatform_admin_extension"]
    if name == "clientplatform_owner_journey":
        _load_clientplatform_modules()
        return globals()["clientplatform_owner_journey"]
    if name == "clientplatform_first_result":
        _load_clientplatform_modules()
        return globals()["clientplatform_first_result"]
    if name == "clientplatform_public_storefront":
        _load_clientplatform_modules()
        return globals()["clientplatform_public_storefront"]
    if name == "clientplatform_promotion":
        _load_clientplatform_modules()
        return globals()["clientplatform_promotion"]
    if name == "clientplatform_promotion_install":
        _load_clientplatform_modules()
        return globals()["clientplatform_promotion_install"]
    if name == "clientplatform_sales":
        _load_clientplatform_modules()
        return globals()["clientplatform_sales"]
    if name == "clientplatform_sales_install":
        _load_clientplatform_modules()
        return globals()["clientplatform_sales_install"]
    if name == "clientplatform_yandex_screen_code":
        _load_clientplatform_modules()
        return globals()["clientplatform_yandex_screen_code"]
    if name == "clientplatform_yandex_analytics":
        _load_clientplatform_modules()
        return globals()["clientplatform_yandex_analytics"]
    if name == "clientplatform_ad_connections":
        _load_clientplatform_modules()
        return globals()["clientplatform_ad_connections"]
    if name == "clientplatform_ad_disconnect":
        _load_clientplatform_modules()
        return globals()["clientplatform_ad_disconnect"]
    if name == "clientplatform_ad_spend":
        _load_clientplatform_modules()
        return globals()["clientplatform_ad_spend"]
    if name == "clientplatform_one_click_experience":
        _load_clientplatform_modules()
        return globals()["clientplatform_one_click_experience"]
    if name == "clientplatform_goal_first_autopilot":
        _load_clientplatform_modules()
        return globals()["clientplatform_goal_first_autopilot"]
    if name == "clientplatform_goal_dashboard":
        _load_clientplatform_modules()
        return globals()["clientplatform_goal_dashboard"]
    if name == "clientplatform_goal_schedule":
        _load_clientplatform_modules()
        return globals()["clientplatform_goal_schedule"]
    if name == "clientplatform_goal_launch":
        _load_clientplatform_modules()
        return globals()["clientplatform_goal_launch"]
    raise AttributeError(name)


__all__ = [
    "clientplatform_ad_connections",
    "clientplatform_ad_disconnect",
    "clientplatform_ad_spend",
    "clientplatform_admin_extension",
    "clientplatform_bot_lifecycle",
    "clientplatform_bot_setup",
    "clientplatform_control",
    "clientplatform_existing_bot_onboarding",
    "clientplatform_first_result",
    "clientplatform_goal_dashboard",
    "clientplatform_goal_first_autopilot",
    "clientplatform_goal_schedule",
    "clientplatform_goal_launch",
    "clientplatform_managed_bot_onboarding",
    "clientplatform_one_click_experience",
    "clientplatform_owner_journey",
    "clientplatform_promotion",
    "clientplatform_promotion_install",
    "clientplatform_public_storefront",
    "clientplatform_sales",
    "clientplatform_sales_install",
    "clientplatform_simple_experience",
    "clientplatform_yandex_analytics",
    "clientplatform_yandex_screen_code",
    "clientplatform_entry",
]
