from __future__ import annotations

import asyncio
import logging
import re
import time

from clientplatform.application.sales_ai_orchestration import (
    apply_sales_ai_analysis,
    cancel_sales_ai_job,
    claim_sales_ai_jobs,
    purge_sales_ai_retention,
    retry_sales_ai_job,
    sales_ai_analysis_egress_permit,
)
from clientplatform.domain.sales_ai_jobs import SalesAIJob, SalesAIJobLeaseLost
from clientplatform.infrastructure.sales_ai_provider import SalesAIProvider, build_sales_ai_provider
from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from core.task_manager import TaskManager

log = logging.getLogger(__name__)
_RUNTIME: "SalesAIWorkerRuntime | None" = None


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return (normalized or "sales_ai_failed")[:120]


class SalesAIWorkerRuntime:
    """Background advisory model worker; it has no messenger sender."""

    def __init__(self, *, task_manager: TaskManager, config: SalesAIRuntimeConfig, provider: SalesAIProvider) -> None:
        self._task_manager = task_manager
        self.config = config
        self._provider = provider
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_retention = 0.0
        self.processed = 0
        self.retried = 0
        self.dead = 0
        self.cancelled = 0
        self.last_error: str | None = None

    def start(self) -> bool:
        if not self.config.enabled or self._running:
            return False
        self._running = True
        self._task = self._task_manager.create(self._run(), name="clientplatform-sales-ai-worker")
        return True

    async def _run(self) -> None:
        try:
            while self._running:
                try:
                    jobs = await asyncio.to_thread(
                        claim_sales_ai_jobs,
                        limit=1,
                        lock_ttl_seconds=self.config.worker_lock_ttl_seconds,
                    )
                    for job in jobs:
                        await self._process(job)
                    now = time.monotonic()
                    if now - self._last_retention >= 60.0:
                        await asyncio.to_thread(
                            purge_sales_ai_retention,
                            raw_message_ttl_hours=self.config.raw_message_ttl_hours,
                            analysis_ttl_days=self.config.analysis_ttl_days,
                        )
                        self._last_retention = now
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # validator: allow-wide-except
                    self.last_error = _error_code(exc)
                    log.exception("Sales AI worker tick failed")
                await asyncio.sleep(self.config.worker_interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            self._task = None

    async def _process(self, job: SalesAIJob) -> None:
        try:
            # Cross-process egress barrier: the consent row remains locked for the
            # entire network request, so disable/provider change cannot return
            # while this request is in flight and no later request can start on the
            # old consent epoch.
            async with sales_ai_analysis_egress_permit(
                job,
                consent_target=self.config.consent_target,
            ) as work:
                analysis = await self._provider.analyze(
                    customer_text=work.customer_text,
                    current_stage=work.current_stage,
                    source_kind=work.source_kind,
                )
            await asyncio.to_thread(
                apply_sales_ai_analysis,
                work=work,
                analysis=analysis,
                provider=self.config.provider,
                model=self.config.model,
                consent_target=self.config.consent_target,
            )
            self.processed += 1
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except PermissionError as exc:
            self.last_error = "sales_ai_consent_unavailable"
            try:
                await asyncio.to_thread(cancel_sales_ai_job, job, reason="consent_target_changed_or_disabled")
                self.cancelled += 1
            except SalesAIJobLeaseLost:
                pass
            log.info("Sales AI job cancelled before egress", extra={"job_id": job.id, "reason": str(exc)[:120]})
        except SalesAIJobLeaseLost:
            self.last_error = "sales_ai_job_lease_lost"
            log.info("Sales AI job stopped after lease change", extra={"job_id": job.id})
        except Exception as exc:  # validator: allow-wide-except
            code = _error_code(exc)
            self.last_error = code
            try:
                updated = await asyncio.to_thread(
                    retry_sales_ai_job,
                    job,
                    error_code=code,
                    max_attempts=self.config.worker_max_attempts,
                )
                if updated.status.value == "dead":
                    self.dead += 1
                else:
                    self.retried += 1
            except SalesAIJobLeaseLost:
                log.warning("Sales AI failure transition lost its lease", extra={"job_id": job.id})
            except Exception:  # validator: allow-wide-except
                log.exception("Sales AI failure transition itself failed", extra={"job_id": job.id, "business_id": job.business_id})
            log.warning("Sales AI advisory job failed", extra={"job_id": job.id, "business_id": job.business_id, "error_code": code})

    def health_snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "running": self._running,
            "provider": self.config.provider,
            "model": self.config.model,
            "processed": self.processed,
            "retried": self.retried,
            "dead": self.dead,
            "cancelled": self.cancelled,
            "last_error": self.last_error,
        }


def bind_sales_ai_worker(task_manager: TaskManager) -> SalesAIWorkerRuntime | None:
    global _RUNTIME
    config = SalesAIRuntimeConfig.from_env()
    if not config.enabled:
        _RUNTIME = None
        return None
    if _RUNTIME is not None and _RUNTIME._running:
        return _RUNTIME
    credentials = EnvironmentCredentialProvider()
    credentials.resolve(config.api_key_reference)
    provider = build_sales_ai_provider(config, credential_provider=credentials)
    runtime = SalesAIWorkerRuntime(task_manager=task_manager, config=config, provider=provider)
    runtime.start()
    _RUNTIME = runtime
    return runtime


__all__ = ["SalesAIWorkerRuntime", "bind_sales_ai_worker"]
