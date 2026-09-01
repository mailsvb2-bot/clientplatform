from __future__ import annotations

import asyncio
import html
import logging
import shutil
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path

import aiofiles
from aiohttp import web

from services.privacy_controls import write_user_data_export_gzip
from services.privacy_export_links import (
    PRIVACY_EXPORT_PREFIX,
    PrivacyExportGrant,
    claim_privacy_export_grant,
    get_privacy_export_grant,
    privacy_export_ttl_minutes,
)

log = logging.getLogger(__name__)

_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _export_paths() -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="clientplatform_privacy_download_"))
    return root, root / "clientplatform-user-data.json.gz"


def _cleanup_export_root(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def _landing_html(token: str) -> str:
    action = f"{PRIVACY_EXPORT_PREFIX}{urllib.parse.quote(token, safe='')}"
    ttl = privacy_export_ttl_minutes()
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Экспорт данных — ClientPlatform</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
    button {{ font: inherit; padding: .8rem 1.2rem; cursor: pointer; }}
    .note {{ color: #555; }}
  </style>
</head>
<body>
  <h1>Экспорт Ваших данных</h1>
  <p>Ссылка действует не более {ttl} минут и позволяет скачать архив только один раз.</p>
  <p class="note">Архив сжат, но не зашифрован. Сохраните его в защищённом месте.</p>
  <form method="post" action="{html.escape(action, quote=True)}">
    <button type="submit">Скачать архив</button>
  </form>
</body>
</html>"""


def _generation_failure_response(grant: PrivacyExportGrant) -> web.Response:
    log.exception("One-time privacy export generation failed: user_id=%s", grant.user_id)
    return web.Response(
        status=500,
        text="Не удалось подготовить экспорт данных. Повторите попытку позже.",
        content_type="text/plain",
        headers=_NO_STORE_HEADERS,
    )


async def privacy_export_landing(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    grant = await asyncio.to_thread(get_privacy_export_grant, token)
    if grant is None:
        raise web.HTTPNotFound(headers=_NO_STORE_HEADERS)
    return web.Response(
        text=_landing_html(token),
        content_type="text/html",
        charset="utf-8",
        headers={
            **_NO_STORE_HEADERS,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
            "X-Frame-Options": "DENY",
        },
    )


async def privacy_export_download(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    grant = await asyncio.to_thread(get_privacy_export_grant, token)
    if grant is None:
        raise web.HTTPNotFound(headers=_NO_STORE_HEADERS)

    root, export_path = await asyncio.to_thread(_export_paths)
    try:
        try:
            result = await asyncio.to_thread(
                write_user_data_export_gzip,
                int(grant.user_id),
                export_path,
            )
        except sqlite3.Error:
            return _generation_failure_response(grant)
        except RuntimeError:
            return _generation_failure_response(grant)
        except OSError:
            return _generation_failure_response(grant)
        except TypeError:
            return _generation_failure_response(grant)
        except ValueError:
            return _generation_failure_response(grant)

        claimed = await asyncio.to_thread(claim_privacy_export_grant, token)
        if claimed is None:
            raise web.HTTPNotFound(headers=_NO_STORE_HEADERS)

        response = web.StreamResponse(
            status=200,
            headers={
                **_NO_STORE_HEADERS,
                "Content-Type": "application/gzip",
                "Content-Disposition": "attachment; filename=clientplatform-user-data.json.gz",
                "Content-Length": str(int(result.compressed_size_bytes)),
            },
        )
        await response.prepare(request)
        async with aiofiles.open(result.path, mode="rb") as stream:
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    break
                await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        await asyncio.to_thread(_cleanup_export_root, root)
