from __future__ import annotations

from wish_builder.adapters import (
    ExternalEvidenceStoreError,
    FilesystemExternalEvidenceStore,
)

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.contracts.runtime import EffectOperation, EffectStatus, ExecutionIdentity
from wish_builder.services.ports import AttemptObservation, TrellisLifecycleState


class FilesystemExternalEvidenceStoreTests(unittest.TestCase):
    @staticmethod
    def observation(root: Path) -> tuple[AttemptObservation, ExecutionIdentity]:
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

    def test_publication_uses_a_short_same_directory_temporary_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ("long-runtime-root-" + "x" * 80)
            store = FilesystemExternalEvidenceStore(root)
            observation, identity = self.observation(root)
            opened: list[Path] = []
            real_open = os.open

            def recording_open(path, flags, mode=0o777):
                opened.append(Path(path))
                return real_open(path, flags, mode)

            with mock.patch(
                "wish_builder.adapters.external_evidence.os.open",
                side_effect=recording_open,
            ):
                reference = store.put(
                    observation,
                    identity=identity,
                    operation=EffectOperation.PREPARE_ATTEMPT,
                )

            temporary_path = next(
                path for path in opened if path.name.startswith(".tmp-")
            )
            object_path = store.objects / (
                reference.digest.removeprefix("sha256:") + ".json"
            )
            self.assertEqual(object_path.parent, temporary_path.parent)
            self.assertNotIn(object_path.name, temporary_path.name)
            self.assertLessEqual(len(temporary_path.name), 40)
            self.assertFalse(temporary_path.exists())
            self.assertEqual(observation.canonical_json_bytes(), store.read(reference.digest))

    def test_verify_existing_never_publishes_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            store = FilesystemExternalEvidenceStore(root)
            observation, identity = self.observation(root)

            with self.assertRaisesRegex(
                ExternalEvidenceStoreError,
                "external_evidence_read_failed",
            ):
                store.verify_existing(
                    observation,
                    identity=identity,
                    operation=EffectOperation.PREPARE_ATTEMPT,
                )

            self.assertFalse(store.objects.exists())

    def test_verify_existing_rejects_corrupt_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            store = FilesystemExternalEvidenceStore(root)
            observation, identity = self.observation(root)
            reference = store.put(
                observation,
                identity=identity,
                operation=EffectOperation.PREPARE_ATTEMPT,
            )
            object_path = store.objects / (
                reference.digest.removeprefix("sha256:") + ".json"
            )
            object_path.write_bytes(b"corrupt")

            with self.assertRaisesRegex(
                ExternalEvidenceStoreError,
                "external_evidence_digest_mismatch",
            ):
                store.verify_existing(
                    observation,
                    identity=identity,
                    operation=EffectOperation.PREPARE_ATTEMPT,
                )


if __name__ == "__main__":
    unittest.main()
