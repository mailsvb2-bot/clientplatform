from pathlib import Path

path = Path("tests/test_clientplatform_control_bot_behavior.py")
source = path.read_text(encoding="utf-8")
old_keyboard = '''    client_portal = handlers._client_portal_keyboard(business_id)
    assert client_portal.inline_keyboard[0][0].text == "Посмотреть доступную запись"
'''
new_keyboard = '''    client_portal = handlers._client_portal_keyboard(business_id)
    assert client_portal.inline_keyboard[0][0].text == "Мои программы"
    assert client_portal.inline_keyboard[0][0].callback_data.startswith("cp:cprograms:")
    assert client_portal.inline_keyboard[1][0].text == "Посмотреть доступную запись"
'''
if source.count(old_keyboard) != 1:
    raise SystemExit("client portal keyboard test anchor mismatch")
source = source.replace(old_keyboard, new_keyboard)
old_results = '''    monkeypatch.setattr(
        handlers,
        "business_delivery_summary",
        lambda **_kwargs: SimpleNamespace(
            customers=2,
            programs=1,
            dispatch_pending=3,
            dispatch_sent=4,
            dispatch_attention=5,
        ),
    )
    results = FakeCallback(f"cp:results:{business_token}")
'''
new_results = '''    monkeypatch.setattr(
        handlers,
        "business_delivery_summary",
        lambda **_kwargs: SimpleNamespace(
            customers=2,
            programs=1,
            dispatch_pending=3,
            dispatch_sent=4,
            dispatch_attention=5,
        ),
    )
    monkeypatch.setattr(
        handlers,
        "list_business_program_progress",
        lambda **_kwargs: [
            SimpleNamespace(
                customer_display_name="Анна",
                program_title="Спокойный сон",
                completed_lessons=1,
                total_lessons=2,
                percent_complete=50,
            )
        ],
    )
    results = FakeCallback(f"cp:results:{business_token}")
'''
if source.count(old_results) != 1:
    raise SystemExit("owner progress behavior test anchor mismatch")
source = source.replace(old_results, new_results)
old_assertions = '''    assert "Клиенты: 2" in text
    assert "Успешно отправлено: 4" in text
    assert "Требуют внимания: 5" in text
'''
new_assertions = '''    assert "Клиенты: 2" in text
    assert "Успешно отправлено: 4" in text
    assert "Требуют внимания: 5" in text
    assert "Анна: Спокойный сон — 1/2 (50%)" in text
'''
if source.count(old_assertions) != 1:
    raise SystemExit("owner progress assertion anchor mismatch")
path.write_text(source.replace(old_assertions, new_assertions), encoding="utf-8")
