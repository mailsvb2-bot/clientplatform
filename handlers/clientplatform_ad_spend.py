"""Compose the ClientPlatform advertising-spend Telegram presentation adapter."""

from clientplatform.presentation.ad_spend_telegram import (
    AdSpendConsentState,
    confirm_ad_spend_consent,
    install_ad_spend_controls,
    open_ad_spend_controls,
    receive_ad_spend_daily_cap,
    receive_ad_spend_hard_cap,
    revoke_ad_spend,
    router,
)

__all__ = [
    "AdSpendConsentState",
    "confirm_ad_spend_consent",
    "install_ad_spend_controls",
    "open_ad_spend_controls",
    "receive_ad_spend_daily_cap",
    "receive_ad_spend_hard_cap",
    "revoke_ad_spend",
    "router",
]
