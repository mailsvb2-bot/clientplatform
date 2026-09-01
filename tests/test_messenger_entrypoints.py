from services.messenger.entrypoints import parse_start_payload


def test_parse_start_payload_variants():
    assert parse_start_payload(None).kind == "plain"
    bridge = parse_start_payload("bridge_abc")
    assert bridge.kind == "bridge"
    assert bridge.value == "abc"
    plain = parse_start_payload("weird")
    assert plain.kind == "plain"
    assert plain.value == "weird"
