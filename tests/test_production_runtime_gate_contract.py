from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_production_gate_skips_pytest_in_runtime_release() -> None:
    gate = _text("scripts/production_gate.py")
    start = gate.index('"scripts/post_deploy_verify.py"')
    end = gate.index('env=gate_env,', start)
    invocation = gate[start:end]

    assert '"--skip-pytest"' in invocation
    assert invocation.index('"--skip-pytest"') < invocation.index(
        '"--require-disaster-recovery-green"'
    )
