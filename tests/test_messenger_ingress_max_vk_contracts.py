from runtime.messenger_ingress import _entry_start_text
from runtime.messenger_payloads import normalise_messenger_text


def test_plain_numeric_text_is_never_rewritten_to_retired_product_routes() -> None:
    assert normalise_messenger_text("1") == "1"
    assert normalise_messenger_text("2") == "2"
    assert _entry_start_text("1") == "1"
    assert _entry_start_text("2") == "2"


def test_clientplatform_owner_deep_links_use_only_canonical_prefix() -> None:
    assert _entry_start_text("start cpo_owner_abc") == "/start cpo_owner_abc"
    assert _entry_start_text("/start cpo_owner_123") == "/start cpo_owner_123"
    assert _entry_start_text("cpo_owner_token") == "/start cpo_owner_token"
    assert _entry_start_text("bridge_token") == "bridge_token"
    assert _entry_start_text("ref_777") == "ref_777"


def test_non_entry_text_is_preserved_for_clientplatform_entry_parser() -> None:
    assert _entry_start_text("бизнес Студия Анны") == "бизнес Студия Анны"
    assert _entry_start_text("privacy") == "privacy"
    assert _entry_start_text("+1") == "+1"
