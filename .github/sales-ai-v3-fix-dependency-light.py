from __future__ import annotations

from pathlib import Path

path = Path("clientplatform/infrastructure/sales_ai_provider.py")
text = path.read_text(encoding="utf-8")

old_typing = "from typing import Any, Mapping, Protocol\n"
new_typing = "from typing import TYPE_CHECKING, Any, Mapping, Protocol\n"
if text.count(old_typing) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:typing_import_marker")
text = text.replace(old_typing, new_typing, 1)

old_http_import = "\nimport aiohttp\n"
if text.count(old_http_import) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:aiohttp_import_marker")
text = text.replace(old_http_import, "\n", 1)

old_secret_import = (
    "from clientplatform.runtime.secrets import EnvironmentCredentialProvider\n"
)
new_secret_import = (
    "if TYPE_CHECKING:\n"
    "    from clientplatform.runtime.secrets import EnvironmentCredentialProvider\n"
)
if text.count(old_secret_import) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:secret_import_marker")
text = text.replace(old_secret_import, new_secret_import, 1)

old_transport = """    ) -> Mapping[str, Any]:
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        await _assert_public_destination(url)
"""
new_transport = """    ) -> Mapping[str, Any]:
        # Keep the provider module dependency-light: canon tests and non-network
        # code paths must be importable without the runtime HTTP stack.
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
text = text.replace(old_transport, new_transport, 1)

old_credentials = (
    "        self._credentials = credential_provider or EnvironmentCredentialProvider()\n"
)
new_credentials = """        if credential_provider is None:
            # Secret decryption/credential machinery is production-only; importing
            # provider contracts with a fake transport must not require cryptography.
            from clientplatform.runtime.secrets import EnvironmentCredentialProvider

            credential_provider = EnvironmentCredentialProvider()
        self._credentials = credential_provider
"""
if text.count(old_credentials) != 1:
    raise SystemExit("SALES_AI_FIX_FAILED:credential_constructor_marker")
text = text.replace(old_credentials, new_credentials, 1)

path.write_text(text, encoding="utf-8")
