from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import SplitResult, quote, urljoin, urlsplit, urlunsplit

from clientplatform.domain.visual_brand import TenantBrandDNA

_MAX_HTML_BYTES = 524_288
_MAX_REDIRECTS = 3
_TIMEOUT_SECONDS = 5.0
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_CUSTOM_COLOR_RE = re.compile(
    r"--(?P<name>[A-Za-z0-9_-]*(?:brand|primary|accent|secondary|text)[A-Za-z0-9_-]*)"
    r"\s*:\s*(?P<color>#[0-9A-Fa-f]{6})",
    re.IGNORECASE,
)
_JSON_LD_TYPES = frozenset({"organization", "localbusiness", "professionalservice"})


class VisualBrandDiscoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicWebsiteTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WebsiteBrandSuggestion:
    source_url: str
    brand: TenantBrandDNA
    changed_fields: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_fields)


class _BrandHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.site_names: list[str] = []
        self.theme_colors: list[str] = []
        self.logo_refs: list[str] = []
        self.style_parts: list[str] = []
        self.meta_text: list[str] = []
        self._style_depth = 0
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in attrs
            if key
        }

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        token = tag.lower()
        if token == "style":
            self._style_depth += 1
            return
        if token == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_ld_depth += 1
            self._json_ld_parts = []
            return
        if token == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key in {"og:site_name", "application-name"} and content:
                self.site_names.append(content)
            elif key == "theme-color" and content:
                self.theme_colors.append(content)
            elif key in {"description", "og:description", "keywords"} and content:
                self.meta_text.append(content)
            elif key in {"logo", "og:logo"} and content:
                self.logo_refs.append(content)
            return
        if token == "link":
            rel = {part.casefold() for part in values.get("rel", "").split()}
            href = values.get("href", "").strip()
            if "logo" in rel and href:
                self.logo_refs.append(href)
            return
        if token == "img":
            marker = " ".join(
                values.get(name, "") for name in ("id", "class", "alt", "aria-label")
            ).casefold()
            src = values.get("src", "").strip()
            if "logo" in marker and src:
                self.logo_refs.append(src)
        style = values.get("style", "")
        if style:
            self.style_parts.append(style)

    def handle_endtag(self, tag: str) -> None:
        token = tag.lower()
        if token == "style" and self._style_depth:
            self._style_depth -= 1
        elif token == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1
            if self._json_ld_depth == 0 and self._json_ld_parts:
                self._consume_json_ld("".join(self._json_ld_parts))
                self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self.style_parts.append(data)
        elif self._json_ld_depth:
            self._json_ld_parts.append(data)

    def _consume_json_ld(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        for item in _walk_json_objects(payload):
            kind = item.get("@type")
            kinds = (
                {str(kind).casefold()}
                if isinstance(kind, str)
                else {str(value).casefold() for value in kind}
                if isinstance(kind, list)
                else set()
            )
            if kinds & _JSON_LD_TYPES:
                name = str(item.get("name") or "").strip()
                if name:
                    self.site_names.append(name)
                logo = item.get("logo")
                if isinstance(logo, dict):
                    logo = logo.get("url")
                if isinstance(logo, str) and logo.strip():
                    self.logo_refs.append(logo.strip())


def _walk_json_objects(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child)


def _normalize_color(value: object) -> str | None:
    token = str(value or "").strip().upper()
    return token if _COLOR_RE.fullmatch(token) else None


def _host_ascii(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise VisualBrandDiscoveryError("brand_website_hostname_invalid") from exc


def _normalized_url(raw_url: str) -> tuple[str, SplitResult]:
    token = str(raw_url or "").strip()
    if not token:
        raise VisualBrandDiscoveryError("brand_website_url_required")
    try:
        parsed = urlsplit(token)
        port = parsed.port
    except ValueError as exc:
        raise VisualBrandDiscoveryError("brand_website_url_invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise VisualBrandDiscoveryError("brand_website_url_must_be_http")
    if parsed.username is not None or parsed.password is not None:
        raise VisualBrandDiscoveryError("brand_website_credentials_forbidden")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    selected_port = port or default_port
    if selected_port not in {80, 443}:
        raise VisualBrandDiscoveryError("brand_website_nonstandard_port_forbidden")
    hostname = _host_ascii(parsed.hostname)
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if selected_port != default_port:
        netloc = f"{hostname}:{selected_port}"
    path = quote(parsed.path or "/", safe="/:@-._~!$&'()*+,;=%")
    query = quote(parsed.query, safe="/?:@-._~!$'()*+,;=&%")
    normalized = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    return normalized, urlsplit(normalized)


def _resolved_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    try:
        resolved = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise VisualBrandDiscoveryError("brand_website_dns_failed") from exc
    addresses = tuple(sorted({str(item[4][0]) for item in resolved if len(item) >= 5}))
    if not addresses:
        raise VisualBrandDiscoveryError("brand_website_dns_empty")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise VisualBrandDiscoveryError("brand_website_dns_invalid") from exc
        if not ip.is_global:
            raise VisualBrandDiscoveryError("brand_website_private_address_forbidden")
    return addresses


def validate_public_website_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
) -> PublicWebsiteTarget:
    normalized, parsed = _normalized_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    hostname = _host_ascii(str(parsed.hostname or ""))
    return PublicWebsiteTarget(
        url=normalized,
        scheme=parsed.scheme,
        hostname=hostname,
        port=port,
        addresses=_resolved_addresses(hostname, port, resolver=resolver),
    )


def _request_target(target: PublicWebsiteTarget, *, address: str) -> tuple[int, dict[str, str], bytes]:
    raw_socket: socket.socket | None = None
    stream: socket.socket | ssl.SSLSocket | None = None
    try:
        raw_socket = socket.create_connection((address, target.port), timeout=_TIMEOUT_SECONDS)
        raw_socket.settimeout(_TIMEOUT_SECONDS)
        stream = raw_socket
        if target.scheme == "https":
            context = ssl.create_default_context()
            stream = context.wrap_socket(raw_socket, server_hostname=target.hostname)
        parsed = urlsplit(target.url)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path = f"{request_path}?{parsed.query}"
        default_port = 443 if target.scheme == "https" else 80
        host_header = target.hostname
        if target.port != default_port:
            host_header = f"{host_header}:{target.port}"
        request = (
            f"GET {request_path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "User-Agent: ClientPlatformBrandDiscovery/1.0\r\n"
            "Accept: text/html,application/xhtml+xml\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        stream.sendall(request)
        response = http.client.HTTPResponse(stream)
        response.begin()
        headers = {str(key).lower(): str(value) for key, value in response.getheaders()}
        length = headers.get("content-length", "").strip()
        if length:
            try:
                declared = int(length)
            except ValueError as exc:
                raise VisualBrandDiscoveryError("brand_website_content_length_invalid") from exc
            if declared > _MAX_HTML_BYTES:
                raise VisualBrandDiscoveryError("brand_website_too_large")
        body = response.read(_MAX_HTML_BYTES + 1)
        if len(body) > _MAX_HTML_BYTES:
            raise VisualBrandDiscoveryError("brand_website_too_large")
        return int(response.status), headers, body
    except VisualBrandDiscoveryError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise VisualBrandDiscoveryError("brand_website_fetch_failed") from exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        elif raw_socket is not None:
            try:
                raw_socket.close()
            except OSError:
                pass


def fetch_public_website_html(
    url: str,
    *,
    resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
) -> tuple[str, str]:
    current = str(url or "").strip()
    for redirect_count in range(_MAX_REDIRECTS + 1):
        target = validate_public_website_url(current, resolver=resolver)
        status, headers, body = _request_target(target, address=target.addresses[0])
        if status in _REDIRECT_STATUSES:
            if redirect_count >= _MAX_REDIRECTS:
                raise VisualBrandDiscoveryError("brand_website_too_many_redirects")
            location = headers.get("location", "").strip()
            if not location:
                raise VisualBrandDiscoveryError("brand_website_redirect_without_location")
            current = urljoin(target.url, location)
            continue
        if status != 200:
            raise VisualBrandDiscoveryError("brand_website_http_status")
        content_encoding = headers.get("content-encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise VisualBrandDiscoveryError("brand_website_encoding_unsupported")
        content_type = headers.get("content-type", "").strip().lower()
        if not (
            content_type.startswith("text/html")
            or content_type.startswith("application/xhtml+xml")
        ):
            raise VisualBrandDiscoveryError("brand_website_html_required")
        charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError as exc:
            raise VisualBrandDiscoveryError("brand_website_charset_invalid") from exc
        return target.url, text
    raise VisualBrandDiscoveryError("brand_website_too_many_redirects")


def _first_clean(values: Iterable[str], *, limit: int) -> str:
    for value in values:
        token = " ".join(str(value or "").split())[:limit].strip()
        if token:
            return token
    return ""


def _css_named_colors(style_text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _CUSTOM_COLOR_RE.finditer(style_text):
        name = match.group("name").casefold()
        color = _normalize_color(match.group("color"))
        if color and name not in found:
            found[name] = color
    return found


def _pick_named_color(named: dict[str, str], words: tuple[str, ...]) -> str | None:
    for name, color in named.items():
        if any(word in name for word in words):
            return color
    return None


def _semantic_keywords(text: str) -> tuple[str, ...]:
    folded = text.casefold()
    vocabulary = (
        (("минимал", "minimal"), "minimal"),
        (("современ", "modern"), "modern"),
        (("преми", "premium", "luxury"), "premium"),
        (("спокой", "calm", "gentle"), "calm"),
        (("натурал", "natural", "organic"), "natural"),
        (("технолог", "tech", "digital"), "technology"),
        (("эксперт", "expert", "professional"), "expert"),
        (("дружелюб", "friendly", "human"), "human"),
    )
    return tuple(label for needles, label in vocabulary if any(item in folded for item in needles))[:6]


def suggest_brand_from_html(
    *,
    business_id: str,
    source_url: str,
    html: str,
    current: TenantBrandDNA | None = None,
) -> WebsiteBrandSuggestion:
    base = (current or TenantBrandDNA(business_id=business_id)).normalized()
    base.assert_business(business_id)
    normalized_url, _parsed = _normalized_url(source_url)
    parser = _BrandHTMLParser()
    parser.feed(str(html or "")[:_MAX_HTML_BYTES])
    style_text = "\n".join(parser.style_parts)
    named = _css_named_colors(style_text)
    theme = next(
        (color for color in (_normalize_color(item) for item in parser.theme_colors) if color),
        None,
    )
    primary = theme or _pick_named_color(named, ("primary", "brand")) or base.primary_color
    accent = _pick_named_color(named, ("accent", "secondary")) or base.accent_color
    text_color = _pick_named_color(named, ("text", "foreground", "on-primary")) or base.text_color
    display_name = _first_clean(parser.site_names, limit=120) or base.display_name
    discovered_keywords = _semantic_keywords(" ".join(parser.meta_text))
    visual_keywords = tuple(dict.fromkeys((*base.visual_keywords, *discovered_keywords)))[:12]
    brand = TenantBrandDNA(
        business_id=base.business_id,
        display_name=display_name,
        tone=base.tone,
        visual_keywords=visual_keywords,
        forbidden_visuals=base.forbidden_visuals,
        primary_color=primary,
        accent_color=accent,
        text_color=text_color,
    ).normalized()
    fields = (
        "display_name",
        "visual_keywords",
        "primary_color",
        "accent_color",
        "text_color",
    )
    changed = tuple(name for name in fields if getattr(base, name) != getattr(brand, name))
    evidence: list[str] = []
    if display_name != base.display_name:
        evidence.append("site_name")
    if theme:
        evidence.append("theme_color")
    if named:
        evidence.append("css_brand_variables")
    if discovered_keywords:
        evidence.append("site_metadata")
    if parser.logo_refs:
        # A discovered logo is useful evidence, but it is not persisted or sent to
        # generation until the canonical render path supports image-conditioned logos.
        evidence.append("logo_reference_detected")
    return WebsiteBrandSuggestion(
        source_url=normalized_url,
        brand=brand,
        changed_fields=changed,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def discover_brand_from_website(
    *,
    business_id: str,
    website_url: str,
    current: TenantBrandDNA | None = None,
) -> WebsiteBrandSuggestion:
    final_url, html = fetch_public_website_html(website_url)
    return suggest_brand_from_html(
        business_id=business_id,
        source_url=final_url,
        html=html,
        current=current,
    )


__all__ = [
    "PublicWebsiteTarget",
    "VisualBrandDiscoveryError",
    "WebsiteBrandSuggestion",
    "discover_brand_from_website",
    "fetch_public_website_html",
    "suggest_brand_from_html",
    "validate_public_website_url",
]
