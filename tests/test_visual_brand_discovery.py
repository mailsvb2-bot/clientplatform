from __future__ import annotations

import socket

import pytest

from clientplatform.application.visual_brand_discovery import (
    VisualBrandDiscoveryError,
    suggest_brand_from_html,
    validate_public_website_url,
)
from clientplatform.domain.visual_brand import TenantBrandDNA


def _resolver(address: str):
    def resolve(host: str, port: int, *, type: int):  # noqa: A002
        assert host
        assert port in {80, 443}
        assert type == socket.SOCK_STREAM
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


def test_validate_public_website_url_normalizes_and_pins_public_dns() -> None:
    target = validate_public_website_url(
        "HTTPS://Example.COM/brand?from=cp#ignored",
        resolver=_resolver("93.184.216.34"),
    )

    assert target.url == "https://example.com/brand?from=cp"
    assert target.hostname == "example.com"
    assert target.port == 443
    assert target.addresses == ("93.184.216.34",)


@pytest.mark.parametrize(
    "url,address,error",
    [
        ("http://127.0.0.1/", "127.0.0.1", "brand_website_private_address_forbidden"),
        ("http://10.1.2.3/", "10.1.2.3", "brand_website_private_address_forbidden"),
        ("http://169.254.1.1/", "169.254.1.1", "brand_website_private_address_forbidden"),
        ("http://[::1]/", "::1", "brand_website_private_address_forbidden"),
        ("https://user:pass@example.com/", "93.184.216.34", "brand_website_credentials_forbidden"),
        ("https://example.com:8443/", "93.184.216.34", "brand_website_nonstandard_port_forbidden"),
    ],
)
def test_validate_public_website_url_rejects_ssrf_shapes(
    url: str,
    address: str,
    error: str,
) -> None:
    with pytest.raises(VisualBrandDiscoveryError, match=error):
        validate_public_website_url(url, resolver=_resolver(address))


def test_validate_public_website_url_rejects_mixed_public_private_dns() -> None:
    def resolver(host: str, port: int, *, type: int):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port)),
        ]

    with pytest.raises(
        VisualBrandDiscoveryError,
        match="brand_website_private_address_forbidden",
    ):
        validate_public_website_url("https://example.com/", resolver=resolver)


def test_brand_discovery_uses_strong_site_signals_and_preserves_safety_rules() -> None:
    current = TenantBrandDNA(
        business_id="business-1",
        display_name="Old name",
        tone=("trustworthy", "human"),
        visual_keywords=("editorial",),
        forbidden_visuals=("fake reviews", "invented statistics"),
        primary_color="#172033",
        accent_color="#E9C46A",
        text_color="#FFFFFF",
    )
    html = """
    <html>
      <head>
        <meta property="og:site_name" content="North Star Clinic">
        <meta name="theme-color" content="#123456">
        <meta name="description" content="Современный спокойный экспертный подход">
        <style>
          :root {
            --brand-primary: #111111;
            --brand-accent: #ABCDEF;
            --text-on-primary: #F0F0F0;
          }
        </style>
        <script type="application/ld+json">
          {"@type":"Organization","name":"North Star Clinic","logo":"/logo.svg"}
        </script>
      </head>
      <body><img class="brand-logo" src="/logo.svg"></body>
    </html>
    """

    suggestion = suggest_brand_from_html(
        business_id="business-1",
        source_url="https://example.com/",
        html=html,
        current=current,
    )

    assert suggestion.brand.display_name == "North Star Clinic"
    assert suggestion.brand.primary_color == "#123456"
    assert suggestion.brand.accent_color == "#ABCDEF"
    assert suggestion.brand.text_color == "#F0F0F0"
    assert suggestion.brand.tone == current.tone
    assert suggestion.brand.forbidden_visuals == current.forbidden_visuals
    assert suggestion.brand.visual_keywords == (
        "editorial",
        "modern",
        "calm",
        "expert",
    )
    assert set(suggestion.changed_fields) == {
        "display_name",
        "visual_keywords",
        "primary_color",
        "accent_color",
        "text_color",
    }
    assert set(suggestion.evidence) == {
        "site_name",
        "theme_color",
        "css_brand_variables",
        "site_metadata",
        "logo_reference_detected",
    }


def test_brand_discovery_does_not_invent_values_when_site_has_no_strong_signals() -> None:
    current = TenantBrandDNA(
        business_id="business-1",
        display_name="Existing",
        visual_keywords=("human",),
        primary_color="#112233",
        accent_color="#445566",
        text_color="#FFFFFF",
    ).normalized()

    suggestion = suggest_brand_from_html(
        business_id="business-1",
        source_url="https://example.com/",
        html="<html><body><h1>Hello</h1><p>ordinary copy</p></body></html>",
        current=current,
    )

    assert suggestion.brand == current
    assert suggestion.changed_fields == ()
    assert suggestion.evidence == ()
    assert suggestion.has_changes is False


def test_validate_public_website_url_percent_encodes_unicode_path_and_query() -> None:
    target = validate_public_website_url(
        "https://example.com/бренд?utm=тест",
        resolver=_resolver("93.184.216.34"),
    )

    assert target.url == (
        "https://example.com/%D0%B1%D1%80%D0%B5%D0%BD%D0%B4"
        "?utm=%D1%82%D0%B5%D1%81%D1%82"
    )
