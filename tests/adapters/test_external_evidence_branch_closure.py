from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters import external_evidence as evidence
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
)
from wish_builder.services.ports import AttemptObservation, TrellisLifecycleState


def _observation(root: Path) -> tuple[AttemptObservation, ExecutionIdentity]:
    operation_id = "TRELLIS-PREPARE-001"
    observation = AttemptObservation(
        operation_id=operation_id,
        status=EffectStatus.APPLIED,
        observed_at="2026-08-19T00:00:00Z",
        lifecycle_state=TrellisLifecycleState.PREPARED,
        effect_digest="sha256:" + "a" * 64,
        attempt_id="ATTEMPT-001",
        trellis_task_id="trellis-task-001",
        worktree_id="worktree-001",
        worktree_path=str(root / "attempt"),
        base_commit="b" * 40,
    )
    identity = ExecutionIdentity(
        "RUN-EVIDENCE",
        1,
        "TASK-001",
        1,
        operation_id,
    )
    return observation, identity


class ExternalEvidenceBranchClosureTests(unittest.TestCase):
    def test_directory_sync_uses_and_closes_a_posix_directory_handle(self) -> None:
        target = Path("/tmp")
        with (
            mock.patch.object(evidence.os, "name", "posix"),
            mock.patch.object(evidence.os, "open", return_value=17) as opened,
            mock.patch.object(evidence.os, "fsync") as synced,
            mock.patch.object(evidence.os, "close") as closed,
        ):
            evidence._sync_directory(target)
        opened.assert_called_once()
        synced.assert_called_once_with(17)
        closed.assert_called_once_with(17)

    def test_directory_sync_closes_the_handle_when_fsync_fails(self) -> None:
        target = Path("/tmp")
        with (
            mock.patch.object(evidence.os, "name", "posix"),
            mock.patch.object(evidence.os, "open", return_value=23),
            mock.patch.object(evidence.os, "fsync", side_effect=OSError("disk")),
            mock.patch.object(evidence.os, "close") as closed,
            self.assertRaises(OSError),
        ):
            evidence._sync_directory(target)
        closed.assert_called_once_with(23)

    def test_store_rejects_a_filesystem_root(self) -> None:
        filesystem_root = Path.cwd().anchor
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            evidence.FilesystemExternalEvidenceStore(filesystem_root)

    def test_reference_validates_observation_identity_and_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observation, identity = _observation(root)
            incomplete = ExecutionIdentity("RUN-EVIDENCE", 1)
            wrong_correlation = ExecutionIdentity(
                "RUN-EVIDENCE", 1, "TASK-001", 1, "OTHER-OPERATION"
            )
            cases = (
                (object(), identity, EffectOperation.PREPARE_ATTEMPT, TypeError),
                (observation, object(), EffectOperation.PREPARE_ATTEMPT, ValueError),
                (observation, incomplete, EffectOperation.PREPARE_ATTEMPT, ValueError),
                (
                    observation,
                    wrong_correlation,
                    EffectOperation.PREPARE_ATTEMPT,
                    ValueError,
                ),
                (observation, identity, "prepare_attempt", TypeError),
                (observation, identity, EffectOperation.TASK_EXECUTION, ValueError),
            )
            for candidate, candidate_identity, operation, exception in cases:
                with self.subTest(exception=exception), self.assertRaises(exception):
                    evidence.FilesystemExternalEvidenceStore._reference(
                        candidate,  # type: ignore[arg-type]
                        identity=candidate_identity,  # type: ignore[arg-type]
                        operation=operation,  # type: ignore[arg-type]
                    )

    def test_read_rejects_bad_digest_and_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = evidence.FilesystemExternalEvidenceStore(temporary)
            for digest in (None, "sha256:short"):
                with self.subTest(digest=digest), self.assertRaises(ValueError):
                    store.read(digest)  # type: ignore[arg-type]

            store.objects.mkdir(parents=True)
            digest = "sha256:" + "a" * 64
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError):
                with self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_read_failed",
                ):
                    store.read(digest)

    def test_verify_existing_accepts_exact_bytes_and_rejects_content_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = evidence.FilesystemExternalEvidenceStore(root / "evidence")
            observation, identity = _observation(root)
            reference = store.put(
                observation,
                identity=identity,
                operation=EffectOperation.PREPARE_ATTEMPT,
            )
            self.assertEqual(
                reference,
                store.verify_existing(
                    observation,
                    identity=identity,
                    operation=EffectOperation.PREPARE_ATTEMPT,
                ),
            )
            with (
                mock.patch.object(store, "read", return_value=b"other"),
                self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_content_mismatch",
                ),
            ):
                store.verify_existing(
                    observation,
                    identity=identity,
                    operation=EffectOperation.PREPARE_ATTEMPT,
                )

    def test_existing_publication_is_idempotent_and_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "object.json"
            target.write_bytes(b"canonical")
            evidence.FilesystemExternalEvidenceStore._publish(target, b"canonical")
            self.assertEqual(b"canonical", target.read_bytes())
            with self.assertRaisesRegex(
                evidence.ExternalEvidenceStoreError,
                "external_evidence_collision",
            ):
                evidence.FilesystemExternalEvidenceStore._publish(target, b"different")

    def test_publication_wraps_initial_read_and_open_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "object.json"
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError):
                with self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_read_failed",
                ):
                    evidence.FilesystemExternalEvidenceStore._publish(target, b"data")

            with mock.patch.object(evidence.os, "open", side_effect=PermissionError):
                with self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_write_failed",
                ):
                    evidence.FilesystemExternalEvidenceStore._publish(target, b"data")

    def test_publication_rejects_short_writes_and_closes_open_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "object.json"
            with (
                mock.patch.object(evidence.os, "open", return_value=31),
                mock.patch.object(evidence.os, "write", return_value=0),
                mock.patch.object(evidence.os, "close") as closed,
                self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_write_failed",
                ),
            ):
                evidence.FilesystemExternalEvidenceStore._publish(target, b"data")
            closed.assert_called_once_with(31)

    def test_link_race_accepts_same_bytes_and_rejects_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "object.json"
            target.write_bytes(b"data")
            real_read = Path.read_bytes

            def missing_then_existing(path: Path) -> bytes:
                if path == target and missing_then_existing.calls == 0:
                    missing_then_existing.calls += 1
                    raise FileNotFoundError
                return real_read(path)

            missing_then_existing.calls = 0
            with (
                mock.patch.object(Path, "read_bytes", missing_then_existing),
                mock.patch.object(evidence.os, "link", side_effect=FileExistsError),
            ):
                evidence.FilesystemExternalEvidenceStore._publish(target, b"data")

            missing_then_existing.calls = 0
            with (
                mock.patch.object(Path, "read_bytes", missing_then_existing),
                mock.patch.object(evidence.os, "link", side_effect=FileExistsError),
                self.assertRaisesRegex(
                    evidence.ExternalEvidenceStoreError,
                    "external_evidence_collision",
                ),
            ):
                evidence.FilesystemExternalEvidenceStore._publish(target, b"different")


if __name__ == "__main__":
    unittest.main()
