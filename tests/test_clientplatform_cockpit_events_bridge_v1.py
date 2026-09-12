from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from clientplatform.domain.tenancy import PlatformRole, TenantContext
from handlers import clientplatform_cockpit_dispatch as cockpit_dispatch

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_events_section_renders_current_funnel_and_creation_button() -> None:
    actor = TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=PlatformRole.OWNER,
    )
    snapshot = SimpleNamespace(
        can_manage=True,
        limitations=(),
        items=(
            SimpleNamespace(
                title="Вебинар по гипнозу",
                local_start="15.09.2026 19:00",
                registered=42,
                join_clicked=31,
                attendance_confirmed=24,
                offer_clicked=11,
                paid=5,
                revenue=(SimpleNamespace(display="25 000 RUB"),),
            ),
        ),
    )
    target = SimpleNamespace(answer=AsyncMock())
    captured_rows: list[list[tuple[str, str]]] = []

    def keyboard(rows: list[list[tuple[str, str]]]):
        captured_rows.extend(rows)
        return rows

    with (
        patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
        patch.object(cockpit_dispatch.one_click.control, "_keyboard", side_effect=keyboard),
    ):
        await cockpit_dispatch.send_cockpit_section(
            target,
            user_id=101,
            business_id=_BUSINESS,
            section="events",
        )

    target.answer.assert_awaited_once()
    text = target.answer.await_args.args[0]
    assert "Вебинары" in text
    assert "регистрации 42" in text
    assert "участие 24" in text
    assert "оплаты 5" in text
    assert "25 000 RUB" in text
    flattened = [button for row in captured_rows for button in row]
    assert any(label == "🎥 Создать вебинар" and callback.startswith("cpev:new:") for label, callback in flattened)


@pytest.mark.asyncio
async def test_events_section_hides_creation_for_read_only_manager_snapshot() -> None:
    actor = TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=PlatformRole.MANAGER,
    )
    snapshot = SimpleNamespace(can_manage=False, limitations=(), items=())
    target = SimpleNamespace(answer=AsyncMock())
    captured_rows: list[list[tuple[str, str]]] = []

    with (
        patch.object(cockpit_dispatch, "resolve_cockpit_events", return_value=snapshot),
        patch.object(cockpit_dispatch.one_click.control, "_keyboard", side_effect=lambda rows: captured_rows.extend(rows) or rows),
    ):
        await cockpit_dispatch.send_cockpit_section(
            target,
            user_id=101,
            business_id=_BUSINESS,
            section="events",
        )

    flattened = [button for row in captured_rows for button in row]
    assert not any(callback.startswith("cpev:new:") for _label, callback in flattened)
