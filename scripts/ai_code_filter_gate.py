from __future__ import annotations

import argparse
import json
import os
# Reviewed: operator advisory gate invokes a configured local AI-code-filter tool without shell.
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOOL_DIR = Path(os.environ.get("AI_CODE_FILTER_DIR", "/root/_external_ai_code_filter"))
DEFAULT_AUDIT_DIR = Path(os.environ.get("CLIENTPLATFORM_AUDIT_DIR", "/root/clientplatform_audits"))


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _process_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_command(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    out_dir: Path,
    timeout: int,
) -> dict[str, object]:
    stdout_path = out_dir / f"{name}.stdout.txt"
    stderr_path = out_dir / f"{name}.stderr.txt"
    cmd_path = out_dir / f"{name}.cmd.txt"

    cmd_path.write_text(" ".join(command) + "\n", encoding="utf-8")

    try:
        # Reviewed: commands are generated from this file's fixed command list and run without shell.
        proc = subprocess.run(  # nosec B603
            list(command),
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout_path.write_text(proc.stdout, encoding="utf-8")
        stderr_path.write_text(proc.stderr, encoding="utf-8")
        return {
            "name": name,
            "exit_code": int(proc.returncode),
            "command": list(command),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(_process_output_text(exc.stdout), encoding="utf-8")
        stderr_path.write_text(_process_output_text(exc.stderr), encoding="utf-8")
        return {
            "name": name,
            "exit_code": 124,
            "command": list(command),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "error": "timeout",
        }


def run_ai_code_filter_gate(
    *,
    project_root: Path,
    tool_dir: Path,
    audit_dir: Path,
    strict: bool,
    timeout: int,
) -> int:
    out_dir = audit_dir / f"ai_code_filter_gate_{_utc_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    ai_filter = tool_dir / "ai_filter.py"
    summary: dict[str, object] = {
        "project_root": str(project_root),
        "tool_dir": str(tool_dir),
        "out_dir": str(out_dir),
        "strict": strict,
        "tool_available": ai_filter.exists(),
        "commands": [],
    }

    if not ai_filter.exists():
        _write_json(out_dir / "summary.json", summary)
        print(f"AI Code Filter unavailable: {ai_filter}")
        print(f"AI_CODE_FILTER_OUT={out_dir}")
        return 1 if strict else 0

    commands: list[tuple[str, list[str]]] = [
        (
            "grep_audit",
            [
                sys.executable,
                str(ai_filter),
                "grep-audit",
                str(project_root),
                "--summary-json",
                str(out_dir / "grep_audit.summary.json"),
                "--output",
                str(out_dir / "grep_audit.report.json"),
                "--ci",
            ],
        ),
        (
            "analyze_messaging_autonomy",
            [
                sys.executable,
                str(ai_filter),
                "analyze",
                str(project_root),
                "--no-ai",
                "--no-drift",
                "--profile",
                "messaging-bot",
                "--profile",
                "autonomy-canon",
                "--output",
                str(out_dir / "analyze_messaging_autonomy.report.json"),
                "--markdown",
                str(out_dir / "analyze_messaging_autonomy.md"),
                "--ci",
                "--max-critical",
                "0",
                "--max-high",
                "0",
            ],
        ),
        (
            "call_graph",
            [
                sys.executable,
                str(ai_filter),
                "call-graph",
                str(project_root),
                "--output",
                str(out_dir / "call_graph.json"),
                "--report",
                str(out_dir / "call_graph.report.json"),
                "--max-unknown-ratio",
                "0.95",
                "--ci",
            ],
        ),
        (
            "quality_matrix",
            [
                sys.executable,
                str(ai_filter),
                "quality-matrix",
                str(project_root),
                "--summary-json",
                str(out_dir / "quality_matrix.summary.json"),
                "--output",
                str(out_dir / "quality_matrix.report.json"),
                "--ci",
            ],
        ),
    ]

    results = [
        _run_command(name=name, command=command, cwd=tool_dir, out_dir=out_dir, timeout=timeout)
        for name, command in commands
    ]
    summary["commands"] = results
    summary["failed_commands"] = [item["name"] for item in results if item["exit_code"] != 0]

    _write_json(out_dir / "summary.json", summary)

    print(f"AI_CODE_FILTER_OUT={out_dir}")
    for item in results:
        print(f"{item['name']} exit={item['exit_code']}")

    if strict and summary["failed_commands"]:
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run external AI Code Filter advisory gate for ClientPlatform.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--tool-dir", default=str(DEFAULT_TOOL_DIR))
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--strict", action="store_true", help="Fail when AI Code Filter reports blocking findings.")
    args = parser.parse_args(argv)

    return run_ai_code_filter_gate(
        project_root=Path(args.project_root).resolve(),
        tool_dir=Path(args.tool_dir).resolve(),
        audit_dir=Path(args.audit_dir).resolve(),
        strict=bool(args.strict),
        timeout=int(args.timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
