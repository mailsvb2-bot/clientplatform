from __future__ import annotations

import os

from core.environment import normalize_app_env

# Canonicalize once, before any config submodule reads APP_ENV. This makes the
# supported aliases ``prod`` and ``production`` indistinguishable everywhere,
# including legacy modules that still read os.environ directly.
os.environ["APP_ENV"] = normalize_app_env()

from config.prod_contract import validate_production_contract  # noqa: E402

validate_production_contract()
