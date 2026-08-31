from scripts import check_vk_webhook_runtime


def test_vk_runtime_preflight_uses_messenger_ingress_port(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MESSENGER_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://app.clientplatform.ru")
    monkeypatch.setenv("VK_GROUP_ID", "241176159")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_CONFIRMATION_TOKEN", "confirm")
    monkeypatch.setenv("VK_SECRET", "secret")
    monkeypatch.setenv("MESSENGER_WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("MESSENGER_WEBHOOK_PORT", "8181")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "9999")

    observed = []

    def fake_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
        observed.append((host, port, timeout))
        return True

    monkeypatch.setattr(check_vk_webhook_runtime, "_port_open", fake_port_open)
    code = check_vk_webhook_runtime.main()
    output = capsys.readouterr().out

    assert code == 0
    assert observed == [("127.0.0.1", 8181, 1.5)]
    assert "127.0.0.1:8181" in output
    assert "127.0.0.1:9999" not in output
