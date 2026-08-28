from __future__ import annotations

import ast
import multiprocessing
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import get_args

from tests.e2e.crash_boundary_registry import (
    CRASH_BOUNDARIES,
    UNPROVEN_PREFIX,
    CrashBoundary,
    EvidenceKind,
)
from tests.ports.test_conformance import FIXED_TIME as PORT_FIXED_TIME
from tests.ports.test_conformance import persisted_request, receipt_from
from tests.services import test_attempts as attempt_fixtures
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.git_identity import capture_workspace_identity
from wish_builder.adapters.git_worktree import (
    AttemptEffectDisposition,
    AttemptWorktree,
    AttemptWorktreeCommand,
    GitWorktreeAdapter,
    GitWorktreeCrash,
    ResultValidation,
    StagedResult,
    StageResultCommand,
)
from wish_builder.contracts import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
    PathCaseMode,
)
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupDisposition,
    CleanupPlan,
    CleanupService,
)
from wish_builder.services.ports import PersistedEffectRequest, PreparedEffect
from wish_builder.services.promotion import (
    PromotionDisposition,
    PromotionPlan,
    PromotionService,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CRASH_EXIT_CODE = 86
SOURCE_BY_SUBSYSTEM = {
    "fake_effect": REPOSITORY_ROOT / "wish_builder/adapters/fake/effects.py",
    "git_worktree": REPOSITORY_ROOT / "wish_builder/adapters/git_worktree.py",
}


def _fake_request() -> PersistedEffectRequest:
    return persisted_request(
        AdapterKind.TASK,
        EffectOperation.TASK_EXECUTION,
        EffectObjectType.WORKER,
        JournalEventType.EFFECT_REQUESTED,
    )


def _crash_fake_apply(root: str, point: str) -> None:
    def crash_at_boundary(observed: str, _: Path) -> None:
        if observed == point:
            os._exit(CRASH_EXIT_CODE)

    FakeTaskPort(
        root,
        clock=lambda: PORT_FIXED_TIME,
        failpoint=crash_at_boundary,
    ).apply(_fake_request())


def _recover_fake_apply(root: str, queue: multiprocessing.Queue) -> None:
    request = _fake_request()
    port = FakeTaskPort(root, clock=lambda: PORT_FIXED_TIME)
    observed = receipt_from(
        port.lookup(request.identity, EffectOperation.TASK_EXECUTION)
    )
    recovered = (
        receipt_from(port.apply(request))
        if observed.status is EffectStatus.ABSENT
        else observed
    )
    replayed = receipt_from(port.apply(request))
    queue.put(
        (
            observed.status.value,
            recovered.status.value,
            replayed == recovered,
            len(tuple(port.effects.glob("*.json"))),
            len(tuple(port.receipts.glob("*.json"))),
            recovered.effect_hash,
        )
    )


def _literal_trigger_names(source_path: Path) -> frozenset[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
    names: set[str] = set()
    nonliteral_lines: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_trigger"
        ):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            names.add(node.args[0].value)
        else:
            nonliteral_lines.append(node.lineno)
    if nonliteral_lines:
        raise AssertionError(
            f"non-literal _trigger calls in {source_path}: {nonliteral_lines}"
        )
    return frozenset(names)


class CrashBoundaryDirectEvidenceTests(unittest.TestCase):
    def _assert_fake_process_crash(
        self,
        point: str,
        *,
        effects_after_crash: int,
        receipts_after_crash: int,
        first_recovery_status: EffectStatus,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "effects"
            context = multiprocessing.get_context("spawn")
            crashed = context.Process(
                target=_crash_fake_apply,
                args=(str(root), point),
            )
            crashed.start()
            crashed.join(timeout=20)
            if crashed.is_alive():
                crashed.kill()
                crashed.join(timeout=5)
                self.fail(f"crash subprocess did not stop at {point}")
            self.assertEqual(CRASH_EXIT_CODE, crashed.exitcode)

            crashed_port = FakeTaskPort(root, clock=lambda: PORT_FIXED_TIME)
            self.assertEqual(
                effects_after_crash,
                len(tuple(crashed_port.effects.glob("*.json"))),
            )
            self.assertEqual(
                receipts_after_crash,
                len(tuple(crashed_port.receipts.glob("*.json"))),
            )

            queue = context.Queue()
            restarted = context.Process(
                target=_recover_fake_apply,
                args=(str(root), queue),
            )
            restarted.start()
            restarted.join(timeout=20)
            if restarted.is_alive():
                restarted.kill()
                restarted.join(timeout=5)
                self.fail(f"recovery subprocess did not finish after {point}")
            self.assertEqual(0, restarted.exitcode)
            result = queue.get(timeout=5)
            queue.close()
            queue.join_thread()

            self.assertEqual(first_recovery_status.value, result[0])
            self.assertEqual(EffectStatus.APPLIED.value, result[1])
            self.assertTrue(result[2])
            self.assertEqual(1, result[3])
            self.assertEqual(1, result[4])
            self.assertIsNotNone(result[5])

            final = receipt_from(
                crashed_port.lookup(
                    _fake_request().identity,
                    EffectOperation.TASK_EXECUTION,
                )
            )
            self.assertEqual(EffectStatus.APPLIED, final.status)
            self.assertEqual(result[5], final.effect_hash)

    def test_before_write_process_crash_recovers_one_effect(self) -> None:
        self._assert_fake_process_crash(
            "before_write",
            effects_after_crash=0,
            receipts_after_crash=0,
            first_recovery_status=EffectStatus.ABSENT,
        )

    def test_after_write_process_crash_reconciles_one_effect(self) -> None:
        self._assert_fake_process_crash(
            "after_write",
            effects_after_crash=1,
            receipts_after_crash=0,
            first_recovery_status=EffectStatus.APPLIED,
        )


class GitCrashBoundaryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.attempts_root = self.root / "attempts"
        attempt_fixtures.initialize_repository(self.repository)
        self.attempts_root.mkdir()
        self.expected = capture_workspace_identity(
            self.repository,
            attempt_fixtures.SCOPES,
        )
        self.base_head = attempt_fixtures.git_text(
            self.repository,
            "rev-parse",
            "HEAD",
        )
        self.base_status = attempt_fixtures.git(
            self.repository,
            "status",
            "--porcelain=v2",
            "-z",
        ).stdout
        self.failpoint = attempt_fixtures.Failpoint()
        self.adapter = self._new_adapter(self.failpoint)
        self.ordinal = 0
        self.initial_worktrees = self._registered_worktrees()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _new_adapter(
        self,
        failpoint: attempt_fixtures.Failpoint | None = None,
    ) -> GitWorktreeAdapter:
        return GitWorktreeAdapter(
            self.repository,
            self.attempts_root,
            self.expected,
            clock=lambda: attempt_fixtures.FIXED_TIME,
            failpoint=failpoint,
        )

    def _effect(
        self,
        command: object,
        operation: EffectOperation,
        object_type: EffectObjectType,
        event_type: JournalEventType,
    ) -> PreparedEffect:
        self.ordinal += 1
        return attempt_fixtures.prepared_effect(
            self.adapter,
            command,
            operation=operation,
            object_type=object_type,
            event_type=event_type,
            ordinal=self.ordinal,
        )

    def _plan_attempt(
        self,
        task_id: str,
        owned_path: str,
        correlation: str,
    ) -> tuple[AttemptWorktreeCommand, PreparedEffect]:
        command = self.adapter.plan_attempt(
            ExecutionIdentity("WISH-001", 1, task_id, 1, correlation),
            owned_paths=(owned_path,),
            protected_paths=(".github/**",),
            path_case_mode=PathCaseMode.INSENSITIVE,
        )
        effect = self._effect(
            command,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.WORKTREE,
            JournalEventType.EFFECT_REQUESTED,
        )
        return command, effect

    def _create_attempt(
        self,
        task_id: str,
        owned_path: str,
        correlation: str,
    ) -> AttemptWorktree:
        _, effect = self._plan_attempt(task_id, owned_path, correlation)
        result = self.adapter.create_attempt(effect)
        self.assertEqual(AttemptEffectDisposition.APPLIED, result.disposition)
        self.assertIsNotNone(result.value)
        assert result.value is not None
        return result.value

    def _commit(
        self,
        attempt: AttemptWorktree,
        relative_path: str,
        content: str,
    ) -> str:
        path = Path(attempt.path)
        (path / relative_path).write_text(content, encoding="utf-8")
        attempt_fixtures.git(path, "add", "--", relative_path)
        attempt_fixtures.git(path, "commit", "-m", f"result {attempt.task_id}")
        return attempt_fixtures.git_text(path, "rev-parse", "HEAD")

    def _prepare_stage(
        self,
        operation_id: str,
    ) -> tuple[ResultValidation, StageResultCommand, PreparedEffect]:
        attempt = self._create_attempt(
            "TASK-001",
            "src/a.txt",
            f"CREATE-{operation_id}",
        )
        self._commit(attempt, "src/a.txt", f"staged {operation_id}\n")
        validation = self.adapter.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        self.assertTrue(validation.accepted, validation)
        command = self.adapter.plan_stage(validation, operation_id=operation_id)
        effect = self._effect(
            command,
            EffectOperation.RESULT_STAGE,
            EffectObjectType.RESULT_BUNDLE,
            JournalEventType.EFFECT_REQUESTED,
        )
        return validation, command, effect

    def _stage(self, operation_id: str) -> StagedResult:
        validation, _, effect = self._prepare_stage(operation_id)
        result = self.adapter.stage_result(effect, validation)
        self.assertEqual(AttemptEffectDisposition.APPLIED, result.disposition)
        self.assertIsNotNone(result.value)
        assert result.value is not None
        return result.value

    def _registered_worktrees(self) -> tuple[str, ...]:
        output = attempt_fixtures.git_text(
            self.repository,
            "worktree",
            "list",
            "--porcelain",
        )
        return tuple(
            line.removeprefix("worktree ")
            for line in output.splitlines()
            if line.startswith("worktree ")
        )

    def _target_reflog(self) -> tuple[str, ...]:
        output = attempt_fixtures.git_text(
            self.repository,
            "reflog",
            "show",
            "--format=%H",
            self.expected.target_full_ref,
        )
        return tuple(output.splitlines())

    def _ref_exists(self, reference: str) -> bool:
        return (
            attempt_fixtures.git(
                self.repository,
                "show-ref",
                "--verify",
                "--quiet",
                reference,
                check=False,
            ).returncode
            == 0
        )

    def _assert_target_unchanged(self) -> None:
        self.assertEqual(
            self.base_head,
            attempt_fixtures.git_text(self.repository, "rev-parse", "HEAD"),
        )
        self.assertEqual(
            self.base_status,
            attempt_fixtures.git(
                self.repository,
                "status",
                "--porcelain=v2",
                "-z",
            ).stdout,
        )

    def _assert_attempt_create_restart(
        self,
        point: str,
        *,
        exists_after_crash: bool,
    ) -> None:
        command, effect = self._plan_attempt(
            "TASK-001",
            "src/a.txt",
            f"CREATE-{point.replace('_', '-').upper()}",
        )
        destination = self.attempts_root / command.directory_name
        self.failpoint.point = point
        with self.assertRaisesRegex(GitWorktreeCrash, point):
            self.adapter.create_attempt(effect)
        self.assertTrue(self.failpoint.triggered)
        self.assertEqual(exists_after_crash, destination.exists())

        restarted = self._new_adapter()
        recovered = restarted.create_attempt(effect)
        replayed = restarted.create_attempt(effect)
        self.assertEqual(AttemptEffectDisposition.APPLIED, recovered.disposition)
        self.assertEqual(recovered, replayed)
        self.assertIsNotNone(recovered.value)
        assert recovered.value is not None
        self.assertEqual(destination.resolve(), Path(recovered.value.path).resolve())
        self.assertEqual(
            len(self.initial_worktrees) + 1,
            len(self._registered_worktrees()),
        )
        self._assert_target_unchanged()

    def test_before_attempt_create_restart_creates_once(self) -> None:
        self._assert_attempt_create_restart(
            "before_attempt_create",
            exists_after_crash=False,
        )

    def test_after_attempt_create_restart_reconciles_once(self) -> None:
        self._assert_attempt_create_restart(
            "after_attempt_create",
            exists_after_crash=True,
        )

    def _assert_result_stage_restart(
        self,
        point: str,
        *,
        exists_after_crash: bool,
    ) -> None:
        validation, command, effect = self._prepare_stage(
            f"STAGE-{point.replace('_', '-').upper()}",
        )
        self.failpoint.point = point
        with self.assertRaisesRegex(GitWorktreeCrash, point):
            self.adapter.stage_result(effect, validation)
        self.assertTrue(self.failpoint.triggered)
        self.assertEqual(exists_after_crash, self._ref_exists(command.staged_ref))

        restarted = self._new_adapter()
        recovered = restarted.stage_result(effect, validation)
        replayed = restarted.stage_result(effect, validation)
        self.assertEqual(AttemptEffectDisposition.APPLIED, recovered.disposition)
        self.assertEqual(recovered, replayed)
        self.assertIsNotNone(recovered.value)
        assert recovered.value is not None
        self.assertEqual(
            recovered.value.result_commit_sha,
            attempt_fixtures.git_text(
                self.repository,
                "show-ref",
                "--verify",
                "--hash",
                command.staged_ref,
            ),
        )
        staged_refs = attempt_fixtures.git_text(
            self.repository,
            "for-each-ref",
            "--format=%(refname)",
            "refs/wish-builder/staged",
        ).splitlines()
        self.assertEqual([command.staged_ref], staged_refs)
        self._assert_target_unchanged()

    def test_before_result_stage_restart_stages_once(self) -> None:
        self._assert_result_stage_restart(
            "before_result_stage",
            exists_after_crash=False,
        )

    def test_after_result_stage_restart_reconciles_once(self) -> None:
        self._assert_result_stage_restart(
            "after_result_stage",
            exists_after_crash=True,
        )

    def _prepare_promotion(self) -> tuple[PromotionPlan, PreparedEffect]:
        staged = self._stage("STAGE-PROMOTION")
        service = PromotionService(self.adapter, attempt_fixtures.graph_index())
        plan = service.plan_next(
            (staged,),
            expected_target_sha=self.base_head,
            operation_id="PROMOTE-BOUNDARY",
            coordinator_epoch=1,
        )
        plan = service.bind_acceptance(
            plan,
            (attempt_fixtures.evidence(staged.manifest.identity, 51),),
        )
        effect = self._effect(
            plan.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        return plan, effect

    def _assert_target_promotion_restart(
        self,
        point: str,
        *,
        applied_after_crash: bool,
    ) -> None:
        plan, effect = self._prepare_promotion()
        reflog_before = self._target_reflog()
        service = PromotionService(self.adapter, attempt_fixtures.graph_index())
        self.failpoint.point = point
        with self.assertRaisesRegex(GitWorktreeCrash, point):
            service.apply(effect, plan)
        self.assertTrue(self.failpoint.triggered)
        expected_after_crash = (
            plan.command.candidate_commit_sha if applied_after_crash else self.base_head
        )
        self.assertEqual(
            expected_after_crash,
            attempt_fixtures.git_text(self.repository, "rev-parse", "HEAD"),
        )
        reflog_after_crash = self._target_reflog()
        self.assertEqual(
            len(reflog_before) + int(applied_after_crash),
            len(reflog_after_crash),
        )

        restarted_adapter = self._new_adapter()
        restarted = PromotionService(
            restarted_adapter,
            attempt_fixtures.graph_index(),
        )
        reconciled = restarted.reconcile(plan)
        self.assertEqual(
            PromotionDisposition.APPLIED
            if applied_after_crash
            else PromotionDisposition.ABSENT,
            reconciled.disposition,
        )
        self.assertEqual(
            plan.command.acceptance_evidence,
            reconciled.receipt.evidence,
        )
        recovered = restarted.apply(effect, plan)
        self.assertEqual(PromotionDisposition.APPLIED, recovered.disposition)
        self.assertEqual(
            plan.command.acceptance_evidence,
            recovered.receipt.evidence,
        )
        reflog_after_recovery = self._target_reflog()
        replayed = restarted.apply(effect, plan)
        self.assertEqual(PromotionDisposition.APPLIED, replayed.disposition)
        self.assertEqual(reflog_after_recovery, self._target_reflog())
        self.assertEqual(len(reflog_before) + 1, len(reflog_after_recovery))
        self.assertEqual(
            plan.command.candidate_commit_sha,
            attempt_fixtures.git_text(self.repository, "rev-parse", "HEAD"),
        )
        self.assertEqual(
            self.base_head,
            attempt_fixtures.git_text(
                self.repository,
                "rev-parse",
                f"{plan.command.candidate_commit_sha}^",
            ),
        )
        self.assertEqual(
            plan.command.source_commit_sha,
            attempt_fixtures.git_text(
                self.repository,
                "show-ref",
                "--verify",
                "--hash",
                plan.command.staged_ref,
            ),
        )

    def test_before_target_promotion_restart_promotes_once(self) -> None:
        self._assert_target_promotion_restart(
            "before_target_promotion",
            applied_after_crash=False,
        )

    def test_after_target_promotion_restart_reconciles_once(self) -> None:
        self._assert_target_promotion_restart(
            "after_target_promotion",
            applied_after_crash=True,
        )

    def _prepare_cleanup(
        self,
    ) -> tuple[
        AttemptWorktree,
        AttemptWorktree,
        CleanupService,
        CleanupPlan,
        PreparedEffect,
    ]:
        attempt = self._create_attempt(
            "TASK-001",
            "src/a.txt",
            "CREATE-CLEANUP-TARGET",
        )
        head = self._commit(attempt, "src/a.txt", "cleanup target\n")
        sibling = self._create_attempt(
            "TASK-002",
            "src/b.txt",
            "CREATE-CLEANUP-SIBLING",
        )
        candidate = CleanupCandidate(
            attempt,
            head,
            (attempt_fixtures.evidence(attempt.identity, 71),),
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        service = CleanupService(
            self.adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: attempt_fixtures.FIXED_TIME,
        )
        plan = service.plan(
            candidate,
            operation_id="CLEANUP-BOUNDARY",
            coordinator_epoch=1,
        )
        effect = self._effect(
            plan.command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
            JournalEventType.CLEANUP_REQUESTED,
        )
        return attempt, sibling, service, plan, effect

    def _assert_attempt_remove_restart(
        self,
        point: str,
        *,
        exists_after_crash: bool,
    ) -> None:
        attempt, sibling, service, plan, effect = self._prepare_cleanup()
        sibling_head = attempt_fixtures.git_text(
            Path(sibling.path),
            "rev-parse",
            "HEAD",
        )
        self.failpoint.point = point
        with self.assertRaisesRegex(GitWorktreeCrash, point):
            service.apply(effect, plan)
        self.assertTrue(self.failpoint.triggered)
        self.assertEqual(exists_after_crash, Path(attempt.path).exists())

        restarted = CleanupService(
            self._new_adapter(),
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: attempt_fixtures.FIXED_TIME,
        )
        recovered = restarted.apply(effect, plan)
        self.assertEqual(
            CleanupDisposition.REMOVED
            if exists_after_crash
            else CleanupDisposition.ALREADY_ABSENT,
            recovered.disposition,
        )
        replayed = restarted.apply(effect, plan)
        self.assertEqual(CleanupDisposition.ALREADY_ABSENT, replayed.disposition)
        self.assertFalse(Path(attempt.path).exists())
        self.assertTrue(Path(sibling.path).exists())
        self.assertEqual(
            sibling_head,
            attempt_fixtures.git_text(Path(sibling.path), "rev-parse", "HEAD"),
        )
        self.assertEqual(
            len(self.initial_worktrees) + 1,
            len(self._registered_worktrees()),
        )
        self._assert_target_unchanged()

    def test_before_attempt_remove_restart_removes_only_target(self) -> None:
        self._assert_attempt_remove_restart(
            "before_attempt_remove",
            exists_after_crash=True,
        )

    def test_after_attempt_remove_restart_reconciles_only_target_absent(self) -> None:
        self._assert_attempt_remove_restart(
            "after_attempt_remove",
            exists_after_crash=False,
        )


class CrashBoundaryRegistryTests(unittest.TestCase):
    def test_registry_exactly_matches_production_crash_failpoints(self) -> None:
        for subsystem, source_path in SOURCE_BY_SUBSYSTEM.items():
            with self.subTest(subsystem=subsystem):
                production_names = _literal_trigger_names(source_path)
                registered_names = frozenset(
                    boundary.failpoint_name
                    for boundary in CRASH_BOUNDARIES
                    if boundary.subsystem == subsystem
                )
                self.assertSetEqual(production_names, registered_names)

        self.assertSetEqual(
            set(SOURCE_BY_SUBSYSTEM),
            {boundary.subsystem for boundary in CRASH_BOUNDARIES},
        )

    def test_registry_identity_and_evidence_contracts_are_unique(self) -> None:
        self.assertIsInstance(CRASH_BOUNDARIES, tuple)
        self.assertTrue(CRASH_BOUNDARIES)
        self.assertTrue(all(type(item) is CrashBoundary for item in CRASH_BOUNDARIES))

        boundary_ids = [item.boundary_id for item in CRASH_BOUNDARIES]
        failpoint_keys = [
            (item.subsystem, item.failpoint_name) for item in CRASH_BOUNDARIES
        ]
        test_ids = [item.test_id for item in CRASH_BOUNDARIES if item.test_id]
        self.assertEqual(len(boundary_ids), len(set(boundary_ids)))
        self.assertEqual(len(failpoint_keys), len(set(failpoint_keys)))
        self.assertEqual(len(test_ids), len(set(test_ids)))

        allowed_evidence = set(get_args(EvidenceKind))
        for boundary in CRASH_BOUNDARIES:
            with self.subTest(boundary=boundary.boundary_id):
                self.assertRegex(boundary.boundary_id, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertTrue(boundary.failpoint_name)
                self.assertTrue(boundary.semantic_boundary.strip())
                self.assertTrue(boundary.expected_recovery.strip())
                if boundary.proven:
                    self.assertIn(boundary.evidence_kind, allowed_evidence)
                    self.assertIsNotNone(boundary.test_id)
                    self.assertFalse(
                        boundary.expected_recovery.startswith(UNPROVEN_PREFIX)
                    )
                else:
                    self.assertIsNone(boundary.evidence_kind)
                    self.assertIsNone(boundary.test_id)
                    self.assertTrue(
                        boundary.expected_recovery.startswith(UNPROVEN_PREFIX)
                    )

    def test_every_claimed_test_is_loadable_and_at_the_declared_level(self) -> None:
        for boundary in CRASH_BOUNDARIES:
            if not boundary.proven:
                continue
            with self.subTest(boundary=boundary.boundary_id):
                assert boundary.test_id is not None
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromName(boundary.test_id)
                self.assertEqual([], loader.errors)
                self.assertGreater(suite.countTestCases(), 0)

                if boundary.evidence_kind == "subprocess_e2e":
                    self.assertTrue(
                        boundary.test_id.startswith("tests.e2e."),
                        boundary.test_id,
                    )
                    if boundary.subsystem == "git_worktree":
                        self.assertTrue(
                            boundary.test_id.startswith(
                                "tests.e2e.test_process_exit_recovery."
                                "ProcessExitRecoveryE2ETests."
                            ),
                            boundary.test_id,
                        )
                else:
                    self.assertFalse(
                        boundary.test_id.startswith("tests.e2e.test_m1_workflow.")
                    )

    def test_unproven_boundaries_do_not_make_success_claims(self) -> None:
        success_claim = re.compile(
            r"\b(successful recovery|recovery (?:is )?proven|recovery succeeds?)\b",
            re.IGNORECASE,
        )
        for boundary in CRASH_BOUNDARIES:
            if boundary.proven:
                continue
            with self.subTest(boundary=boundary.boundary_id):
                requirement = boundary.expected_recovery.removeprefix(UNPROVEN_PREFIX)
                self.assertNotRegex(requirement, success_claim)


if __name__ == "__main__":
    unittest.main()
