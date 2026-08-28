from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from wish_builder.adapters.git_identity import capture_workspace_identity
from wish_builder.adapters.git_worktree import (
    AttemptEffectDisposition,
    AttemptWorktree,
    GitBoundaryError,
    GitTreeEntry,
    GitWorktreeAdapter,
    GitWorktreeCrash,
    StageResultCommand,
    StagedResult,
)
from wish_builder.contracts import (
    ActorType,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    PathCaseMode,
    Requirement,
    RequirementStatus,
    RiskLevel,
    RuntimeReasonCode,
    Task,
    TaskStatus,
    canonical_json_bytes,
)
from wish_builder.contracts.models import ExecutionManifest
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupDisposition,
    CleanupPlan,
    CleanupService,
)
from wish_builder.services.journal import AppendResult, AppendStatus, JournalHead
from wish_builder.services.ports import PreparedEffect
from wish_builder.services.promotion import (
    PromotionCommand,
    PromotionDisposition,
    PromotionPlan,
    PromotionService,
)


FIXED_TIME = "2026-08-19T01:00:00Z"
GENESIS_HASH = "sha256:" + "0" * 64
SCOPES = (".github/**", "docs/**", "src/a.txt", "src/b.txt")


def git(
    repository: Path,
    *arguments: str,
    input_data: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "946684800 +0000",
            "GIT_AUTHOR_EMAIL": "tests@wish-builder.invalid",
            "GIT_AUTHOR_NAME": "Wish Builder Tests",
            "GIT_COMMITTER_DATE": "946684800 +0000",
            "GIT_COMMITTER_EMAIL": "tests@wish-builder.invalid",
            "GIT_COMMITTER_NAME": "Wish Builder Tests",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_data,
        capture_output=True,
        check=False,
        env=environment,
        shell=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.decode(errors="replace"))
    return completed


def git_text(repository: Path, *arguments: str) -> str:
    return git(repository, *arguments).stdout.decode("utf-8").strip()


def initialize_repository(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Wish Builder Tests")
    git(path, "config", "user.email", "tests@wish-builder.invalid")
    for relative, content in (
        ("src/a.txt", "a-base\n"),
        ("src/b.txt", "b-base\n"),
        ("docs/outside.txt", "outside\n"),
        (".github/protected.txt", "protected\n"),
    ):
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")


def command_hash(command: object) -> str:
    primitive = command.to_primitive()  # type: ignore[attr-defined]
    return "sha256:" + hashlib.sha256(canonical_json_bytes(primitive)).hexdigest()


def prepared_effect(
    adapter: GitWorktreeAdapter,
    command: object,
    *,
    operation: EffectOperation,
    object_type: EffectObjectType,
    event_type: JournalEventType,
    ordinal: int,
) -> PreparedEffect:
    if hasattr(command, "identity"):
        source = command.identity
        identity = ExecutionIdentity(
            source.run_id,
            source.coordinator_epoch,
            source.task_id,
            source.attempt,
            command.operation_id,
        )
    else:
        identity = ExecutionIdentity(
            command.run_id,
            command.coordinator_epoch,
            command.task_id,
            command.attempt,
            command.operation_id,
        )
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-{ordinal:03d}",
        event_type=event_type,
        identity=identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=FIXED_TIME,
        previous_event_hash=GENESIS_HASH,
        payload=EffectRequestPayload(
            operation,
            adapter=adapter_kind(),
            object_type=object_type,
            normalized_target_hash=adapter.expected_workspace.workspace_hash,
            request_payload_hash=command_hash(command),
            expected_sequence=0,
            fencing_token=identity.coordinator_epoch,
        ),
    )
    appended = AppendResult(
        AppendStatus.COMMITTED,
        JournalHead(event.sequence, event.event_hash),
        event,
    )
    return PreparedEffect.from_append_result(appended, command)


def adapter_kind():
    from wish_builder.contracts import AdapterKind

    return AdapterKind.GIT


def evidence(identity: ExecutionIdentity, ordinal: int = 1) -> EvidenceRef:
    raw = f"evidence-{ordinal}".encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return EvidenceRef(
        1,
        digest,
        len(raw),
        EvidenceType.GIT,
        EvidenceProducer(identity, external_object_id=f"attempt-evidence-{ordinal}"),
        FIXED_TIME,
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        EvidenceRole.REQUIRED,
        digest,
    )


def graph_index() -> GraphIndex:
    requirement = Requirement(
        "REQ-001",
        "Exercise deterministic promotion",
        RequirementStatus.APPROVED,
    )
    tasks = tuple(
        Task(
            id=f"TASK-{ordinal:03d}",
            title=f"Task {ordinal}",
            requirement_ids=("REQ-001",),
            depends_on=(),
            owned_paths=(f"src/{'a' if ordinal == 1 else 'b'}.txt",),
            acceptance_criteria=("passes",),
            regression_commands=("test",),
            rollback="revert",
            wave=1,
            risk=RiskLevel.LOW,
            status=TaskStatus.PR_OPEN,
        )
        for ordinal in (1, 2)
    )
    manifest = ExecutionManifest(
        1,
        "WISH-001",
        "test promotion",
        "main",
        (requirement,),
        tasks,
        max_concurrency=2,
    )
    return GraphIndex.compile(manifest)


class Failpoint:
    def __init__(self) -> None:
        self.point: str | None = None
        self.triggered = False

    def __call__(self, point: str, _: Path) -> None:
        if point == self.point and not self.triggered:
            self.triggered = True
            raise GitWorktreeCrash(point)


class AttemptBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.attempts_root = self.root / "attempts"
        initialize_repository(self.repository)
        self.attempts_root.mkdir()
        self.expected = capture_workspace_identity(self.repository, SCOPES)
        self.failpoint = Failpoint()
        self.adapter = GitWorktreeAdapter(
            self.repository,
            self.attempts_root,
            self.expected,
            clock=lambda: FIXED_TIME,
            failpoint=self.failpoint,
        )
        self.ordinal = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def effect(
        self,
        command: object,
        operation: EffectOperation,
        object_type: EffectObjectType,
        event_type: JournalEventType,
    ) -> PreparedEffect:
        self.ordinal += 1
        return prepared_effect(
            self.adapter,
            command,
            operation=operation,
            object_type=object_type,
            event_type=event_type,
            ordinal=self.ordinal,
        )

    def create_attempt(
        self,
        task_id: str,
        owned_path: str,
        *,
        correlation: str,
        protected: tuple[str, ...] = (".github/**",),
    ) -> AttemptWorktree:
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            task_id,
            1,
            correlation,
        )
        command = self.adapter.plan_attempt(
            identity,
            owned_paths=(owned_path,),
            protected_paths=protected,
            path_case_mode=PathCaseMode.INSENSITIVE,
        )
        effect = self.effect(
            command,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.WORKTREE,
            JournalEventType.EFFECT_REQUESTED,
        )
        result = self.adapter.create_attempt(effect)
        self.assertEqual(
            AttemptEffectDisposition.APPLIED,
            result.disposition,
            result,
        )
        assert result.value is not None
        return result.value

    def commit(self, attempt: AttemptWorktree, relative: str, content: str) -> str:
        path = Path(attempt.path)
        (path / relative).write_text(content, encoding="utf-8")
        git(path, "add", "--", relative)
        git(path, "commit", "-m", f"result {attempt.task_id}")
        return git_text(path, "rev-parse", "HEAD")

    def stage(self, attempt: AttemptWorktree, operation: str) -> StagedResult:
        validation = self.adapter.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        self.assertTrue(validation.accepted, validation)
        command = self.adapter.plan_stage(validation, operation_id=operation)
        effect = self.effect(
            command,
            EffectOperation.RESULT_STAGE,
            EffectObjectType.RESULT_BUNDLE,
            JournalEventType.EFFECT_REQUESTED,
        )
        staged = self.adapter.stage_result(effect, validation)
        self.assertEqual(AttemptEffectDisposition.APPLIED, staged.disposition)
        assert staged.value is not None
        return staged.value

    def test_attempt_is_fresh_isolated_and_requires_exact_durable_request(self) -> None:
        target_head = git_text(self.repository, "rev-parse", "HEAD")
        target_status = git(self.repository, "status", "--porcelain=v2", "-z").stdout
        identity = ExecutionIdentity(
            "WISH-001", 1, "TASK-001", 1, "CREATE-001"
        )
        command = self.adapter.plan_attempt(
            identity,
            owned_paths=("src/a.txt",),
            protected_paths=(".github/**",),
            path_case_mode=PathCaseMode.INSENSITIVE,
        )

        with self.assertRaisesRegex(TypeError, "PreparedEffect"):
            self.adapter.create_attempt(command)  # type: ignore[arg-type]
        effect = self.effect(
            command,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.WORKTREE,
            JournalEventType.EFFECT_REQUESTED,
        )
        first = self.adapter.create_attempt(effect)
        replay = self.adapter.create_attempt(effect)

        self.assertEqual(AttemptEffectDisposition.APPLIED, first.disposition)
        self.assertEqual(first.value, replay.value)
        assert first.value is not None
        self.assertNotEqual(self.repository, Path(first.value.path))
        self.assertEqual(target_head, git_text(Path(first.value.path), "rev-parse", "HEAD"))
        self.assertEqual(target_head, git_text(self.repository, "rev-parse", "HEAD"))
        self.assertEqual(
            target_status,
            git(self.repository, "status", "--porcelain=v2", "-z").stdout,
        )

    def test_result_manifest_pins_owned_change_base_tree_and_stage_ref(self) -> None:
        attempt = self.create_attempt(
            "TASK-001",
            "src/a.txt",
            correlation="CREATE-001",
        )
        result_commit = self.commit(attempt, "src/a.txt", "implemented\n")

        validation = self.adapter.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        self.assertTrue(validation.accepted, validation)
        assert validation.manifest is not None
        self.assertEqual(result_commit, validation.manifest.result_commit_sha)
        self.assertEqual(attempt.base_commit_sha, validation.manifest.base_commit_sha)
        self.assertEqual(("src/a.txt",), tuple(
            item.path for item in validation.manifest.changed_paths
        ))
        self.assertNotEqual(
            validation.manifest.base_tree_sha,
            validation.manifest.result_tree_sha,
        )

        staged = self.stage(attempt, "STAGE-001")
        self.assertEqual(
            result_commit,
            git_text(self.repository, "show-ref", "--verify", "--hash", staged.staged_ref),
        )

    def test_result_rejects_dirty_unowned_protected_wrong_parent_and_link_mode(self) -> None:
        cases = (
            ("TASK-001", "src/a.txt", "docs/outside.txt", "unowned"),
            ("TASK-002", ".github/**", ".github/protected.txt", "protected"),
        )
        for index, (task, owned, changed, reason) in enumerate(cases, 1):
            with self.subTest(reason=reason):
                attempt = self.create_attempt(
                    task,
                    owned,
                    correlation=f"CREATE-{index:03d}",
                    protected=(".github/**",),
                )
                self.commit(attempt, changed, f"{reason}-changed\n")
                validation = self.adapter.validate_result(
                    attempt,
                    process_tree_terminated=True,
                )
                self.assertFalse(validation.accepted)
                self.assertEqual(RuntimeReasonCode.GIT_STATE_CONFLICT, validation.reason_code)
                self.assertTrue(any(reason in item for item in validation.violations))

        dirty = self.create_attempt(
            "TASK-001",
            "src/a.txt",
            correlation="CREATE-010",
        )
        self.commit(dirty, "src/a.txt", "committed\n")
        (Path(dirty.path) / "src" / "untracked.txt").write_text(
            "dirty\n", encoding="utf-8"
        )
        self.assertFalse(
            self.adapter.validate_result(
                dirty,
                process_tree_terminated=True,
            ).accepted
        )

        wrong_parent = self.create_attempt(
            "TASK-002",
            "src/b.txt",
            correlation="CREATE-011",
        )
        self.commit(wrong_parent, "src/b.txt", "first\n")
        self.commit(wrong_parent, "src/b.txt", "second\n")
        parent_validation = self.adapter.validate_result(
            wrong_parent,
            process_tree_terminated=True,
        )
        self.assertFalse(parent_validation.accepted)
        self.assertIn("result_parent_mismatch", parent_validation.violations)

        link = self.create_attempt(
            "TASK-001",
            "src/a.txt",
            correlation="CREATE-012",
        )
        blob = git(
            Path(link.path),
            "hash-object",
            "-w",
            "--stdin",
            input_data=b"../outside\n",
        ).stdout.decode().strip()
        git(
            Path(link.path),
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},src/link",
        )
        git(Path(link.path), "commit", "-m", "link mode")
        link_validation = self.adapter.validate_result(
            link,
            process_tree_terminated=True,
        )
        self.assertFalse(link_validation.accepted)
        self.assertIn("git_tree_mode_or_type_rejected", link_validation.violations)

        with self.assertRaisesRegex(Exception, "canonical|escape|portable"):
            self.adapter.plan_attempt(
                ExecutionIdentity(
                    "WISH-001", 1, "TASK-001", 2, "CREATE-099"
                ),
                owned_paths=("../escape",),
            )
        for hostile_path in ("src/windows?.txt", "src/windows*.txt"):
            with self.subTest(hostile_path=hostile_path), self.assertRaisesRegex(
                GitBoundaryError,
                "git_path_not_portable",
            ):
                GitTreeEntry(hostile_path, "100644", "0" * 40, 0)

    def test_repository_base_tree_identity_and_case_collisions_fail_closed(self) -> None:
        attempt = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-015"
        )
        self.commit(attempt, "src/a.txt", "identity\n")

        wrong_tree = replace(attempt, base_tree_sha="0" * 40)
        tree_result = self.adapter.validate_result(
            wrong_tree,
            process_tree_terminated=True,
        )
        self.assertFalse(tree_result.accepted)
        self.assertIn("base_tree_mismatch", tree_result.violations)

        wrong_repository = replace(
            attempt,
            local_repository_id="sha256:" + "0" * 64,
        )
        repository_result = self.adapter.validate_result(
            wrong_repository,
            process_tree_terminated=True,
        )
        self.assertFalse(repository_result.accepted)
        self.assertIn("attempt_identity_changed", repository_result.violations)

        collision = self.create_attempt(
            "TASK-002", "src/b.txt", correlation="CREATE-016"
        )
        collision_root = Path(collision.path)
        git(collision_root, "config", "core.ignorecase", "false")
        blob = git(
            collision_root,
            "hash-object",
            "-w",
            "--stdin",
            input_data=b"case collision\n",
        ).stdout.decode().strip()
        git(
            collision_root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},src/A.txt",
        )
        git(collision_root, "commit", "-m", "case collision")
        collision_result = self.adapter.validate_result(
            collision,
            process_tree_terminated=True,
        )
        self.assertFalse(collision_result.accepted)
        self.assertIn("git_path_collision", collision_result.violations)

        identity = ExecutionIdentity(
            "WISH-001", 1, "TASK-001", 2, "CREATE-017"
        )
        command = self.adapter.plan_attempt(
            identity,
            owned_paths=("src/a.txt",),
            protected_paths=(".github/**",),
            path_case_mode=PathCaseMode.INSENSITIVE,
        )
        effect = self.effect(
            command,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.WORKTREE,
            JournalEventType.EFFECT_REQUESTED,
        )
        (self.repository / "src/a.txt").write_text("target drift\n")
        git(self.repository, "add", "src/a.txt")
        git(self.repository, "commit", "-m", "target drift")
        with self.assertRaisesRegex(GitBoundaryError, "workspace_drift"):
            self.adapter.create_attempt(effect)
        self.assertFalse((self.attempts_root / command.directory_name).exists())

    def test_promotion_uses_graph_order_and_produces_linear_owned_tree(self) -> None:
        first = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-021"
        )
        second = self.create_attempt(
            "TASK-002", "src/b.txt", correlation="CREATE-022"
        )
        self.commit(first, "src/a.txt", "a-result\n")
        self.commit(second, "src/b.txt", "b-result\n")
        staged_first = self.stage(first, "STAGE-021")
        staged_second = self.stage(second, "STAGE-022")
        service = PromotionService(self.adapter, graph_index())

        plan_one = service.plan_next(
            (staged_second, staged_first),
            expected_target_sha=self.expected.base_commit_sha,
            operation_id="PROMOTE-021",
            coordinator_epoch=1,
        )
        unbound_effect = self.effect(
            plan_one.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        with self.assertRaisesRegex(ValueError, "acceptance evidence"):
            service.apply(unbound_effect, plan_one)
        self.assertEqual(
            self.expected.base_commit_sha,
            git_text(self.repository, "rev-parse", "HEAD"),
        )
        plan_one = service.bind_acceptance(
            plan_one,
            (evidence(staged_first.manifest.identity, 21),),
        )
        self.assertEqual("TASK-001", plan_one.command.task_id)
        effect_one = self.effect(
            plan_one.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        observed_one = service.apply(effect_one, plan_one)
        self.assertEqual(PromotionDisposition.APPLIED, observed_one.disposition)
        first_head = git_text(self.repository, "rev-parse", "HEAD")
        self.assertEqual(plan_one.command.candidate_commit_sha, first_head)

        plan_two = service.plan_next(
            (staged_second,),
            expected_target_sha=first_head,
            operation_id="PROMOTE-022",
            coordinator_epoch=1,
        )
        plan_two = service.bind_acceptance(
            plan_two,
            (evidence(staged_second.manifest.identity, 22),),
        )
        effect_two = self.effect(
            plan_two.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        observed_two = service.apply(effect_two, plan_two)
        self.assertEqual(PromotionDisposition.APPLIED, observed_two.disposition)
        self.assertEqual(
            first_head,
            git_text(self.repository, "rev-parse", "HEAD^"),
        )
        self.assertEqual("a-result\n", (self.repository / "src/a.txt").read_text())
        self.assertEqual("b-result\n", (self.repository / "src/b.txt").read_text())

    def test_stage_and_promotion_recompute_untrusted_manifests_and_candidates(self) -> None:
        attempt = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-025"
        )
        self.commit(attempt, "src/a.txt", "verified-result\n")
        validation = self.adapter.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        self.assertTrue(validation.accepted)
        assert validation.manifest is not None

        forged_manifest = replace(
            validation.manifest,
            total_blob_bytes=validation.manifest.total_blob_bytes + 1,
        )
        forged_validation = replace(validation, manifest=forged_manifest)
        forged_stage_command = self.adapter.plan_stage(
            forged_validation,
            operation_id="STAGE-025-FORGED",
        )
        forged_stage_effect = self.effect(
            forged_stage_command,
            EffectOperation.RESULT_STAGE,
            EffectObjectType.RESULT_BUNDLE,
            JournalEventType.EFFECT_REQUESTED,
        )
        with self.assertRaisesRegex(
            GitBoundaryError,
            "result_manifest_size_mismatch",
        ):
            self.adapter.stage_result(forged_stage_effect, forged_validation)
        self.assertFalse(
            git(
                self.repository,
                "show-ref",
                "--verify",
                forged_stage_command.staged_ref,
                check=False,
            ).stdout
        )

        staged = self.stage(attempt, "STAGE-025")
        service = PromotionService(self.adapter, graph_index())
        valid_plan = service.plan_next(
            (staged,),
            expected_target_sha=self.expected.base_commit_sha,
            operation_id="PROMOTE-025",
            coordinator_epoch=1,
        )
        valid_plan = service.bind_acceptance(
            valid_plan,
            (evidence(staged.manifest.identity, 25),),
        )
        forged_commit = git(
            self.repository,
            "commit-tree",
            attempt.base_tree_sha,
            "-p",
            attempt.base_commit_sha,
            input_data=b"forged candidate\n",
        ).stdout.decode("ascii").strip()
        forged_command = replace(
            valid_plan.command,
            candidate_commit_sha=forged_commit,
            candidate_tree_sha=attempt.base_tree_sha,
        )
        forged_plan = PromotionPlan(forged_command, staged)
        forged_effect = self.effect(
            forged_command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        forged_observation = service.apply(forged_effect, forged_plan)
        self.assertEqual(PromotionDisposition.UNKNOWN, forged_observation.disposition)
        self.assertIn(
            "promotion_candidate_tree_mismatch",
            forged_observation.details,
        )
        self.assertEqual(
            self.expected.base_commit_sha,
            git_text(self.repository, "rev-parse", "HEAD"),
        )

    def test_crash_after_target_update_reconciles_without_second_promotion(self) -> None:
        attempt = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-031"
        )
        self.commit(attempt, "src/a.txt", "crash-result\n")
        staged = self.stage(attempt, "STAGE-031")
        service = PromotionService(self.adapter, graph_index())
        plan = service.plan_next(
            (staged,),
            expected_target_sha=self.expected.base_commit_sha,
            operation_id="PROMOTE-031",
            coordinator_epoch=1,
        )
        plan = service.bind_acceptance(
            plan,
            (evidence(staged.manifest.identity, 31),),
        )
        effect = self.effect(
            plan.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )
        self.failpoint.point = "after_target_promotion"
        with self.assertRaises(GitWorktreeCrash):
            service.apply(effect, plan)

        self.assertEqual(plan.command.candidate_commit_sha, git_text(self.repository, "rev-parse", "HEAD"))
        restarted = PromotionService(self.adapter, graph_index())
        (self.repository / "src/a.txt").write_text(
            "dirty after promotion\n",
            encoding="utf-8",
        )
        unresolved = restarted.reconcile(plan)
        self.assertEqual(PromotionDisposition.UNKNOWN, unresolved.disposition)
        self.assertIn("promoted_target_worktree_dirty", unresolved.details)

        (self.repository / "src/a.txt").write_text(
            "crash-result\n",
            encoding="utf-8",
        )
        reconciled = restarted.reconcile(plan)
        self.assertEqual(PromotionDisposition.APPLIED, reconciled.disposition)
        self.assertEqual(
            staged.result_commit_sha,
            git_text(self.repository, "show-ref", "--verify", "--hash", staged.staged_ref),
        )

    def test_repository_lock_coalesces_concurrent_exact_promotion_replays(self) -> None:
        attempt = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-035"
        )
        self.commit(attempt, "src/a.txt", "concurrent\n")
        staged = self.stage(attempt, "STAGE-035")
        first_service = PromotionService(self.adapter, graph_index())
        second_service = PromotionService(self.adapter, graph_index())
        plan = first_service.plan_next(
            (staged,),
            expected_target_sha=self.expected.base_commit_sha,
            operation_id="PROMOTE-035",
            coordinator_epoch=1,
        )
        plan = first_service.bind_acceptance(
            plan,
            (evidence(staged.manifest.identity, 35),),
        )
        effect = self.effect(
            plan.command,
            EffectOperation.RESULT_PROMOTION,
            EffectObjectType.GIT_REF,
            JournalEventType.PROMOTION_REQUESTED,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            observations = tuple(
                future.result(timeout=60)
                for future in (
                    pool.submit(first_service.apply, effect, plan),
                    pool.submit(second_service.apply, effect, plan),
                )
            )
        self.assertEqual(
            (PromotionDisposition.APPLIED, PromotionDisposition.APPLIED),
            tuple(item.disposition for item in observations),
        )
        self.assertEqual(
            plan.command.candidate_commit_sha,
            git_text(self.repository, "rev-parse", "HEAD"),
        )
        self.assertEqual(
            self.expected.base_commit_sha,
            git_text(self.repository, "rev-parse", "HEAD^"),
        )

    def test_cleanup_removes_only_reconciled_terminated_unchanged_attempt(self) -> None:
        safe = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-041"
        )
        safe_head = self.commit(safe, "src/a.txt", "safe\n")
        safe_evidence = (evidence(safe.identity),)
        cleanup = CleanupService(
            self.adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: FIXED_TIME,
        )
        safe_candidate = CleanupCandidate(
            safe,
            safe_head,
            safe_evidence,
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        safe_plan = cleanup.plan(
            safe_candidate,
            operation_id="CLEANUP-041",
            coordinator_epoch=1,
        )
        safe_effect = self.effect(
            safe_plan.command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
            JournalEventType.CLEANUP_REQUESTED,
        )
        removed = cleanup.apply(safe_effect, safe_plan)
        self.assertEqual(CleanupDisposition.REMOVED, removed.disposition)
        self.assertFalse(Path(safe.path).exists())
        self.assertEqual(safe_evidence, removed.evidence)

        active = self.create_attempt(
            "TASK-002", "src/b.txt", correlation="CREATE-042"
        )
        active_candidate = CleanupCandidate(
            active,
            active.base_commit_sha,
            (evidence(active.identity, 2),),
            reconciliation_complete=True,
            process_tree_terminated=False,
            outcome_known=True,
        )
        active_plan = cleanup.plan(
            active_candidate,
            operation_id="CLEANUP-042",
            coordinator_epoch=1,
        )
        active_effect = self.effect(
            active_plan.command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
            JournalEventType.CLEANUP_REQUESTED,
        )
        retained = cleanup.apply(active_effect, active_plan)
        self.assertEqual(CleanupDisposition.QUARANTINED, retained.disposition)
        self.assertEqual(
            RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
            retained.reason_code,
        )
        self.assertTrue(Path(active.path).exists())

    def test_cleanup_quarantines_changed_identity_unknown_outcome_and_disk_pressure(self) -> None:
        changed = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-051"
        )
        candidate = CleanupCandidate(
            changed,
            changed.base_commit_sha,
            (evidence(changed.identity, 3),),
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        cleanup = CleanupService(
            self.adapter,
            available_bytes=lambda: 1,
            minimum_free_bytes=100,
            clock=lambda: FIXED_TIME,
        )
        plan = cleanup.plan(
            candidate,
            operation_id="CLEANUP-051",
            coordinator_epoch=1,
        )
        (Path(changed.path) / "src/a.txt").write_text("changed after plan\n")
        effect = self.effect(
            plan.command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
            JournalEventType.CLEANUP_REQUESTED,
        )

        report = cleanup.apply_many(((effect, plan),))
        self.assertEqual(CleanupDisposition.QUARANTINED, report.observations[0].disposition)
        self.assertTrue(report.dispatch_blocked)
        self.assertEqual((changed.external_object_id,), report.retained_object_ids)
        self.assertTrue(Path(changed.path).exists())

        ambiguous = self.create_attempt(
            "TASK-002", "src/b.txt", correlation="CREATE-052"
        )
        ambiguous_candidate = CleanupCandidate(
            ambiguous,
            ambiguous.base_commit_sha,
            (evidence(ambiguous.identity, 4),),
            reconciliation_complete=False,
            process_tree_terminated=True,
            outcome_known=False,
        )
        ambiguous_plan = cleanup.plan(
            ambiguous_candidate,
            operation_id="CLEANUP-052",
            coordinator_epoch=1,
        )
        self.assertEqual(
            RuntimeReasonCode.WORKER_OUTCOME_UNKNOWN,
            ambiguous_plan.quarantine_reason,
        )

        original = Path(ambiguous.path)
        moved = original.with_name(original.name + "-moved")
        original.rename(moved)
        original.mkdir()
        identity_plan = cleanup.plan(
            ambiguous_candidate,
            operation_id="CLEANUP-053",
            coordinator_epoch=1,
        )
        self.assertIsNotNone(identity_plan.quarantine_reason)
        self.assertFalse(identity_plan.inspection.identity_ok)

        empty_report = cleanup.apply_many(())
        self.assertTrue(empty_report.dispatch_blocked)

    def test_cleanup_binds_target_and_cannot_bypass_safety_after_restart(self) -> None:
        attempt = self.create_attempt(
            "TASK-001", "src/a.txt", correlation="CREATE-055"
        )
        head = self.commit(attempt, "src/a.txt", "cleanup candidate\n")
        candidate = CleanupCandidate(
            attempt,
            head,
            (evidence(attempt.identity, 5),),
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        cleanup = CleanupService(
            self.adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: FIXED_TIME,
        )
        plan = cleanup.plan(
            candidate,
            operation_id="CLEANUP-055",
            coordinator_epoch=1,
        )
        self.assertEqual(
            self.adapter.expected_workspace.workspace_hash,
            plan.command.target_workspace_hash,
        )
        effect = self.effect(
            plan.command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
            JournalEventType.CLEANUP_REQUESTED,
        )

        unsafe_candidate = replace(candidate, process_tree_terminated=False)
        with self.assertRaisesRegex(ValueError, "safety evidence"):
            CleanupPlan(
                plan.command,
                unsafe_candidate,
                plan.inspection,
                None,
            )

        (self.repository / "docs/outside.txt").write_text(
            "advanced target\n",
            encoding="utf-8",
        )
        git(self.repository, "add", "docs/outside.txt")
        git(self.repository, "commit", "-m", "advance target")
        restarted_workspace = capture_workspace_identity(self.repository, SCOPES)
        restarted_adapter = GitWorktreeAdapter(
            self.repository,
            self.attempts_root,
            restarted_workspace,
            clock=lambda: FIXED_TIME,
        )
        restarted_cleanup = CleanupService(
            restarted_adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: FIXED_TIME,
        )
        with self.assertRaisesRegex(ValueError, "repository identity"):
            restarted_cleanup.apply(effect, plan)
        self.assertTrue(Path(attempt.path).exists())


if __name__ == "__main__":
    unittest.main()
