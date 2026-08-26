from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "independent-ai-review.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_external_ai_review_disable_is_explicit_and_reviewed_in_repo() -> None:
    text = _workflow_text()

    assert "EXTERNAL_AI_REVIEW_MODE: disabled" in text
    assert "required|disabled" in text
    assert "external_ai_review_mode: ${{ steps.policy.outputs.external_ai_review_mode }}" in text
    assert text.count("needs.prepare.outputs.external_ai_review_mode == 'required'") == 2


def test_disabled_policy_skips_providers_and_publishes_honest_gate_result() -> None:
    text = _workflow_text()

    assert "EXTERNAL_AI_REVIEW_MODE: ${{ needs.prepare.outputs.external_ai_review_mode }}" in text
    assert "[ \"$CLAUDE_RESULT\" != 'skipped' ] || [ \"$GEMINI_RESULT\" != 'skipped' ]" in text
    assert "External AI review disabled but provider jobs were not skipped" in text
    assert "external AI review temporarily disabled by trusted repository policy" in text
    assert "Unknown external AI review mode; failed closed" in text


def test_required_provider_contract_is_preserved_for_reenable() -> None:
    text = _workflow_text()

    assert "Claude adversarial review did not pass" in text
    assert "Gemini system review did not pass" in text
    assert "passed Claude + Gemini for current base+head" in text
    assert "--reviewer claude" in text
    assert "--reviewer gemini" in text
