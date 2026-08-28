from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import stat
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters import git_worktree as git_boundary
from wish_builder.adapters.git_identity import FilesystemIdentity
from wish_builder.adapters.git_worktree import (
    AttemptEffectDisposition,
    AttemptResultManifest,
    AttemptWorktree,
    AttemptWorktreeCommand,
    ChangedPath,
    GitBoundaryError,
    GitTreeEntry,
    GitWorktreeAdapter,
    RepositoryEffect,
    ResultValidation,
    StageResultCommand,
    StagedResult,
)
from wish_builder.adapters.git_identity import WorkspaceIdentity
from wish_builder.contracts import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEventType,
    PathCaseMode,
    RuntimeReasonCode,
    canonical_json_bytes,
)
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupCommand,
    CleanupInspection,
    CleanupPlan,
)
from wish_builder.services.ports import PreparedEffect
from wish_builder.services.promotion import PromotionCommand, PromotionPlan


OID = "0" * 40
SHA256 = "sha256:" + "1" * 64
FIXED_TIME = "2026-08-20T00:00:00Z"


def attempt_identity(correlation_id: str = "OP-COVERAGE") -> ExecutionIdentity:
    return ExecutionIdentity(
        "WISH-COVERAGE",
        1,
        "TASK-COVERAGE",
        1,
        correlation_id,
    )


def filesystem_identity() -> FilesystemIdentity:
    return FilesystemIdentity(
        lexical_path="attempt",
        canonical_path="attempt",
        link_device=1,
        link_inode=2,
        target_device=1,
        target_inode=2,
        is_link_or_reparse_point=False,
        access_control_hash=SHA256,
    )


def tree_entry(path: str = "src/a.txt") -> GitTreeEntry:
    return GitTreeEntry(path, "100644", OID, 1)


def result_manifest(
    *,
    changed_paths: tuple[ChangedPath, ...] | None = None,
) -> AttemptResultManifest:
    changes = changed_paths or (
        ChangedPath("src/a.txt", None, tree_entry()),
    )
    return AttemptResultManifest(
        schema_version=1,
        identity=attempt_identity(),
        local_repository_id=SHA256,
        attempt_hash=SHA256,
        base_commit_sha=OID,
        base_tree_sha=OID,
        result_commit_sha=OID,
        result_tree_sha=OID,
        path_case_mode=PathCaseMode.SENSITIVE,
        changed_paths=changes,
        total_blob_bytes=1,
        portable_profile_hash=SHA256,
    )


def attempt_worktree() -> AttemptWorktree:
    root = filesystem_identity()
    return AttemptWorktree(
        identity=attempt_identity(),
        path="attempt",
        external_object_id="worktree-1",
        local_repository_id=SHA256,
        target_workspace_hash=SHA256,
        worktree_root=root,
        git_dir=root,
        base_commit_sha=OID,
        base_tree_sha=OID,
        owned_paths=("src/**",),
        allowed_auxiliary_paths=(),
        protected_paths=(".github/**",),
        path_case_mode=PathCaseMode.SENSITIVE,
    )


def attempt_command() -> AttemptWorktreeCommand:
    return AttemptWorktreeCommand(
        operation_id="OP-COVERAGE",
        identity=attempt_identity(),
        local_repository_id=SHA256,
        target_workspace_hash=SHA256,
        base_commit_sha=OID,
        base_tree_sha=OID,
        directory_name="attempt-coverage",
        owned_paths=("src/**",),
        allowed_auxiliary_paths=(),
        protected_paths=(".github/**",),
        path_case_mode=PathCaseMode.SENSITIVE,
    )


def adapter_stub() -> GitWorktreeAdapter:
    adapter = object.__new__(GitWorktreeAdapter)
    adapter.repository = Path(".")
    adapter.attempts_root = Path(".")
    adapter._expected_workspace = SimpleNamespace(
        base_commit_sha=OID,
        common_dir=SimpleNamespace(canonical_path="."),
        local_repository_id=SHA256,
        local_worktree_id=SHA256,
        scopes=(".github/**", "src/**"),
        target_full_ref="refs/heads/main",
        workspace_hash=SHA256,
    )
    adapter.attempts_root_identity = filesystem_identity()
    adapter._clock = lambda: FIXED_TIME
    adapter._failpoint = None
    adapter._object_id_length = 40
    return adapter


def boundary_effect(
    command: object,
    *,
    event_type: JournalEventType,
    operation: EffectOperation,
    object_type: EffectObjectType,
    target_hash: str = SHA256,
    identity: ExecutionIdentity | None = None,
) -> PreparedEffect:
    if identity is None:
        source = getattr(command, "identity", command)
        identity = ExecutionIdentity(
            source.run_id,
            source.coordinator_epoch,
            source.task_id,
            source.attempt,
            command.operation_id,
        )
    request = SimpleNamespace(
        event=SimpleNamespace(event_type=event_type),
        identity=identity,
        payload=SimpleNamespace(
            operation=operation,
            adapter=AdapterKind.GIT,
            object_type=object_type,
            normalized_target_hash=target_hash,
        ),
    )
    effect = object.__new__(PreparedEffect)
    primitive = command.to_primitive()
    object.__setattr__(effect, "request", request)
    object.__setattr__(effect, "command", command)
    object.__setattr__(effect, "_operation_id", command.operation_id)
    object.__setattr__(effect, "_command_bytes", canonical_json_bytes(primitive))
    object.__setattr__(effect, "_command_hash", SHA256)
    return effect


def evidence(identity: ExecutionIdentity) -> EvidenceRef:
    return EvidenceRef(
        1,
        SHA256,
        1,
        EvidenceType.GIT,
        EvidenceProducer(identity, external_object_id="evidence-1"),
        FIXED_TIME,
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        EvidenceRole.REQUIRED,
        SHA256,
    )


def staged_result() -> StagedResult:
    return StagedResult(
        result_manifest(),
        "refs/wish-builder/staged/test",
        SHA256,
    )


def promotion_plan(*, acceptance_bound: bool = True) -> PromotionPlan:
    source = staged_result()
    acceptance = (evidence(source.manifest.identity),) if acceptance_bound else ()
    command = PromotionCommand(
        operation_id="OP-PROMOTION",
        run_id=source.run_id,
        coordinator_epoch=1,
        task_id=source.task_id,
        attempt=source.attempt,
        topological_position=1,
        local_repository_id=source.local_repository_id,
        target_workspace_hash=SHA256,
        expected_target_sha=OID,
        staged_ref=source.staged_ref,
        result_manifest_hash=source.result_manifest_hash,
        source_commit_sha=source.result_commit_sha,
        source_tree_sha=source.result_tree_sha,
        candidate_commit_sha="2" * 40,
        candidate_tree_sha="3" * 40,
        acceptance_evidence=acceptance,
    )
    return PromotionPlan(command, source)


def cleanup_plan() -> CleanupPlan:
    attempt = attempt_worktree()
    proof = evidence(attempt.identity)
    candidate = CleanupCandidate(
        attempt,
        OID,
        (proof,),
        reconciliation_complete=True,
        process_tree_terminated=True,
        outcome_known=True,
    )
    inspection = CleanupInspection(
        exists=True,
        identity_ok=True,
        clean=True,
        observed_head_sha=OID,
        target_workspace_hash=SHA256,
        state_hash=SHA256,
    )
    command = CleanupCommand(
        operation_id="OP-CLEANUP",
        run_id=attempt.run_id,
        coordinator_epoch=1,
        task_id=attempt.task_id,
        attempt=attempt.attempt_number,
        local_repository_id=attempt.local_repository_id,
        target_workspace_hash=SHA256,
        external_object_id=attempt.external_object_id,
        expected_head_sha=OID,
        observed_state_hash=inspection.state_hash,
        evidence_digests=(proof.digest,),
        remove_allowed=True,
    )
    return CleanupPlan(command, candidate, inspection)


class GitWorktreeBoundaryCoverageTests(unittest.TestCase):
    def test_portable_paths_reject_escapes_aliases_and_resource_exhaustion(self) -> None:
        invalid = (
            (None, "git_path_invalid"),
            ("", "git_path_invalid"),
            ("e" + chr(0x301), "git_path_not_nfc"),
            (chr(0xD800), "git_path_invalid_utf8"),
            ("a" * 1025, "git_path_limit"),
            ("/root", "git_path_escape"),
            ("src\\outside", "git_path_escape"),
            ("src\x00outside", "git_path_escape"),
            ("src//file", "git_path_escape"),
            ("src/../file", "git_path_escape"),
            ("a" * 256, "git_component_limit"),
            ("src/name:stream", "git_path_not_portable"),
            ("src/trailing.", "git_path_not_portable"),
            ("src/control" + chr(0x1F), "git_path_not_portable"),
            ("src/file?", "git_path_not_portable"),
            (".git", "git_path_not_portable"),
            ("COM1.txt", "git_path_not_portable"),
        )
        for path, code in invalid:
            with self.subTest(path=repr(path)), self.assertRaises(
                GitBoundaryError
            ) as raised:
                git_boundary._validate_portable_path(path)  # type: ignore[arg-type]
            self.assertEqual(code, raised.exception.code)
        for path in ("a" * 1025, "a" * 256):
            with self.subTest(reason=path[:8]), self.assertRaises(
                GitBoundaryError
            ) as raised:
                git_boundary._validate_portable_path(path)
            self.assertEqual(RuntimeReasonCode.LIMIT_EXCEEDED, raised.exception.reason_code)

    def test_path_patterns_reject_noncanonical_and_empty_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            git_boundary._nonempty(None, "value")
        with self.assertRaisesRegex(ValueError, "full sha256"):
            git_boundary._sha256("not-a-digest", "digest")
        with self.assertRaises(TypeError):
            git_boundary._canonical_pattern(1)
        for pattern in (
            "",
            "e" + chr(0x301),
            "src\\file",
            "/src",
            "src/",
            "src/*",
        ):
            with self.subTest(pattern=repr(pattern)), self.assertRaises(ValueError):
                git_boundary._canonical_pattern(pattern)
        with self.assertRaisesRegex(TypeError, "iterable path patterns"):
            git_boundary._patterns(None, "owned_paths", nonempty=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            git_boundary._patterns((), "owned_paths", nonempty=True)

    def test_tree_entries_and_changed_paths_fail_closed(self) -> None:
        for arguments in (
            ("src/a.txt", "120000", OID, 1),
            ("src/a.txt", "100644", "F" * 40, 1),
            ("src/a.txt", "100644", OID, True),
            ("src/a.txt", "100644", OID, -1),
            ("src/a.txt", "100644", OID, git_boundary.MAX_BLOB_BYTES + 1),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                GitTreeEntry(*arguments)  # type: ignore[arg-type]

        entry = tree_entry()
        with self.assertRaisesRegex(ValueError, "requires a base or result"):
            ChangedPath("src/a.txt", None, None)
        with self.assertRaisesRegex(TypeError, "GitTreeEntry"):
            ChangedPath("src/a.txt", object(), None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "path does not match"):
            ChangedPath("src/b.txt", entry, None)

        self.assertEqual("added", ChangedPath(entry.path, None, entry).change)
        self.assertEqual("deleted", ChangedPath(entry.path, entry, None).change)
        self.assertEqual("modified", ChangedPath(entry.path, entry, entry).change)

    def test_attempt_contracts_reject_ambiguous_identity_and_roots(self) -> None:
        command = {
            "operation_id": "OP-COVERAGE",
            "identity": attempt_identity(),
            "local_repository_id": SHA256,
            "target_workspace_hash": SHA256,
            "base_commit_sha": OID,
            "base_tree_sha": OID,
            "directory_name": "attempt-1",
            "owned_paths": ("src/**",),
            "allowed_auxiliary_paths": (),
            "protected_paths": (".github/**",),
            "path_case_mode": PathCaseMode.SENSITIVE,
        }
        invalid_command = (
            {"identity": ExecutionIdentity("WISH-COVERAGE", 1)},
            {"identity": attempt_identity("OP-OTHER")},
            {"directory_name": "nested/attempt"},
            {"directory_name": ".."},
            {"owned_paths": ()},
            {"path_case_mode": "sensitive"},
        )
        for changes in invalid_command:
            arguments = dict(command)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                AttemptWorktreeCommand(**arguments)  # type: ignore[arg-type]

        root = filesystem_identity()
        attempt = {
            "identity": attempt_identity(),
            "path": "attempt",
            "external_object_id": "worktree-1",
            "local_repository_id": SHA256,
            "target_workspace_hash": SHA256,
            "worktree_root": root,
            "git_dir": root,
            "base_commit_sha": OID,
            "base_tree_sha": OID,
            "owned_paths": ("src/**",),
            "allowed_auxiliary_paths": (),
            "protected_paths": (".github/**",),
            "path_case_mode": PathCaseMode.SENSITIVE,
        }
        invalid_attempt = (
            {"identity": ExecutionIdentity("WISH-COVERAGE", 1)},
            {"worktree_root": object()},
            {"git_dir": object()},
            {"path_case_mode": "sensitive"},
        )
        for changes in invalid_attempt:
            arguments = dict(attempt)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                AttemptWorktree(**arguments)  # type: ignore[arg-type]

    def test_result_manifest_rejects_unstable_or_ambiguous_changes(self) -> None:
        first = ChangedPath("src/a.txt", None, tree_entry("src/a.txt"))
        second = ChangedPath("src/b.txt", None, tree_entry("src/b.txt"))
        valid = {
            "schema_version": 1,
            "identity": attempt_identity(),
            "local_repository_id": SHA256,
            "attempt_hash": SHA256,
            "base_commit_sha": OID,
            "base_tree_sha": OID,
            "result_commit_sha": OID,
            "result_tree_sha": OID,
            "path_case_mode": PathCaseMode.SENSITIVE,
            "changed_paths": (first, second),
            "total_blob_bytes": 2,
            "portable_profile_hash": SHA256,
        }
        invalid = (
            {"schema_version": 2},
            {"identity": ExecutionIdentity("WISH-COVERAGE", 1)},
            {"path_case_mode": "sensitive"},
            {"changed_paths": [first]},
            {"changed_paths": (second, first)},
            {"changed_paths": (first, first)},
            {"total_blob_bytes": True},
            {"total_blob_bytes": git_boundary.MAX_TREE_BYTES + 1},
        )
        for changes in invalid:
            arguments = dict(valid)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                AttemptResultManifest(**arguments)  # type: ignore[arg-type]

    def test_result_and_stage_contracts_require_exact_success_shapes(self) -> None:
        manifest = result_manifest()
        rejected = ResultValidation(
            False,
            None,
            RuntimeReasonCode.GIT_STATE_CONFLICT,
            ("unsafe_result",),
            SHA256,
        )
        self.assertFalse(rejected.accepted)
        accepted = ResultValidation(True, manifest, None, (), SHA256)
        self.assertTrue(accepted.accepted)

        invalid_validation = (
            (1, manifest, None, (), SHA256),
            (True, None, None, (), SHA256),
            (
                True,
                manifest,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                (),
                SHA256,
            ),
            (False, manifest, RuntimeReasonCode.GIT_STATE_CONFLICT, ("x",), SHA256),
            (False, None, None, ("x",), SHA256),
            (False, None, RuntimeReasonCode.GIT_STATE_CONFLICT, (), SHA256),
            (
                False,
                None,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                ["x"],
                SHA256,
            ),
        )
        for arguments in invalid_validation:
            with self.subTest(arguments=arguments), self.assertRaises(
                (TypeError, ValueError)
            ):
                ResultValidation(*arguments)  # type: ignore[arg-type]

        stage = {
            "operation_id": "OP-COVERAGE",
            "identity": attempt_identity(),
            "local_repository_id": SHA256,
            "target_workspace_hash": SHA256,
            "result_manifest_hash": manifest.manifest_hash,
            "result_commit_sha": OID,
            "result_tree_sha": OID,
            "staged_ref": "refs/wish-builder/staged/WISH-COVERAGE/TASK-COVERAGE/1",
        }
        for changes in (
            {"identity": ExecutionIdentity("WISH-COVERAGE", 1)},
            {"staged_ref": "refs/heads/main"},
        ):
            arguments = dict(stage)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                StageResultCommand(**arguments)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            StagedResult(object(), stage["staged_ref"], SHA256)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            StagedResult(manifest, "refs/heads/main", SHA256)

    def test_repository_effects_bind_status_value_and_reason_exactly(self) -> None:
        applied_receipt = EffectReceipt(
            1,
            attempt_identity(),
            EffectOperation.REPOSITORY_UPDATE,
            EffectStatus.APPLIED,
            "2026-08-20T00:00:00Z",
            effect_hash=SHA256,
        )
        absent_receipt = EffectReceipt(
            1,
            attempt_identity(),
            EffectOperation.REPOSITORY_UPDATE,
            EffectStatus.ABSENT,
            "2026-08-20T00:00:00Z",
        )
        value = object()
        applied = RepositoryEffect(
            applied_receipt,
            AttemptEffectDisposition.APPLIED,
            value=value,
        )
        self.assertEqual("success", applied.to_outcome().kind.value)
        RepositoryEffect(
            absent_receipt,
            AttemptEffectDisposition.ABSENT,
            reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
        )

        invalid = (
            (object(), AttemptEffectDisposition.APPLIED, value, None, ()),
            (applied_receipt, "applied", value, None, ()),
            (
                applied_receipt,
                AttemptEffectDisposition.ABSENT,
                None,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                (),
            ),
            (applied_receipt, AttemptEffectDisposition.APPLIED, None, None, ()),
            (
                applied_receipt,
                AttemptEffectDisposition.APPLIED,
                value,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                (),
            ),
            (
                absent_receipt,
                AttemptEffectDisposition.ABSENT,
                value,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                (),
            ),
            (absent_receipt, AttemptEffectDisposition.ABSENT, None, None, ()),
            (
                absent_receipt,
                AttemptEffectDisposition.ABSENT,
                None,
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                ["detail"],
            ),
        )
        for receipt, disposition, result, reason, details in invalid:
            with self.subTest(disposition=disposition), self.assertRaises(
                (TypeError, ValueError)
            ):
                RepositoryEffect(
                    receipt,  # type: ignore[arg-type]
                    disposition,  # type: ignore[arg-type]
                    value=result,
                    reason_code=reason,
                    details=details,  # type: ignore[arg-type]
                )

    def test_adapter_entry_points_reject_unbound_boundary_objects(self) -> None:
        adapter = object.__new__(GitWorktreeAdapter)
        attempt = attempt_worktree()
        manifest = result_manifest()
        accepted = ResultValidation(True, manifest, None, (), SHA256)
        rejected = ResultValidation(
            False,
            None,
            RuntimeReasonCode.GIT_STATE_CONFLICT,
            ("unsafe_result",),
            SHA256,
        )
        stage = StageResultCommand(
            operation_id="OP-COVERAGE",
            identity=attempt_identity(),
            local_repository_id=SHA256,
            target_workspace_hash=SHA256,
            result_manifest_hash=manifest.manifest_hash,
            result_commit_sha=OID,
            result_tree_sha=OID,
            staged_ref="refs/wish-builder/staged/test",
        )

        with self.assertRaises(TypeError):
            adapter.inspect_attempt(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter._tree_entries("HEAD", "sensitive")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter._verify_result_manifest(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter.validate_result(object(), process_tree_terminated=True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter.validate_result(attempt, process_tree_terminated=1)  # type: ignore[arg-type]
        containment = adapter.validate_result(attempt, process_tree_terminated=False)
        self.assertEqual(
            RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
            containment.reason_code,
        )
        with self.assertRaises(ValueError):
            adapter.plan_stage(rejected, operation_id="OP-STAGE")

        with self.assertRaises(TypeError):
            adapter._validate_stage_binding(object(), accepted)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            adapter._validate_stage_binding(stage, rejected)
        mismatched = StageResultCommand(
            operation_id=stage.operation_id,
            identity=stage.identity,
            local_repository_id=stage.local_repository_id,
            target_workspace_hash=stage.target_workspace_hash,
            result_manifest_hash=SHA256,
            result_commit_sha="2" * 40,
            result_tree_sha=stage.result_tree_sha,
            staged_ref=stage.staged_ref,
        )
        with self.assertRaisesRegex(ValueError, "does not bind"):
            adapter._validate_stage_binding(mismatched, accepted)
        with self.assertRaises(TypeError):
            adapter._validate_stage_effect(object(), accepted)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            adapter.prepare_promotion(
                object(),  # type: ignore[arg-type]
                expected_target_sha=OID,
                topological_position=1,
                operation_id="OP-PROMOTION",
                coordinator_epoch=1,
            )
        with self.assertRaises(TypeError):
            with adapter.materialize_promotion_candidate(object()):  # type: ignore[arg-type]
                self.fail("an invalid plan must not materialize")
        with self.assertRaises(TypeError):
            adapter.inspect_promotion(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter._validate_promotion_effect(object(), object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter._verify_staged_source(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter.inspect_cleanup(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            adapter._validate_cleanup_effect(object(), object())  # type: ignore[arg-type]

    def test_attempt_planning_and_effect_binding_fail_closed(self) -> None:
        adapter = adapter_stub()
        command = AttemptWorktreeCommand(
            operation_id="OP-COVERAGE",
            identity=attempt_identity(),
            local_repository_id=SHA256,
            target_workspace_hash=SHA256,
            base_commit_sha=OID,
            base_tree_sha=OID,
            directory_name="attempt-coverage",
            owned_paths=("src/**",),
            allowed_auxiliary_paths=(),
            protected_paths=(".github/**",),
            path_case_mode=PathCaseMode.SENSITIVE,
        )
        effect = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
        )
        self.assertIs(command, adapter._validate_attempt_effect(effect))

        invalid_identity = ExecutionIdentity("WISH-COVERAGE", 1)
        with self.assertRaisesRegex(ValueError, "complete attempt"):
            adapter.plan_attempt(invalid_identity, owned_paths=("src/**",))
        identity_without_correlation = ExecutionIdentity(
            "WISH-COVERAGE", 1, "TASK-COVERAGE", 1
        )
        with self.assertRaisesRegex(ValueError, "correlation"):
            adapter.plan_attempt(
                identity_without_correlation,
                owned_paths=("src/**",),
            )
        with mock.patch.object(adapter, "_guard_target"):
            with self.assertRaisesRegex(GitBoundaryError, "scope_not_pinned"):
                adapter.plan_attempt(
                    attempt_identity(),
                    owned_paths=("outside/**",),
                )

        wrong_command = object.__new__(PreparedEffect)
        object.__setattr__(wrong_command, "request", effect.request)
        object.__setattr__(wrong_command, "command", object())
        with self.assertRaisesRegex(TypeError, "prepared command"):
            adapter._validate_attempt_effect(wrong_command)

        invalid_effects = []
        wrong_event = boundary_effect(
            command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
        )
        invalid_effects.append((wrong_event, "effect_requested"))
        wrong_boundary = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.WORKTREE,
        )
        invalid_effects.append((wrong_boundary, "wrong effect boundary"))
        wrong_target = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
            target_hash="sha256:" + "2" * 64,
        )
        invalid_effects.append((wrong_target, "target hash"))
        wrong_identity = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
            identity=ExecutionIdentity(
                "WISH-COVERAGE", 1, "TASK-OTHER", 1, "OP-COVERAGE"
            ),
        )
        invalid_effects.append((wrong_identity, "identity"))
        stale_command = replace(
            command,
            local_repository_id="sha256:" + "2" * 64,
        )
        stale = boundary_effect(
            stale_command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
        )
        invalid_effects.append((stale, "stale"))
        for candidate, message in invalid_effects:
            with self.subTest(message=message), self.assertRaises(ValueError):
                adapter._validate_attempt_effect(candidate)

        absent = git_boundary._AttemptObservation(
            False,
            True,
            None,
            None,
            True,
            (),
        )
        observed = adapter._attempt_observation_effect(command, absent)
        self.assertEqual(AttemptEffectDisposition.ABSENT, observed.disposition)

    def test_stage_effect_binding_and_conflict_observation_fail_closed(self) -> None:
        adapter = adapter_stub()
        manifest = result_manifest()
        validation = ResultValidation(True, manifest, None, (), SHA256)
        command = StageResultCommand(
            operation_id="OP-STAGE",
            identity=manifest.identity,
            local_repository_id=manifest.local_repository_id,
            target_workspace_hash=SHA256,
            result_manifest_hash=manifest.manifest_hash,
            result_commit_sha=manifest.result_commit_sha,
            result_tree_sha=manifest.result_tree_sha,
            staged_ref="refs/wish-builder/staged/test",
        )
        effect = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.RESULT_BUNDLE,
        )
        self.assertIs(command, adapter._validate_stage_effect(effect, validation))

        wrong_command = object.__new__(PreparedEffect)
        object.__setattr__(wrong_command, "request", effect.request)
        object.__setattr__(wrong_command, "command", object())
        with self.assertRaisesRegex(TypeError, "prepared command"):
            adapter._validate_stage_effect(wrong_command, validation)

        invalid_effects = []
        wrong_event = boundary_effect(
            command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.RESULT_BUNDLE,
        )
        invalid_effects.append(wrong_event)
        wrong_boundary = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.RESULT_BUNDLE,
        )
        invalid_effects.append(wrong_boundary)
        wrong_target = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.RESULT_BUNDLE,
            target_hash="sha256:" + "2" * 64,
        )
        invalid_effects.append(wrong_target)
        wrong_identity = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.RESULT_BUNDLE,
            identity=ExecutionIdentity(
                manifest.identity.run_id,
                manifest.identity.coordinator_epoch,
                "TASK-OTHER",
                manifest.identity.attempt,
                command.operation_id,
            ),
        )
        invalid_effects.append(wrong_identity)
        for candidate in invalid_effects:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                adapter._validate_stage_effect(candidate, validation)

        adapter._expected_workspace.local_repository_id = "sha256:" + "2" * 64
        with self.assertRaisesRegex(ValueError, "stale"):
            adapter._validate_stage_effect(effect, validation)
        adapter._expected_workspace.local_repository_id = SHA256

        with mock.patch.object(git_boundary, "_run_git", return_value=b"f" * 40):
            conflict = adapter._stage_observation(command, validation)
        self.assertEqual(AttemptEffectDisposition.UNKNOWN, conflict.disposition)

    def test_promotion_effect_and_staged_source_checks_fail_closed(self) -> None:
        adapter = adapter_stub()
        plan = promotion_plan()
        effect = boundary_effect(
            plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
        )
        adapter._validate_promotion_effect(effect, plan)

        with self.assertRaisesRegex(TypeError, "PromotionPlan"):
            adapter._validate_promotion_effect(effect, object())  # type: ignore[arg-type]
        mismatched = replace(plan.command, candidate_commit_sha="4" * 40)
        mismatched_effect = boundary_effect(
            mismatched,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
        )
        with self.assertRaisesRegex(ValueError, "does not bind"):
            adapter._validate_promotion_effect(mismatched_effect, plan)

        unbound = promotion_plan(acceptance_bound=False)
        unbound_effect = boundary_effect(
            unbound.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
        )
        with self.assertRaisesRegex(ValueError, "acceptance evidence"):
            adapter._validate_promotion_effect(unbound_effect, unbound)

        invalid_effects = []
        wrong_event = boundary_effect(
            plan.command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
        )
        invalid_effects.append(wrong_event)
        wrong_boundary = boundary_effect(
            plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.GIT_REF,
        )
        invalid_effects.append(wrong_boundary)
        wrong_target = boundary_effect(
            plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
            target_hash="sha256:" + "2" * 64,
        )
        invalid_effects.append(wrong_target)
        wrong_identity = boundary_effect(
            plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
            identity=ExecutionIdentity(
                plan.command.run_id,
                plan.command.coordinator_epoch,
                "TASK-OTHER",
                plan.command.attempt,
                plan.command.operation_id,
            ),
        )
        invalid_effects.append(wrong_identity)
        for candidate in invalid_effects:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                adapter._validate_promotion_effect(candidate, plan)

        adapter._expected_workspace.local_repository_id = "sha256:" + "2" * 64
        with self.assertRaisesRegex(ValueError, "repository identity"):
            adapter._validate_promotion_effect(effect, plan)
        with self.assertRaisesRegex(GitBoundaryError, "staged_repository_mismatch"):
            adapter._verify_staged_source(plan.source)
        adapter._expected_workspace.local_repository_id = SHA256

        with (
            mock.patch.object(adapter, "_verify_result_manifest"),
            mock.patch.object(git_boundary, "_run_git", return_value=b"f" * 40),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "staged_ref_drift"):
                adapter._verify_staged_source(plan.source)
        with (
            mock.patch.object(adapter, "_verify_result_manifest"),
            mock.patch.object(
                git_boundary,
                "_run_git",
                return_value=plan.source.result_commit_sha.encode("ascii"),
            ),
            mock.patch.object(git_boundary, "_git_text", return_value="f" * 40),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "staged_tree_drift"):
                adapter._verify_staged_source(plan.source)

    def test_cleanup_effect_binding_and_quarantine_checks_fail_closed(self) -> None:
        adapter = adapter_stub()
        plan = cleanup_plan()
        effect = boundary_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
        )
        adapter._validate_cleanup_effect(effect, plan)

        with self.assertRaisesRegex(TypeError, "CleanupPlan"):
            adapter._validate_cleanup_effect(effect, object())  # type: ignore[arg-type]
        forged_candidate = object.__new__(CleanupCandidate)
        object.__setattr__(forged_candidate, "attempt", object())
        with self.assertRaisesRegex(TypeError, "AttemptWorktree"):
            adapter.inspect_cleanup(forged_candidate)
        mismatched = replace(plan.command, expected_head_sha="2" * 40)
        mismatched_effect = boundary_effect(
            mismatched,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
        )
        with self.assertRaisesRegex(ValueError, "does not bind"):
            adapter._validate_cleanup_effect(mismatched_effect, plan)

        invalid_effects = []
        wrong_event = boundary_effect(
            plan.command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
        )
        invalid_effects.append(wrong_event)
        wrong_boundary = boundary_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.CLEANUP_ITEM,
        )
        invalid_effects.append(wrong_boundary)
        wrong_target = boundary_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
            target_hash="sha256:" + "2" * 64,
        )
        invalid_effects.append(wrong_target)
        wrong_identity = boundary_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
            identity=ExecutionIdentity(
                plan.command.run_id,
                plan.command.coordinator_epoch,
                "TASK-OTHER",
                plan.command.attempt,
                plan.command.operation_id,
            ),
        )
        invalid_effects.append(wrong_identity)
        for candidate in invalid_effects:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                adapter._validate_cleanup_effect(candidate, plan)

        adapter._expected_workspace.local_repository_id = "sha256:" + "2" * 64
        with self.assertRaisesRegex(ValueError, "repository identity"):
            adapter._validate_cleanup_effect(effect, plan)
        adapter._expected_workspace.local_repository_id = SHA256

        unsafe_candidate = replace(plan.candidate, evidence=())
        forged_plan = object.__new__(CleanupPlan)
        object.__setattr__(forged_plan, "command", plan.command)
        object.__setattr__(forged_plan, "candidate", unsafe_candidate)
        object.__setattr__(forged_plan, "inspection", plan.inspection)
        object.__setattr__(forged_plan, "quarantine_reason", None)
        with self.assertRaisesRegex(ValueError, "bypasses quarantine"):
            adapter._validate_cleanup_effect(effect, forged_plan)

    def test_repository_profile_and_identity_guards_reject_drift(self) -> None:
        adapter = adapter_stub()

        with mock.patch.object(git_boundary, "_git_text", return_value="true"):
            with self.assertRaisesRegex(GitBoundaryError, "shallow_repository"):
                adapter._verify_repository_profile()
        with (
            mock.patch.object(git_boundary, "_git_text", return_value="false"),
            mock.patch.object(Path, "exists", return_value=True),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "alternates"):
                adapter._verify_repository_profile()
        with (
            mock.patch.object(
                git_boundary,
                "_git_text",
                side_effect=("false", "refs/replace/injected"),
            ),
            mock.patch.object(Path, "exists", return_value=False),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "replace_refs"):
                adapter._verify_repository_profile()

        changed_root = replace(
            adapter.attempts_root_identity,
            target_inode=adapter.attempts_root_identity.target_inode + 1,
        )
        with mock.patch.object(
            git_boundary,
            "capture_filesystem_identity",
            return_value=changed_root,
        ):
            with self.assertRaisesRegex(GitBoundaryError, "attempt_root_drift"):
                adapter._guard_attempt_root()

        observed = SimpleNamespace(
            local_repository_id="sha256:" + "2" * 64,
            local_worktree_id=SHA256,
            target_full_ref="refs/heads/main",
        )
        with mock.patch.object(
            git_boundary,
            "capture_workspace_identity",
            return_value=observed,
        ):
            with self.assertRaisesRegex(GitBoundaryError, "workspace_drift"):
                adapter._guard_target_structure()

    def test_tree_and_manifest_resource_boundaries_fail_closed(self) -> None:
        adapter = adapter_stub()
        record = b"100644 blob " + OID.encode("ascii") + b"\tsrc/a.txt\0"

        with (
            mock.patch.object(git_boundary, "MAX_TREE_ENTRIES", 0),
            mock.patch.object(git_boundary, "_run_git", return_value=record),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "tree_entry_limit"):
                adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)

        with (
            mock.patch.object(git_boundary, "_run_git", return_value=record + record),
            mock.patch.object(git_boundary, "_git_text", return_value="1"),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "duplicate_path"):
                adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)

        with (
            mock.patch.object(git_boundary, "_run_git", return_value=record),
            mock.patch.object(
                git_boundary,
                "_git_text",
                return_value=str(git_boundary.MAX_BLOB_BYTES + 1),
            ),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "blob_size_limit"):
                adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)

        records = b"".join(
            b"100644 blob "
            + OID.encode("ascii")
            + f"\tsrc/{index}.txt\0".encode("ascii")
            for index in range(9)
        )
        with (
            mock.patch.object(git_boundary, "_run_git", return_value=records),
            mock.patch.object(
                git_boundary,
                "_git_text",
                return_value=str(git_boundary.MAX_BLOB_BYTES),
            ),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "tree_size_limit"):
                adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)

        base_commit = "a" * 40
        base_tree = "b" * 40
        result_commit = "c" * 40
        result_tree = "d" * 40
        manifest = replace(
            result_manifest(),
            base_commit_sha=base_commit,
            base_tree_sha=base_tree,
            result_commit_sha=result_commit,
            result_tree_sha=result_tree,
            portable_profile_hash=git_boundary.PORTABLE_GIT_PROFILE_HASH,
        )
        adapter._expected_workspace.local_repository_id = "sha256:" + "2" * 64
        with self.assertRaisesRegex(GitBoundaryError, "repository_mismatch"):
            adapter._verify_result_manifest(manifest)
        adapter._expected_workspace.local_repository_id = SHA256
        with self.assertRaisesRegex(GitBoundaryError, "portable_profile_drift"):
            adapter._verify_result_manifest(
                replace(manifest, portable_profile_hash=SHA256)
            )

        expected_parents = f"{result_commit} {base_commit}"
        text_failures = (
            (("e" * 40, result_tree, expected_parents), "base_tree_mismatch"),
            ((base_tree, "e" * 40, expected_parents), "result_tree_mismatch"),
            ((base_tree, result_tree, result_commit), "result_parent_mismatch"),
        )
        for values, code in text_failures:
            with self.subTest(code=code), mock.patch.object(
                git_boundary,
                "_git_text",
                side_effect=values,
            ):
                with self.assertRaisesRegex(GitBoundaryError, code):
                    adapter._verify_result_manifest(manifest)

        with (
            mock.patch.object(
                git_boundary,
                "_git_text",
                side_effect=(base_tree, result_tree, expected_parents),
            ),
            mock.patch.object(adapter, "_tree_entries", side_effect=({}, {})),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "change_mismatch"):
                adapter._verify_result_manifest(manifest)

        wrong_size = replace(manifest, total_blob_bytes=2)
        with (
            mock.patch.object(
                git_boundary,
                "_git_text",
                side_effect=(base_tree, result_tree, expected_parents),
            ),
            mock.patch.object(
                adapter,
                "_tree_entries",
                side_effect=({}, {"src/a.txt": tree_entry()}),
            ),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "size_mismatch"):
                adapter._verify_result_manifest(wrong_size)

    def test_materialized_result_and_promotion_boundaries_fail_closed(self) -> None:
        adapter = adapter_stub()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve(strict=True)
            attempt = replace(attempt_worktree(), path=str(root))
            deleted = ChangedPath("gone.txt", tree_entry("gone.txt"), None)
            adapter._validate_materialized_result(attempt, (deleted,))

            one_file_walk = ((str(root), (), ("candidate",)),)
            candidate = root / "candidate"
            real_lstat = git_boundary.os.lstat

            def lstat_result(result):
                def observe(path):
                    if Path(path) == candidate:
                        return result
                    return real_lstat(path)

                return observe

            regular = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1)
            with (
                mock.patch.object(git_boundary, "MAX_TREE_ENTRIES", 0),
                mock.patch.object(git_boundary.os, "walk", return_value=one_file_walk),
                mock.patch.object(
                    git_boundary.os,
                    "lstat",
                    side_effect=lstat_result(regular),
                ),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "worktree_path_limit"):
                    adapter._validate_materialized_result(attempt, (deleted,))

            linked = SimpleNamespace(st_mode=stat.S_IFLNK, st_nlink=1)
            with (
                mock.patch.object(git_boundary.os, "walk", return_value=one_file_walk),
                mock.patch.object(
                    git_boundary.os,
                    "lstat",
                    side_effect=lstat_result(linked),
                ),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "link_or_reparse"):
                    adapter._validate_materialized_result(attempt, (deleted,))

            special = SimpleNamespace(st_mode=0, st_nlink=1)
            with (
                mock.patch.object(git_boundary.os, "walk", return_value=one_file_walk),
                mock.patch.object(
                    git_boundary.os,
                    "lstat",
                    side_effect=lstat_result(special),
                ),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "special_file"):
                    adapter._validate_materialized_result(attempt, (deleted,))

            added = ChangedPath("candidate", None, tree_entry("candidate"))
            directory = SimpleNamespace(st_mode=stat.S_IFDIR, st_nlink=1)
            with (
                mock.patch.object(git_boundary.os, "walk", return_value=()),
                mock.patch.object(
                    git_boundary.os,
                    "lstat",
                    side_effect=lstat_result(directory),
                ),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "not_regular"):
                    adapter._validate_materialized_result(attempt, (added,))

            hardlink = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=2)
            with (
                mock.patch.object(git_boundary.os, "walk", return_value=()),
                mock.patch.object(
                    git_boundary.os,
                    "lstat",
                    side_effect=lstat_result(hardlink),
                ),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "hardlink_rejected"):
                    adapter._validate_materialized_result(attempt, (added,))

        plan = promotion_plan()
        with self.assertRaisesRegex(ValueError, "unbound plan"):
            with adapter.materialize_promotion_candidate(plan):
                self.fail("bound plans must not materialize")

        with self.assertRaisesRegex(ValueError, "coordinator_epoch"):
            adapter.prepare_promotion(
                plan.source,
                expected_target_sha=OID,
                topological_position=1,
                operation_id="OP-PROMOTION",
                coordinator_epoch=0,
            )
        adapter._mutation_lock = SimpleNamespace(acquire=lambda: nullcontext())
        with mock.patch.object(adapter, "_guard_target"):
            with self.assertRaisesRegex(GitBoundaryError, "expected_target_stale"):
                adapter.prepare_promotion(
                    plan.source,
                    expected_target_sha="2" * 40,
                    topological_position=1,
                    operation_id="OP-PROMOTION",
                    coordinator_epoch=1,
                )

        with (
            mock.patch.object(
                adapter,
                "_build_candidate_tree",
                return_value=plan.command.candidate_tree_sha,
            ),
            mock.patch.object(git_boundary, "_git_text_with_input", return_value=OID),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "commit_mismatch"):
                adapter._verify_promotion_plan(plan)
        with (
            mock.patch.object(
                adapter,
                "_build_candidate_tree",
                return_value=plan.command.candidate_tree_sha,
            ),
            mock.patch.object(
                git_boundary,
                "_git_text_with_input",
                return_value=plan.command.candidate_commit_sha,
            ),
            mock.patch.object(
                git_boundary,
                "_git_text",
                side_effect=(OID, plan.command.candidate_commit_sha),
            ),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "candidate_invalid"):
                adapter._verify_promotion_plan(plan)

    def test_repository_lock_covers_posix_and_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            lock = git_boundary._RepositoryMutationLock(
                Path(raw_root) / "repository.lock",
                0.01,
            )
            flock = mock.Mock()
            fake_fcntl = SimpleNamespace(
                LOCK_EX=1,
                LOCK_NB=2,
                LOCK_UN=4,
                flock=flock,
            )
            with (
                mock.patch.object(git_boundary.os, "name", "posix"),
                mock.patch.dict("sys.modules", {"fcntl": fake_fcntl}),
                lock.acquire(),
            ):
                pass
            self.assertEqual(2, flock.call_count)

            failing_fcntl = SimpleNamespace(
                LOCK_EX=1,
                LOCK_NB=2,
                LOCK_UN=4,
                flock=mock.Mock(side_effect=OSError("lock denied")),
            )
            with (
                mock.patch.object(git_boundary.os, "name", "posix"),
                mock.patch.dict("sys.modules", {"fcntl": failing_fcntl}),
                self.assertRaisesRegex(GitBoundaryError, "repository_lock_busy"),
                lock.acquire(),
            ):
                self.fail("a terminal lock failure must not enter the critical section")

            locking = mock.Mock()
            fake_msvcrt = SimpleNamespace(
                LK_NBLCK=1,
                LK_UNLCK=2,
                locking=locking,
            )
            with (
                mock.patch.object(git_boundary.os, "name", "nt"),
                mock.patch.dict("sys.modules", {"msvcrt": fake_msvcrt}),
                lock.acquire(),
            ):
                pass
            self.assertEqual(
                [
                    mock.call(mock.ANY, fake_msvcrt.LK_NBLCK, 1),
                    mock.call(mock.ANY, fake_msvcrt.LK_UNLCK, 1),
                ],
                locking.call_args_list,
            )

    def test_constructor_rejects_mismatched_linked_and_nondirectory_roots(self) -> None:
        def path_identity(path: Path, inode: int) -> FilesystemIdentity:
            canonical = str(path.resolve(strict=True))
            return FilesystemIdentity(
                lexical_path=canonical,
                canonical_path=canonical,
                link_device=1,
                link_inode=inode,
                target_device=1,
                target_inode=inode,
                is_link_or_reparse_point=False,
                access_control_hash=SHA256,
            )

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            repository = root / "repository"
            other = root / "other"
            attempts = root / "attempts"
            repository.mkdir()
            other.mkdir()
            attempts.mkdir()
            repository_identity = path_identity(repository, 2)
            common_identity = path_identity(root, 1)
            workspace = WorkspaceIdentity(
                local_repository_id=SHA256,
                local_worktree_id=SHA256,
                common_dir=common_identity,
                worktree_root=repository_identity,
                git_dir=repository_identity,
                target_full_ref="refs/heads/main",
                base_commit_sha=OID,
                scopes=("src/**",),
                index_dirty_fingerprint=SHA256,
            )

            with (
                mock.patch.object(Path, "resolve", side_effect=OSError("missing")),
                self.assertRaisesRegex(GitBoundaryError, "workspace_unavailable"),
            ):
                GitWorktreeAdapter(repository, attempts, workspace)

            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=git_boundary.GitIdentityError("probe_failed"),
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_unavailable"),
            ):
                GitWorktreeAdapter(repository, attempts, workspace)

            with self.assertRaisesRegex(GitBoundaryError, "workspace_identity_mismatch"):
                GitWorktreeAdapter(other, attempts, workspace)

            linked = replace(
                path_identity(attempts, 3),
                is_link_or_reparse_point=True,
            )
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    return_value=linked,
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_is_link"),
            ):
                GitWorktreeAdapter(repository, attempts, workspace)

            root_file = root / "attempt-root-file"
            root_file.write_bytes(b"not a directory")
            nondirectory = path_identity(root_file, 4)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    return_value=nondirectory,
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_not_directory"),
            ):
                GitWorktreeAdapter(repository, root_file, workspace)

            canonical_attempts = path_identity(attempts, 3)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(
                        canonical_attempts,
                        git_boundary.GitIdentityError("probe_failed"),
                    ),
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_unavailable"),
            ):
                GitWorktreeAdapter(repository, attempts, workspace)

            drifted_attempts = replace(canonical_attempts, target_inode=99)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(canonical_attempts, drifted_attempts),
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_drift"),
            ):
                GitWorktreeAdapter(repository, attempts, workspace)

            nested_attempts = repository / "attempts"
            nested_attempts.mkdir()
            nested_identity = path_identity(nested_attempts, 5)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(nested_identity, nested_identity),
                ),
                self.assertRaisesRegex(GitBoundaryError, "attempt_root_inside_target"),
            ):
                GitWorktreeAdapter(repository, nested_attempts, workspace)

            outer_identity = path_identity(root, 6)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(outer_identity, outer_identity),
                ),
                self.assertRaisesRegex(GitBoundaryError, "target_inside_attempt_root"),
            ):
                GitWorktreeAdapter(repository, root, workspace)

            short_alias = replace(
                canonical_attempts,
                lexical_path=str(root / "RUNNER~1" / "attempts"),
            )
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(
                        short_alias,
                        canonical_attempts,
                        canonical_attempts,
                    ),
                ),
                mock.patch.object(
                    GitWorktreeAdapter,
                    "_verify_repository_profile",
                ),
                mock.patch.object(
                    git_boundary,
                    "_git_object_id_length",
                    return_value=40,
                ),
            ):
                adapter = GitWorktreeAdapter(repository, attempts, workspace)
                adapter._guard_attempt_root()
            self.assertEqual(attempts.resolve(strict=True), adapter.attempts_root)
            self.assertEqual(canonical_attempts, adapter.attempts_root_identity)

    def test_create_attempt_reconciles_both_known_post_failure_outcomes(self) -> None:
        adapter = adapter_stub()
        adapter._mutation_lock = SimpleNamespace(acquire=lambda: nullcontext())
        command = attempt_command()
        effect = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.REPOSITORY_UPDATE,
            object_type=EffectObjectType.WORKTREE,
        )
        absent = git_boundary._AttemptObservation(
            False,
            True,
            None,
            None,
            True,
            ("attempt_absent",),
        )
        exact = git_boundary._AttemptObservation(
            True,
            True,
            attempt_worktree(),
            OID,
            True,
            (),
        )
        with (
            mock.patch.object(adapter, "_guard_attempt_root"),
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(adapter, "_trigger"),
            mock.patch.object(adapter, "_inspect_attempt", side_effect=(absent, exact)),
            mock.patch.object(
                git_boundary,
                "_run_git",
                side_effect=GitBoundaryError("create_failed"),
            ),
        ):
            applied = adapter.create_attempt(effect)
        self.assertEqual(AttemptEffectDisposition.APPLIED, applied.disposition)

        with (
            mock.patch.object(adapter, "_guard_attempt_root"),
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(adapter, "_trigger"),
            mock.patch.object(adapter, "_inspect_attempt", side_effect=(absent, absent)),
            mock.patch.object(
                git_boundary,
                "_run_git",
                side_effect=GitBoundaryError("create_failed"),
            ),
        ):
            not_created = adapter.create_attempt(effect)
        self.assertEqual(AttemptEffectDisposition.ABSENT, not_created.disposition)

    def test_attempt_inspection_and_current_state_report_identity_replacement(self) -> None:
        adapter = adapter_stub()
        command = attempt_command()
        regular_file = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
        with (
            mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
            mock.patch.object(git_boundary.os, "lstat", return_value=regular_file),
        ):
            replaced_observation = adapter._inspect_attempt(command)
        self.assertEqual(("attempt_path_replaced",), replaced_observation.details)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve(strict=True)
            path = root / "attempt"
            path.mkdir()
            adapter.attempts_root = root
            attempt = replace(attempt_worktree(), path=str(path))
            changed_root = replace(filesystem_identity(), target_inode=10)
            changed_git = replace(filesystem_identity(), target_inode=11)
            common = replace(filesystem_identity(), target_inode=12)
            with (
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=(changed_root, changed_git, common),
                ),
                mock.patch.object(
                    git_boundary,
                    "_git_text",
                    side_effect=("git-dir", "common-dir", OID),
                ),
                mock.patch.object(git_boundary, "_run_git", return_value=b""),
            ):
                _, clean, details = adapter._attempt_current_state(attempt)
        self.assertTrue(clean)
        self.assertIn("worktree_root_identity", details)
        self.assertIn("git_dir_identity", details)

    def test_result_validation_rejects_empty_changes_and_final_state_races(self) -> None:
        adapter = adapter_stub()
        attempt = attempt_worktree()
        git_values = (f"{OID} {OID}", OID, OID)
        with (
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(
                adapter,
                "_attempt_current_state",
                return_value=(OID, True, ()),
            ),
            mock.patch.object(git_boundary, "_git_text", side_effect=git_values),
            mock.patch.object(adapter, "_tree_entries", side_effect=({}, {})),
        ):
            empty = adapter.validate_result(attempt, process_tree_terminated=True)
        self.assertFalse(empty.accepted)
        self.assertIn("result_has_no_changes", empty.violations)

        with (
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(
                adapter,
                "_attempt_current_state",
                side_effect=((OID, True, ()), ("2" * 40, True, ())),
            ),
            mock.patch.object(git_boundary, "_git_text", side_effect=git_values),
            mock.patch.object(
                adapter,
                "_tree_entries",
                side_effect=({}, {"src/a.txt": tree_entry()}),
            ),
            mock.patch.object(adapter, "_validate_materialized_result"),
        ):
            raced = adapter.validate_result(attempt, process_tree_terminated=True)
        self.assertFalse(raced.accepted)
        self.assertIn("result_validation_race", raced.violations)

    def test_promotion_candidate_rejects_invalid_state_and_incomplete_cleanup(self) -> None:
        adapter = adapter_stub()
        adapter._mutation_lock = SimpleNamespace(acquire=lambda: nullcontext())
        plan = promotion_plan(acceptance_bound=False)
        with tempfile.TemporaryDirectory() as raw_root:
            adapter.attempts_root = Path(raw_root)
            with (
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(adapter, "_guard_target"),
                mock.patch.object(adapter, "_verify_staged_source"),
                mock.patch.object(adapter, "_verify_promotion_plan"),
                mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
                mock.patch.object(git_boundary, "_run_git", return_value=b""),
                mock.patch.object(git_boundary, "_git_text", return_value="f" * 40),
                self.assertRaisesRegex(GitBoundaryError, "acceptance_candidate_invalid"),
            ):
                with adapter.materialize_promotion_candidate(plan):
                    self.fail("an invalid candidate must not be exposed")

            with (
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(adapter, "_guard_target"),
                mock.patch.object(adapter, "_verify_staged_source"),
                mock.patch.object(adapter, "_verify_promotion_plan"),
                mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
                mock.patch.object(git_boundary, "_run_git", return_value=b""),
                mock.patch.object(
                    git_boundary,
                    "_git_text",
                    side_effect=(
                        plan.command.candidate_commit_sha,
                        plan.command.candidate_tree_sha,
                    ),
                ),
                self.assertRaisesRegex(
                    GitBoundaryError,
                    "acceptance_candidate_cleanup_incomplete",
                ),
            ):
                with adapter.materialize_promotion_candidate(plan) as candidate:
                    candidate.mkdir()

    def test_candidate_tree_handles_stale_paths_and_deletions(self) -> None:
        adapter = adapter_stub()
        source = staged_result()
        with (
            mock.patch.object(adapter, "_guard_attempt_root"),
            mock.patch.object(
                adapter,
                "_tree_entries",
                return_value={"src/a.txt": tree_entry()},
            ),
            self.assertRaisesRegex(GitBoundaryError, "promotion_path_stale"),
        ):
            adapter._build_candidate_tree(source, OID)

        deleted = ChangedPath("src/a.txt", tree_entry(), None)
        deletion_manifest = replace(
            result_manifest(changed_paths=(deleted,)),
            total_blob_bytes=0,
        )
        deletion_source = git_boundary.StagedResult(
            deletion_manifest,
            "refs/wish-builder/staged/delete",
            SHA256,
        )
        with tempfile.TemporaryDirectory() as raw_root:
            adapter.attempts_root = Path(raw_root)
            run_git = mock.Mock(return_value=b"")
            with (
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(
                    adapter,
                    "_tree_entries",
                    return_value={"src/a.txt": tree_entry()},
                ),
                mock.patch.object(git_boundary, "_run_git", run_git),
                mock.patch.object(
                    git_boundary,
                    "_git_text_environment",
                    return_value=OID,
                ),
            ):
                self.assertEqual(OID, adapter._build_candidate_tree(deletion_source, OID))
        self.assertTrue(
            any("--force-remove" in call.args[1] for call in run_git.call_args_list)
        )

    def test_promotion_and_cleanup_inspections_cover_drift_boundaries(self) -> None:
        adapter = adapter_stub()
        plan = promotion_plan()
        observed_workspace = SimpleNamespace(
            base_commit_sha="4" * 40,
            scopes=(),
        )
        with (
            mock.patch.object(
                adapter,
                "_guard_target_structure",
                return_value=observed_workspace,
            ),
            mock.patch.object(adapter, "_verify_staged_source"),
            mock.patch.object(adapter, "_verify_promotion_plan"),
            mock.patch.object(git_boundary, "_git_text", return_value="5" * 40),
        ):
            promotion = adapter._inspect_promotion_locked(plan)
        self.assertEqual("unknown", promotion.disposition.value)
        self.assertEqual("target_ref_drift", promotion.details[0])

        cleanup = cleanup_plan()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            missing_path = root / "missing-attempt"
            missing_attempt = replace(
                cleanup.candidate.attempt,
                path=str(missing_path),
            )
            missing_candidate = replace(cleanup.candidate, attempt=missing_attempt)
            with mock.patch.object(
                adapter,
                "_registered_worktree_paths",
                return_value=(missing_path,),
            ):
                missing = adapter.inspect_cleanup(missing_candidate)
            self.assertEqual(("registered_worktree_path_missing",), missing.details)

            live_path = root / "live-attempt"
            live_path.mkdir()
            live_attempt = replace(cleanup.candidate.attempt, path=str(live_path))
            live_candidate = replace(cleanup.candidate, attempt=live_attempt)
            with (
                mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
                mock.patch.object(
                    adapter,
                    "_attempt_current_state",
                    return_value=(OID, True, ()),
                ),
            ):
                unregistered = adapter.inspect_cleanup(live_candidate)
            self.assertEqual(("worktree_registration_missing",), unregistered.details)

    def test_cleanup_reconciliation_classifies_every_known_outcome(self) -> None:
        adapter = adapter_stub()
        adapter._mutation_lock = SimpleNamespace(acquire=lambda: nullcontext())
        plan = cleanup_plan()
        effect = boundary_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
        )
        absent = CleanupInspection(
            False,
            True,
            True,
            None,
            SHA256,
            SHA256,
        )
        present = CleanupInspection(
            True,
            True,
            True,
            OID,
            SHA256,
            SHA256,
        )
        ambiguous = CleanupInspection(
            True,
            False,
            False,
            OID,
            SHA256,
            SHA256,
            ("identity_ambiguous",),
        )

        def apply_after(reconciled: CleanupInspection, *, fail: bool):
            outcome = (
                GitBoundaryError("remove_failed")
                if fail
                else None
            )
            with (
                mock.patch.object(adapter, "_validate_cleanup_effect"),
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(adapter, "_guard_target"),
                mock.patch.object(adapter, "_trigger"),
                mock.patch.object(
                    adapter,
                    "inspect_cleanup",
                    side_effect=(plan.inspection, reconciled),
                ),
                mock.patch.object(git_boundary, "_run_git", side_effect=outcome),
            ):
                return adapter.apply_cleanup(effect, plan)

        self.assertEqual("removed", apply_after(absent, fail=True).disposition.value)
        self.assertEqual("quarantined", apply_after(present, fail=True).disposition.value)
        self.assertEqual("unknown", apply_after(ambiguous, fail=True).disposition.value)
        self.assertEqual("removed", apply_after(absent, fail=False).disposition.value)
        self.assertEqual("unknown", apply_after(ambiguous, fail=False).disposition.value)

    def test_git_object_format_and_adapter_constructor_fail_closed(self) -> None:
        with mock.patch.object(git_boundary, "_git_text", return_value="sha256"):
            self.assertEqual(64, git_boundary._git_object_id_length(Path(".")))
        with mock.patch.object(git_boundary, "_git_text", return_value="unknown"):
            with self.assertRaisesRegex(GitBoundaryError, "unsupported"):
                git_boundary._git_object_id_length(Path("."))

        with self.assertRaises(TypeError):
            GitWorktreeAdapter(".", ".", object())  # type: ignore[arg-type]
        placeholder = object.__new__(WorkspaceIdentity)
        for invalid_timeout in (0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(lock_timeout_seconds=invalid_timeout):
                with self.assertRaises(ValueError):
                    GitWorktreeAdapter(
                        ".",
                        ".",
                        placeholder,
                        lock_timeout_seconds=invalid_timeout,
                    )
        with self.assertRaises(TypeError):
            GitWorktreeAdapter(".", ".", placeholder, clock=object())  # type: ignore[arg-type]

    def test_git_transport_text_and_lock_failures_have_stable_codes(self) -> None:
        with mock.patch.object(
            git_boundary.subprocess, "run", side_effect=OSError("missing")
        ):
            with self.assertRaisesRegex(GitBoundaryError, "git_unavailable"):
                git_boundary._run_git(Path("."), ("status",))

        text_calls = (
            lambda: git_boundary._git_text(Path("."), "status"),
            lambda: git_boundary._git_text_environment(
                Path("."), ("status",), {}
            ),
            lambda: git_boundary._git_text_with_input(
                Path("."), ("hash-object",), b"input", environment={}
            ),
        )
        for action in text_calls:
            with self.subTest(action=action), mock.patch.object(
                git_boundary, "_run_git", return_value=b"\xff"
            ):
                with self.assertRaisesRegex(
                    GitBoundaryError, "git_output_invalid_utf8"
                ):
                    action()

        lock = git_boundary._RepositoryMutationLock(Path("missing.lock"), 1)
        with mock.patch.object(git_boundary.os, "open", side_effect=OSError("denied")):
            with self.assertRaisesRegex(
                GitBoundaryError, "repository_lock_open_failed"
            ), lock.acquire():
                self.fail("a failed lock open must not enter")

    def test_identity_scan_and_tree_decode_errors_fail_closed(self) -> None:
        adapter = adapter_stub()
        with mock.patch.object(
            git_boundary,
            "capture_filesystem_identity",
            side_effect=git_boundary.GitIdentityError("probe_failed"),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "attempt_root_drift"):
                adapter._guard_attempt_root()
        with mock.patch.object(
            git_boundary,
            "capture_workspace_identity",
            side_effect=git_boundary.GitIdentityError("probe_failed"),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "workspace_drift"):
                adapter._guard_target_structure()

        with mock.patch.object(git_boundary, "_run_git", return_value=b"worktree \xff\0"):
            with self.assertRaisesRegex(GitBoundaryError, "git_path_invalid_utf8"):
                adapter._registered_worktree_paths()
        with mock.patch.object(
            adapter,
            "_registered_worktree_paths",
            side_effect=GitBoundaryError("worktree_list_failed"),
        ):
            observed = adapter._inspect_attempt(attempt_command())
        self.assertEqual(("worktree_list_failed",), observed.details)
        with (
            mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
            mock.patch.object(git_boundary.os, "lstat", side_effect=OSError("race")),
        ):
            observed = adapter._inspect_attempt(attempt_command())
        self.assertEqual(("attempt_lstat_failed:OSError",), observed.details)

        with tempfile.TemporaryDirectory() as raw_attempts:
            adapter.attempts_root = Path(raw_attempts)
            (adapter.attempts_root / attempt_command().directory_name).mkdir()
            with (
                mock.patch.object(
                    adapter, "_registered_worktree_paths", return_value=()
                ),
                mock.patch.object(
                    git_boundary,
                    "capture_filesystem_identity",
                    side_effect=ValueError("invalid identity"),
                ),
            ):
                observed = adapter._inspect_attempt(attempt_command())
        self.assertEqual(("ValueError",), observed.details)

        invalid_records = (
            (b"invalid\0", "git_tree_record_invalid"),
            (
                b"100644 blob " + OID.encode("ascii") + b"\tsrc/\xff\0",
                "git_tree_record_invalid",
            ),
        )
        for raw, code in invalid_records:
            with self.subTest(code=code), mock.patch.object(
                git_boundary, "_run_git", return_value=raw
            ):
                with self.assertRaisesRegex(GitBoundaryError, code):
                    adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)
        record = b"100644 blob " + OID.encode("ascii") + b"\tsrc/a.txt\0"
        with (
            mock.patch.object(git_boundary, "_run_git", return_value=record),
            mock.patch.object(git_boundary, "_git_text", return_value="not-a-size"),
        ):
            with self.assertRaisesRegex(GitBoundaryError, "git_blob_size_invalid"):
                adapter._tree_entries("HEAD", PathCaseMode.SENSITIVE)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve(strict=True)
            attempt = replace(attempt_worktree(), path=str(root))
            candidate = root / "candidate"
            real_lstat = git_boundary.os.lstat

            def lstat(path, *args, **kwargs):
                if Path(path) == candidate:
                    raise OSError("race")
                return real_lstat(path, *args, **kwargs)

            with (
                mock.patch.object(
                    git_boundary.os,
                    "walk",
                    return_value=((str(root), (), ("candidate",)),),
                ),
                mock.patch.object(git_boundary.os, "lstat", side_effect=lstat),
            ):
                with self.assertRaisesRegex(GitBoundaryError, "worktree_scan_race"):
                    adapter._validate_materialized_result(attempt, ())

            parent = root / "src"
            parent.mkdir()
            escaped = ChangedPath("src/deleted.txt", tree_entry("src/deleted.txt"), None)
            real_resolve = Path.resolve

            def resolve(path: Path, *args, **kwargs):
                if path == parent:
                    return root.parent
                return real_resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve):
                with self.assertRaisesRegex(GitBoundaryError, "git_path_escape"):
                    adapter._validate_materialized_result(attempt, (escaped,))

        with mock.patch.object(adapter, "_guard_target", side_effect=ValueError("bad")):
            rejected = adapter.validate_result(
                attempt_worktree(), process_tree_terminated=True
            )
        self.assertIn("ValueError", rejected.violations)

    def test_stage_promotion_and_cleanup_recovery_paths_are_observable(self) -> None:
        adapter = adapter_stub()
        adapter._mutation_lock = SimpleNamespace(acquire=lambda: nullcontext())
        manifest = result_manifest()
        validation = ResultValidation(True, manifest, None, (), SHA256)
        command = StageResultCommand(
            operation_id="OP-STAGE-RECOVERY",
            identity=manifest.identity,
            local_repository_id=manifest.local_repository_id,
            target_workspace_hash=SHA256,
            result_manifest_hash=manifest.manifest_hash,
            result_commit_sha=manifest.result_commit_sha,
            result_tree_sha=manifest.result_tree_sha,
            staged_ref="refs/wish-builder/staged/recovery",
        )
        effect = boundary_effect(
            command,
            event_type=JournalEventType.EFFECT_REQUESTED,
            operation=EffectOperation.RESULT_STAGE,
            object_type=EffectObjectType.RESULT_BUNDLE,
        )
        absent = SimpleNamespace(disposition=AttemptEffectDisposition.ABSENT)
        recovered = SimpleNamespace(disposition=AttemptEffectDisposition.APPLIED)
        with (
            mock.patch.object(adapter, "_validate_stage_effect", return_value=command),
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(adapter, "_verify_result_manifest"),
            mock.patch.object(
                adapter, "_stage_observation", side_effect=(absent, recovered)
            ),
            mock.patch.object(
                git_boundary, "_run_git", side_effect=GitBoundaryError("stage_failed")
            ),
        ):
            self.assertIs(recovered, adapter.stage_result(effect, validation))

        inspected = SimpleNamespace(disposition=AttemptEffectDisposition.APPLIED)
        with (
            mock.patch.object(adapter, "_validate_stage_binding"),
            mock.patch.object(adapter, "_guard_target_structure"),
            mock.patch.object(adapter, "_verify_result_manifest"),
            mock.patch.object(adapter, "_stage_observation", return_value=inspected),
        ):
            self.assertIs(inspected, adapter.inspect_stage(command, validation))

        with mock.patch.object(git_boundary, "_run_git", return_value=b"\xff"):
            invalid = adapter._stage_observation(command, validation)
        self.assertEqual(AttemptEffectDisposition.UNKNOWN, invalid.disposition)

        plan = promotion_plan()
        promotion_effect = boundary_effect(
            plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
        )
        absent_promotion = SimpleNamespace(
            disposition=git_boundary.PromotionDisposition.ABSENT
        )
        recovered_promotion = SimpleNamespace(
            disposition=git_boundary.PromotionDisposition.UNKNOWN
        )
        with (
            mock.patch.object(adapter, "_validate_promotion_effect"),
            mock.patch.object(
                adapter,
                "_inspect_promotion_locked",
                side_effect=(absent_promotion, recovered_promotion),
            ),
            mock.patch.object(adapter, "_guard_target"),
            mock.patch.object(adapter, "_verify_staged_source"),
            mock.patch.object(adapter, "_verify_promotion_plan"),
            mock.patch.object(
                git_boundary,
                "_run_git",
                side_effect=GitBoundaryError("promotion_failed"),
            ),
        ):
            self.assertIs(
                recovered_promotion,
                adapter.apply_promotion(promotion_effect, plan),
            )

        cleanup = cleanup_plan().candidate
        with mock.patch.object(
            adapter,
            "_registered_worktree_paths",
            side_effect=GitBoundaryError("worktree_list_failed"),
        ):
            inspection = adapter.inspect_cleanup(cleanup)
        self.assertEqual(("worktree_list_failed",), inspection.details)
        with (
            mock.patch.object(adapter, "_registered_worktree_paths", return_value=()),
            mock.patch.object(git_boundary.os, "lstat", side_effect=OSError("race")),
        ):
            inspection = adapter.inspect_cleanup(cleanup)
        self.assertEqual(("attempt_lstat_failed:OSError",), inspection.details)

        unbound = promotion_plan(acceptance_bound=False)
        with tempfile.TemporaryDirectory() as raw_attempts:
            adapter.attempts_root = Path(raw_attempts)
            with (
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(adapter, "_guard_target"),
                mock.patch.object(adapter, "_verify_staged_source"),
                mock.patch.object(adapter, "_verify_promotion_plan"),
                mock.patch.object(
                    git_boundary,
                    "_git_text",
                    side_effect=(
                        unbound.command.candidate_commit_sha,
                        unbound.command.candidate_tree_sha,
                    ),
                ),
                mock.patch.object(git_boundary, "_run_git", return_value=b""),
                mock.patch.object(
                    adapter,
                    "_registered_worktree_paths",
                    side_effect=GitBoundaryError("worktree_list_failed"),
                ),
                self.assertRaisesRegex(
                    GitBoundaryError, "acceptance_candidate_cleanup_incomplete"
                ),
            ):
                with adapter.materialize_promotion_candidate(unbound):
                    pass

        with tempfile.TemporaryDirectory() as raw_attempts:
            adapter.attempts_root = Path(raw_attempts)
            registered = mock.Mock(return_value=())

            def run_git(_repository, arguments, **_kwargs):
                if "remove" in arguments:
                    raise GitBoundaryError("remove_failed")
                return b""

            with (
                mock.patch.object(adapter, "_guard_attempt_root"),
                mock.patch.object(adapter, "_guard_target"),
                mock.patch.object(adapter, "_verify_staged_source"),
                mock.patch.object(adapter, "_verify_promotion_plan"),
                mock.patch.object(
                    git_boundary,
                    "_git_text",
                    side_effect=(
                        unbound.command.candidate_commit_sha,
                        unbound.command.candidate_tree_sha,
                    ),
                ),
                mock.patch.object(git_boundary, "_run_git", side_effect=run_git),
                mock.patch.object(
                    adapter,
                    "_registered_worktree_paths",
                    registered,
                ),
                self.assertRaisesRegex(
                    GitBoundaryError, "acceptance_candidate_cleanup_incomplete"
                ),
            ):
                with adapter.materialize_promotion_candidate(unbound) as candidate:
                    registered.return_value = (candidate,)


if __name__ == "__main__":
    unittest.main()
