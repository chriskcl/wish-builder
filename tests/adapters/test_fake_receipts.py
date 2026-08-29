from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from tests.ports.test_conformance import FIXED_TIME, persisted_request, receipt_from
from wish_builder.adapters.fake import FakeEffectCrash, FakeTaskPort
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceiptValue,
    EffectStatus,
    JournalEventType,
)


def _concurrent_apply(root: str, queue: multiprocessing.Queue) -> None:
    request = persisted_request(
        AdapterKind.TASK,
        EffectOperation.TASK_EXECUTION,
        EffectObjectType.WORKER,
        JournalEventType.EFFECT_REQUESTED,
    )
    outcome = FakeTaskPort(root, clock=lambda: FIXED_TIME).apply(request)
    value = outcome.value
    queue.put(
        (
            type(value) is EffectReceiptValue,
            None
            if type(value) is not EffectReceiptValue
            else value.receipt.status.value,
            None
            if type(value) is not EffectReceiptValue
            else value.receipt.effect_hash,
        )
    )


class FakeReceiptRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_crash_after_effect_before_receipt_reconciles_applied_once(self) -> None:
        crashed = False

        def failpoint(point: str, path: Path) -> None:
            nonlocal crashed
            if point == "after_effect_before_receipt" and not crashed:
                crashed = True
                raise FakeEffectCrash(point)

        port = FakeTaskPort(
            self.root,
            clock=lambda: FIXED_TIME,
            failpoint=failpoint,
        )
        with self.assertRaisesRegex(FakeEffectCrash, "after_effect"):
            port.apply(self.request)
        self.assertEqual(1, len(tuple(port.effects.glob("*.json"))))
        self.assertEqual(0, len(tuple(port.receipts.glob("*.json"))))

        restarted = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        reconciled = receipt_from(
            restarted.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.APPLIED, reconciled.status)
        self.assertEqual(1, len(tuple(restarted.effects.glob("*.json"))))
        self.assertEqual(1, len(tuple(restarted.receipts.glob("*.json"))))
        self.assertEqual(reconciled, receipt_from(restarted.apply(self.request)))

    def test_missing_corrupt_and_orphan_receipts_have_explicit_semantics(self) -> None:
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        applied = receipt_from(port.apply(self.request))
        effect_path = next(port.effects.glob("*.json"))
        receipt_path = next(port.receipts.glob("*.json"))

        receipt_path.unlink()
        rebuilt = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(applied, rebuilt)
        self.assertTrue(receipt_path.is_file())

        receipt_path.write_bytes(b'{"status":"applied","status":"absent"}\n')
        corrupt = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.UNKNOWN, corrupt.status)
        self.assertTrue(corrupt.evidence)

        receipt_path.write_bytes(applied.canonical_json_bytes())
        effect_path.unlink()
        orphan = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.UNKNOWN, orphan.status)

        receipt_path.unlink()
        absent = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.ABSENT, absent.status)

    def test_oversized_receipt_is_bounded_and_unknown(self) -> None:
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        receipt_from(port.apply(self.request))
        receipt_path = next(port.receipts.glob("*.json"))
        receipt_path.write_bytes(b"x" * (1_048_576 + 2))
        unknown = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.UNKNOWN, unknown.status)
        self.assertGreater(unknown.evidence[0].byte_length, 1_048_576)
        self.assertLess(unknown.evidence[0].byte_length, 1_050_000)

    def test_before_effect_storage_fault_never_blindly_retries(self) -> None:
        def failpoint(point: str, path: Path) -> None:
            if point == "before_effect":
                raise OSError("disk unavailable")

        port = FakeTaskPort(
            self.root,
            clock=lambda: FIXED_TIME,
            failpoint=failpoint,
        )
        outcome = receipt_from(port.apply(self.request))
        self.assertEqual(EffectStatus.UNKNOWN, outcome.status)
        self.assertTrue(outcome.evidence)
        self.assertEqual(0, len(tuple(port.effects.glob("*.json"))))

    def test_two_processes_create_one_exact_effect(self) -> None:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [
            context.Process(target=_concurrent_apply, args=(str(self.root), queue))
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=5) for _ in processes]
        self.assertTrue(all(item[0] for item in results))
        self.assertEqual({EffectStatus.APPLIED.value}, {item[1] for item in results})
        self.assertEqual(1, len({item[2] for item in results}))
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        self.assertEqual(1, len(tuple(port.effects.glob("*.json"))))
        self.assertEqual(1, len(tuple(port.receipts.glob("*.json"))))


if __name__ == "__main__":
    unittest.main()
