from __future__ import annotations

import json

from scripts.ai_review_gate import classify_risk, is_sensitive_path, review_blocks, validate_review


def _review(
    *,
    reviewer: str = "claude",
    base_sha: str = "b" * 40,
    head_sha: str = "a" * 40,
    verdict: str = "PASS",
    findings=None,
):
    return {
        "schema_version": 2,
        "reviewer": reviewer,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "verdict": verdict,
        "summary": "review completed",
        "findings": [] if findings is None else findings,
    }


def _finding(*, severity: str = "high", finding_id: str = "AI-001"):
    return {
        "id": finding_id,
        "severity": severity,
        "category": "tenant-isolation",
        "path": "services/example.py",
        "line": 42,
        "evidence": "lookup is scoped only by object id",
        "reproduction": "tenant A updates tenant B object id",
        "recommendation": "scope lookup by tenant and add a regression test",
    }


def test_docs_only_change_is_l0() -> None:
    result = classify_risk(["docs/operator_notes.md", "README.md"])
    assert result.level == "L0"


def test_application_code_is_l2() -> None:
    result = classify_risk(["clientplatform/application/retention.py"])
    assert result.level == "L2"


def test_governance_and_tenant_changes_are_l3() -> None:
    assert classify_risk(["AGENTS.md"]).level == "L3"
    assert classify_risk(["docs/CLIENTPLATFORM_CANON_TZ.md"]).level == "L3"
    assert classify_risk(["docs/CLIENTPLATFORM_UNICORN_ROADMAP.md"]).level == "L3"
    assert classify_risk(["clientplatform/domain/tenancy.py"]).level == "L3"
    assert classify_risk([".github/workflows/ci.yml"]).level == "L3"


def test_sensitive_paths_are_never_sent_to_reviewer() -> None:
    assert is_sensitive_path(".env")
    assert is_sensitive_path("config/.env.production")
    assert is_sensitive_path("keys/service.pem")
    assert not is_sensitive_path("services/payments/api.py")


def test_review_contract_is_bound_to_exact_reviewer_base_and_head() -> None:
    review = _review()
    assert validate_review(review, reviewer="claude", base_sha="b" * 40, head_sha="a" * 40) == ()
    assert validate_review(review, reviewer="gemini", base_sha="b" * 40, head_sha="a" * 40)
    assert validate_review(review, reviewer="claude", base_sha="c" * 40, head_sha="a" * 40)
    assert validate_review(review, reviewer="claude", base_sha="b" * 40, head_sha="b" * 40)


def test_pass_cannot_hide_material_finding() -> None:
    review = _review(verdict="PASS", findings=[_finding(severity="high")])
    errors = validate_review(review, reviewer="claude", base_sha="b" * 40, head_sha="a" * 40)
    assert any("critical/high findings require BLOCK" in error for error in errors)


def test_block_requires_material_finding() -> None:
    review = _review(verdict="BLOCK", findings=[_finding(severity="medium")])
    errors = validate_review(review, reviewer="claude", base_sha="b" * 40, head_sha="a" * 40)
    assert any("BLOCK verdict requires" in error for error in errors)


def test_material_block_is_valid_and_blocks_gate() -> None:
    review = _review(verdict="BLOCK", findings=[_finding(severity="critical")])
    assert validate_review(review, reviewer="claude", base_sha="b" * 40, head_sha="a" * 40) == ()
    assert review_blocks(review)


def test_context_never_sends_sensitive_file_diff(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(gate, "changed_paths", lambda _base, _head: (".env", "services/example.py"))

    def fake_run_git(*args: str) -> str:
        calls.append(args)
        if args[0] == "ls-tree":
            return ".env\nservices/example.py\n"
        if args[0] == "show":
            return "trusted text\n"
        if args[0] == "diff":
            assert ".env" not in args
            assert "services/example.py" in args
            return "safe diff\n"
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_run_git", fake_run_git)
    context = gate.build_context("b" * 40, "a" * 40)

    assert "safe diff" in context.text
    assert "[OMITTED SENSITIVE PATH]" in context.text
    assert any(call[0] == "diff" for call in calls)


def test_github_status_is_bound_to_exact_sha(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    captured: dict[str, object] = {}

    def fake_http_json(url, *, headers, payload, timeout, attempts=3):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout, attempts=attempts)
        return {"id": 1}

    monkeypatch.setattr(gate, "_http_json", fake_http_json)
    sha = "a" * 40
    gate.publish_github_status(
        token="token",
        repository="mailsvb2-bot/clientplatform",
        sha=sha,
        state="success",
        description="passed",
        target_url="https://github.com/example/run",
        context="AI Review / gate",
        timeout=30,
    )

    assert str(captured["url"]).endswith(f"/statuses/{sha}")
    assert captured["payload"] == {
        "state": "success",
        "description": "passed",
        "context": "AI Review / gate",
        "target_url": "https://github.com/example/run",
    }


def test_pricing_tables_are_date_aware() -> None:
    from datetime import date

    import scripts.ai_review_gate as gate

    intro = gate.pricing_for_model("claude-sonnet-5", on_date=date(2026, 8, 31))
    standard = gate.pricing_for_model("claude-sonnet-5", on_date=date(2026, 9, 1))
    opus = gate.pricing_for_model("claude-opus-5", on_date=date(2026, 8, 20))
    gemini = gate.pricing_for_model("gemini-3.7-flash", on_date=date(2026, 8, 20))

    assert str(intro.input_usd_per_million) == "2"
    assert str(intro.output_usd_per_million) == "10"
    assert str(standard.input_usd_per_million) == "3"
    assert str(standard.output_usd_per_million) == "15"
    assert str(opus.input_usd_per_million) == "5"
    assert str(opus.output_usd_per_million) == "25"
    assert str(gemini.input_usd_per_million) == "0.75"
    assert str(gemini.output_usd_per_million) == "3.75"


def test_usage_cost_counts_gemini_thought_tokens_as_output() -> None:
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    usage = gate.ProviderUsage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        thought_tokens=100_000,
    )
    pricing = gate.Pricing(Decimal("0.75"), Decimal("3.75"))

    assert gate.usage_cost_usd(usage, pricing) == Decimal("1.500")


def test_estimated_max_cost_uses_context_and_output_ceiling() -> None:
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    pricing = gate.Pricing(Decimal("5"), Decimal("25"))
    estimate = gate.estimated_max_cost_usd("x" * 100_000, pricing)

    assert estimate > Decimal("0.6")
    assert estimate < Decimal("1.0")


def test_latest_provider_status_deduplicates_exact_head(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    def fake_get(url, *, headers, timeout, attempts=3):
        assert url.endswith("/commits/" + "a" * 40 + "/status?per_page=100")
        return {
            "statuses": [
                {"context": "other", "state": "success"},
                {"context": gate._provider_status_context("claude", "b" * 40), "state": "failure"},
            ]
        }

    monkeypatch.setattr(gate, "_http_get_json", fake_get)
    state = gate.get_latest_status_state(
        token="token",
        repository="mailsvb2-bot/clientplatform",
        sha="a" * 40,
        context=gate._provider_status_context("claude", "b" * 40),
    )
    assert state == "failure"


def test_pull_head_lookup_is_exact_and_read_only(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    captured = {}

    def fake_get(url, *, headers, timeout, attempts=3):
        captured["url"] = url
        captured["headers"] = headers
        return {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}

    monkeypatch.setattr(gate, "_http_get_json", fake_get)
    head_sha, base_sha = gate.get_pull_ref_shas(
        token="token",
        repository="mailsvb2-bot/clientplatform",
        pull_number=209,
    )

    assert head_sha == "a" * 40
    assert base_sha == "b" * 40
    assert captured["url"].endswith("/pulls/209")
    assert captured["headers"]["authorization"] == "Bearer token"


def test_monthly_spend_sums_only_current_provider_month(monkeypatch) -> None:
    import io
    import json
    import zipfile
    from datetime import datetime, timezone
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    def zipped(record):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("claude-cost.json", json.dumps(record))
        return buffer.getvalue()

    base = "d" * 40
    artifacts = {
        1: {
            "schema_version": 1,
            "gate_revision": gate.GATE_REVISION,
            "reviewer": "claude",
            "base_sha": base,
            "head_sha": "a" * 40,
            "month": "2026-08",
            "charged_usd": "0.25",
        },
        2: {
            "schema_version": 1,
            "gate_revision": gate.GATE_REVISION,
            "reviewer": "claude",
            "base_sha": base,
            "head_sha": "b" * 40,
            "month": "2026-08",
            "charged_usd": "0.50",
        },
        3: {
            "schema_version": 1,
            "gate_revision": gate.GATE_REVISION,
            "reviewer": "claude",
            "base_sha": base,
            "head_sha": "c" * 40,
            "month": "2026-07",
            "charged_usd": "9.00",
        },
        5: {
            "schema_version": 1,
            "gate_revision": gate.GATE_REVISION,
            "reviewer": "claude",
            "base_sha": base,
            "head_sha": "a" * 40,
            "month": "2026-08",
            "charged_usd": "0.30",
        },
        6: {
            "schema_version": 1,
            "gate_revision": "v2",
            "reviewer": "claude",
            "head_sha": "e" * 40,
            "month": "2026-08",
            "charged_usd": "0.10",
        },
    }

    def fake_get_json(url, *, headers, timeout, attempts=3):
        assert "actions/artifacts" in url
        assert "name=ai-review-cost-claude" in url
        return {
            "artifacts": [
                {"id": 1, "name": "ai-review-cost-claude", "expired": False},
                {"id": 2, "name": "ai-review-cost-claude", "expired": False},
                {"id": 3, "name": "ai-review-cost-claude", "expired": False},
                {"id": 4, "name": "ai-review-cost-gemini-4-d", "expired": False},
                {"id": 5, "name": "ai-review-cost-claude", "expired": False},
                {"id": 6, "name": "ai-review-cost-claude", "expired": False},
            ]
        }

    def fake_download(*, token, repository, artifact_id, timeout):
        assert token == "token"
        assert repository == "mailsvb2-bot/clientplatform"
        return zipped(artifacts[artifact_id])

    monkeypatch.setattr(gate, "_http_get_json", fake_get_json)
    monkeypatch.setattr(gate, "_download_github_artifact_zip", fake_download)

    ledger = gate.github_monthly_cost_ledger(
        token="token",
        repository="mailsvb2-bot/clientplatform",
        reviewer="claude",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    # Same exact base+head/revision may have an original and conservative recovery artifact.
    # Count the larger current duplicate once and preserve legacy monthly spend:
    # max(0.25, 0.30) + 0.50 + 0.10 = 0.90. Legacy v2 has no base and
    # therefore contributes spend but cannot satisfy current exact-ref dedupe.
    assert ledger.total_usd == Decimal("0.90")
    assert ledger.current_gate_refs == frozenset({(base, "a" * 40), (base, "b" * 40)})


def test_provider_status_context_binds_full_base_sha() -> None:
    import scripts.ai_review_gate as gate

    base = "b" * 40
    context = gate._provider_status_context("claude", base)
    assert gate.GATE_REVISION in context
    assert context.endswith(base)


def test_same_head_on_new_base_is_not_current_cost_evidence() -> None:
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    old_base = "b" * 40
    new_base = "c" * 40
    head = "a" * 40
    ledger = gate.CostLedgerSummary(Decimal("0.25"), frozenset({(old_base, head)}))

    assert (old_base, head) in ledger.current_gate_refs
    assert (new_base, head) not in ledger.current_gate_refs
    assert gate._provider_status_context("claude", old_base) != gate._provider_status_context("claude", new_base)


def test_gemini_estimate_reserves_thinking_budget() -> None:
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    pricing = gate.Pricing(Decimal("0.75"), Decimal("3.75"))
    normal = gate.estimated_max_cost_usd("x" * 100_000, pricing)
    guarded = gate.estimated_max_cost_usd(
        "x" * 100_000,
        pricing,
        extra_output_tokens=gate.MAX_GEMINI_THOUGHT_TOKENS_RESERVE,
    )
    assert guarded > normal
    assert guarded - normal == (
        Decimal(gate.MAX_GEMINI_THOUGHT_TOKENS_RESERVE)
        * Decimal("3.75")
        / Decimal(1_000_000)
    )


def test_provider_usage_rejects_negative_or_boolean_counts() -> None:
    import pytest

    import scripts.ai_review_gate as gate

    with pytest.raises(gate.ReviewError):
        gate.ProviderUsage(input_tokens=-1, output_tokens=0)
    with pytest.raises(gate.ReviewError):
        gate.ProviderUsage(input_tokens=True, output_tokens=0)


def test_artifact_download_authenticates_only_github_redirect(monkeypatch) -> None:
    import urllib.error
    from email.message import Message

    import scripts.ai_review_gate as gate

    captured = {}

    class FakeOpener:
        def open(self, request, timeout):
            captured["github_auth"] = request.get_header("Authorization")
            headers = Message()
            headers["Location"] = "https://signed-artifacts.example.test/archive.zip"
            raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)

    def fake_bytes(url, *, headers, timeout, attempts=3):
        captured["signed_url"] = url
        captured["signed_headers"] = headers
        return b"zip-bytes"

    monkeypatch.setattr(gate.urllib.request, "build_opener", lambda *_handlers: FakeOpener())
    monkeypatch.setattr(gate, "_http_get_bytes", fake_bytes)

    result = gate._download_github_artifact_zip(
        token="secret-token",
        repository="mailsvb2-bot/clientplatform",
        artifact_id=123,
        timeout=30,
    )

    assert result == b"zip-bytes"
    assert captured["github_auth"] == "Bearer secret-token"
    assert captured["signed_url"].startswith("https://signed-artifacts.example.test/")
    assert "authorization" not in {key.lower() for key in captured["signed_headers"]}


def test_cached_anthropic_input_is_charged_conservatively() -> None:
    from datetime import date
    from decimal import Decimal

    import scripts.ai_review_gate as gate

    pricing = gate.pricing_for_model("claude-sonnet-5", on_date=date(2026, 8, 20))
    usage = gate.ProviderUsage(input_tokens=0, output_tokens=0, cached_input_tokens=1_000_000)

    assert gate.usage_cost_usd(usage, pricing) == Decimal("4")


def test_anthropic_paid_post_is_single_attempt(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    captured = {}
    review_json = json.dumps(
        {
            "schema_version": 2,
            "reviewer": "claude",
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "verdict": "PASS",
            "summary": "ok",
            "findings": [],
        }
    )

    def fake_http_json(url, *, headers, payload, timeout, attempts=3):
        captured["attempts"] = attempts
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": review_json}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    monkeypatch.setattr(gate, "_http_json", fake_http_json)
    response = gate.call_anthropic(
        api_key="key", model="claude-sonnet-5", prompt="review", timeout=30
    )

    assert captured["attempts"] == 1
    assert response.usage is not None


def test_gemini_response_usage_includes_thoughts_and_has_generation_cap(monkeypatch) -> None:
    import scripts.ai_review_gate as gate

    captured = {}
    review_json = json.dumps(
        {
            "schema_version": 2,
            "reviewer": "gemini",
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "verdict": "PASS",
            "summary": "ok",
            "findings": [],
        }
    )

    def fake_http_json(url, *, headers, payload, timeout, attempts=3):
        captured["payload"] = payload
        captured["attempts"] = attempts
        return {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": review_json}]}],
            "usage": {
                "total_input_tokens": 1000,
                "total_output_tokens": 200,
                "total_thought_tokens": 300,
            },
        }

    monkeypatch.setattr(gate, "_http_json", fake_http_json)
    response = gate.call_gemini(api_key="key", model="gemini-3.7-flash", prompt="review", timeout=30)

    assert captured["payload"]["generation_config"]["max_output_tokens"] == 6000
    assert captured["payload"]["generation_config"]["tool_choice"] == "none"
    assert captured["attempts"] == 1
    assert response.usage is not None
    assert response.usage.billed_output_tokens == 500


def _review_args(tmp_path, *, head_sha: str):
    from argparse import Namespace

    return Namespace(
        github_token_env="GITHUB_TOKEN",
        repository="mailsvb2-bot/clientplatform",
        pull_number=209,
        timeout=30,
        head=head_sha,
        base="b" * 40,
        max_context_bytes=700_000,
        expected_risk="L2",
        reviewer="claude",
        model="claude-sonnet-5",
        monthly_budget_usd="20",
        max_review_usd="4",
        api_key_env="ANTHROPIC_API_KEY",
        cost_output=str(tmp_path / "cost.json"),
        output=str(tmp_path / "review.json"),
        target_url="https://github.com/example/run",
    )


def test_stale_pr_head_skips_before_any_paid_or_context_work(monkeypatch, tmp_path) -> None:
    import scripts.ai_review_gate as gate

    head = "a" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: ("c" * 40, "b" * 40))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context must not be built")),
    )

    import pytest

    with pytest.raises(gate.ReviewError, match="stale pull request refs"):
        gate.cmd_review(args)
    assert not (tmp_path / "cost.json").exists()


def test_ambiguous_prior_paid_attempt_refuses_second_call(monkeypatch, tmp_path) -> None:
    from decimal import Decimal

    import pytest
    import scripts.ai_review_gate as gate

    head = "a" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: (head, "b" * 40))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: gate.ReviewContext(
            base_sha="b" * 40,
            head_sha=head,
            changed_paths=("clientplatform/example.py",),
            risk=gate.RiskAssessment("L2", ("test",)),
            text="context",
        ),
    )
    monkeypatch.setattr(gate, "review_instructions", lambda *_args, **_kwargs: "review")
    monkeypatch.setattr(
        gate,
        "github_monthly_cost_ledger",
        lambda **_kwargs: gate.CostLedgerSummary(Decimal("1"), frozenset({("b" * 40, head)})),
    )
    monkeypatch.setattr(gate, "get_latest_status_state", lambda **_kwargs: None)
    monkeypatch.setattr(
        gate,
        "call_anthropic",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider must not be called twice")),
    )

    with pytest.raises(gate.ReviewError, match="prior paid-attempt reservation"):
        gate.cmd_review(args)


def test_network_failure_keeps_max_cost_reservation(monkeypatch, tmp_path) -> None:
    import json
    from decimal import Decimal

    import pytest
    import scripts.ai_review_gate as gate

    head = "a" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: (head, "b" * 40))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: gate.ReviewContext(
            base_sha="b" * 40,
            head_sha=head,
            changed_paths=("clientplatform/example.py",),
            risk=gate.RiskAssessment("L2", ("test",)),
            text="context",
        ),
    )
    monkeypatch.setattr(gate, "review_instructions", lambda *_args, **_kwargs: "review")
    monkeypatch.setattr(
        gate,
        "github_monthly_cost_ledger",
        lambda **_kwargs: gate.CostLedgerSummary(Decimal("0"), frozenset()),
    )
    monkeypatch.setattr(gate, "get_latest_status_state", lambda **_kwargs: None)
    provider_states: list[str] = []
    monkeypatch.setattr(
        gate,
        "_publish_provider_status",
        lambda **kwargs: provider_states.append(kwargs["state"]),
    )
    monkeypatch.setattr(
        gate,
        "call_anthropic",
        lambda **_kwargs: (_ for _ in ()).throw(gate.ReviewError("ambiguous network failure")),
    )

    with pytest.raises(gate.ReviewError, match="ambiguous network failure"):
        gate.cmd_review(args)

    payload = json.loads((tmp_path / "cost.json").read_text(encoding="utf-8"))
    assert payload["record_state"] == "reserved_max"
    assert Decimal(payload["charged_usd"]) == Decimal(payload["estimated_max_usd"])
    assert payload["base_sha"] == "b" * 40
    assert payload["head_sha"] == head
    assert provider_states == ["pending"]


def test_stale_base_sha_fails_before_paid_work(monkeypatch, tmp_path) -> None:
    import pytest
    import scripts.ai_review_gate as gate

    head = "a" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: (head, "c" * 40))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context must not be built")),
    )

    with pytest.raises(gate.ReviewError, match="stale pull request refs"):
        gate.cmd_review(args)
    assert not (tmp_path / "cost.json").exists()


def test_pending_provider_status_blocks_retry_and_recovers_cost(monkeypatch, tmp_path) -> None:
    from decimal import Decimal
    import json
    import pytest
    import scripts.ai_review_gate as gate

    head = "a" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: (head, "b" * 40))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: gate.ReviewContext(
            base_sha="b" * 40, head_sha=head,
            changed_paths=("clientplatform/example.py",),
            risk=gate.RiskAssessment("L2", ("test",)), text="context",
        ),
    )
    monkeypatch.setattr(gate, "review_instructions", lambda *_args, **_kwargs: "review")
    monkeypatch.setattr(
        gate, "github_monthly_cost_ledger",
        lambda **_kwargs: gate.CostLedgerSummary(Decimal("1"), frozenset()),
    )
    monkeypatch.setattr(gate, "get_latest_status_state", lambda **_kwargs: "pending")
    monkeypatch.setattr(
        gate, "call_anthropic",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )

    with pytest.raises(gate.ReviewError, match="ambiguous paid attempt"):
        gate.cmd_review(args)

    payload = json.loads((tmp_path / "cost.json").read_text(encoding="utf-8"))
    assert payload["record_state"] == "recovered_max"
    assert payload["head_sha"] == head


def test_paid_result_that_becomes_stale_fails_closed(monkeypatch, tmp_path) -> None:
    from decimal import Decimal

    import pytest
    import scripts.ai_review_gate as gate

    head = "a" * 40
    base = "b" * 40
    args = _review_args(tmp_path, head_sha=head)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    refs = iter([(head, base), (head, base), (head, "c" * 40)])
    monkeypatch.setattr(gate, "get_pull_ref_shas", lambda **_kwargs: next(refs))
    monkeypatch.setattr(
        gate,
        "build_context",
        lambda *_args, **_kwargs: gate.ReviewContext(
            base_sha=base,
            head_sha=head,
            changed_paths=("clientplatform/example.py",),
            risk=gate.RiskAssessment("L2", ("test",)),
            text="context",
        ),
    )
    monkeypatch.setattr(gate, "review_instructions", lambda *_args, **_kwargs: "review")
    monkeypatch.setattr(
        gate,
        "github_monthly_cost_ledger",
        lambda **_kwargs: gate.CostLedgerSummary(Decimal("0"), frozenset()),
    )
    monkeypatch.setattr(gate, "get_latest_status_state", lambda **_kwargs: None)
    states: list[str] = []
    monkeypatch.setattr(gate, "_publish_provider_status", lambda **kwargs: states.append(kwargs["state"]))
    monkeypatch.setattr(
        gate,
        "call_anthropic",
        lambda **_kwargs: gate.ProviderResponse(
            text=json.dumps(_review(base_sha=base, head_sha=head)),
            usage=gate.ProviderUsage(input_tokens=10, output_tokens=10),
        ),
    )

    with pytest.raises(gate.ReviewError, match="stale pull request refs"):
        gate.cmd_review(args)

    assert states == ["pending", "error"]
    payload = json.loads((tmp_path / "cost.json").read_text(encoding="utf-8"))
    assert payload["record_state"] == "final"
    assert payload["base_sha"] == base
    assert payload["head_sha"] == head


def test_provider_nonterminal_responses_fail_closed(monkeypatch) -> None:
    import pytest
    import scripts.ai_review_gate as gate

    monkeypatch.setattr(
        gate, "_http_json",
        lambda *args, **kwargs: {
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "{}"}],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )
    with pytest.raises(gate.ReviewError, match="did not complete normally"):
        gate.call_anthropic(api_key="key", model="claude-sonnet-5", prompt="review", timeout=30)

    monkeypatch.setattr(
        gate, "_http_json",
        lambda *args, **kwargs: {
            "status": "failed",
            "steps": [],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 0, "total_thought_tokens": 0},
        },
    )
    with pytest.raises(gate.ReviewError, match="did not complete normally"):
        gate.call_gemini(api_key="key", model="gemini-3.7-flash", prompt="review", timeout=30)


def test_workflow_artifact_and_secret_contracts() -> None:
    from pathlib import Path

    workflow = Path(".github/workflows/independent-ai-review.yml").read_text(encoding="utf-8")
    assert "if-no-files-found: error" not in workflow
    # Provider secrets are scoped to the paid review steps, not job-wide env.
    assert workflow.count("ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}") == 1
    assert workflow.count("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}") == 1
    assert workflow.count("queue: max") == 2
    # Cost artifacts use stable names for server-side REST filtering. On workflow reruns,
    # upload only when a record exists and overwrite that run's prior immutable artifact.
    assert workflow.count("name: ai-review-cost-claude") == 1
    assert workflow.count("name: ai-review-cost-gemini") == 1
    assert workflow.count("overwrite: true") == 2
    assert workflow.count("hashFiles('artifacts/ai-review/") == 2
    # Human-readable review artifacts are unique across rerun attempts.
    assert workflow.count("github.run_attempt") == 2
