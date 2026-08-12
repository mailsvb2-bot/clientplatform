from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str, marker: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"SALES_AI_FIX_FAILED:{marker}:count={count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Keep provider contracts importable in the dependency-light canon contour.
provider_path = Path("clientplatform/infrastructure/sales_ai_provider.py")
provider = provider_path.read_text(encoding="utf-8")
provider = provider.replace(
    "from typing import Any, Mapping, Protocol\n",
    "from typing import TYPE_CHECKING, Any, Mapping, Protocol\n",
    1,
)
provider = provider.replace("\nimport aiohttp\n", "\n", 1)
provider = provider.replace(
    "from clientplatform.runtime.secrets import EnvironmentCredentialProvider\n",
    "if TYPE_CHECKING:\n    from clientplatform.runtime.secrets import EnvironmentCredentialProvider\n",
    1,
)
provider = provider.replace(
    """    ) -> Mapping[str, Any]:
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        await _assert_public_destination(url)
""",
    """    ) -> Mapping[str, Any]:
        # Provider contracts and fake-transport tests must remain dependency-light.
        try:
            import aiohttp
        except ImportError as exc:
            raise SalesAIProviderError(
                "aiohttp is required for Sales AI network transport"
            ) from exc
        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        await _assert_public_destination(url)
""",
    1,
)
provider = provider.replace(
    "        self._credentials = credential_provider or EnvironmentCredentialProvider()\n",
    """        if credential_provider is None:
            # Secret/crypto machinery is required only for a real provider call.
            from clientplatform.runtime.secrets import EnvironmentCredentialProvider

            credential_provider = EnvironmentCredentialProvider()
        self._credentials = credential_provider
""",
    1,
)
if "\nimport aiohttp\n" in provider:
    raise SystemExit("SALES_AI_FIX_FAILED:provider_http_import")
provider_path.write_text(provider, encoding="utf-8")

# 2) Retention cleanup is lifecycle-independent from provider enablement.
replace_once(
    "clientplatform/runtime/sales_ai.py",
    "def __init__(self, *, task_manager: TaskManager, config: SalesAIRuntimeConfig, provider: SalesAIProvider) -> None:\n",
    "def __init__(self, *, task_manager: TaskManager, config: SalesAIRuntimeConfig, provider: SalesAIProvider | None) -> None:\n",
    "runtime_provider_optional",
)
replace_once(
    "clientplatform/runtime/sales_ai.py",
    """    def start(self) -> bool:
        if not self.config.enabled or self._running:
            return False
""",
    """    def start(self) -> bool:
        # Retention must keep running after provider work is disabled.
        if self._running:
            return False
""",
    "runtime_start_retention",
)
replace_once(
    "clientplatform/runtime/sales_ai.py",
    """                    jobs = await asyncio.to_thread(
                        claim_sales_ai_jobs,
                        limit=1,
                        lock_ttl_seconds=self.config.worker_lock_ttl_seconds,
                    )
                    for job in jobs:
                        await self._process(job)
                    now = time.monotonic()
""",
    """                    if self.config.enabled:
                        jobs = await asyncio.to_thread(
                            claim_sales_ai_jobs,
                            limit=1,
                            lock_ttl_seconds=self.config.worker_lock_ttl_seconds,
                        )
                        for job in jobs:
                            await self._process(job)
                    now = time.monotonic()
""",
    "runtime_claim_only_when_enabled",
)
replace_once(
    "clientplatform/runtime/sales_ai.py",
    """    async def _process(self, job: SalesAIJob) -> None:
        try:
""",
    """    async def _process(self, job: SalesAIJob) -> None:
        try:
            if self._provider is None:
                raise RuntimeError("Sales AI provider is unavailable while processing is enabled")
""",
    "runtime_provider_guard",
)
replace_once(
    "clientplatform/runtime/sales_ai.py",
    """    config = SalesAIRuntimeConfig.from_env()
    if not config.enabled:
        _RUNTIME = None
        return None
    if _RUNTIME is not None and _RUNTIME._running:
        return _RUNTIME
    credentials = EnvironmentCredentialProvider()
    credentials.resolve(config.api_key_reference)
    provider = build_sales_ai_provider(config, credential_provider=credentials)
    runtime = SalesAIWorkerRuntime(task_manager=task_manager, config=config, provider=provider)
""",
    """    config = SalesAIRuntimeConfig.from_env()
    if _RUNTIME is not None and _RUNTIME._running:
        return _RUNTIME
    provider: SalesAIProvider | None = None
    if config.enabled:
        credentials = EnvironmentCredentialProvider()
        credentials.resolve(config.api_key_reference)
        provider = build_sales_ai_provider(config, credential_provider=credentials)
    runtime = SalesAIWorkerRuntime(task_manager=task_manager, config=config, provider=provider)
""",
    "runtime_bind_retention_when_disabled",
)

# 3) Carry an explicit source-order + plan version through draft egress.
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    """@dataclass(frozen=True, slots=True)
class SalesAIWorkInput:
    job: SalesAIJob
    customer_text: str
    current_stage: str
    source_kind: str
    consent_epoch: int
    text_was_redacted: bool


""",
    """@dataclass(frozen=True, slots=True)
class SalesAIWorkInput:
    job: SalesAIJob
    customer_text: str
    current_stage: str
    source_kind: str
    consent_epoch: int
    text_was_redacted: bool


@dataclass(frozen=True, slots=True)
class SalesAIDraftEvidence:
    lead: SalesLead
    customer_text: str
    analysis: SalesAIAnalysis
    action_kind: str
    verified_offer: SalesAIVerifiedOffer | None
    source_order_key: str
    plan_id: str


""",
    "draft_evidence_dataclass",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    "def _load_latest_evidence_in_conn(conn: Any, *, actor: TenantContext, lead_id: str) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:\n",
    "def _load_latest_evidence_in_conn(conn: Any, *, actor: TenantContext, lead_id: str) -> SalesAIDraftEvidence:\n",
    "draft_load_return_type",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    "    return lead, message_text, analysis, action_kind, offer\n",
    """    return SalesAIDraftEvidence(
        lead=lead,
        customer_text=message_text,
        analysis=analysis,
        action_kind=action_kind,
        verified_offer=offer,
        source_order_key=source_order_key,
        plan_id=plan_id,
    )
""",
    "draft_load_return_value",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    "def load_latest_sales_ai_evidence(*, actor: TenantContext, lead_id: str) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:\n",
    "def load_latest_sales_ai_evidence(*, actor: TenantContext, lead_id: str) -> SalesAIDraftEvidence:\n",
    "draft_public_return_type",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    """    consent_target: str,
) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:
""",
    """    consent_target: str,
) -> SalesAIDraftEvidence:
""",
    "draft_prepare_return_type",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    """    lead, text, analysis, action, offer = _load_latest_evidence_in_conn(
        conn,
        actor=current,
        lead_id=lead_id,
    )
    prepared = prepare_sales_ai_text(text, mode=consent.data_mode)
    return lead, prepared.text, analysis, action, offer
""",
    """    evidence = _load_latest_evidence_in_conn(
        conn,
        actor=current,
        lead_id=lead_id,
    )
    prepared = prepare_sales_ai_text(evidence.customer_text, mode=consent.data_mode)
    return SalesAIDraftEvidence(
        lead=evidence.lead,
        customer_text=prepared.text,
        analysis=evidence.analysis,
        action_kind=evidence.action_kind,
        verified_offer=evidence.verified_offer,
        source_order_key=evidence.source_order_key,
        plan_id=evidence.plan_id,
    )
""",
    "draft_prepare_value",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    ") -> AsyncIterator[tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]]:\n",
    ") -> AsyncIterator[SalesAIDraftEvidence]:\n",
    "draft_permit_return_type",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    """    async with holder as evidence:
        yield evidence


__all__ = [
""",
    """    async with holder as evidence:
        yield evidence


def validate_sales_ai_draft_freshness(
    *,
    actor: TenantContext,
    lead_id: str,
    expected_source_order_key: str,
    expected_plan_id: str,
) -> None:
    """Fail closed if customer evidence or the canonical plan changed during drafting."""
    with get_db_ro() as conn:
        current = _load_latest_evidence_in_conn(conn, actor=actor, lead_id=lead_id)
        if current.source_order_key != expected_source_order_key:
            raise ValueError("sales AI draft became stale because a newer customer message arrived")
        if current.plan_id != expected_plan_id:
            raise ValueError("sales AI draft became stale because the canonical action plan changed")


__all__ = [
""",
    "draft_freshness_validator",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    "    \"SalesAIWorkInput\",\n",
    "    \"SalesAIDraftEvidence\",\n    \"SalesAIWorkInput\",\n",
    "draft_export_dataclass",
)
replace_once(
    "clientplatform/application/sales_ai_orchestration.py",
    "    \"sales_ai_draft_egress_permit\",\n",
    "    \"sales_ai_draft_egress_permit\",\n    \"validate_sales_ai_draft_freshness\",\n",
    "draft_export_validator",
)
