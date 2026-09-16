from __future__ import annotations

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from services.messenger.reply_dispatcher import _plain_language_clientplatform_interaction


def _render(text: str) -> CustomerInteractionMessage:
    return _plain_language_clientplatform_interaction(
        CustomerInteractionMessage(
            text=text,
            rows=((CustomerInteractionButton(label="Оплаты", command="cpm:payments"),),),
        )
    )


def test_native_messenger_dispatch_hides_internal_revenue_jargon() -> None:
    rendered = _render(
        "✅ Оплата сохранена: 3 500.00 RUB. Канонический факт выручки подтверждён."
    )
    assert "Выручка учтена в результатах бизнеса" in rendered.text
    assert "каноничес" not in rendered.text.casefold()


def test_native_messenger_dispatch_hides_internal_refund_jargon() -> None:
    rendered = _render(
        "Подтверждение изменит статус оплаты и создаст отдельный канонический факт возврата."
    )
    assert "возврат в результатах бизнеса" in rendered.text
    assert "каноничес" not in rendered.text.casefold()
