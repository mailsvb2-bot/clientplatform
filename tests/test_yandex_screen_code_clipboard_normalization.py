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


class YandexScreenCodeClipboardNormalizationTests(unittest.TestCase):
    def test_observed_production_style_code_is_accepted(self) -> None:
        self.assertEqual(normalize_yandex_confirmation_code(_CODE), _CODE)

    def test_invisible_browser_format_marks_are_removed(self) -> None:
        copied = "\u2066\u200b" + _CODE[:8] + "\u200d" + _CODE[8:] + "\u2069\ufeff"
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
            "abc\tdef",
            "abc\ndef",
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
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="test-client",
                client_secret="test-secret",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=transport,
        )
        copied = "\u2066ｈｈ２ｖｒｖｉｚ\u200bｊ２ｎｚｂｆ２ｈ\u2069"

        token = provider.exchange_code(code=copied, verifier="test-verifier")

        self.assertEqual(token.access_token, "test-access-token")
        self.assertIsNotNone(transport.body)
        fields = parse_qs((transport.body or b"").decode("ascii"), keep_blank_values=True)
        self.assertEqual(fields["code"], [_CODE])
        self.assertEqual(fields["grant_type"], ["authorization_code"])
        self.assertEqual(fields["code_verifier"], ["test-verifier"])


if __name__ == "__main__":
    unittest.main()
