"""Deterministic targeted mutation gate for active-M1 safety invariants.

The gate deliberately keeps mutation generation small and reviewable.  It uses
``unittest`` as the test engine, applies a fixed registry of source mutations to
temporary package copies, and never edits the repository checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPORT_SCHEMA_VERSION = 2
MINIMUM_MUTATION_SCORE = 90.0
DEFAULT_TIMEOUT_SECONDS = 120.0
_RESULT_PREFIX = "WISH_BUILDER_MUTATION_TEST_RESULT="
_MUTATION_ID_RE = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\Z")

_TEST_BOOTSTRAP = r"""
import json
import sys
import unittest

mutant_root, repository_root, *test_ids = sys.argv[1:]
sys.path[:] = [mutant_root, repository_root] + [
    item
    for item in sys.path
    if item not in {"", mutant_root, repository_root}
]
loader = unittest.defaultTestLoader
suite = loader.loadTestsFromNames(test_ids)
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
payload = {
    "error_test_ids": sorted(case.id() for case, _trace in result.errors),
    "errors": len(result.errors),
    "failed_test_ids": sorted(case.id() for case, _trace in result.failures),
    "failures": len(result.failures),
    "load_errors": len(loader.errors),
    "skipped": len(result.skipped),
    "successful": result.wasSuccessful(),
    "tests_run": result.testsRun,
}
print("WISH_BUILDER_MUTATION_TEST_RESULT=" + json.dumps(
    payload, sort_keys=True, separators=(",", ":")
))
raise SystemExit(0 if result.wasSuccessful() and not loader.errors else 1)
"""


class MutationGateError(RuntimeError):
    """A configuration or execution fault that must close the gate."""


class MutationStatus(StrEnum):
    KILLED = "killed"
    SURVIVED = "survived"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MutationSpec:
    mutation_id: str
    invariant: str
    source_path: str
    before: str
    after: str
    test_ids: tuple[str, ...]
    safety_invariant: bool = True

    def __post_init__(self) -> None:
        if not _MUTATION_ID_RE.fullmatch(self.mutation_id):
            raise ValueError("mutation_id must be a stable uppercase identifier")
        if type(self.invariant) is not str or not self.invariant.strip():
            raise ValueError("invariant must be non-empty")
        path = PurePosixPath(self.source_path)
        if (
            type(self.source_path) is not str
            or not self.source_path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.source_path
            or path.suffix != ".py"
        ):
            raise ValueError("source_path must be a relative POSIX Python path")
        if type(self.before) is not str or not self.before:
            raise ValueError("before must be non-empty")
        if type(self.after) is not str or self.after == self.before:
            raise ValueError("after must differ from before")
        if (
            type(self.test_ids) is not tuple
            or not self.test_ids
            or not all(type(item) is str and item.strip() for item in self.test_ids)
            or len(set(self.test_ids)) != len(self.test_ids)
        ):
            raise ValueError("test_ids must be a non-empty unique tuple")
        if type(self.safety_invariant) is not bool:
            raise TypeError("safety_invariant must be a bool")


@dataclass(frozen=True, slots=True)
class TestRunSummary:
    successful: bool
    tests_run: int
    failures: int
    errors: int
    skipped: int
    failed_test_ids: tuple[str, ...] = ()
    error_test_ids: tuple[str, ...] = ()
    infrastructure_error: str | None = None

    def __post_init__(self) -> None:
        if type(self.successful) is not bool:
            raise TypeError("successful must be a bool")
        for name in ("tests_run", "failures", "errors", "skipped"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("failed_test_ids", "error_test_ids"):
            value = getattr(self, name)
            if type(value) is not tuple or not all(type(item) is str for item in value):
                raise TypeError(f"{name} must be a tuple of strings")
        if self.infrastructure_error is not None and (
            type(self.infrastructure_error) is not str or not self.infrastructure_error
        ):
            raise ValueError("infrastructure_error must be non-empty or null")

    def to_primitive(self) -> dict[str, object]:
        return {
            "error_test_ids": list(self.error_test_ids),
            "errors": self.errors,
            "failed_test_ids": list(self.failed_test_ids),
            "failures": self.failures,
            "infrastructure_error": self.infrastructure_error,
            "skipped": self.skipped,
            "successful": self.successful,
            "tests_run": self.tests_run,
        }


@dataclass(frozen=True, slots=True)
class MutationResult:
    mutation_id: str
    invariant: str
    safety_invariant: bool
    status: MutationStatus
    test_ids: tuple[str, ...]
    test_run: TestRunSummary
    source_path: str

    def __post_init__(self) -> None:
        if type(self.status) is not MutationStatus:
            raise TypeError("status must be a MutationStatus")

    def to_primitive(self) -> dict[str, object]:
        return {
            "invariant": self.invariant,
            "mutation_id": self.mutation_id,
            "safety_invariant": self.safety_invariant,
            "source_path": self.source_path,
            "status": self.status.value,
            "test_ids": list(self.test_ids),
            "test_run": self.test_run.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class MutationPolicy:
    passed: bool
    score: float
    killed: int
    survived: int
    errors: int
    surviving_safety_mutations: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_primitive(self) -> dict[str, object]:
        return {
            "errors": self.errors,
            "killed": self.killed,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "score": self.score,
            "survived": self.survived,
            "surviving_safety_mutations": list(self.surviving_safety_mutations),
        }


@dataclass(frozen=True, slots=True)
class MutationGateReport:
    baseline: TestRunSummary
    minimum_score: float
    results: tuple[MutationResult, ...]
    policy: MutationPolicy

    @property
    def passed(self) -> bool:
        return self.baseline.successful and self.policy.passed

    def to_primitive(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.to_primitive(),
            "minimum_score": self.minimum_score,
            "mutation_count": len(self.results),
            "policy": self.policy.to_primitive(),
            "results": [item.to_primitive() for item in self.results],
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "passed" if self.passed else "failed",
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_primitive(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


def _test_id(*parts: str) -> str:
    return "".join(parts)


DEFAULT_MUTATIONS = (
    MutationSpec(
        "SER-OBJECT-KEY-ORDER",
        "Canonical JSON object keys are sorted before hashing.",
        "wish_builder/contracts/serialization.py",
        "        sort_keys=True,\n",
        "        sort_keys=False,\n",
        (
            _test_id(
                "tests.packaging.test_ci_mutation_gate.MutationGateTests.",
                "test_canonical_serializer_sorts_arbitrary_mapping_keys",
            ),
        ),
    ),
    MutationSpec(
        "SER-SIGNED-INTEGER-RANGE",
        "Canonical contract integers remain within signed 64-bit bounds.",
        "wish_builder/contracts/serialization.py",
        "        if not MIN_CANONICAL_INTEGER <= value <= MAX_CANONICAL_INTEGER:\n",
        (
            "        if False and not MIN_CANONICAL_INTEGER <= value "
            "<= MAX_CANONICAL_INTEGER:\n"
        ),
        (
            _test_id(
                "tests.contracts.test_contract_edge_coverage.",
                "DiagnosticAndSerializationEdgeTests.",
                "test_canonical_serializer_rejects_unsafe_graphs_and_scalars",
            ),
        ),
    ),
    MutationSpec(
        "SER-NORMALIZED-KEY-COLLISION",
        "Canonical JSON rejects object keys that collide after normalization.",
        "wish_builder/contracts/serialization.py",
        "                if normalized_key in result:\n",
        "                if False and normalized_key in result:\n",
        (
            _test_id(
                "tests.contracts.test_contract_edge_coverage.",
                "DiagnosticAndSerializationEdgeTests.",
                "test_canonical_serializer_rejects_unsafe_graphs_and_scalars",
            ),
        ),
    ),
    MutationSpec(
        "GATE-CURRENT-WORKSPACE-BINDING",
        "A decision cannot be admitted after current workspace drift.",
        "wish_builder/kernel/gates.py",
        "    if current_workspace_hash != request.workspace_hash:\n",
        "    if False and current_workspace_hash != request.workspace_hash:\n",
        (
            _test_id(
                "tests.kernel.test_gates.GateDecisionTests.",
                "test_full_decision_mismatch_matrix_fails_closed",
            ),
            _test_id(
                "tests.kernel.test_gates.GateDecisionTests.",
                "test_exact_replay_is_rejected_after_workspace_drift",
            ),
        ),
    ),
    MutationSpec(
        "GATE-DIRECT-CLI-CHANNEL",
        "Only the direct CLI decision channel is admitted in active M1.",
        "wish_builder/kernel/gates.py",
        "    if submission.source_channel is not DecisionChannel.DIRECT_CLI:\n",
        (
            "    if False and submission.source_channel is not "
            "DecisionChannel.DIRECT_CLI:\n"
        ),
        (
            _test_id(
                "tests.kernel.test_gates.GateDecisionTests.",
                "test_chat_relay_is_denied_in_active_m1",
            ),
        ),
    ),
    MutationSpec(
        "GATE-HUMAN-ACTOR",
        "Only a human actor type can submit an active-M1 Gate decision.",
        "wish_builder/kernel/gates.py",
        "    if submission.actor.actor_type is not ActorType.HUMAN:\n",
        "    if False and submission.actor.actor_type is not ActorType.HUMAN:\n",
        (
            _test_id(
                "tests.kernel.test_gates.GateDecisionTests.",
                "test_only_human_actor_type_may_decide",
            ),
        ),
    ),
    MutationSpec(
        "GATE-CANDIDATE-HASH-BINDING",
        "A decision remains bound to the exact reviewed candidate hash.",
        "wish_builder/kernel/gates.py",
        "    if submitted_request.candidate_hash != request.candidate_hash:\n",
        (
            "    if False and submitted_request.candidate_hash "
            "!= request.candidate_hash:\n"
        ),
        (
            _test_id(
                "tests.kernel.test_gates.GateDecisionTests.",
                "test_full_decision_mismatch_matrix_fails_closed",
            ),
        ),
    ),
    MutationSpec(
        "STATE-TRANSITION-TABLE",
        "Only transitions present in the closed state table are admitted.",
        "wish_builder/kernel/state.py",
        (
            "    if (transition.from_state, transition.to_state) not in table.get(\n"
            "        transition.event_type, set()\n"
            "    ):\n"
        ),
        (
            "    if False and (transition.from_state, transition.to_state) "
            "not in table.get(\n"
            "        transition.event_type, set()\n"
            "    ):\n"
        ),
        (
            _test_id(
                "tests.kernel.test_state.StateKernelTests.",
                "test_illegal_transition_and_blocked_run_close_readiness",
            ),
        ),
    ),
    MutationSpec(
        "STATE-TERMINATED-ATTEMPT-RECLAIM",
        "A terminated attempt can be reserved again only through fenced reclaim.",
        "wish_builder/kernel/state.py",
        (
            "        (RuntimeState.PLANNED, RuntimeState.RESERVED),\n"
            "        (RuntimeState.TERMINATED, RuntimeState.RESERVED),\n"
        ),
        "        (RuntimeState.PLANNED, RuntimeState.RESERVED),\n",
        (
            _test_id(
                "tests.kernel.test_state.StateKernelTests.",
                "test_terminated_reservation_can_only_be_reclaimed_by_a_higher_epoch",
            ),
        ),
    ),
    MutationSpec(
        "STATE-CONTIGUOUS-SEQUENCE",
        "State transitions cannot skip a Journal sequence.",
        "wish_builder/kernel/state.py",
        (
            "    if transition.sequence != snapshot.last_sequence + 1:\n"
            "        return rejected(ApplyReason.SEQUENCE_GAP)\n"
        ),
        (
            "    if False and transition.sequence != snapshot.last_sequence + 1:\n"
            "        return rejected(ApplyReason.SEQUENCE_GAP)\n"
        ),
        (
            _test_id(
                "tests.kernel.test_state.StateKernelTests.",
                "test_exact_duplicate_conflict_stale_gap_hash_and_epoch_are_typed",
            ),
        ),
    ),
    MutationSpec(
        "STATE-PREVIOUS-HASH-BINDING",
        "State transitions remain bound to the previous Journal hash.",
        "wish_builder/kernel/state.py",
        (
            "    if transition.previous_event_hash != snapshot.last_event_hash:\n"
            "        return rejected(ApplyReason.HASH_CHAIN_MISMATCH)\n"
        ),
        (
            "    if False and transition.previous_event_hash "
            "!= snapshot.last_event_hash:\n"
            "        return rejected(ApplyReason.HASH_CHAIN_MISMATCH)\n"
        ),
        (
            _test_id(
                "tests.kernel.test_state.StateKernelTests.",
                "test_exact_duplicate_conflict_stale_gap_hash_and_epoch_are_typed",
            ),
        ),
    ),
    MutationSpec(
        "BACKEND-TRELLIS-COMPATIBILITY-BINDING",
        (
            "Backend evidence remains bound to the exact admitted Trellis "
            "graph and projection compatibility record."
        ),
        "wish_builder/services/backend_admission.py",
        "        == bundle.trellis_compatibility_digest\n",
        "        != bundle.trellis_compatibility_digest\n",
        (
            _test_id(
                "tests.services.test_backend_admission.BackendAdmissionTests.",
                "test_backend_binding_rejects_different_trellis_compatibility_digest",
            ),
        ),
    ),
    MutationSpec(
        "REPLAY-CANONICAL-FRAME",
        "Replay rejects complete Journal frames that are not canonical JSONL.",
        "wish_builder/services/replay.py",
        "                    if not canonical:\n",
        "                    if False and not canonical:\n",
        (
            _test_id(
                "tests.services.test_replay.StreamingReplayTests.",
                "test_complete_noncanonical_event_blocks",
            ),
        ),
    ),
    MutationSpec(
        "REPLAY-HASH-CHAIN",
        "Replay rejects an event whose previous hash breaks the Journal chain.",
        "wish_builder/services/replay.py",
        (
            "                    if event.previous_event_hash "
            "!= snapshot.last_event_hash:\n"
        ),
        (
            "                    if False and event.previous_event_hash "
            "!= snapshot.last_event_hash:\n"
        ),
        (
            _test_id(
                "tests.services.test_replay.StreamingReplayTests.",
                "test_incomplete_nonfinal_segment_and_hash_break_both_block",
            ),
        ),
    ),
    MutationSpec(
        "RECOVERY-VERIFIED-REPLAY",
        "Lease recovery cannot project authority from a blocked replay.",
        "wish_builder/services/recovery.py",
        "    if replay.status is ReplayStatus.BLOCKED:\n",
        "    if False and replay.status is ReplayStatus.BLOCKED:\n",
        (
            _test_id(
                "tests.services.test_recovery.CoordinatorLeaseRecoveryTests.",
                "test_corrupt_verified_replay_blocks_acquisition_without_writing",
            ),
        ),
    ),
    MutationSpec(
        "RECOVERY-OBSERVED-DISPATCH-CLEARS-PENDING",
        "An observed dispatch is removed from the pending reconciliation set.",
        "wish_builder/services/recovery.py",
        (
            "                        pending_dispatches.pop("
            "event.payload.receipt.identity, None)\n"
        ),
        (
            "                        pending_dispatches.get("
            "event.payload.receipt.identity)\n"
        ),
        (
            _test_id(
                "tests.processes.test_coordinator.ForegroundCoordinatorTests.",
                "test_verified_recovery_result_reconstructs_a_live_coordinator",
            ),
        ),
    ),
    MutationSpec(
        "LEASE-OWNER-IDENTITY-BINDING",
        "Lease admission binds the exact process and workspace owner identity.",
        "wish_builder/services/recovery.py",
        "            and lease.owner == self._owner\n",
        "            and True\n",
        (
            _test_id(
                "tests.services.test_recovery.CoordinatorLeaseRecoveryTests.",
                "test_pid_reuse_and_workspace_mismatch_cannot_renew_or_release",
            ),
        ),
    ),
    MutationSpec(
        "CODEX-CANCEL-PRESERVES-TERMINAL-RESULT",
        "Cancelling a completed Codex turn preserves its terminal state and result.",
        "wish_builder/adapters/providers/codex_app_server.py",
        (
            "                state=source.state,\n"
            "                effect_digest=_effect_digest(\"cancel_turn\", typed.command_hash),\n"
            "                attempt_id=command.attempt_id,\n"
            "                channel_id=command.channel_id,\n"
            "                message_id=source.message_id,\n"
            "                turn_id=command.turn_id,\n"
            "                result_digest=source.result_digest,\n"
            "                evidence=(\"codex_turn_completed_interrupted\",),\n"
            "            )\n"
            "            self._operation(command.operation_id)[\"observation\"] = (\n"
            "                observation.to_primitive()\n"
            "            )\n"
        ),
        (
            "                state=TurnState.DONE,\n"
            "                effect_digest=_effect_digest(\"cancel_turn\", typed.command_hash),\n"
            "                attempt_id=command.attempt_id,\n"
            "                channel_id=command.channel_id,\n"
            "                message_id=source.message_id,\n"
            "                turn_id=command.turn_id,\n"
            "                result_digest=_effect_digest(\"cancel_turn\", typed.command_hash),\n"
            "                evidence=(\"codex_turn_completed_interrupted\",),\n"
            "            )\n"
            "            self._operation(command.operation_id)[\"observation\"] = (\n"
            "                observation.to_primitive()\n"
            "            )\n"
        ),
        (
            _test_id(
                "tests.adapters.test_codex_app_server.CodexAppServerChannelTests.",
                "test_cancel_waits_for_terminal_interrupted_notification",
            ),
        ),
    ),
)


def validate_mutation_specs(
    repository_root: Path,
    specs: Sequence[MutationSpec],
) -> dict[str, str]:
    """Validate the registry and return each source file's normalized text."""

    root = repository_root.resolve(strict=True)
    if not specs:
        raise MutationGateError("mutation registry is empty")
    ids = [spec.mutation_id for spec in specs]
    if len(ids) != len(set(ids)):
        raise MutationGateError("mutation ids must be unique")

    sources: dict[str, str] = {}
    for spec in specs:
        if type(spec) is not MutationSpec:
            raise MutationGateError("mutation registry contains an invalid spec")
        source = root.joinpath(*PurePosixPath(spec.source_path).parts)
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise MutationGateError(
                f"{spec.mutation_id}: source file is unavailable"
            ) from exc
        if (
            not resolved.is_relative_to(root)
            or source.is_symlink()
            or not source.is_file()
        ):
            raise MutationGateError(
                f"{spec.mutation_id}: source must be a regular repository file"
            )
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise MutationGateError(
                f"{spec.mutation_id}: source is not readable UTF-8"
            ) from exc
        if text.count(spec.before) != 1:
            raise MutationGateError(
                f"{spec.mutation_id}: mutation anchor must occur exactly once"
            )
        sources[spec.source_path] = text
    return sources


def evaluate_policy(
    results: Sequence[MutationResult],
    *,
    minimum_score: float = MINIMUM_MUTATION_SCORE,
) -> MutationPolicy:
    """Apply the score and zero-surviving-safety-mutation policies."""

    if (
        type(minimum_score) not in {int, float}
        or isinstance(minimum_score, bool)
        or not 0.0 <= float(minimum_score) <= 100.0
    ):
        raise ValueError("minimum_score must be between 0 and 100")
    if not results:
        return MutationPolicy(
            False,
            0.0,
            0,
            0,
            0,
            (),
            ("no_mutations_executed",),
        )
    killed = sum(item.status is MutationStatus.KILLED for item in results)
    survived = sum(item.status is MutationStatus.SURVIVED for item in results)
    errors = sum(item.status is MutationStatus.ERROR for item in results)
    score = round(100.0 * killed / len(results), 2)
    safety_survivors = tuple(
        item.mutation_id
        for item in results
        if item.safety_invariant and item.status is MutationStatus.SURVIVED
    )
    reasons: list[str] = []
    if killed == 0:
        reasons.append("no_mutation_was_killed")
    if score < float(minimum_score):
        reasons.append("mutation_score_below_minimum")
    if safety_survivors:
        reasons.append("surviving_safety_mutation")
    if errors:
        reasons.append("mutation_execution_error")
    return MutationPolicy(
        not reasons,
        score,
        killed,
        survived,
        errors,
        safety_survivors,
        tuple(reasons),
    )


def run_mutation_gate(
    repository_root: Path,
    specs: Sequence[MutationSpec] = DEFAULT_MUTATIONS,
    *,
    python_executable: str | os.PathLike[str] = sys.executable,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    minimum_score: float = MINIMUM_MUTATION_SCORE,
) -> MutationGateReport:
    """Run baseline and targeted mutants against temporary source copies."""

    if (
        type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    root = repository_root.resolve(strict=True)
    registry = tuple(specs)
    source_text = validate_mutation_specs(root, registry)
    test_ids = _ordered_unique(
        test_id for spec in registry for test_id in spec.test_ids
    )

    with tempfile.TemporaryDirectory(prefix="wish-builder-mutation-baseline-") as raw:
        baseline_root = Path(raw)
        _copy_source_roots(root, baseline_root, registry)
        baseline = _run_tests(
            root,
            baseline_root,
            test_ids,
            str(python_executable),
            float(timeout_seconds),
        )
    if not baseline.successful:
        policy = MutationPolicy(
            False,
            0.0,
            0,
            0,
            0,
            (),
            ("baseline_tests_failed",),
        )
        return MutationGateReport(baseline, float(minimum_score), (), policy)

    results: list[MutationResult] = []
    for spec in registry:
        with tempfile.TemporaryDirectory(
            prefix=f"wish-builder-mutation-{spec.mutation_id.lower()}-"
        ) as raw:
            mutant_root = Path(raw)
            _copy_source_roots(root, mutant_root, registry)
            mutant = mutant_root.joinpath(*PurePosixPath(spec.source_path).parts)
            original = source_text[spec.source_path]
            changed = original.replace(spec.before, spec.after, 1)
            mutant.write_text(changed, encoding="utf-8", newline="\n")
            run = _run_tests(
                root,
                mutant_root,
                spec.test_ids,
                str(python_executable),
                float(timeout_seconds),
            )
        if run.infrastructure_error is not None:
            status = MutationStatus.ERROR
        elif run.successful:
            status = MutationStatus.SURVIVED
        elif run.errors or not run.failures:
            status = MutationStatus.ERROR
        else:
            status = MutationStatus.KILLED
        results.append(
            MutationResult(
                spec.mutation_id,
                spec.invariant,
                spec.safety_invariant,
                status,
                spec.test_ids,
                run,
                spec.source_path,
            )
        )

    policy = evaluate_policy(results, minimum_score=float(minimum_score))
    return MutationGateReport(
        baseline,
        float(minimum_score),
        tuple(results),
        policy,
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _copy_source_roots(
    repository_root: Path,
    destination: Path,
    specs: Sequence[MutationSpec],
) -> None:
    top_levels = _ordered_unique(
        PurePosixPath(spec.source_path).parts[0] for spec in specs
    )
    for name in top_levels:
        source = repository_root / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        else:
            shutil.copy2(source, target)


def _run_tests(
    repository_root: Path,
    mutant_root: Path,
    test_ids: Sequence[str],
    python_executable: str,
    timeout_seconds: float,
) -> TestRunSummary:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        python_executable,
        "-I",
        "-S",
        "-c",
        _TEST_BOOTSTRAP,
        str(mutant_root),
        str(repository_root),
        *test_ids,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=mutant_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = (
            "test_process_timeout"
            if isinstance(exc, subprocess.TimeoutExpired)
            else "test_process_start_failed"
        )
        return TestRunSummary(False, 0, 0, 0, 0, infrastructure_error=detail)

    payload_lines = [
        line.removeprefix(_RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(_RESULT_PREFIX)
    ]
    if len(payload_lines) != 1:
        return TestRunSummary(
            False,
            0,
            0,
            0,
            0,
            infrastructure_error="test_result_missing_or_ambiguous",
        )
    try:
        payload = json.loads(payload_lines[0])
        summary = TestRunSummary(
            payload["successful"],
            payload["tests_run"],
            payload["failures"],
            payload["errors"],
            payload["skipped"],
            tuple(payload["failed_test_ids"]),
            tuple(payload["error_test_ids"]),
        )
        load_errors = payload["load_errors"]
        if type(load_errors) is not int or load_errors < 0:
            raise ValueError("invalid load_errors")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return TestRunSummary(
            False,
            0,
            0,
            0,
            0,
            infrastructure_error="test_result_invalid",
        )
    if load_errors:
        return TestRunSummary(
            False,
            summary.tests_run,
            summary.failures,
            summary.errors,
            summary.skipped,
            summary.failed_test_ids,
            summary.error_test_ids,
            "test_loader_error",
        )
    if summary.tests_run <= 0:
        return TestRunSummary(
            False,
            summary.tests_run,
            summary.failures,
            summary.errors,
            summary.skipped,
            summary.failed_test_ids,
            summary.error_test_ids,
            "no_tests_executed",
        )
    expected_return_code = 0 if summary.successful else 1
    if completed.returncode != expected_return_code:
        return TestRunSummary(
            False,
            summary.tests_run,
            summary.failures,
            summary.errors,
            summary.skipped,
            summary.failed_test_ids,
            summary.error_test_ids,
            "test_process_exit_mismatch",
        )
    return summary


def _write_report(path: Path, report_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(report_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed active-M1 safety mutation registry in temporary "
            "package copies."
        )
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="also atomically write the canonical JSON report to this path",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-test-process timeout (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = run_mutation_gate(
            REPOSITORY_ROOT,
            timeout_seconds=arguments.timeout_seconds,
        )
        report_bytes = report.to_json_bytes()
        if arguments.json_output is not None:
            _write_report(arguments.json_output, report_bytes)
    except (MutationGateError, OSError, ValueError) as exc:
        error = {
            "error": str(exc) or type(exc).__name__,
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "error",
        }
        report_bytes = (
            json.dumps(
                error,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if arguments.json_output is not None:
            try:
                _write_report(arguments.json_output, report_bytes)
            except OSError:
                pass
        sys.stdout.buffer.write(report_bytes)
        return 2
    sys.stdout.buffer.write(report_bytes)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
