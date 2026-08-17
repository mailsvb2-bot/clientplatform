from __future__ import annotations

import unittest

from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import normalize_yandex_confirmation_code


_CODE = "esnknh4jfvf3tyn5"


class YandexScreenCodeClipboardNormalizationTests(unittest.TestCase):
    def test_invisible_browser_format_marks_are_removed(self) -> None:
        copied = "\u2066\u200b" + _CODE[:8] + "\u200d" + _CODE[8:] + "\u2069\ufeff"
        self.assertEqual(normalize_yandex_confirmation_code(copied), _CODE)

    def test_fragment_prefix_with_format_marks_is_accepted(self) -> None:
        self.assertEqual(
            normalize_yandex_confirmation_code("\u200e# \u200f" + _CODE + "\u2060"),
            _CODE,
        )

    def test_visible_non_ascii_and_internal_whitespace_still_fail_closed(self) -> None:
        for value in ("яндекс", "abc def", "abc\tdef", "abc\ndef"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
                    normalize_yandex_confirmation_code(value)

    def test_oversized_input_is_not_sanitized_into_acceptance(self) -> None:
        value = "\u200b" + ("a" * 1024)
        with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
            normalize_yandex_confirmation_code(value)


if __name__ == "__main__":
    unittest.main()
