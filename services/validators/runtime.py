from __future__ import annotations

import ast
import logging
import re

from core.paths import ROOT as PROJECT_ROOT
from services.validators.base import ValidationError

log = logging.getLogger(__name__)

EXCLUDED_SCAN_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    ".tox",
    ".nox",
    ".eggs",
    ".cache",
    ".patch_backups",
    "site-packages",
    "dist-packages",
    "node_modules",
    "build",
    "dist",
}


def _project_py_files():
    for file_path in PROJECT_ROOT.rglob("*.py"):
        rel_path = file_path.relative_to(PROJECT_ROOT)
        if set(rel_path.parts) & EXCLUDED_SCAN_DIR_NAMES:
            continue
        yield file_path, rel_path.as_posix()


def validate_background_tasks(strict: bool = False) -> None:
    """Validate non-canonical asyncio.create_task usage in project-owned code.

    The rule is intentionally scoped:
    - never scan virtualenvs, third-party packages, generated caches or operator backup dirs;
    - allow only explicit owner modules that centralize background execution;
    - do not flag this validator's own explanatory/search strings.
    """
    from services.validators.base import ValidationError

    allowed_files = {
        "core/task_manager.py",
        "services/db_writer.py",
        "services/scheduler.py",
        "services/background_scheduler.py",
        "services/validators/runtime.py",
    }

    forbidden_token = "asyncio." + "create_task"
    findings: list[str] = []

    for file_path, rel in _project_py_files():
        if rel in allowed_files:
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        if forbidden_token in text:
            findings.append(rel)

    if findings:
        msg = (
            "Forbidden asyncio.create_task usage found in non-owner project code: "
            f"{findings}. Route background work through the canonical task/scheduler owner."
        )
        if strict:
            raise ValidationError(msg)


def validate_single_scheduler(strict: bool = True) -> None:
    """Architectural guardrails (v16.4).

    - session_timers must never be imported from runtime code
    - scheduled_jobs must not be read/written outside schema migration / deprecated module
    - jobs pipeline must not rely on unix-int run_at/time.time/datetime.now
    """
    bad_imports: list[str] = []
    import_re = re.compile(r"^\s*(from\s+services\.session_timers\s+import\b|import\s+services\.session_timers\b)")
    for p, rel in _project_py_files():
        if rel in {"services/session_timers.py"}:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(txt.splitlines(), start=1):
            if import_re.search(line):
                bad_imports.append(f"{rel}:{i}")

    if bad_imports:
        msg = "Forbidden import of deprecated session_timers: " + ", ".join(bad_imports[:30])
        if strict:
            raise ValidationError(msg)
        log.warning(msg)

    allow = {
        "services/schema.py",
        "services/schema_tables.py",
        "services/migrations/scheduled_jobs_to_jobs_v1.py",
        "services/session_timers.py",
    }
    bad_scheduled: list[str] = []
    sql_re = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE)\b[^\n;]*\bscheduled_jobs\b", re.IGNORECASE)
    for p, rel in _project_py_files():
        if rel in allow:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if sql_re.search(txt):
            bad_scheduled.append(rel)

    if bad_scheduled:
        msg = f"Forbidden scheduled_jobs SQL usage found in: {sorted(set(bad_scheduled))}"
        if strict:
            raise ValidationError(msg)
        log.warning(msg)

    jobs_pipeline = {"services/jobs.py", "core/engine.py"}
    unix_markers = ["time.time(", "datetime.now(", "run_at INTEGER", "run_at  INTEGER"]
    bad_unix: list[str] = []
    for p, rel in _project_py_files():
        if rel not in jobs_pipeline:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in txt for marker in unix_markers):
            bad_unix.append(rel)

    if bad_unix:
        msg = f"Forbidden unix-time markers in jobs pipeline: {sorted(set(bad_unix))}"
        if strict:
            raise ValidationError(msg)
        log.warning(msg)


def _function_scopes(text: str) -> list[tuple[int, int, str]]:
    """Return source ranges for functions, innermost first during lookup."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scopes: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            scopes.append((int(node.lineno), end, str(node.name)))
    return scopes


def _function_at_line(scopes: list[tuple[int, int, str]], line_number: int) -> str:
    matching = [scope for scope in scopes if scope[0] <= line_number <= scope[1]]
    if not matching:
        return ""
    start, end, name = min(matching, key=lambda item: item[1] - item[0])
    del start, end
    return name


def validate_wide_except_policy(*, strict: bool = True) -> None:
    """Strict gate for accidental broad exception handling.

    File-wide exceptions remain supported only for legacy owners. New runtime
    boundaries are allowed at named function scope, so one justified catch-all
    cannot silently authorize broad exception handling elsewhere in the module.
    A line-local suppression marker is also supported for a reviewed one-off:
    ``# validator: allow-wide-except``.
    """

    allow_files = {
        "main.py",
        "app.py",
        "core/engine.py",
        "core/middlewares.py",
        "services/scheduler.py",
        "core/task_manager.py",
        "core/ai/action_gateway.py",
        "core/ai/decision_core.py",
        "scripts/validate_project.py",
        "runtime/health_server.py",
        "runtime/messenger_webhooks.py",
        "runtime/payment_http.py",
        "services/messenger/reply_dispatcher.py",
        "services/db_writer.py",
        "services/validator.py",
        "services/db/core.py",
        "handlers/menu.py",
        "handlers/start.py",
        "services/messenger/audio_delivery.py",
        "services/messenger/text_ui.py",
        "services/migrations/_helpers.py",
        "services/mood.py",
        "services/mood_text_flow.py",
        "services/ai/client.py",
        "services/ai/pricing.py",
        "services/ai_copywriter.py",
        "services/weather.py",
    }
    allow_functions = {
        "runtime/messenger_ingress.py": {"_process_and_persist", "vk_webhook", "max_webhook"},
        "services/messenger/delivery_outbox.py": {"_worker_loop"},
        "clientplatform/application/dispatch_worker.py": {"_release_claims", "run_dispatch_batch"},
        "tests/test_clientplatform_connections_dispatch_outbox.py": {"managed"},
        # Reviewed external boundaries. These functions translate provider,
        # transport and malformed-callback failures into sanitized outcomes;
        # no provider payload, OAuth token or secret is returned to the user.
        "runtime/ad_oauth_http.py": {"yandex_direct_oauth_callback"},
        "handlers/clientplatform_ad_connections.py": {
            "connect_yandex_direct",
            "choose_ad_connection",
            "choose_yandex_campaign",
            "request_ad_regions",
            "prepare_ad_publication",
            "confirm_yandex_publication",
        },
        "handlers/clientplatform_ad_disconnect.py": {"revoke_ad_connection"},
        # OAuth completion is a reviewed fail-closed cleanup boundary: every
        # unexpected provider/local failure must settle the durable completion
        # lease and then be re-raised. Keep this allowance function-scoped.
        "clientplatform/application/ad_connections.py": {
            "complete_yandex_direct_oauth",
            "process_one_ad_publication",
        },
        "handlers/clientplatform_yandex_screen_code.py": {
            "connect_yandex_direct_screen_code",
            "cancel_yandex_direct_screen_code",
            "_erase_confirmation_code_message",
            "complete_yandex_direct_screen_code",
        },
        "handlers/clientplatform_managed_bot_onboarding.py": {
            "request_managed_bot_creation"
        },
        # Goal-first advertising has bounded external boundaries of the same
        # kind: Telegram files, image decoders and Yandex provider calls. Keep
        # each reviewed allowance at function scope rather than weakening the
        # project-wide rule or allowing an entire new file.
        "handlers/clientplatform_goal_first_autopilot.py": {
            "receive_custom_text",
            "clear_custom_media",
            "_finish_generated_image",
            "prepare_real_launch",
        },
        "handlers/clientplatform_goal_launch.py": {"prepare_real_launch"},
        "clientplatform/application/ad_publication_assets.py": {"_normalized_image"},
        "clientplatform/application/ad_goal_publication.py": {
            "_sync_submitted_media",
            "submit_goal_publication",
        },
    }
    suppression_markers = {
        "# validator: allow-wide-except",
        "# validator: allow-except-exception",
    }

    bad: list[str] = []
    forbidden_names = {"BaseException", "Exception"}
    bare_re = re.compile(r"^\s*except\s*:\s*(#.*)?$")
    single_re = re.compile(r"^\s*except\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")
    tuple_re = re.compile(r"^\s*except\s*\((?P<body>[^)]*)\)\s*(?:as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*:")

    for pth, rel in _project_py_files():
        if rel.startswith("services/validators/"):
            continue
        try:
            text = pth.read_text(encoding="utf-8")
            lines = text.splitlines()
        except OSError:
            continue
        scopes = _function_scopes(text)

        for i, line in enumerate(lines, start=1):
            function_allowed = _function_at_line(scopes, i) in allow_functions.get(rel, set())
            line_allowed = any(marker in line for marker in suppression_markers)
            allowed = rel in allow_files or function_allowed or line_allowed

            if bare_re.match(line):
                if not allowed:
                    bad.append(f"{rel}:{i} bare except not allowed")
                continue

            tuple_match = tuple_re.match(line)
            if tuple_match:
                names = [name.strip() for name in tuple_match.group("body").split(",") if name.strip()]
                simple = [name.split(".")[-1] for name in names]
                is_wide = (
                    len(simple) >= 4
                    or ("OSError" in simple and "RuntimeError" in simple)
                    or any(name in forbidden_names for name in simple)
                )
                if is_wide and not allowed:
                    bad.append(f"{rel}:{i} wide tuple except not allowed: ({', '.join(simple)})")
                continue

            single_match = single_re.match(line)
            if single_match:
                name = single_match.group("name")
                if name in forbidden_names and not allowed:
                    bad.append(f"{rel}:{i} except {name} not allowed")

    if bad:
        msg = "Wide-except policy failed: " + "; ".join(bad[:30])
        if strict:
            raise ValidationError(msg)
        log.warning(msg)
