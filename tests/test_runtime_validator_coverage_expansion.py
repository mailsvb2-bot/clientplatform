from __future__ import annotations

from pathlib import Path

import pytest

from services.validators import runtime


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_project_file_scan_excludes_generated_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    good = write(tmp_path, "services/good.py", "x = 1")
    write(tmp_path, ".venv/ignored.py", "x = 2")
    write(tmp_path, "node_modules/ignored.py", "x = 3")
    files = list(runtime._project_py_files())
    assert files == [(good, "services/good.py")]


def test_background_task_validator_clean_allowed_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    token = "asyncio." + "create" + "_task"
    write(tmp_path, "services/good.py", "async def run():\n    return None\n")
    runtime.validate_background_tasks(strict=True)

    write(tmp_path, "core/task_manager.py", f"def owner():\n    return {token}(work())\n")
    runtime.validate_background_tasks(strict=True)

    write(tmp_path, "services/bad.py", f"def bad():\n    return {token}(work())\n")
    runtime.validate_background_tasks(strict=False)
    with pytest.raises(runtime.ValidationError, match="Forbidden"):
        runtime.validate_background_tasks(strict=True)

    bad = tmp_path / "services" / "bad.py"
    bad.write_bytes(b"\xff" + token.encode())
    with pytest.raises(runtime.ValidationError):
        runtime.validate_background_tasks(strict=True)


def test_single_scheduler_validator_all_failure_classes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    write(tmp_path, "services/jobs.py", "def run():\n    return 1\n")
    runtime.validate_single_scheduler(strict=True)

    deprecated_import = "services." + "session_timers"
    write(tmp_path, "services/bad_import.py", f"from {deprecated_import} import start\n")
    runtime.validate_single_scheduler(strict=False)
    with pytest.raises(runtime.ValidationError, match="deprecated"):
        runtime.validate_single_scheduler(strict=True)
    (tmp_path / "services" / "bad_import.py").unlink()

    table = "scheduled" + "_jobs"
    write(tmp_path, "services/bad_sql.py", f"SQL = 'SELECT * FROM {table}'\n")
    with pytest.raises(runtime.ValidationError, match="SQL usage"):
        runtime.validate_single_scheduler(strict=True)
    (tmp_path / "services" / "bad_sql.py").unlink()

    write(tmp_path, "services/schema.py", f"SQL = 'CREATE TABLE {table}'\n")
    runtime.validate_single_scheduler(strict=True)

    write(tmp_path, "services/jobs.py", "def now():\n    return time." + "time()\n")
    with pytest.raises(runtime.ValidationError, match="unix-time"):
        runtime.validate_single_scheduler(strict=True)
    write(tmp_path, "services/jobs.py", "def run():\n    return 1\n")

    unreadable = tmp_path / "services" / "gone.py"
    unreadable.write_text("x=1", encoding="utf-8")
    original = Path.read_text

    def sometimes_missing(self: Path, *args, **kwargs):
        if self.name == "gone.py":
            raise OSError("gone")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", sometimes_missing)
    runtime.validate_single_scheduler(strict=True)


def test_function_scope_helpers() -> None:
    source = """
def outer():
    def inner():
        return 1
    return inner()

async def async_fn():
    return 2
"""
    scopes = runtime._function_scopes(source)
    assert any(name == "outer" for _, _, name in scopes)
    assert any(name == "inner" for _, _, name in scopes)
    assert any(name == "async_fn" for _, _, name in scopes)
    inner_line = next(start for start, _, name in scopes if name == "inner")
    assert runtime._function_at_line(scopes, inner_line) == "inner"
    assert runtime._function_at_line(scopes, 999) == ""
    assert runtime._function_scopes("def broken(") == []


def test_wide_except_policy_allowed_suppressed_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    exc = "Excep" + "tion"
    base = "Base" + "Exception"

    write(tmp_path, "services/good.py", "try:\n    x = 1\nexcept ValueError:\n    x = 2\n")
    runtime.validate_wide_except_policy(strict=True)

    write(tmp_path, "app.py", f"try:\n    x = 1\nexcept {exc}:\n    x = 2\n")
    runtime.validate_wide_except_policy(strict=True)

    write(
        tmp_path,
        "runtime/messenger_ingress.py",
        f"def _process_and_persist():\n    try:\n        return 1\n    except {exc}:\n        return 2\n",
    )
    runtime.validate_wide_except_policy(strict=True)

    write(
        tmp_path,
        "services/suppressed.py",
        f"try:\n    x = 1\nexcept {exc}:  # validator: allow-wide-except\n    x = 2\n",
    )
    runtime.validate_wide_except_policy(strict=True)

    write(tmp_path, "services/bad.py", f"try:\n    x = 1\nexcept {exc}:\n    x = 2\n")
    runtime.validate_wide_except_policy(strict=False)
    with pytest.raises(runtime.ValidationError, match="except Exception"):
        runtime.validate_wide_except_policy(strict=True)
    (tmp_path / "services" / "bad.py").unlink()

    write(tmp_path, "services/bare.py", "try:\n    x = 1\nexcept:\n    x = 2\n")
    with pytest.raises(runtime.ValidationError, match="bare except"):
        runtime.validate_wide_except_policy(strict=True)
    (tmp_path / "services" / "bare.py").unlink()

    write(
        tmp_path,
        "services/tuple.py",
        f"try:\n    x = 1\nexcept (ValueError, TypeError, OSError, RuntimeError):\n    x = 2\n",
    )
    with pytest.raises(runtime.ValidationError, match="wide tuple"):
        runtime.validate_wide_except_policy(strict=True)
    (tmp_path / "services" / "tuple.py").unlink()

    write(tmp_path, "services/base.py", f"try:\n    x = 1\nexcept {base}:\n    x = 2\n")
    with pytest.raises(runtime.ValidationError, match="BaseException"):
        runtime.validate_wide_except_policy(strict=True)
