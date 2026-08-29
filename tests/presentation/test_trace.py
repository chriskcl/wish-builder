from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import unittest

from tests.contracts.test_manifest_v2 import valid_manifest_v2
from tests.kernel.test_validation import valid_manifest
from wish_builder.contracts.decoder import decode_manifest_primitive
from wish_builder.contracts.manifest_v2_decoder import decode_manifest_v2_primitive
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    AdapterKind,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservation,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RecoveryPayload,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.serialization import canonical_json_bytes, canonical_sha256
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import AttemptProjection, KernelSnapshot
from wish_builder.presentation.trace import (
    TraceExportError,
    TraceExportFault,
    TraceLimits,
    build_trace_projection,
    export_trace,
    render_trace_json,
    render_trace_markdown,
    trace_json_bytes,
    trace_json_sha256,
    trace_markdown_bytes,
    trace_markdown_sha256,
)

NOW = "2026-08-19T03:00:00Z"
ZERO_HASH = "sha256:" + "0" * 64


def hash_ref(number: int) -> str:
    return "sha256:" + f"{number:064x}"


def decoded_v1(raw: dict[str, object] | None = None):
    result = decode_manifest_primitive(valid_manifest() if raw is None else raw)
    if not result.ok or result.value is None:
        raise AssertionError(result.report.render_text())
    return result.value


def decoded_v2(raw: dict[str, object] | None = None):
    result = decode_manifest_v2_primitive(valid_manifest_v2() if raw is None else raw)
    if not result.ok or result.value is None:
        raise AssertionError(result.report.render_text())
    return result.value


def empty_state(manifest):
    snapshot = KernelSnapshot.initial(manifest.run_id, 1, TaskDag.compile(manifest))
    return snapshot, GraphIndex.compile(manifest, snapshot)


def coordinator_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.COORDINATOR,
        "coordinator-001",
        "host-001",
        100,
        "process-start-100",
    )


def human_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.HUMAN,
        "local-account-001",
        "host-001",
        200,
        "process-start-200",
    )


def journal_fixture(manifest):
    request = DecisionRequest(
        command=CommandIdentity(
            schema_version=1,
            command_id="COMMAND-001",
            request_id="REQUEST-001",
            kind=CommandKind.DECIDE,
            expected_sequence=1,
            request_nonce="nonce-001",
            actor=coordinator_actor(),
            source_channel=SourceChannel.COORDINATOR,
            submitted_at=NOW,
        ),
        decision_type=DecisionType.GATE_B,
        candidate_hash=hash_ref(1),
        workspace_hash=hash_ref(2),
        expected_actor_id="local-account-001",
        options=(
            DecisionChoice.APPROVE,
            DecisionChoice.REVISE,
            DecisionChoice.REJECT,
        ),
    )
    first = JournalEvent.create(
        sequence=1,
        event_id="EVENT-001",
        event_type=JournalEventType.DECISION_REQUESTED,
        identity=ExecutionIdentity(manifest.run_id, 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=NOW,
        previous_event_hash=ZERO_HASH,
        payload=DecisionRequestPayload(request),
    )

    decision = DecisionCommand(
        decision_id="DECISION-001",
        request=request,
        choice=DecisionChoice.APPROVE,
        actor=human_actor(),
        source_channel=SourceChannel.DIRECT_CLI,
        decided_at="2026-08-19T03:00:01Z",
    )
    observation = DecisionObservation(
        decision,
        event_sequence=2,
        submission_hash="sha256:" + canonical_sha256(decision.to_primitive()),
    )
    second = JournalEvent.create(
        sequence=2,
        event_id="EVENT-002",
        event_type=JournalEventType.DECISION_OBSERVED,
        identity=ExecutionIdentity(manifest.run_id, 1),
        actor_type=ActorType.HUMAN,
        actor_id="local-account-001",
        recorded_at="2026-08-19T03:00:01Z",
        previous_event_hash=first.event_hash,
        payload=DecisionObservedPayload(observation),
    )

    identity = ExecutionIdentity(
        manifest.run_id,
        1,
        "TASK-001",
        1,
        "CORRELATION-001",
    )
    third = JournalEvent.create(
        sequence=3,
        event_id="EVENT-003",
        event_type=JournalEventType.DISPATCH_REQUESTED,
        identity=identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at="2026-08-19T03:00:02Z",
        previous_event_hash=second.event_hash,
        payload=EffectRequestPayload(
            EffectOperation.WORKER_DISPATCH,
            AdapterKind.TASK,
            EffectObjectType.WORKER,
            hash_ref(3),
            hash_ref(4),
            2,
            1,
        ),
    )

    evidence = EvidenceRef(
        schema_version=1,
        digest=hash_ref(10),
        byte_length=10,
        evidence_type=EvidenceType.RESULT,
        producer=EvidenceProducer(
            identity,
            event_id="EVENT-004",
            external_object_id="worker-result-001",
        ),
        created_at="2026-08-19T03:00:03Z",
        sensitivity=EvidenceSensitivity.INTERNAL,
        render_policy=EvidenceRenderPolicy.TEXT,
        role=EvidenceRole.REQUIRED,
        structured_subject_hash=hash_ref(11),
    )
    receipt = EffectReceipt(
        schema_version=1,
        identity=identity,
        operation=EffectOperation.WORKER_DISPATCH,
        status=EffectStatus.APPLIED,
        observed_at="2026-08-19T03:00:03Z",
        effect_hash=hash_ref(12),
        external_object_id="worker-001",
        evidence=(evidence,),
    )
    fourth = JournalEvent.create(
        sequence=4,
        event_id="EVENT-004",
        event_type=JournalEventType.DISPATCH_OBSERVED,
        identity=identity,
        actor_type=ActorType.ADAPTER,
        actor_id="fake-task-adapter",
        recorded_at="2026-08-19T03:00:03Z",
        previous_event_hash=third.event_hash,
        payload=EffectObservationPayload(AdapterKind.TASK, receipt),
    )
    fifth = JournalEvent.create(
        sequence=5,
        event_id="EVENT-005",
        event_type=JournalEventType.RECOVERY_COMPLETED,
        identity=ExecutionIdentity(manifest.run_id, 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at="2026-08-19T03:00:04Z",
        previous_event_hash=fourth.event_hash,
        payload=RecoveryPayload(4, fourth.event_hash, ("TASK-001",), (evidence,)),
    )
    events = (first, second, third, fourth, fifth)
    initial, _ = empty_state(manifest)
    snapshot = dataclasses.replace(
        initial,
        last_sequence=fifth.sequence,
        last_event_id=fifth.event_id,
        last_event_hash=fifth.event_hash,
    )
    index = GraphIndex.compile(manifest, snapshot)
    return events, snapshot, index, evidence


def forge_event(event: JournalEvent, **changes: object) -> JournalEvent:
    forged = object.__new__(JournalEvent)
    for field in dataclasses.fields(event):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(event, field.name)),
        )
    return forged


class SinglePassEvents:
    def __init__(self, events: tuple[JournalEvent, ...]) -> None:
        self.events = events
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations != 1:
            raise AssertionError("Journal iterable was restarted")
        yield from self.events


class BrokenEvents:
    def __init__(self, first: JournalEvent) -> None:
        self.first = first

    def __iter__(self):
        yield self.first
        raise OSError("injected stream read failure")


class TraceProjectionTests(unittest.TestCase):
    def assert_fault(self, fault: TraceExportFault, callback) -> TraceExportError:
        with self.assertRaises(TraceExportError) as raised:
            callback()
        self.assertIs(fault, raised.exception.fault)
        self.assertEqual(fault.value, raised.exception.reason)
        return raised.exception

    def test_v1_and_v2_manifests_render_from_typed_empty_state(self) -> None:
        for manifest in (decoded_v1(), decoded_v2()):
            with self.subTest(schema_version=manifest.schema_version):
                snapshot, index = empty_state(manifest)
                artifacts = export_trace(manifest, snapshot, index, ())
                value = json.loads(artifacts.json_bytes)

                self.assertEqual(manifest.run_id, value["run"]["run_id"])
                self.assertEqual(
                    manifest.schema_version, value["run"]["manifest_schema_version"]
                )
                self.assertEqual(
                    list(index.topological_order),
                    [task["id"] for task in value["tasks"]],
                )
                self.assertEqual(
                    artifacts.json_bytes, render_trace_json(artifacts.projection)
                )
                self.assertEqual(
                    artifacts.markdown_bytes,
                    render_trace_markdown(artifacts.projection),
                )
                self.assertEqual(
                    "sha256:" + hashlib.sha256(artifacts.json_bytes).hexdigest(),
                    artifacts.json_sha256,
                )
                self.assertTrue(artifacts.json_bytes.endswith(b"\n"))
                self.assertTrue(artifacts.markdown_bytes.endswith(b"\n"))
                self.assertNotIn(b"\r", artifacts.json_bytes)
                self.assertNotIn(b"\r", artifacts.markdown_bytes)

        manifest = decoded_v2()
        snapshot, index = empty_state(manifest)
        source = json.loads(trace_json_bytes(manifest, snapshot, index, ()))["run"][
            "source"
        ]
        self.assertEqual("trellis", source["kind"])
        self.assertEqual(manifest.trellis_revision, source["trellis_revision"])
        tasks = json.loads(trace_json_bytes(manifest, snapshot, index, ()))["tasks"]
        expected_mapping = {
            item.task_id: item.trellis_task_id for item in manifest.task_id_mapping
        }
        self.assertEqual(
            expected_mapping,
            {item["id"]: item["trellis_task_id"] for item in tasks},
        )

    def test_commands_evidence_digests_and_trace_hashes_share_one_projection(
        self,
    ) -> None:
        manifest = decoded_v1()
        events, snapshot, index, evidence = journal_fixture(manifest)
        source = SinglePassEvents(events)
        artifacts = export_trace(manifest, snapshot, index, source)
        value = json.loads(artifacts.json_bytes)

        self.assertEqual(1, source.iterations)
        self.assertEqual([1, 2, 3], [item["sequence"] for item in value["commands"]])
        self.assertEqual(
            [evidence.digest], [item["digest"] for item in value["evidence"]]
        )
        self.assertEqual(5, value["run"]["event_count"])
        self.assertEqual("recovery_completed", value["run"]["last_event_type"])
        self.assertEqual(
            "sha256:"
            + hashlib.sha256(canonical_json_bytes(value["commands"])).hexdigest(),
            value["component_digests"]["commands"],
        )
        self.assertEqual(
            "sha256:"
            + hashlib.sha256(canonical_json_bytes(value["evidence"])).hexdigest(),
            value["component_digests"]["evidence"],
        )
        self.assertIn(b"COMMAND-001", artifacts.markdown_bytes)
        self.assertIn(evidence.digest.encode(), artifacts.markdown_bytes)

        self.assertEqual(
            artifacts.json_sha256,
            trace_json_sha256(manifest, snapshot, index, iter(events)),
        )
        self.assertEqual(
            artifacts.markdown_sha256,
            trace_markdown_sha256(manifest, snapshot, index, iter(events)),
        )
        self.assertEqual(
            artifacts.markdown_bytes,
            trace_markdown_bytes(manifest, snapshot, index, iter(events)),
        )

    def test_transition_evidence_is_sorted_by_digest(self) -> None:
        manifest = decoded_v1()
        initial, _ = empty_state(manifest)
        identity = ExecutionIdentity(manifest.run_id, 1)

        def evidence(number: int) -> EvidenceRef:
            return EvidenceRef(
                schema_version=1,
                digest=hash_ref(number),
                byte_length=number,
                evidence_type=EvidenceType.RESULT,
                producer=EvidenceProducer(identity, event_id="EVENT-TRANSITION"),
                created_at=NOW,
                sensitivity=EvidenceSensitivity.INTERNAL,
                render_policy=EvidenceRenderPolicy.TEXT,
                role=EvidenceRole.REQUIRED,
            )

        later = evidence(20)
        earlier = evidence(10)
        event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-TRANSITION",
            event_type=JournalEventType.RUN_INITIALIZED,
            identity=identity,
            actor_type=ActorType.SYSTEM,
            actor_id="wishctl",
            recorded_at=NOW,
            previous_event_hash=ZERO_HASH,
            payload=TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
                (later, earlier),
            ),
        )
        snapshot = dataclasses.replace(
            initial,
            phase=RuntimeState.PREFLIGHT,
            last_sequence=1,
            last_event_id=event.event_id,
            last_event_hash=event.event_hash,
        )
        index = GraphIndex.compile(manifest, snapshot)
        value = json.loads(trace_json_bytes(manifest, snapshot, index, (event,)))
        self.assertEqual(
            [earlier.digest, later.digest],
            [item["digest"] for item in value["evidence"]],
        )

    def test_task_and_attempt_order_uses_graph_position_then_stable_identity(
        self,
    ) -> None:
        manifest = decoded_v1()
        initial, _ = empty_state(manifest)
        attempts = (
            AttemptProjection(
                "TASK-004", 2, "CORRELATION-004-B", 1, RuntimeState.PLANNED
            ),
            AttemptProjection(
                "TASK-001", 2, "CORRELATION-001-B", 1, RuntimeState.PLANNED
            ),
            AttemptProjection(
                "TASK-001", 1, "CORRELATION-001-A", 1, RuntimeState.PLANNED
            ),
            AttemptProjection(
                "TASK-004", 1, "CORRELATION-004-A", 1, RuntimeState.PLANNED
            ),
        )
        snapshot = dataclasses.replace(initial, attempts=attempts)
        index = GraphIndex.compile(manifest, snapshot)
        value = json.loads(trace_json_bytes(manifest, snapshot, index, ()))

        self.assertEqual(
            list(index.topological_order), [item["id"] for item in value["tasks"]]
        )
        self.assertEqual(
            [("TASK-001", 1), ("TASK-001", 2), ("TASK-004", 1), ("TASK-004", 2)],
            [(item["task_id"], item["attempt"]) for item in value["attempts"]],
        )
        requirement = next(
            item for item in value["requirements"] if item["id"] == "REQ-004"
        )
        self.assertEqual(["TASK-004"], requirement["task_ids"])

    def test_shuffled_equivalent_inputs_crlf_and_decomposed_unicode_are_stable(
        self,
    ) -> None:
        left = valid_manifest()
        right = copy.deepcopy(left)
        left["goal"] = "Ship Café\ntrace"
        right["goal"] = "Ship Cafe\u0301\r\ntrace"
        left["requirements"][1]["text"] = "Résumé\nflow"
        right["requirements"][1]["text"] = "Re\u0301sume\u0301\r\nflow"
        right["requirements"].reverse()
        right["tasks"].reverse()
        right["protected_paths"].reverse()

        first = decoded_v1(left)
        second = decoded_v1(right)
        self.assertEqual(first, second)
        first_snapshot, first_index = empty_state(first)
        second_snapshot, second_index = empty_state(second)
        first_artifacts = export_trace(first, first_snapshot, first_index, ())
        second_artifacts = export_trace(second, second_snapshot, second_index, ())

        self.assertEqual(first_artifacts.json_bytes, second_artifacts.json_bytes)
        self.assertEqual(
            first_artifacts.markdown_bytes, second_artifacts.markdown_bytes
        )
        self.assertEqual(first_artifacts.json_sha256, second_artifacts.json_sha256)
        self.assertIn("Café".encode(), first_artifacts.json_bytes)
        self.assertNotIn(b"\r", first_artifacts.markdown_bytes)

        v2_left = valid_manifest_v2()
        v2_right = copy.deepcopy(v2_left)
        v2_right["requirements"].reverse()
        v2_right["tasks"].reverse()
        v2_right["task_id_mapping"] = dict(
            reversed(list(v2_right["task_id_mapping"].items()))
        )
        first_v2 = decoded_v2(v2_left)
        second_v2 = decoded_v2(v2_right)
        first_snapshot, first_index = empty_state(first_v2)
        second_snapshot, second_index = empty_state(second_v2)
        self.assertEqual(
            trace_json_bytes(first_v2, first_snapshot, first_index, ()),
            trace_json_bytes(second_v2, second_snapshot, second_index, ()),
        )

    def test_raw_mappings_are_rejected_at_every_presentation_boundary(self) -> None:
        manifest = decoded_v1()
        snapshot, index = empty_state(manifest)
        self.assert_fault(
            TraceExportFault.INVALID_INPUT,
            lambda: build_trace_projection(valid_manifest(), snapshot, index, ()),
        )
        self.assert_fault(
            TraceExportFault.INVALID_INPUT,
            lambda: build_trace_projection(manifest, snapshot, index, ({},)),
        )
        self.assert_fault(
            TraceExportFault.INVALID_INPUT,
            lambda: build_trace_projection(manifest, snapshot, index, (), limits={}),
        )
        self.assert_fault(
            TraceExportFault.INVALID_INPUT,
            lambda: render_trace_json({}),
        )

    def test_graph_and_snapshot_mismatch_fails_before_consuming_journal(self) -> None:
        manifest = decoded_v1()
        snapshot, index = empty_state(manifest)
        changed = dataclasses.replace(
            snapshot,
            tasks=(
                dataclasses.replace(snapshot.tasks[0], state=RuntimeState.APPROVED),
                *snapshot.tasks[1:],
            ),
        )
        source = SinglePassEvents(())
        self.assert_fault(
            TraceExportFault.GRAPH_MISMATCH,
            lambda: build_trace_projection(manifest, changed, index, source),
        )
        self.assertEqual(0, source.iterations)

    def test_journal_stream_read_failure_is_a_named_input_fault(self) -> None:
        manifest = decoded_v1()
        events, snapshot, index, _ = journal_fixture(manifest)
        error = self.assert_fault(
            TraceExportFault.INVALID_INPUT,
            lambda: build_trace_projection(
                manifest,
                snapshot,
                index,
                BrokenEvents(events[0]),
            ),
        )
        self.assertEqual(2, error.sequence)

    def test_sequence_hash_run_and_terminal_head_corruption_are_named(self) -> None:
        manifest = decoded_v1()
        events, snapshot, index, _ = journal_fixture(manifest)
        cases = (
            (events[1:], "missing genesis event"),
            ((events[0], *events[2:]), "sequence gap"),
            (
                (
                    events[0],
                    forge_event(events[1], previous_event_hash=hash_ref(999)),
                    *events[2:],
                ),
                "previous hash",
            ),
            (
                (
                    forge_event(
                        events[0],
                        identity=ExecutionIdentity("WISH-OTHER", 1),
                    ),
                    *events[1:],
                ),
                "run mismatch",
            ),
            (
                (
                    forge_event(events[0], payload_hash=hash_ref(998)),
                    *events[1:],
                ),
                "payload hash",
            ),
            (events[:-1], "terminal head"),
        )
        for corrupt, label in cases:
            with self.subTest(label=label):
                self.assert_fault(
                    TraceExportFault.EVENT_CHAIN_MISMATCH,
                    lambda corrupt=corrupt: build_trace_projection(
                        manifest, snapshot, index, corrupt
                    ),
                )

    def test_all_projection_and_output_caps_fail_closed(self) -> None:
        manifest = decoded_v1()
        events, snapshot, index, _ = journal_fixture(manifest)
        cases = (
            (
                TraceExportFault.EVENT_LIMIT_EXCEEDED,
                TraceLimits(max_events=4),
            ),
            (
                TraceExportFault.COMMAND_LIMIT_EXCEEDED,
                TraceLimits(max_commands=2),
            ),
            (
                TraceExportFault.EVIDENCE_LIMIT_EXCEEDED,
                TraceLimits(max_evidence=0),
            ),
            (
                TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
                TraceLimits(max_output_bytes=32),
            ),
        )
        for fault, limits in cases:
            with self.subTest(fault=fault):
                self.assert_fault(
                    fault,
                    lambda limits=limits: build_trace_projection(
                        manifest,
                        snapshot,
                        index,
                        iter(events),
                        limits=limits,
                    ),
                )

        projection = build_trace_projection(manifest, snapshot, index, events)
        self.assert_fault(
            TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
            lambda: render_trace_json(projection, max_output_bytes=32),
        )
        self.assert_fault(
            TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
            lambda: render_trace_markdown(projection, max_output_bytes=32),
        )

    def test_json_and_markdown_have_pinned_golden_hashes(self) -> None:
        manifest = decoded_v1()
        events, snapshot, index, _ = journal_fixture(manifest)
        artifacts = export_trace(manifest, snapshot, index, events)
        self.assertEqual(
            "sha256:c8fe195f3de444db79cdc246526e53e12c3871d2f54516f1065fe69855dffc73",
            artifacts.json_sha256,
        )
        self.assertEqual(
            "sha256:3cbe46c865df69db3821f4fcb1af4f007e70f3bab4237ce58aee6ca6f1cfc712",
            artifacts.markdown_sha256,
        )


if __name__ == "__main__":
    unittest.main()
