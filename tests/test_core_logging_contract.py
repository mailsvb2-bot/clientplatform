import logging

import pytest

import core.logging as logging_config


def _reset_configured() -> tuple[bool, object | None]:
    existed = hasattr(logging_config.setup_logging, "_configured")
    value = getattr(logging_config.setup_logging, "_configured", None)
    if existed:
        delattr(logging_config.setup_logging, "_configured")
    return existed, value


def _restore_configured(existed: bool, value: object | None) -> None:
    if hasattr(logging_config.setup_logging, "_configured"):
        delattr(logging_config.setup_logging, "_configured")
    if existed:
        logging_config.setup_logging._configured = value


def _run_isolated_setup(monkeypatch, tmp_path, handler_factory) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    configured_before = _reset_configured()
    monkeypatch.setenv("LOG_FILE_DISABLED", "0")
    monkeypatch.delenv("DISABLE_FILE_LOGGING", raising=False)
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "clientplatform.log"))
    monkeypatch.setattr(logging_config, "RotatingFileHandler", handler_factory)
    try:
        logging_config.setup_logging()
        assert logging_config.setup_logging._configured is True
    finally:
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
                handler.close()
        root.handlers[:] = handlers_before
        root.setLevel(level_before)
        _restore_configured(*configured_before)


def test_setup_logging_file_handler_path_is_deterministic(monkeypatch, tmp_path) -> None:
    created: list[logging.Handler] = []

    def handler_factory(*_args, **_kwargs):
        handler = logging.Handler()
        created.append(handler)
        return handler

    _run_isolated_setup(monkeypatch, tmp_path, handler_factory)
    assert len(created) == 1
    assert created[0].level == logging.INFO
    assert created[0].formatter is not None


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_setup_logging_file_handler_failures_are_nonfatal(
    monkeypatch, tmp_path, error_type
) -> None:
    def handler_factory(*_args, **_kwargs):
        raise error_type("synthetic logging boundary")

    _run_isolated_setup(monkeypatch, tmp_path, handler_factory)
