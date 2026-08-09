from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from clientplatform.domain.programs import ContentKind, normalize_content_kind


class ExternalMediaProvider(StrEnum):
    YANDEX_DISK = "yandex_disk"
    GOOGLE_DRIVE = "google_drive"
    DROPBOX = "dropbox"
    ONEDRIVE = "onedrive"
    STREAMING = "streaming"
    DIRECT = "direct"


_PROVIDER_LABELS = {
    ExternalMediaProvider.YANDEX_DISK: "Яндекс Диск",
    ExternalMediaProvider.GOOGLE_DRIVE: "Google Drive",
    ExternalMediaProvider.DROPBOX: "Dropbox",
    ExternalMediaProvider.ONEDRIVE: "OneDrive",
    ExternalMediaProvider.STREAMING: "видеосервис",
    ExternalMediaProvider.DIRECT: "внешнее облако",
}
_STREAMING_ROOT_HOSTS = {
    "youtube.com",
    "youtu.be",
    "rutube.ru",
    "vk.com",
    "vimeo.com",
}


@dataclass(frozen=True, slots=True)
class ExternalMediaReference:
    url: str
    provider: ExternalMediaProvider

    @property
    def provider_label(self) -> str:
        return _PROVIDER_LABELS[self.provider]


def _normalized_host(value: str) -> str:
    host = str(value or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError("Ссылка должна содержать адрес сайта")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Локальные адреса нельзя использовать как материал")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    if not address.is_global:
        raise ValueError("Внутренние и локальные IP-адреса запрещены")
    return host


def _host_is_or_subdomain(host: str, root: str) -> bool:
    """Match one DNS boundary without accepting lookalikes such as evilyoutube.com."""

    return host == root or host.endswith(f".{root}")


def detect_external_media_provider(url: str) -> ExternalMediaProvider:
    host = _normalized_host(urlsplit(str(url or "").strip()).hostname or "")
    if host == "disk.yandex.ru" or host.endswith(".disk.yandex.ru") or host == "yadi.sk":
        return ExternalMediaProvider.YANDEX_DISK
    if host == "drive.google.com" or host.endswith(".drive.google.com"):
        return ExternalMediaProvider.GOOGLE_DRIVE
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        return ExternalMediaProvider.DROPBOX
    if host in {"1drv.ms", "onedrive.live.com"} or host.endswith(".onedrive.live.com"):
        return ExternalMediaProvider.ONEDRIVE
    if any(_host_is_or_subdomain(host, root) for root in _STREAMING_ROOT_HOSTS):
        return ExternalMediaProvider.STREAMING
    return ExternalMediaProvider.DIRECT


def normalize_external_media_url(value: str) -> ExternalMediaReference:
    raw = str(value or "").strip()
    if len(raw) > 2048:
        raise ValueError("Ссылка слишком длинная")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("Нужна публичная ссылка, начинающаяся с https://")
    if parsed.username or parsed.password:
        raise ValueError("Ссылка не должна содержать логин или пароль")
    host = _normalized_host(parsed.hostname or "")
    port = parsed.port
    if port not in {None, 443}:
        raise ValueError("Ссылка должна использовать обычный защищённый HTTPS-доступ")
    normalized = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return ExternalMediaReference(
        url=normalized,
        provider=detect_external_media_provider(normalized),
    )


def external_delivery_kind(
    reference: ExternalMediaReference,
    requested: ContentKind | str,
) -> ContentKind:
    kind = normalize_content_kind(requested)
    if reference.provider == ExternalMediaProvider.STREAMING:
        return ContentKind.LINK
    return kind


def google_drive_direct_url(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    file_id = ""
    if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
        file_id = parts[2]
    if not file_id:
        file_id = parse_qs(parsed.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        raise ValueError("Не удалось определить файл Google Drive. Откройте доступ по ссылке")
    return f"https://drive.google.com/uc?export=download&id={quote(file_id, safe='')}"


def dropbox_direct_url(url: str) -> str:
    parsed = urlsplit(url)
    host = "dl.dropboxusercontent.com"
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("dl", None)
    query.pop("raw", None)
    return urlunsplit(("https", host, parsed.path, urlencode(query, doseq=True), ""))


def onedrive_direct_url(url: str) -> str:
    # Microsoft documents the public shares API URL as a stable content endpoint.
    import base64

    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"


__all__ = [
    "ExternalMediaProvider",
    "ExternalMediaReference",
    "detect_external_media_provider",
    "dropbox_direct_url",
    "external_delivery_kind",
    "google_drive_direct_url",
    "normalize_external_media_url",
    "onedrive_direct_url",
]