from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from unittest import mock

from tests.processes.test_acceptance import command, promotion, task
from wish_builder.contracts.manifest_v2 import NetworkPolicy
from wish_builder.contracts.runtime import (
    ExecutionIdentity,
    OutcomeKind,
    RuntimeReasonCode,
)
from wish_builder.processes.acceptance import (
    ProcessAcceptancePort,
    _path_within,
    _timestamp,
)
from wish_builder.processes.containment import ContainmentCapability, ContainmentStatus
from wish_builder.processes.runner import (
    CapturedStream,
    DirectoryIdentity,
    ExecutableIdentity,
    ProcessFailure,
    ProcessOutcome,
    ProcessOutcomeStatus,
    StreamName,
)


HASH = "a" * 64
T = TypeVar("T")


def identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        "RUN-ACCEPTANCE-CLOSURE",
        1,
        "TASK-001",
        1,
        "DISPATCH-001",
    )


def executable() -> ExecutableIdentity:
    return ExecutableIdentity(
        "python",
        "python",
        1,
        2,
        1,
        2,
        0o755,
        10,
        20,
        HASH,
    )


def directory() -> DirectoryIdentity:
    return DirectoryIdentity("repo", "repo", 1, 2, 1, 2, 0o755)


def stream(name: StreamName, *, data: bytes = b"", complete: bool = True) -> CapturedStream:
    return CapturedStream(name, data, data, len(data), 0, False, False, complete)


def successful_outcome(*, result: bytes = b"") -> ProcessOutcome:
    return ProcessOutcome(
        ProcessOutcomeStatus.SUCCESS,
        OutcomeKind.SUCCESS,
        None,
        None,
        None,
        executable(),
        directory(),
        "sha256:" + HASH,
        (),
        ContainmentCapability(ContainmentStatus.PROVEN, "test"),
        None,
        0,
        stream(StreamName.STDOUT),
        stream(StreamName.STDERR),
        stream(StreamName.RESULT, data=result),
        0.01,
        False,
        True,
    )


def altered(value: T, **changes: object) -> T:
    clone = copy.copy(value)
    valid_names = {field.name for field in fields(type(value))}
    if not set(changes) <= valid_names:
        raise AssertionError("invalid dataclass field")
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


class _Runner:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def run(self, request: object) -> object:
        del request
        return self._outcome


class ProcessAcceptanceBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)

    def port(self, outcome: object | None = None, **kwargs: object) -> ProcessAcceptancePort:
        values: dict[str, object] = {
            "identity": identity(),
            "executable_profiles": {"python": sys.executable},
            "clock": lambda: "2026-08-19T00:00:20Z",
        }
        if outcome is not None:
            values["runner_factory"] = lambda spec: _Runner(outcome)
        values.update(kwargs)
        return ProcessAcceptancePort(**values)

    def test_timestamp_and_path_helpers_cover_both_contract_outcomes(self) -> None:
        self.assertEqual(
            "2026-08-19T01:02:03Z",
            _timestamp(lambda: datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC)),
        )
        for value in (object(), datetime(2026, 8, 19, 1, 2, 3)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _timestamp(lambda value=value: value)
        self.assertTrue(_path_within(self.repository, self.repository))
        self.assertFalse(_path_within(self.repository, self.repository / "child"))

    def test_constructor_rejects_invalid_collaborators_and_identity_maps(self) -> None:
        invalid_calls = (
            ({"identity": object()}, TypeError),
            ({"identities": {"": identity()}}, ValueError),
            ({"identities": {1: identity()}}, ValueError),
            ({"identities": {"TASK-001": object()}}, TypeError),
            ({"identity": ExecutionIdentity("RUN-ONE", 1)}, ValueError),
            ({"runner_factory": object()}, TypeError),
            ({"clock": object()}, TypeError),
        )
        for kwargs, error in invalid_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                ProcessAcceptancePort(**kwargs)

    def test_verify_rejects_invalid_argument_types(self) -> None:
        with self.assertRaises(TypeError):
            self.port().verify(object(), self.repository, promotion())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.port().verify(
                task(command("")), self.repository, object()  # type: ignore[arg-type]
            )

    def test_every_incomplete_execution_identity_is_rejected(self) -> None:
        variants = (
            {"run_id": None},
            {"task_id": "TASK-OTHER"},
            {"attempt": None},
            {"coordinator_epoch": 0},
        )
        for changes in variants:
            malformed = identity()
            for name, value in changes.items():
                object.__setattr__(malformed, name, value)
            with self.subTest(changes=changes):
                result = ProcessAcceptancePort(
                    identities={"TASK-001": malformed},
                    executable_profiles={"python": sys.executable},
                ).verify(task(command("")), self.repository, promotion())
                self.assertEqual(RuntimeReasonCode.INVARIANT_VIOLATION, result.reason_code)

    def test_repository_must_exist_and_be_a_directory(self) -> None:
        missing = self.repository / "missing"
        result = self.port().verify(task(command("")), missing, promotion())
        self.assertEqual(RuntimeReasonCode.GIT_STATE_CONFLICT, result.reason_code)

        regular_file = self.repository / "file.txt"
        regular_file.write_text("not a directory", encoding="utf-8")
        result = self.port().verify(task(command("")), regular_file, promotion())
        self.assertEqual(RuntimeReasonCode.GIT_STATE_CONFLICT, result.reason_code)

    def test_command_policy_and_working_directory_fail_closed(self) -> None:
        allowed = replace(command(""), network_policy=NetworkPolicy.ALLOWED)
        result = self.port().verify(task(allowed), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.POLICY_DENIED, result.reason_code)

        missing_cwd = replace(command(""), working_directory="missing")
        result = self.port().verify(task(missing_cwd), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, result.reason_code)

        child = self.repository / "child"
        child.mkdir()
        escaped = replace(command(""), working_directory="child")
        with mock.patch(
            "wish_builder.processes.acceptance._path_within", return_value=False
        ):
            result = self.port().verify(task(escaped), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.GIT_STATE_CONFLICT, result.reason_code)

    def test_environment_and_request_configuration_errors_fail_closed(self) -> None:
        spec = replace(command(""), environment_allowlist=("BROKEN_VALUE",))
        with mock.patch(
            "wish_builder.processes.acceptance.os.environ",
            {"BROKEN_VALUE": "value\x00tail"},
        ):
            result = self.port().verify(task(spec), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, result.reason_code)

        with mock.patch(
            "wish_builder.processes.acceptance.ProcessRequest.create",
            side_effect=OSError("identity changed"),
        ):
            result = self.port().verify(task(command("")), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, result.reason_code)

    def test_executable_lookup_covers_path_search_and_unavailable_profile(self) -> None:
        no_profile = ProcessAcceptancePort(
            identity=identity(),
            runner_factory=lambda spec: _Runner(successful_outcome()),
        )
        with mock.patch(
            "wish_builder.processes.acceptance.shutil.which", return_value=sys.executable
        ):
            result = no_profile.verify(task(command("")), self.repository, promotion())
        self.assertTrue(result.accepted)

        with mock.patch("wish_builder.processes.acceptance.shutil.which", return_value=None):
            result = no_profile.verify(task(command("")), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, result.reason_code)

    def test_result_channel_requires_a_json_object(self) -> None:
        result = self.port(successful_outcome(result=b"[]")).verify(
            task(command("")), self.repository, promotion()
        )
        self.assertEqual(RuntimeReasonCode.EVIDENCE_INVALID, result.reason_code)

    def test_invalid_runner_result_is_unknown(self) -> None:
        result = self.port(object()).verify(task(command("")), self.repository, promotion())
        self.assertEqual(RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN, result.reason_code)

    def test_each_process_outcome_admission_guard_fails_closed(self) -> None:
        accepted = successful_outcome()
        incomplete_stdout = altered(
            accepted.stdout,
            complete=False,
        )
        incomplete_stderr = altered(
            accepted.stderr,
            complete=False,
        )
        incomplete_result = altered(
            accepted.result,
            complete=False,
        )
        variants = (
            altered(
                accepted,
                status=ProcessOutcomeStatus.EXIT_FAILURE,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.CHECK_FAILED,
            ),
            altered(accepted, kind=OutcomeKind.TERMINAL),
            altered(accepted, reason_code=RuntimeReasonCode.EXTERNAL_TIMEOUT),
            altered(accepted, failure=ProcessFailure.NONZERO_EXIT),
            altered(accepted, exit_code=3),
            altered(accepted, termination_attempted=True),
            altered(accepted, termination_proven=False),
            altered(accepted, stdout=incomplete_stdout),
            altered(accepted, stderr=incomplete_stderr),
            altered(accepted, result=incomplete_result),
        )
        self.assertIsNone(ProcessAcceptancePort._admission_failure(accepted))
        for outcome in variants:
            with self.subTest(outcome=outcome):
                self.assertIsNotNone(ProcessAcceptancePort._admission_failure(outcome))


if __name__ == "__main__":
    unittest.main()
