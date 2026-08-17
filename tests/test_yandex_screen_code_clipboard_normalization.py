from __future__ import annotations

import unittest

from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import normalize_yandex_confirmation_code


_CODE = "hh2vrvizj2nzbf2h"


class YandexScreenCodeClipboardNormalizationTests(unittest.TestCase):
    def test_invisible_browser_format_marks_are_removed(self) -> None:
        copied = "\u2066\u200b" + _CODE[:8] + "\u200d" + _CODE[8:] + "\u2069\ufeff"
        self.assertEqual(normalize_yandex_confirmation_code(copied), _CODE)

    def test_unicode_compatibility_forms_are_canonicalized_to_ascii(self) -> None:
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

    def test_visible_non_ascii_and_internal_whitespace_still_fail_closed(self) -> None:
        for value in ("яндекс", "abc def", "abc\tdef", "abc\ndef", "abc🙂def"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
                    normalize_yandex_confirmation_code(value)

    def test_oversized_input_is_not_sanitized_into_acceptance(self) -> None:
        value = "\u200b" + ("a" * 1024)
        with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
            normalize_yandex_confirmation_code(value)


if __name__ == "__main__":
    unittest.main()
