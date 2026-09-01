from __future__ import annotations

"""Run the full pytest suite under coverage and enforce non-decreasing ratchets."""

import json
import math
import os
import shlex
import subprocess  # nosec B404 - fixed internal quality commands only
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "coverage-baseline.json"
SOURCE_PACKAGES = ("services", "handlers", "core", "runtime", "config")


@dataclass(frozen=True)
class CoverageBaseline:
    total_percent: float
    branch_percent: float | None
    comparison_tolerance: float
    require_update_on_improvement: bool


def _finite_percent(value: object, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 100.0:
        raise ValueError(f"{field} must be finite and between 0 and 100")
    return parsed


def _load_baseline_payload(
    payload: dict[str, Any],
    *,
    require_branch: bool = False,
) -> CoverageBaseline:
    schema_version = payload.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("coverage baseline schema_version must be 1 or 2")
    source = payload.get("source")
    if source != list(SOURCE_PACKAGES):
        raise ValueError("coverage baseline source does not match the canonical production surface")
    tolerance = _finite_percent(
        payload.get("comparison_tolerance", 0.01),
        field="comparison_tolerance",
    )
    if tolerance > 1.0:
        raise ValueError("comparison_tolerance must not exceed 1 percentage point")

    branch_percent: float | None = None
    if schema_version == 2:
        branch_percent = _finite_percent(payload.get("branch_percent"), field="branch_percent")
    if require_branch and branch_percent is None:
        raise ValueError("coverage baseline schema_version 2 with branch_percent is required")

    return CoverageBaseline(
        total_percent=_finite_percent(payload.get("total_percent"), field="total_percent"),
        branch_percent=branch_percent,
        comparison_tolerance=tolerance,
        require_update_on_improvement=bool(payload.get("require_update_on_improvement", True)),
    )


def load_baseline(path: Path = BASELINE_PATH) -> CoverageBaseline:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("coverage baseline must be a JSON object")
    return _load_baseline_payload(payload, require_branch=True)


def load_base_branch_baseline(base_ref: str | None) -> CoverageBaseline | None:
    if not base_ref:
        return None
    completed = subprocess.run(  # nosec B603 - fixed git executable and validated ref from GitHub Actions
        ["git", "show", f"{base_ref}:coverage-baseline.json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise ValueError("base-branch coverage baseline must be a JSON object")
    return _load_baseline_payload(payload)


def _metric_baseline_percent(baseline: CoverageBaseline, *, metric: str) -> float:
    if metric == "combined":
        return baseline.total_percent
    if metric == "branch":
        if baseline.branch_percent is None:
            raise ValueError("branch coverage baseline is unavailable")
        return baseline.branch_percent
    raise ValueError(f"unsupported coverage metric: {metric}")


def _metric_label(metric: str) -> str:
    if metric == "combined":
        return "coverage"
    if metric == "branch":
        return "branch coverage"
    raise ValueError(f"unsupported coverage metric: {metric}")


def ratchet_errors(
    current_percent: float,
    baseline: CoverageBaseline,
    base_branch_baseline: CoverageBaseline | None = None,
    *,
    metric: str = "combined",
) -> list[str]:
    errors: list[str] = []
    tolerance = baseline.comparison_tolerance
    baseline_percent = _metric_baseline_percent(baseline, metric=metric)
    label = _metric_label(metric)

    if base_branch_baseline is not None:
        base_percent: float | None
        if metric == "branch" and base_branch_baseline.branch_percent is None:
            base_percent = None
        else:
            base_percent = _metric_baseline_percent(base_branch_baseline, metric=metric)
        if base_percent is not None:
            base_tolerance = max(tolerance, base_branch_baseline.comparison_tolerance)
            if baseline_percent + base_tolerance < base_percent:
                errors.append(
                    f"tracked {label} baseline was lowered: "
                    f"base={base_percent:.2f}% head={baseline_percent:.2f}%"
                )

    if current_percent + tolerance < baseline_percent:
        errors.append(
            f"{label} regressed below the tracked baseline: "
            f"current={current_percent:.2f}% baseline={baseline_percent:.2f}%"
        )
    if baseline.require_update_on_improvement and current_percent > baseline_percent + tolerance:
        field = "branch_percent" if metric == "branch" else "total_percent"
        errors.append(
            f"{label} improved; raise coverage-baseline.json {field} to lock the gain: "
            f"current={current_percent:.2f}% baseline={baseline_percent:.2f}%"
        )
    return errors


def _run(command: list[str], *, env: dict[str, str]) -> int:
    print(f"cmd: {shlex.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)  # nosec B603
    return int(completed.returncode)


def _read_coverage_totals(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    totals = payload.get("totals") if isinstance(payload, dict) else None
    if not isinstance(totals, dict):
        raise ValueError("coverage JSON does not contain totals")
    _finite_percent(totals.get("percent_covered"), field="percent_covered")
    _finite_percent(
        totals.get("percent_branches_covered"),
        field="percent_branches_covered",
    )
    try:
        num_branches = int(totals.get("num_branches"))
    except (TypeError, ValueError) as exc:
        raise ValueError("num_branches must be an integer") from exc
    if num_branches <= 0:
        raise ValueError("branch coverage requires at least one measured branch")
    return totals


def _base_ref_from_environment() -> str | None:
    explicit = str(os.getenv("COVERAGE_BASE_REF") or "").strip()
    if explicit:
        return explicit
    github_base = str(os.getenv("GITHUB_BASE_REF") or "").strip()
    return f"origin/{github_base}" if github_base else None


def _artifact_directory() -> Path:
    configured = str(os.getenv("COVERAGE_ARTIFACT_DIR") or "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        path = Path(tempfile.mkdtemp(prefix="clientplatform-coverage-"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_summary(
    artifact_dir: Path,
    *,
    totals: dict[str, Any] | None,
    baseline: CoverageBaseline | None,
    base_branch_baseline: CoverageBaseline | None,
    test_exit_code: int,
    report_exit_code: int,
    combined_errors: list[str],
    branch_errors: list[str],
    metadata_errors: list[str],
) -> Path:
    total_percent = float(totals["percent_covered"]) if totals is not None else None
    branch_percent = (
        float(totals["percent_branches_covered"]) if totals is not None else None
    )
    common_passed = (
        test_exit_code == 0
        and report_exit_code == 0
        and totals is not None
        and baseline is not None
        and not metadata_errors
    )
    combined_passed = common_passed and not combined_errors
    branch_passed = common_passed and not branch_errors
    passed = combined_passed and branch_passed
    errors = [*metadata_errors, *combined_errors, *branch_errors]

    summary = {
        "schema_version": 2,
        "passed": passed,
        "combined_passed": combined_passed,
        "branch_passed": branch_passed,
        "total_percent": total_percent,
        "branch_percent": branch_percent,
        "baseline_percent": baseline.total_percent if baseline is not None else None,
        "branch_baseline_percent": (
            baseline.branch_percent if baseline is not None else None
        ),
        "base_branch_baseline_percent": (
            base_branch_baseline.total_percent if base_branch_baseline is not None else None
        ),
        "base_branch_branch_baseline_percent": (
            base_branch_baseline.branch_percent if base_branch_baseline is not None else None
        ),
        "line_coverage": {
            "percent": totals.get("percent_statements_covered") if totals is not None else None,
            "covered": totals.get("covered_lines") if totals is not None else None,
            "missing": totals.get("missing_lines") if totals is not None else None,
            "statements": totals.get("num_statements") if totals is not None else None,
        },
        "branch_coverage": {
            "percent": branch_percent,
            "baseline_percent": baseline.branch_percent if baseline is not None else None,
            "covered": totals.get("covered_branches") if totals is not None else None,
            "missing": totals.get("missing_branches") if totals is not None else None,
            "branches": totals.get("num_branches") if totals is not None else None,
        },
        "test_exit_code": test_exit_code,
        "report_exit_code": report_exit_code,
        "combined_errors": combined_errors,
        "branch_errors": branch_errors,
        "metadata_errors": metadata_errors,
        "errors": errors,
    }
    json_path = artifact_dir / "coverage-summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_text = f"{total_percent:.2f}%" if total_percent is not None else "unavailable"
    branch_text = f"{branch_percent:.2f}%" if branch_percent is not None else "unavailable"
    baseline_text = f"{baseline.total_percent:.2f}%" if baseline is not None else "invalid"
    branch_baseline_text = (
        f"{baseline.branch_percent:.2f}%"
        if baseline is not None and baseline.branch_percent is not None
        else "invalid"
    )
    markdown_lines = [
        "## Coverage ratchets",
        "",
        f"- Overall result: {'PASS' if passed else 'FAIL'}",
        f"- Combined line + branch coverage: **{total_text}** / baseline **{baseline_text}**",
        f"- Branch coverage: **{branch_text}** / baseline **{branch_baseline_text}**",
    ]
    if totals is not None:
        line_percent = float(totals.get("percent_statements_covered", 0.0))
        markdown_lines.extend(
            [
                f"- Line coverage: **{line_percent:.2f}%** "
                f"({totals.get('covered_lines', 0)}/{totals.get('num_statements', 0)})",
                f"- Branches covered: {totals.get('covered_branches', 0)}/{totals.get('num_branches', 0)}",
            ]
        )
    if metadata_errors:
        markdown_lines.extend(["", "### Metadata errors", *[f"- {error}" for error in metadata_errors]])
    if combined_errors:
        markdown_lines.extend(["", "### Combined coverage errors", *[f"- {error}" for error in combined_errors]])
    if branch_errors:
        markdown_lines.extend(["", "### Branch coverage errors", *[f"- {error}" for error in branch_errors]])
    markdown = "\n".join(markdown_lines) + "\n"
    (artifact_dir / "coverage-summary.md").write_text(markdown, encoding="utf-8")

    step_summary = str(os.getenv("GITHUB_STEP_SUMMARY") or "").strip()
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    return json_path


def main() -> int:
    artifact_dir = _artifact_directory()
    data_file = artifact_dir / ".coverage"
    coverage_json = artifact_dir / "coverage.json"
    coverage_xml = artifact_dir / "coverage.xml"
    coverage_html = artifact_dir / "htmlcov"

    env = os.environ.copy()
    env["COVERAGE_FILE"] = str(data_file)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["APP_ENV"] = "test"
    env["LOAD_DOTENV"] = "0"

    print(f"coverage artifacts: {artifact_dir}", flush=True)
    erase_code = _run([sys.executable, "-m", "coverage", "erase"], env=env)
    if erase_code != 0:
        _write_summary(
            artifact_dir,
            totals=None,
            baseline=None,
            base_branch_baseline=None,
            test_exit_code=erase_code,
            report_exit_code=erase_code,
            combined_errors=[],
            branch_errors=[],
            metadata_errors=["coverage erase failed"],
        )
        return erase_code

    test_exit_code = _run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            # PostgreSQL parity/concurrency tests create a transient copy of the
            # repository under this path. It is coverage input, not a second test
            # tree; collecting it again causes duplicate-module import collisions.
            "--ignore=_coverage_pg_data",
        ],
        env=env,
    )

    report_codes = [
        _run([sys.executable, "-m", "coverage", "report", "--show-missing"], env=env),
        _run([sys.executable, "-m", "coverage", "json", "-o", str(coverage_json), "--pretty-print"], env=env),
        _run([sys.executable, "-m", "coverage", "xml", "-o", str(coverage_xml)], env=env),
        _run([sys.executable, "-m", "coverage", "html", "-d", str(coverage_html)], env=env),
    ]
    report_exit_code = next((code for code in report_codes if code != 0), 0)

    combined_errors: list[str] = []
    branch_errors: list[str] = []
    metadata_errors: list[str] = []
    totals: dict[str, Any] | None = None
    baseline: CoverageBaseline | None = None
    base_branch_baseline: CoverageBaseline | None = None
    try:
        totals = _read_coverage_totals(coverage_json)
        baseline = load_baseline()
        base_branch_baseline = load_base_branch_baseline(_base_ref_from_environment())
        combined_errors.extend(
            ratchet_errors(
                float(totals["percent_covered"]),
                baseline,
                base_branch_baseline,
                metric="combined",
            )
        )
        branch_errors.extend(
            ratchet_errors(
                float(totals["percent_branches_covered"]),
                baseline,
                base_branch_baseline,
                metric="branch",
            )
        )
    except json.JSONDecodeError as exc:
        metadata_errors.append(f"coverage metadata error: {type(exc).__name__}: {exc}")
    except OSError as exc:
        metadata_errors.append(f"coverage metadata error: {type(exc).__name__}: {exc}")
    except ValueError as exc:
        metadata_errors.append(f"coverage metadata error: {type(exc).__name__}: {exc}")

    _write_summary(
        artifact_dir,
        totals=totals,
        baseline=baseline,
        base_branch_baseline=base_branch_baseline,
        test_exit_code=test_exit_code,
        report_exit_code=report_exit_code,
        combined_errors=combined_errors,
        branch_errors=branch_errors,
        metadata_errors=metadata_errors,
    )

    if test_exit_code != 0:
        return test_exit_code
    if report_exit_code != 0:
        return report_exit_code
    errors = [*metadata_errors, *combined_errors, *branch_errors]
    if errors:
        for error in errors:
            print(f"COVERAGE_RATCHET_FAILED: {error}", file=sys.stderr, flush=True)
        return 1
    assert totals is not None and baseline is not None and baseline.branch_percent is not None
    print(
        "COVERAGE_RATCHET_OK "
        f"combined={float(totals['percent_covered']):.2f}%/"
        f"{baseline.total_percent:.2f}% "
        f"branch={float(totals['percent_branches_covered']):.2f}%/"
        f"{baseline.branch_percent:.2f}%",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
