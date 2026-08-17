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


class YandexScreenCodeOpaqueBoundaryTests(unittest.TestCase):
    def test_observed_production_style_code_is_accepted(self) -> None:
        self.assertEqual(normalize_yandex_confirmation_code(_CODE), _CODE)

    def test_provider_owned_code_content_remains_opaque_to_clientplatform(self) -> None:
        for value in (
            "яндекс",
            "abc def",
            "abc\tdef",
            "abc\ndef",
            "\u200bopaque\u2069",
            "abc🙂def",
            "ｈｈ２ｖｒｖｉｚｊ２ｎｚｂｆ２ｈ",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_yandex_confirmation_code(value), value.strip())

    def test_fragment_prefix_and_outer_whitespace_remain_tolerated(self) -> None:
        self.assertEqual(
            normalize_yandex_confirmation_code(f"  # {_CODE}  "),
            _CODE,
        )

    def test_empty_non_string_and_oversized_input_fail_closed(self) -> None:
        for value in (None, "", "   ", "a" * 1025, " " + ("a" * 1024)):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
                    normalize_yandex_confirmation_code(value)

    def test_opaque_code_is_urlencoded_and_sent_unchanged_to_yandex(self) -> None:
        transport = _CapturingTransport()
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="test-client",
                client_secret="test-secret",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=transport,
        )
        opaque_code = "abc\u200b def🙂"

        token = provider.exchange_code(code=opaque_code, verifier="test-verifier")

        self.assertEqual(token.access_token, "test-access-token")
        self.assertIsNotNone(transport.body)
        fields = parse_qs((transport.body or b"").decode("ascii"), keep_blank_values=True)
        self.assertEqual(fields["code"], [opaque_code])
        self.assertEqual(fields["grant_type"], ["authorization_code"])
        self.assertEqual(fields["code_verifier"], ["test-verifier"])


if __name__ == "__main__":
    unittest.main()
