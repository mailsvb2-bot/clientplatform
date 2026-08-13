from __future__ import annotations

from services.db import schema


def test_sales_ai_schema_follows_sales_and_precedes_offer_ladders() -> None:
    names = [part.__name__.rsplit(".", 1)[-1] for part in schema.PARTS]
    assert names.index("clientplatform_sales") < names.index("clientplatform_sales_ai")
    assert names.index("clientplatform_sales_ai") < names.index("clientplatform_offer_ladders")
