from __future__ import annotations

import unittest

from clientplatform.domain.sales_ai_jobs import normalize_sales_ai_source_order


class SalesAIJobOrderTests(unittest.TestCase):
    def test_decimal_provider_ids_sort_lexicographically_after_normalization(self) -> None:
        first = normalize_sales_ai_source_order("9")
        second = normalize_sales_ai_source_order("10")
        later = normalize_sales_ai_source_order("10000000000000000000000000000000")
        self.assertLess(first, second)
        self.assertLess(second, later)
        self.assertEqual(len(first), 32)

    def test_rejects_non_decimal_or_oversized_order(self) -> None:
        with self.assertRaises(ValueError):
            normalize_sales_ai_source_order("1.5")
        with self.assertRaises(ValueError):
            normalize_sales_ai_source_order("1" * 33)


if __name__ == "__main__":
    unittest.main()
