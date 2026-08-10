from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar

_T = TypeVar("_T")
DEFAULT_PAGE_SIZE = 12


@dataclass(frozen=True)
class Page(Generic[_T]):
    items: tuple[_T, ...]
    index: int
    count: int
    total_items: int

    @property
    def has_previous(self) -> bool:
        return self.index > 0

    @property
    def has_next(self) -> bool:
        return self.index + 1 < self.count


def paginate(
    items: Sequence[_T],
    raw_page: object = 0,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page[_T]:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    try:
        requested = int(str(raw_page).strip())
    except (TypeError, ValueError):
        requested = 0
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    index = min(max(requested, 0), page_count - 1)
    start = index * page_size
    return Page(
        items=tuple(items[start : start + page_size]),
        index=index,
        count=page_count,
        total_items=len(items),
    )
