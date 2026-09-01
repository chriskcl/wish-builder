from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.ports.trellis_helpers import (
    BASE_COMMIT,
    FIXED_TIME,
    HASH_A,
    HASH_B,
    HEAD_COMMIT,
    prepared,
)
from wish_builder.adapters.trellis import lifecycle
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.services.ports import (
    AttemptObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    TrellisLifecycleState,
)


TRELLIS_TASK_ID = "TRELLIS-TASK-001"
ATTEMPT_ID = "ATTEMPT-001"
WORKTREE_ID = "WORKTREE-001"


def _bridge_metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "bridgeProtocolVersion": 1,
        "corePackageName": "@mindfoldhq/trellis-core",
        "corePackageVersion": "0.6.15",
        "coreArchiveSha256": "sha256:" + "1" * 64,
        "coreArchiveVerified": True,
        "corePackageTreeSha256": "sha256:" + "2" * 64,
        "operationSchemaVersion": None,
        "capabilitySchemaVersion": None,
        "operationKinds": [],
    }
    value.update(changes)
    return value


def _success_response(
    action: str,
    observation: dict[str, object],
    *,
    bridge: object | None = None,
) -> dict[str, object]:
    return {
        "protocolVersion": 1,
        "ok": True,
        "action": action,
        "observation": observation,
        "bridge": _bridge_metadata() if bridge is None else bridge,
    }


def _attempt_observation(
    operation_id: str,
    *,
    status: str = "applied",
    evidence: list[str] | None = None,
    worktree_path: str = "C:/wish-builder-test-worktree",
) -> dict[str, object]:
    applied = status == "applied"
    return {
        "operationId": operation_id,
        "status": status,
        "observedAt": FIXED_TIME,
        "lifecycleState": "prepared" if applied else status,
        "effectDigest": HASH_A if applied else None,
        "attemptId": ATTEMPT_ID if applied else None,
        "trellisTaskId": TRELLIS_TASK_ID if applied else None,
        "worktreeId": WORKTREE_ID if applied else None,
        "worktreePath": worktree_path if applied else None,
        "baseCommit": BASE_COMMIT if applied else None,
        "evidence": evidence or [],
    }


def _check_observation(
    operation_id: str,
    *,
    status: str = "applied",
    evidence: list[str] | None = None,
) -> dict[str, object]:
    applied = status == "applied"
    return {
        "operationId": operation_id,
        "status": status,
        "observedAt": FIXED_TIME,
        "effectDigest": HASH_A if applied else None,
        "attemptId": ATTEMPT_ID if applied else None,
        "passed": True if applied else None,
        "headCommit": HEAD_COMMIT if applied else None,
        "checkDigest": HASH_B if applied else None,
        "evidence": evidence or [],
    }


def _finish_observation(
    operation_id: str,
    *,
    status: str = "applied",
    evidence: list[str] | None = None,
) -> dict[str, object]:
    applied = status == "applied"
    return {
        "operationId": operation_id,
        "status": status,
        "observedAt": FIXED_TIME,
        "effectDigest": HASH_A if applied else None,
        "attemptId": ATTEMPT_ID if applied else None,
        "finished": True if applied else None,
        "deliveredCommit": HEAD_COMMIT if applied else None,
        "finishDigest": HASH_B if applied else None,
        "evidence": evidence or [],
    }


def _prepare_command(operation_id: str = "OP-PREPARE-001") -> PrepareAttempt:
    return PrepareAttempt(
        operation_id,
        "WISH-001",
        "PARENT-001",
        TRELLIS_TASK_ID,
        "TASK-001",
        1,
        "DISPATCH-001",
        HASH_A,
        HASH_B,
        BASE_COMMIT,
    )


def _check_command(operation_id: str = "OP-CHECK-001") -> CheckAttempt:
    return CheckAttempt(
        operation_id,
        ATTEMPT_ID,
        TRELLIS_TASK_ID,
        "TASK-001",
        HASH_A,
        HEAD_COMMIT,
    )


def _finish_command(operation_id: str = "OP-FINISH-001") -> FinishAttempt:
    return FinishAttempt(
        operation_id,
        ATTEMPT_ID,
        TRELLIS_TASK_ID,
        "TASK-001",
        HEAD_COMMIT,
        HASH_B,
    )


class TrellisLifecycleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.working = self.root / "working"
        self.worktree = self.root / "worktree"
        for directory in (self.checkout, self.working, self.worktree):
            directory.mkdir()
        self.node = self.root / "node.exe"
        self.bridge = self.root / "bridge.mjs"
        self.node.write_bytes(b"test executable placeholder")
        self.bridge.write_bytes(b"test bridge placeholder")

    def port(self, **changes: object) -> lifecycle.TrellisCoreLifecyclePort:
        options: dict[str, object] = {
            "bridge_command": (str(self.node), str(self.bridge)),
            "checkout_root": self.checkout,
            "working_directory": self.working,
            "trellis_task_id": TRELLIS_TASK_ID,
            "worktree_path": self.worktree,
            "worktree_id": WORKTREE_ID,
            "environment": {"WISH_BUILDER_TEST": "lifecycle"},
            "clock": lambda: FIXED_TIME,
        }
        options.update(changes)
        return lifecycle.TrellisCoreLifecyclePort(**options)  # type: ignore[arg-type]

    def test_apply_sends_exact_requests_and_decodes_typed_successes(self) -> None:
        prepare_effect = prepared(_prepare_command(), event_number=1)
        check_effect = prepared(_check_command(), event_number=2)
        finish_effect = prepared(_finish_command(), event_number=3)
        observations = {
            "lifecycle_prepare": _attempt_observation(
                prepare_effect.operation_id,
                worktree_path=str(self.worktree),
            ),
            "lifecycle_check": _check_observation(check_effect.operation_id),
            "lifecycle_finish": _finish_observation(finish_effect.operation_id),
        }
        requests: list[dict[str, object]] = []

        def invoke(**kwargs: object) -> tuple[dict[str, object], int]:
            request = json.loads(kwargs["raw"])
            requests.append(request)
            action = request["action"]
            return _success_response(action, observations[action]), 0

        with mock.patch.object(lifecycle, "_invoke_bridge", side_effect=invoke):
            attempt = self.port().prepare_attempt(prepare_effect)
            checked = self.port().check_attempt(check_effect)
            finished = self.port().finish_attempt(finish_effect)

        self.assertIsInstance(attempt, AttemptObservation)
        self.assertIs(attempt.status, EffectStatus.APPLIED)
        self.assertIs(attempt.lifecycle_state, TrellisLifecycleState.PREPARED)
        self.assertIsInstance(checked, CheckObservation)
        self.assertTrue(checked.passed)
        self.assertIsInstance(finished, FinishObservation)
        self.assertTrue(finished.finished)
        for request, action, kind, effect in (
            (requests[0], "lifecycle_prepare", "prepare_attempt", prepare_effect),
            (requests[1], "lifecycle_check", "check_attempt", check_effect),
            (requests[2], "lifecycle_finish", "finish_attempt", finish_effect),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    {
                        "protocolVersion": 1,
                        "action": action,
                        "checkoutRoot": str(self.checkout),
                        "operationKind": kind,
                        "commandHash": effect.command_hash,
                        "command": effect.command.to_primitive(),
                        "worktreePath": (
                            str(self.worktree) if kind == "prepare_attempt" else None
                        ),
                        "worktreeId": WORKTREE_ID if kind == "prepare_attempt" else None,
                    },
                    request,
                )

    def test_transport_envelope_bridge_and_observation_failures_preserve_reason(self) -> None:
        effect = prepared(_prepare_command())
        malformed = _attempt_observation(effect.operation_id)
        malformed.pop("evidence")
        cases = (
            (
                lifecycle._BridgeTransportError("timeout"),
                "lifecycle_timeout",
            ),
            (
                (
                    {
                        "protocolVersion": 1,
                        "ok": False,
                        "action": "lifecycle_prepare",
                        "error": {
                            "code": "failed",
                            "message": "bridge failed",
                            "details": {"reason": "task_status_mismatch"},
                        },
                    },
                    1,
                ),
                "task_status_mismatch",
            ),
            (
                (
                    _success_response(
                        "lifecycle_prepare",
                        _attempt_observation(effect.operation_id),
                        bridge=_bridge_metadata(corePackageVersion="0.6.14"),
                    ),
                    0,
                ),
                "lifecycle_bridge_identity",
            ),
            (
                (_success_response("lifecycle_prepare", malformed), 0),
                "lifecycle_response_invalid",
            ),
        )
        for bridge_result, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                patcher = (
                    mock.patch.object(
                        lifecycle, "_invoke_bridge", side_effect=bridge_result
                    )
                    if isinstance(bridge_result, Exception)
                    else mock.patch.object(
                        lifecycle, "_invoke_bridge", return_value=bridge_result
                    )
                )
                with patcher:
                    observed = self.port().prepare_attempt(effect)
                self.assertIs(observed.status, EffectStatus.UNKNOWN)
                self.assertIs(observed.lifecycle_state, TrellisLifecycleState.UNKNOWN)
                self.assertEqual((expected_reason,), observed.evidence)

    def test_inspect_sends_exact_requests_and_decodes_applied_absent_and_hash_mismatch(self) -> None:
        responses = {
            "prepare_attempt": _attempt_observation(
                "OP-INSPECT-PREPARE",
                worktree_path=str(self.worktree),
            ),
            "check_attempt": _check_observation(
                "OP-INSPECT-CHECK", status="absent"
            ),
            "finish_attempt": _finish_observation(
                "OP-INSPECT-FINISH",
                status="unknown",
                evidence=["request_payload_hash_mismatch"],
            ),
        }
        requests: list[dict[str, object]] = []

        def invoke(**kwargs: object) -> tuple[dict[str, object], int]:
            request = json.loads(kwargs["raw"])
            requests.append(request)
            return (
                _success_response(
                    "lifecycle_inspect",
                    responses[request["operationKind"]],
                ),
                0,
            )

        with mock.patch.object(lifecycle, "_invoke_bridge", side_effect=invoke):
            attempt = self.port().inspect_attempt(
                "OP-INSPECT-PREPARE", expected_request_payload_hash=HASH_A
            )
            checked = self.port().inspect_check(
                "OP-INSPECT-CHECK", expected_request_payload_hash=HASH_A
            )
            finished = self.port().inspect_finish(
                "OP-INSPECT-FINISH", expected_request_payload_hash=HASH_A
            )

        self.assertIs(attempt.status, EffectStatus.APPLIED)
        self.assertIs(checked.status, EffectStatus.ABSENT)
        self.assertIs(finished.status, EffectStatus.UNKNOWN)
        self.assertEqual(("request_payload_hash_mismatch",), finished.evidence)
        for request, kind, operation_id in zip(
            requests,
            ("prepare_attempt", "check_attempt", "finish_attempt"),
            ("OP-INSPECT-PREPARE", "OP-INSPECT-CHECK", "OP-INSPECT-FINISH"),
            strict=True,
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    {
                        "protocolVersion": 1,
                        "action": "lifecycle_inspect",
                        "checkoutRoot": str(self.checkout),
                        "trellisTaskId": TRELLIS_TASK_ID,
                        "operationKind": kind,
                        "operationId": operation_id,
                        "expectedRequestPayloadHash": HASH_A,
                    },
                    request,
                )

    def test_input_and_task_identity_guards_stop_before_transport(self) -> None:
        port = self.port()
        with mock.patch.object(lifecycle, "_invoke_bridge") as invoke:
            for inspect in (
                port.inspect_attempt,
                port.inspect_check,
                port.inspect_finish,
            ):
                for operation_id in (None, 1, ""):
                    with self.subTest(inspect=inspect.__name__, operation_id=operation_id):
                        with self.assertRaises(TypeError):
                            inspect(operation_id)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    inspect("OP-INSPECT", expected_request_payload_hash="not-a-digest")

            wrong_task = PrepareAttempt(
                "OP-WRONG-TASK",
                "WISH-001",
                "PARENT-001",
                "OTHER-TASK",
                "TASK-001",
                1,
                "DISPATCH-001",
                HASH_A,
                HASH_B,
                BASE_COMMIT,
            )
            observed = port.prepare_attempt(prepared(wrong_task))
            invoke.assert_not_called()

        self.assertIs(observed.status, EffectStatus.UNKNOWN)
        self.assertEqual(("trellis_task_identity_mismatch",), observed.evidence)

    def test_constructor_rejects_invalid_lifecycle_boundaries(self) -> None:
        invalid = (
            ({"trellis_task_id": ""}, ValueError),
            ({"worktree_path": "relative/path"}, ValueError),
            ({"worktree_id": ""}, ValueError),
            ({"timeout_seconds": True}, ValueError),
            ({"clock": object()}, TypeError),
        )
        for changes, error_type in invalid:
            with self.subTest(changes=changes), self.assertRaises(error_type):
                self.port(**changes)

    def test_check_finish_and_effect_type_guards_fail_before_transport(self) -> None:
        port = self.port()
        wrong_check = replace(_check_command(), trellis_task_id="OTHER-TASK")
        wrong_finish = replace(_finish_command(), trellis_task_id="OTHER-TASK")
        with mock.patch.object(lifecycle, "_invoke_bridge") as invoke:
            checked = port.check_attempt(prepared(wrong_check))
            finished = port.finish_attempt(prepared(wrong_finish))
            with self.assertRaises(TypeError):
                port.prepare_attempt(object())  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                port.prepare_attempt(prepared(_check_command()))  # type: ignore[arg-type]
            invoke.assert_not_called()

        self.assertIs(checked.status, EffectStatus.UNKNOWN)
        self.assertIs(finished.status, EffectStatus.UNKNOWN)
        self.assertEqual(("trellis_task_identity_mismatch",), checked.evidence)
        self.assertEqual(("trellis_task_identity_mismatch",), finished.evidence)

    def test_inspect_and_bridge_schema_failures_remain_typed_unknowns(self) -> None:
        with mock.patch.object(
            lifecycle,
            "_invoke_bridge",
            side_effect=lifecycle._BridgeTransportError("timeout"),
        ):
            inspected = self.port().inspect_attempt("OP-INSPECT-FAILURE")
        self.assertIs(inspected.status, EffectStatus.UNKNOWN)
        self.assertEqual(("lifecycle_timeout",), inspected.evidence)

        effect = prepared(_prepare_command())
        invalid_responses = (
            (_success_response("lifecycle_prepare", _attempt_observation(effect.operation_id)), 1),
            (_success_response("wrong_action", _attempt_observation(effect.operation_id)), 0),
            (
                _success_response(
                    "lifecycle_prepare",
                    _attempt_observation(effect.operation_id),
                    bridge=object(),
                ),
                0,
            ),
            (
                {
                    "protocolVersion": 1,
                    "ok": False,
                    "action": "lifecycle_prepare",
                    "error": {"code": "failed"},
                },
                0,
            ),
            (
                {
                    "protocolVersion": 1,
                    "ok": False,
                    "action": "lifecycle_prepare",
                    "error": "failed",
                },
                1,
            ),
        )
        for response in invalid_responses:
            with self.subTest(response=response), mock.patch.object(
                lifecycle,
                "_invoke_bridge",
                return_value=response,
            ):
                observed = self.port().prepare_attempt(effect)
            self.assertIs(observed.status, EffectStatus.UNKNOWN)

        self.assertEqual("lifecycle_unavailable", lifecycle._reason(None))


if __name__ == "__main__":
    unittest.main()
