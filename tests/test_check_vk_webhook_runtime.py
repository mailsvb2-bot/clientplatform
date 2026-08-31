from scripts import check_vk_webhook_runtime


def _configure_vk_preflight(monkeypatch) -> None:
    monkeypatch.setenv("MESSENGER_WEBHOOK_ENABLED", "1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://app.clientplatform.ru")
    monkeypatch.setenv("VK_GROUP_ID", "241176159")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_CONFIRMATION_TOKEN", "confirm")
    monkeypatch.setenv("MESSENGER_WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("MESSENGER_WEBHOOK_PORT", "8181")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "9999")


def test_vk_runtime_preflight_uses_messenger_ingress_port(monkeypatch, capsys) -> None:
    _configure_vk_preflight(monkeypatch)
    monkeypatch.setenv("VK_SECRET", "secret")

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


def test_vk_runtime_preflight_warns_when_optional_secret_is_empty(monkeypatch, capsys) -> None:
    _configure_vk_preflight(monkeypatch)
    monkeypatch.delenv("VK_SECRET", raising=False)
    monkeypatch.setattr(check_vk_webhook_runtime, "_port_open", lambda *_args, **_kwargs: True)

    code = check_vk_webhook_runtime.main()
    output = capsys.readouterr().out

    assert code == 0
    assert "WARN: VK_SECRET=<empty>" in output
    assert "WARN: VK_SECRET is empty; secret verification cannot be enforced" in output
    assert "VK WEBHOOK PREFLIGHT: OK" in output
