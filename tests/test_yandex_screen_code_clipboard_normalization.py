from __future__ import annotations

import unittest
from urllib.parse import parse_qs

from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_screen_code import (
    YANDEX_SCREEN_CODE_REDIRECT_URI,
    YandexScreenCodeDirectProvider,
    normalize_yandex_confirmation_code,
)


_CODE = "hh2vrvizj2nzbf2h"


class _CapturingTransport:
    def __init__(self) -> None:
        self.body: bytes | None = None

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, dict[str, str], bytes]:
        del method, url, headers, timeout
        self.body = body
        return 200, {}, b'{"access_token":"test-access-token","token_type":"bearer"}'


class _SequencedOAuthTransport:
    def __init__(self, *, first_error: str) -> None:
        self.first_error = first_error
        self.bodies: list[bytes | None] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, dict[str, str], bytes]:
        del method, url, headers, timeout
        self.bodies.append(body)
        if len(self.bodies) == 1:
            payload = ('{"error":"' + self.first_error + '"}').encode("utf-8")
            return 400, {}, payload
        return 200, {}, b'{"access_token":"fallback-access-token","token_type":"bearer"}'


class _AlwaysInvalidGrantTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, dict[str, str], bytes]:
        del method, url, headers, body, timeout
        self.calls += 1
        return 400, {}, b'{"error":"invalid_grant"}'


def _provider(transport: object) -> YandexScreenCodeDirectProvider:
    return YandexScreenCodeDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="test-client",
            client_secret="test-secret",
            redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
        ),
        transport=transport,  # type: ignore[arg-type]
    )


def _request_code(body: bytes | None) -> str:
    fields = parse_qs((body or b"").decode("ascii"), keep_blank_values=True)
    return fields["code"][0]


class YandexScreenCodeClipboardNormalizationTests(unittest.TestCase):
    def test_observed_production_style_code_is_accepted(self) -> None:
        self.assertEqual(normalize_yandex_confirmation_code(_CODE), _CODE)

    def test_invisible_browser_format_marks_are_removed(self) -> None:
        copied = "\u2066\u200b" + _CODE[:8] + "\u200d" + _CODE[8:] + "\u2069\ufeff"
        self.assertEqual(normalize_yandex_confirmation_code(copied), _CODE)

    def test_variation_selector_and_control_artifacts_are_removed(self) -> None:
        copied = _CODE[:4] + "\ufe0f\t\n" + _CODE[4:]
        self.assertEqual(normalize_yandex_confirmation_code(copied), _CODE)

    def test_unicode_compatibility_forms_that_resolve_to_ascii_are_canonicalized(self) -> None:
        fullwidth = "ｈｈ２ｖｒｖｉｚｊ２ｎｚｂｆ２ｈ"
        self.assertEqual(normalize_yandex_confirmation_code(fullwidth), _CODE)

    def test_compatibility_forms_and_format_marks_can_coexist(self) -> None:
        copied = "\u2066ｈｈ２ｖｒｖｉｚ\u200bｊ２ｎｚｂｆ２ｈ\u2069"
        self.assertEqual(normalize_yandex_confirmation_code(copied), _CODE)

    def test_fragment_prefix_with_format_marks_is_accepted(self) -> None:
        self.assertEqual(
            normalize_yandex_confirmation_code("\u200e# \u200f" + _CODE + "\u2060"),
            _CODE,
        )

    def test_other_visible_provider_owned_content_remains_opaque(self) -> None:
        for value in (
            "яндекс",
            "abc def",
            "abc🙂def",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_yandex_confirmation_code(value), value.strip())

    def test_empty_non_string_and_oversized_input_fail_closed(self) -> None:
        for value in (None, "", "   ", "a" * 1025, "\u200b" + ("a" * 1024)):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
                    normalize_yandex_confirmation_code(value)

    def test_provider_sends_clipboard_canonicalized_code_to_yandex(self) -> None:
        transport = _CapturingTransport()
        provider = _provider(transport)
        copied = "\u2066ｈｈ２ｖｒｖｉｚ\u200bｊ２ｎｚｂｆ２ｈ\u2069"

        token = provider.exchange_code(code=copied, verifier="test-verifier")

        self.assertEqual(token.access_token, "test-access-token")
        self.assertEqual(_request_code(transport.body), _CODE)

    def test_invalid_grant_retries_once_with_stricter_clipboard_candidate(self) -> None:
        transport = _SequencedOAuthTransport(first_error="invalid_grant")
        provider = _provider(transport)
        copied = _CODE[:4] + "\u00a0" + _CODE[4:]

        token = provider.exchange_code(code=copied, verifier="test-verifier")

        self.assertEqual(token.access_token, "fallback-access-token")
        self.assertEqual(len(transport.bodies), 2)
        self.assertEqual(_request_code(transport.bodies[0]), _CODE[:4] + " " + _CODE[4:])
        self.assertEqual(_request_code(transport.bodies[1]), _CODE)

    def test_bad_verification_code_retries_once_with_stricter_clipboard_candidate(self) -> None:
        transport = _SequencedOAuthTransport(first_error="bad_verification_code")
        provider = _provider(transport)
        copied = _CODE[:4] + "\u00a0" + _CODE[4:]

        token = provider.exchange_code(code=copied, verifier="test-verifier")

        self.assertEqual(token.access_token, "fallback-access-token")
        self.assertEqual(len(transport.bodies), 2)
        self.assertEqual(_request_code(transport.bodies[0]), _CODE[:4] + " " + _CODE[4:])
        self.assertEqual(_request_code(transport.bodies[1]), _CODE)

    def test_non_code_oauth_failure_is_never_retried(self) -> None:
        transport = _SequencedOAuthTransport(first_error="invalid_client")
        provider = _provider(transport)
        copied = _CODE[:4] + "\u00a0" + _CODE[4:]

        with self.assertRaisesRegex(YandexDirectError, "provider_invalid_client"):
            provider.exchange_code(code=copied, verifier="test-verifier")

        self.assertEqual(len(transport.bodies), 1)

    def test_invalid_grant_without_alternative_candidate_is_not_retried(self) -> None:
        transport = _AlwaysInvalidGrantTransport()
        provider = _provider(transport)

        with self.assertRaisesRegex(YandexDirectError, "provider_invalid_grant"):
            provider.exchange_code(code=_CODE, verifier="test-verifier")

        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
