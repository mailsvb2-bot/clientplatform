from __future__ import annotations
"""Backward-compatible public database package.

Write-capable runtime helpers are implemented in :mod:`services.db.core`.
The public read-only context is deliberately provided by
:mod:`services.db.read_only`, which enables and verifies database-level
read-only mode before yielding a connection wrapper.
"""

from services.db.core import (
    DB_PATH,
    PROJECT_ROOT,
    atomic_db,
    db,
    execute,
    get_connection,
    get_db,
    mark_delivery_once,
    tx,
    unmark_delivery,
    was_delivered,
    write,
)
from services.db.read_only import get_db_ro

from services.db import schema

import sys as _sys
import types as _types


class _CallableDbPackage(_types.ModuleType):
    def __call__(self, *args, **kwargs):
        return db(*args, **kwargs)


_sys.modules[__name__].__class__ = _CallableDbPackage
