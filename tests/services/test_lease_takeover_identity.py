from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.kernel.test_validation import valid_manifest
from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessProbeResult,
    LeaseOwnerProcessState,
)
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    LeaseOwner,
    decode_manifest_primitive,
)
from wish_builder.services.journal import (
    DurableJournal,
    LeaseStateCode,
)
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseMutationStatus,
    recover_coordinator_lease,
)

RUN_ID = "RUN-TAKEOVER-IDENTITY"
MANIFEST_DIGEST = "sha256:" + "a" * 64
BASE_TIME = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if not self.values:
            raise RuntimeError("clock exhausted")
        return self.values.pop(0)


class Probe:
    def __init__(self, result: LeaseOwnerProcessProbeResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[LeaseOwner, str]] = []

    def __call__(
        self,
        owner: LeaseOwner,
        *,
        local_host_id: str,
    ) -> LeaseOwnerProcessProbeResult:
        self.calls.append((owner, local_host_id))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def manifest():
    primitive = valid_manifest()
    primitive["run_id"] = RUN_ID
    decoded = decode_manifest_primitive(primitive)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def owner(
    coordinator_id: str,
    *,
    host_id: str = "host-test",
    process_id: int = 100,
    process_start_id: str | None = None,
    workspace: str = "3",
) -> LeaseOwner:
    return LeaseOwner(
        ActorIdentity(
            ActorType.COORDINATOR,
            coordinator_id,
            host_id,
            process_id,
            process_start_id or f"process-start-{coordinator_id}",
        ),
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + workspace * 64,
        "sha256:" + "4" * 64,
    )


def recovery(root: str | Path):
    return recover_coordinator_lease(
        root,
        manifest(),
        coordinator_epoch=1,
        repair_derived=False,
    )


def service(
    root: str | Path,
    lease_owner: LeaseOwner,
    clock: Clock,
    probe: Probe,
) -> CoordinatorLeaseService:
    return CoordinatorLeaseService(
        DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(
                root,
                RUN_ID,
                authority_clock=clock,
                lock_timeout_seconds=10,
            ),
        ),
        lambda: recovery(root),
        run_id=RUN_ID,
        owner=lease_owner,
        manifest_digest=MANIFEST_DIGEST,
        lease_ttl_seconds=90,
        lease_clock_skew_seconds=2,
        prior_owner_process_probe=probe,
    )


class LeaseTakeoverIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def segment_lines(self) -> list[bytes]:
        segment = self.root / "segments" / "segment-00000001.jsonl"
        return [] if not segment.exists() else segment.read_bytes().splitlines()

    def acquire_initial(self, lease_owner: LeaseOwner) -> None:
        initial = service(
            self.root,
            lease_owner,
            Clock(BASE_TIME),
            Probe(RuntimeError("initial acquire must not probe")),
        ).acquire(event_id="EVENT-ACQUIRE-A", lease_id="LEASE-A")
        self.assertEqual(LeaseMutationStatus.COMMITTED, initial.status)

    def test_initial_acquire_and_same_owner_expired_reacquire_do_not_probe(self) -> None:
        holder = owner("coordinator-a")
        probe = Probe(RuntimeError("same owner must not probe"))
        controller = service(
            self.root,
            holder,
            Clock(BASE_TIME, BASE_TIME + timedelta(seconds=93)),
            probe,
        )

        acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        reacquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A-SECOND",
            lease_id="LEASE-A-SECOND",
        )

        self.assertEqual(LeaseMutationStatus.COMMITTED, acquired.status)
        self.assertEqual(LeaseMutationStatus.COMMITTED, reacquired.status)
        self.assertEqual([], probe.calls)
        self.assertEqual(2, len(self.segment_lines()))

    def test_dead_prior_exact_process_allows_expired_takeover(self) -> None:
        prior_owner = owner("coordinator-a", process_id=4321)
        self.acquire_initial(prior_owner)
        probe = Probe(LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD))
        contender = service(
            self.root,
            owner("coordinator-b", process_id=8765),
            Clock(BASE_TIME + timedelta(seconds=93)),
            probe,
        )

        takeover = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.COMMITTED, takeover.status)
        self.assertEqual([(prior_owner, "host-test")], probe.calls)
        assert takeover.lease_state is not None and takeover.lease_state.lease is not None
        self.assertEqual(2, takeover.lease_state.lease.fencing_token)
        self.assertEqual("coordinator-b", takeover.lease_state.lease.coordinator_id)
        self.assertEqual(2, len(self.segment_lines()))

    def test_alive_prior_exact_process_fails_closed_without_append(self) -> None:
        prior_owner = owner("coordinator-a", process_id=4321)
        self.acquire_initial(prior_owner)
        clock = Clock(BASE_TIME + timedelta(seconds=93))
        probe = Probe(
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.EXACT_ALIVE)
        )
        contender = service(
            self.root,
            owner("coordinator-b", process_id=8765),
            clock,
            probe,
        )

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, denied.lease_state_code)
        self.assertIn("exact_alive", denied.detail or "")
        self.assertEqual([(prior_owner, "host-test")], probe.calls)
        self.assertEqual(0, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_pid_reuse_fails_closed_without_append(self) -> None:
        prior_owner = owner(
            "coordinator-a",
            process_id=4321,
            process_start_id="process-start-original",
        )
        self.acquire_initial(prior_owner)
        clock = Clock(BASE_TIME + timedelta(seconds=93))
        probe = Probe(LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.PID_REUSED))
        contender = service(
            self.root,
            owner(
                "coordinator-a",
                process_id=4321,
                process_start_id="process-start-reused",
            ),
            clock,
            probe,
        )

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-REUSED",
            lease_id="LEASE-REUSED",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(
            LeaseStateCode.LEASE_IDENTITY_MISMATCH,
            denied.lease_state_code,
        )
        self.assertIn("pid_reused", denied.detail or "")
        self.assertEqual([(prior_owner, "host-test")], probe.calls)
        self.assertEqual(0, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_cross_host_unknown_fails_closed_without_append(self) -> None:
        prior_owner = owner("coordinator-a", host_id="host-a")
        self.acquire_initial(prior_owner)
        clock = Clock(BASE_TIME + timedelta(seconds=93))
        probe = Probe(LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.UNKNOWN))
        contender = service(
            self.root,
            owner("coordinator-b", host_id="host-b"),
            clock,
            probe,
        )

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, denied.lease_state_code)
        self.assertIn("unknown", denied.detail or "")
        self.assertEqual([(prior_owner, "host-b")], probe.calls)
        self.assertEqual(0, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_different_host_fails_closed_without_append(self) -> None:
        prior_owner = owner("coordinator-a", host_id="host-a")
        self.acquire_initial(prior_owner)
        clock = Clock(BASE_TIME + timedelta(seconds=93))
        probe = Probe(
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DIFFERENT_HOST)
        )
        contender = service(
            self.root,
            owner("coordinator-b", host_id="host-b"),
            clock,
            probe,
        )

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, denied.lease_state_code)
        self.assertIn("different_host", denied.detail or "")
        self.assertEqual([(prior_owner, "host-b")], probe.calls)
        self.assertEqual(0, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_probe_error_fails_closed_without_append(self) -> None:
        prior_owner = owner("coordinator-a")
        self.acquire_initial(prior_owner)
        clock = Clock(BASE_TIME + timedelta(seconds=93))
        probe = Probe(OSError("identity backend unavailable"))
        contender = service(
            self.root,
            owner("coordinator-b"),
            clock,
            probe,
        )

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, denied.lease_state_code)
        self.assertIn("probe_error", denied.detail or "")
        self.assertEqual([(prior_owner, "host-test")], probe.calls)
        self.assertEqual(0, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))


if __name__ == "__main__":
    unittest.main()
