"""Fenced single-node generation transactions for the fixture engine host."""
from __future__ import annotations

from dataclasses import dataclass

from app.runtime.engine_host.store import FixtureStore, Package, StoreError


class UpdateError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class UpdateResult:
    operation_id: str
    engine_id: str
    generation: int
    digest: str
    state: str
    fence: int = 0


class FixtureUpdater:
    """Persist each update phase and route only through a fenced CAS.

    Package verification and process readiness are performed by the orchestrator;
    this class supplies the durable phase boundary used by recovery.
    """

    PHASES = (
        "prepared", "verified", "staged", "candidate_started", "handshake_complete",
        "selftest_complete", "candidate_ready", "active", "draining", "retained", "complete",
    )

    def __init__(self, store: FixtureStore):
        self.store = store

    @staticmethod
    def _result(row: dict) -> UpdateResult:
        generation = (row.get("target_generation") if row.get("target_generation") is not None
                      else row["expected_generation"])
        return UpdateResult(row["operation_id"], row["engine_id"], int(generation),
                            row["digest"], row["state"], int(row.get("expected_fence", 0)))

    def prepare(self, *, engine_id: str, digest: str, expected_generation: int,
                idempotency_key: str, version: str = "fixture", generation: int | None = None,
                expected_fence: int | None = None, payload_digest: str = "",
                runtime_digest: str = "", sdk_version: str = "",
                task_schema: str = "engine.task/v1", result_schema: str = "engine.result/v1",
                capability_version: str = "fixture/v1", identity: dict | None = None) -> UpdateResult:
        if not digest or not idempotency_key:
            raise UpdateError("invalid_input")
        package = Package(engine_id=engine_id, digest=digest, version=version,
                          generation=generation or expected_generation + 1, state="candidate",
                          payload_digest=payload_digest, runtime_digest=runtime_digest,
                          sdk_version=sdk_version, task_schema=task_schema,
                          result_schema=result_schema, capability_version=capability_version)
        already_present = self.store.package(engine_id, digest) is not None
        self.store.add_package(package, identity=identity)
        request = {
            "version": version, "payload_digest": payload_digest,
            "runtime_digest": runtime_digest, "sdk_version": sdk_version,
            "task_schema": task_schema, "result_schema": result_schema,
            "capability_version": capability_version,
        }
        try:
            row = self.store.start_update(
                engine_id=engine_id, digest=digest, expected_generation=expected_generation,
                expected_fence=expected_fence, idempotency_key=idempotency_key, request=request,
            )
        except Exception:
            if not already_present:
                self.store.discard_package_if_unreferenced(engine_id, digest)
            raise
        return self._result(row)

    def phase(self, operation_id: str, phase: str, *, allowed_from=None,
              detail: dict | None = None) -> UpdateResult:
        if phase not in self.PHASES and phase not in {"failed", "blocked"}:
            raise UpdateError("unknown_update_phase")
        return self._result(self.store.transition_update(
            operation_id, phase, allowed_from=allowed_from, detail=detail))

    def verified(self, operation_id: str, identity: dict) -> UpdateResult:
        return self.phase(operation_id, "verified", allowed_from=("prepared",), detail=identity)

    def staged(self, operation_id: str, staging: dict) -> UpdateResult:
        return self.phase(operation_id, "staged", allowed_from=("verified",), detail=staging)

    def candidate_started(self, operation_id: str, launch: dict) -> UpdateResult:
        return self.phase(operation_id, "candidate_started", allowed_from=("staged",), detail=launch)

    def handshake_complete(self, operation_id: str, receipt: dict) -> UpdateResult:
        return self.phase(operation_id, "handshake_complete",
                          allowed_from=("candidate_started",), detail=receipt)

    def selftest_complete(self, operation_id: str, receipt: dict) -> UpdateResult:
        if receipt.get("ok") is not True:
            raise UpdateError("candidate_selftest_failed")
        return self.phase(operation_id, "selftest_complete",
                          allowed_from=("handshake_complete",), detail=receipt)

    def candidate_ready(self, operation_id: str, *, capacity_receipt: dict | None = None) -> UpdateResult:
        receipt = capacity_receipt or {"fixture_only": True, "admitted": True}
        if receipt.get("admitted") is not True:
            raise UpdateError("capacity_not_admitted")
        return self.phase(operation_id, "candidate_ready",
                          allowed_from=("selftest_complete",), detail=receipt)

    def activate(self, operation_id: str) -> UpdateResult:
        # Store.activate itself is the atomic CAS. Keeping the call usable from a
        # repository-only test does not let the orchestrator skip readiness.
        return self._result(self.store.activate(operation_id))

    def drain(self, operation_id: str, receipt: dict) -> UpdateResult:
        return self.phase(operation_id, "draining", allowed_from=("active",), detail=receipt)

    def retain(self, operation_id: str, receipt: dict) -> UpdateResult:
        return self.phase(operation_id, "retained", allowed_from=("draining",), detail=receipt)

    def complete(self, operation_id: str, receipt: dict | None = None) -> UpdateResult:
        return self.phase(operation_id, "complete", allowed_from=("retained",), detail=receipt)

    def prepare_rollback(self, *, engine_id: str, digest: str, expected_generation: int,
                         idempotency_key: str | None = None,
                         expected_fence: int | None = None,
                         identity: dict | None = None) -> UpdateResult:
        request = {"identity": identity or {}}
        row = self.store.prepare_rollback(
            engine_id, digest, expected_generation, idempotency_key=idempotency_key,
            expected_fence=expected_fence, request=request)
        return self._result(row)

    def rollback(self, *, engine_id: str, digest: str, expected_generation: int,
                 idempotency_key: str | None = None,
                 expected_fence: int | None = None,
                 identity: dict | None = None) -> UpdateResult:
        """Compatibility alias: rollback is only prepared; readiness precedes activate."""
        return self.prepare_rollback(
            engine_id=engine_id, digest=digest, expected_generation=expected_generation,
            idempotency_key=idempotency_key, expected_fence=expected_fence,
            identity=identity)
