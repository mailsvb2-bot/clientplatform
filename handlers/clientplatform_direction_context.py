from __future__ import annotations

from dataclasses import dataclass

from aiogram.fsm.context import FSMContext

_DIRECTION_ID_KEY = "clientplatform_direction_context_id"
_DIRECTION_TITLE_KEY = "clientplatform_direction_context_title"
_DIRECTION_BUSINESS_KEY = "clientplatform_direction_context_business_id"


@dataclass(frozen=True, slots=True)
class DirectionContext:
    business_id: str
    direction_id: str
    title: str


async def read_direction_context(
    state: FSMContext,
    *,
    business_id: str,
) -> DirectionContext | None:
    data = await state.get_data()
    if not isinstance(data, dict):
        return None
    if str(data.get(_DIRECTION_BUSINESS_KEY) or "") != str(business_id):
        return None
    direction_id = str(data.get(_DIRECTION_ID_KEY) or "").strip()
    title = " ".join(str(data.get(_DIRECTION_TITLE_KEY) or "").split()).strip()
    if not direction_id or not title:
        return None
    return DirectionContext(
        business_id=str(business_id),
        direction_id=direction_id,
        title=title,
    )


async def set_direction_context(
    state: FSMContext,
    *,
    business_id: str,
    direction_id: str,
    title: str,
) -> DirectionContext:
    context = DirectionContext(
        business_id=str(business_id),
        direction_id=str(direction_id),
        title=" ".join(str(title or "").split()).strip(),
    )
    if not context.title:
        raise ValueError("direction context title must not be empty")
    data = await state.get_data()
    data.update(
        {
            _DIRECTION_BUSINESS_KEY: context.business_id,
            _DIRECTION_ID_KEY: context.direction_id,
            _DIRECTION_TITLE_KEY: context.title,
        }
    )
    await state.set_data(data)
    return context


async def clear_direction_context(state: FSMContext) -> None:
    data = await state.get_data()
    for key in (
        _DIRECTION_BUSINESS_KEY,
        _DIRECTION_ID_KEY,
        _DIRECTION_TITLE_KEY,
    ):
        data.pop(key, None)
    await state.set_data(data)


async def clear_flow_preserving_direction(
    state: FSMContext,
    *,
    business_id: str,
) -> DirectionContext | None:
    context = await read_direction_context(state, business_id=business_id)
    await state.clear()
    if context is not None:
        await set_direction_context(
            state,
            business_id=context.business_id,
            direction_id=context.direction_id,
            title=context.title,
        )
    return context


def direction_heading(context: DirectionContext | None) -> str:
    if context is None:
        return ""
    return f"🧭 Направление: {context.title}\n\n"


__all__ = [
    "DirectionContext",
    "clear_direction_context",
    "clear_flow_preserving_direction",
    "direction_heading",
    "read_direction_context",
    "set_direction_context",
]
