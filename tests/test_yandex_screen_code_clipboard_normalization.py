from __future__ import annotations

import unittest

from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_screen_code import normalize_yandex_confirmation_code


_CODE = "hh2vrvizj2nzbf2h"


class YandexScreenCodeClipboardNormalizationTests(unittest.TestCase):
    def test_observed_production_style_code_is_accepted(self) -> None:
        self.assertEqual(normalize_yandex_confirmation_code(_CODE), _CODE)

    def test_confirmation_code_remains_opaque_to_clientplatform(self) -> None:
        for value in (
            "яндекс",
            "abc def",
            "abc\tdef",
            "abc\ndef",
            "\u200bopaque\u2069",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_yandex_confirmation_code(value), value.strip())

    def test_fragment_prefix_and_outer_whitespace_remain_tolerated(self) -> None:
        self.assertEqual(
            normalize_yandex_confirmation_code(f"  # {_CODE}  "),
            _CODE,
        )

    def test_empty_and_oversized_input_fail_closed(self) -> None:
        for value in ("", "   ", "a" * 1025):
            with self.subTest(length=len(value)):
                with self.assertRaisesRegex(YandexDirectError, "oauth_code_invalid"):
                    normalize_yandex_confirmation_code(value)


if __name__ == "__main__":
    unittest.main()
