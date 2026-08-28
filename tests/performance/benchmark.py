"""Run the exact active-M1 replay and graph performance envelopes."""

from __future__ import annotations

import ctypes
import dataclasses
import gc
import os
import platform
import tempfile
import time
from pathlib import Path

from tests.performance.evidence import build_evidence, capture_environment, digest
from tests.performance.helpers import (
    append_status_events,
    envelope_manifest,
    write_large_journal,
)
from tests.services.test_replay import EPOCH, admitted_manifest
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services.checkpoints import CheckpointPolicy, CheckpointStore
from wish_builder.services.replay import ReplayStatus, replay_journal

TOTAL_EVENTS = 100_000
CHECKPOINT_SEQUENCE = TOTAL_EVENTS - 10


def run_controlled_benchmarks(
    work_root: Path,
    *,
    replay_samples: int = 5,
    graph_samples: int = 7,
    graph_iterations: int = 100,
) -> dict[str, object]:
    _require_positive("replay_samples", replay_samples)
    _require_positive("graph_samples", graph_samples)
    _require_positive("graph_iterations", graph_iterations)
    work_root = work_root.resolve(strict=True)
    environment = capture_environment(work_root)
    manifest = admitted_manifest()

    with tempfile.TemporaryDirectory(prefix="wish-builder-perf-", dir=work_root) as raw:
        root = Path(raw)
        journal = root / "journal"
        checkpoint_event = write_large_journal(
            journal,
            through_sequence=CHECKPOINT_SEQUENCE,
        )
        setup = replay_journal(
            journal,
            manifest,
            coordinator_epoch=EPOCH,
            repair_derived=False,
        )
        _require_replay(setup, CHECKPOINT_SEQUENCE, checkpoint_event.event_hash)
        store = CheckpointStore(root / "checkpoints")
        store.publish(
            manifest,
            setup.snapshot,
            setup.graph_index,
            setup.journal_position,
        )
        final_event = append_status_events(
            journal,
            first_sequence=CHECKPOINT_SEQUENCE + 1,
            through_sequence=TOTAL_EVENTS,
            previous_hash=checkpoint_event.event_hash,
        )
        empty_cold_store = CheckpointStore(root / "cold-without-checkpoint")

        cold_samples_ms: list[int] = []
        for _ in range(replay_samples):
            gc.collect()
            started = time.perf_counter_ns()
            result = replay_journal(
                journal,
                manifest,
                coordinator_epoch=EPOCH,
                checkpoint_store=empty_cold_store,
                repair_derived=False,
            )
            elapsed = _elapsed_ms(started)
            _require_replay(result, TOTAL_EVENTS, final_event.event_hash)
            if result.checkpoint_used or result.events_replayed != TOTAL_EVENTS:
                raise AssertionError("genesis replay unexpectedly used a checkpoint")
            cold_samples_ms.append(elapsed)

        checkpoint_samples_ms: list[int] = []
        for _ in range(replay_samples):
            gc.collect()
            started = time.perf_counter_ns()
            result = replay_journal(
                journal,
                manifest,
                coordinator_epoch=EPOCH,
                checkpoint_store=store,
                checkpoint_policy=CheckpointPolicy(event_interval=TOTAL_EVENTS + 1),
                repair_derived=False,
            )
            elapsed = _elapsed_ms(started)
            _require_replay(result, TOTAL_EVENTS, final_event.event_hash)
            if not result.checkpoint_used or result.events_replayed != 10:
                raise AssertionError(
                    "checkpoint replay did not consume the bounded tail"
                )
            checkpoint_samples_ms.append(elapsed)

        segment = journal / "segments" / "segment-00000001.jsonl"
        replay_identity = {
            "checkpoint_sequence": CHECKPOINT_SEQUENCE,
            "event_count": TOTAL_EVENTS,
            "journal_bytes": segment.stat().st_size,
            "journal_head_hash": final_event.event_hash,
            "manifest_hash": digest(manifest.to_primitive()),
            "tail_event_count": TOTAL_EVENTS - CHECKPOINT_SEQUENCE,
            "workload_id": "canonical-status-chain-v1",
        }
        peak_rss_bytes = _peak_rss_bytes()

    graph_manifest = envelope_manifest(manifest)
    reordered = dataclasses.replace(
        graph_manifest,
        tasks=tuple(reversed(graph_manifest.tasks)),
    )
    reference_dag, reference_index = _compile_graph_pair(graph_manifest)
    reordered_dag, reordered_index = _compile_graph_pair(reordered)
    _require_graph_equivalence(
        reference_dag,
        reference_index,
        reordered_dag,
        reordered_index,
    )

    graph_elapsed_ms: list[int] = []
    for _ in range(graph_samples):
        gc.collect()
        started = time.perf_counter_ns()
        for _iteration in range(graph_iterations):
            first_dag, first_index = _compile_graph_pair(graph_manifest)
            second_dag, second_index = _compile_graph_pair(reordered)
        elapsed = _elapsed_ms(started)
        _require_graph_equivalence(
            first_dag,
            first_index,
            second_dag,
            second_index,
        )
        graph_elapsed_ms.append(elapsed)

    graph_identity = {
        "edge_count": reference_dag.edge_count,
        "graph_index_digest": reference_index.digest,
        "iterations_per_sample": graph_iterations,
        "manifest_hash": digest(graph_manifest.to_primitive()),
        "task_count": len(graph_manifest.tasks),
        "workload_id": "canonical-and-reordered-compile-pair-v1",
    }
    return build_evidence(
        environment=environment,
        replay_identity=replay_identity,
        cold_samples_ms=cold_samples_ms,
        checkpoint_samples_ms=checkpoint_samples_ms,
        peak_rss_bytes=peak_rss_bytes,
        graph_identity=graph_identity,
        graph_samples_ms=graph_elapsed_ms,
    )


def _compile_graph_pair(manifest):
    return TaskDag.compile(manifest), GraphIndex.compile(manifest)


def _require_replay(result, sequence: int, event_hash: str) -> None:
    if result.status is not ReplayStatus.RECOVERED:
        raise AssertionError(f"replay did not recover: {result.status}")
    if result.snapshot.last_sequence != sequence or (
        result.events_replayed != sequence and not result.checkpoint_used
    ):
        # Checkpoint replay intentionally returns only the tail event count.
        raise AssertionError("replay sequence does not match the workload")
    if result.head.event_hash != event_hash:
        raise AssertionError("replay head does not match the deterministic journal")
    if not result.graph_index.verify(admitted_manifest(), result.snapshot):
        raise AssertionError("replayed GraphIndex does not match a full rebuild")


def _require_graph_equivalence(
    first_dag, first_index, second_dag, second_index
) -> None:
    if len(first_dag.nodes) != 64 or first_dag.edge_count != 512:
        raise AssertionError("graph workload is not the 64-task/512-edge envelope")
    if first_dag != second_dag or first_index != second_index:
        raise AssertionError("graph compilation changed when task input order changed")
    if first_index.digest != second_index.digest:
        raise AssertionError("graph index digest is not deterministic")


def _elapsed_ms(started_ns: int) -> int:
    elapsed_ns = time.perf_counter_ns() - started_ns
    return max(1, (elapsed_ns + 500_000) // 1_000_000)


def _require_positive(name: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _peak_rss_bytes() -> int:
    if os.name != "nt":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if platform.system() == "Darwin" else peak * 1024)

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.argtypes = ()
    get_process.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    get_memory.restype = ctypes.c_int
    if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


__all__ = ["run_controlled_benchmarks"]
