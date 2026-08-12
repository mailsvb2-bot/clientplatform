from __future__ import annotations

from pathlib import Path

path = Path("clientplatform/infrastructure/sales_ai_provider.py")
text = path.read_text(encoding="utf-8")
old_import = "\nimport aiohttp\n"
if text.count(old_import) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:aiohttp_import_marker")
text = text.replace(old_import, "\n", 1)
old_transport = """    ) -> Mapping[str, Any]:
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        await _assert_public_destination(url)
"""
new_transport = """    ) -> Mapping[str, Any]:
        # Keep the provider module dependency-light: canon tests and non-network
        # code paths must be importable without the optional runtime HTTP stack.
        # Production already installs aiohttp via the locked aiogram dependency.
        try:
            import aiohttp
        except ImportError as exc:
            raise SalesAIProviderError(
                "aiohttp is required for Sales AI network transport"
            ) from exc
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        await _assert_public_destination(url)
"""
if text.count(old_transport) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:transport_marker")
path.write_text(text.replace(old_transport, new_transport, 1), encoding="utf-8")
