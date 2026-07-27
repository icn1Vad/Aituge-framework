from __future__ import annotations

import asyncio
import logging

from contract.application.runtime_service import RuntimeContractReviewService
from contract.config import Settings


logger = logging.getLogger(__name__)


class ContractDispatcher:
    """Persistent Contract Attempt dispatcher and Framework reconciler."""

    def __init__(
        self,
        runtime_service: RuntimeContractReviewService,
        settings: Settings,
    ) -> None:
        self.runtime_service = runtime_service
        self.poll_seconds = settings.dispatcher_poll_seconds

    def run_once(self) -> tuple[int, int]:
        dispatched = self.runtime_service.dispatch_pending_attempts()
        reconciled = self.runtime_service.reconcile_active_reviews()
        return dispatched, reconciled

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                dispatched, reconciled = await asyncio.to_thread(self.run_once)
                if dispatched or reconciled:
                    logger.info(
                        "Contract dispatcher cycle completed: dispatched=%s reconciled=%s",
                        dispatched,
                        reconciled,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Contract dispatcher cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue

