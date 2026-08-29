"""Canonical evidence and conservative gates for controlled M1 benchmarks."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import platform
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA_VERSION = 2
BENCHMARK_SUITE = "active-m1-controlled-performance"
MINIMUM_CONTROLLED_SAMPLES = 5
MAX_EVIDENCE_BYTES = 1024 * 1024
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

POLICY: dict[str, object] = {
    "policy_id": "active-m1-controlled-v3",
    "minimum_samples": MINIMUM_CONTROLLED_SAMPLES,
    "absolute_limits": {
        "cold_replay_p95_ms": 15_000,
        "cold_replay_p99_ms": 30_000,
        "checkpoint_tail_p95_ms": 3_000,
        "checkpoint_tail_p99_ms": 10_000,
        "coordinator_peak_rss_bytes": 512 * 1024 * 1024,
        # One graph iteration compiles the canonical and reordered DAG/index pair.
        # Batching makes the measurement large compared with timer/scheduler noise.
        "graph_batch_p99_ms_per_iteration": 100,
    },
    "relative_limits": {
        "p50_regression_percent": 20,
        "noise_floor_ms": {
            "cold_replay": 500,
            "checkpoint_tail": 50,
            "graph_batch": 100,
        },
        "peak_rss_noise_floor_bytes": 16 * 1024 * 1024,
    },
}


@dataclass(frozen=True, slots=True)
class GateReport:
    passed: bool
    findings: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_primitive(self) -> dict[str, object]:
        return {
            "findings": list(self.findings),
            "passed": self.passed,
            "warnings": list(self.warnings),
        }


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def integer_summary(samples_ms: list[int]) -> dict[str, int]:
    if (
        type(samples_ms) is not list
        or not samples_ms
        or any(type(item) is not int or item < 0 for item in samples_ms)
    ):
        raise ValueError("samples must be a non-empty list of non-negative integers")
    ordered = sorted(samples_ms)
    def nearest_rank(percent: int) -> int:
        rank = (len(ordered) * percent + 99) // 100
        return ordered[max(0, rank - 1)]

    return {
        "minimum_ms": ordered[0],
        "p50_ms": nearest_rank(50),
        "p95_ms": nearest_rank(95),
        "p99_ms": nearest_rank(99),
        "sample_count": len(ordered),
    }


def capture_environment(storage_path: Path) -> dict[str, object]:
    resolved = storage_path.resolve(strict=True)
    clock = time.get_clock_info("perf_counter")
    identity: dict[str, object] = {
        "clock": {
            "implementation": clock.implementation,
            "monotonic": clock.monotonic,
            "resolution_ns": max(1, round(clock.resolution * 1_000_000_000)),
        },
        "platform": {
            "architecture_bits": struct.calcsize("P") * 8,
            "cpu_identifier": _cpu_identifier(),
            "logical_cpu_count": os.cpu_count(),
            "machine": platform.machine(),
            "os_name": os.name,
            "release": platform.release(),
            "system": platform.system(),
            "version": platform.version(),
        },
        "python": {
            "abi_tag": getattr(sys.implementation, "cache_tag", None),
            "executable_sha256": _file_sha256(Path(sys.executable)),
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "storage": _storage_identity(resolved),
    }
    return {"identity": identity, "identity_digest": digest(identity)}


def build_evidence(
    *,
    environment: dict[str, object],
    replay_identity: dict[str, object],
    cold_samples_ms: list[int],
    checkpoint_samples_ms: list[int],
    peak_rss_bytes: int,
    graph_identity: dict[str, object],
    graph_samples_ms: list[int],
    recorded_at_utc: str | None = None,
) -> dict[str, object]:
    if type(peak_rss_bytes) is not int or peak_rss_bytes < 0:
        raise ValueError("peak_rss_bytes must be a non-negative integer")
    replay = {
        "identity": replay_identity,
        "identity_digest": digest(replay_identity),
        "measurements": {
            "checkpoint_tail_elapsed_ms": checkpoint_samples_ms,
            "cold_replay_elapsed_ms": cold_samples_ms,
            "peak_rss_bytes": peak_rss_bytes,
        },
        "summaries": {
            "checkpoint_tail": integer_summary(checkpoint_samples_ms),
            "cold_replay": integer_summary(cold_samples_ms),
        },
    }
    graph = {
        "identity": graph_identity,
        "identity_digest": digest(graph_identity),
        "measurements": {"batch_elapsed_ms": graph_samples_ms},
        "summaries": {"batch": integer_summary(graph_samples_ms)},
    }
    if recorded_at_utc is None:
        recorded_at_utc = (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
    return {
        "benchmark_suite": BENCHMARK_SUITE,
        "environment": environment,
        "policy": copy.deepcopy(POLICY),
        "policy_digest": digest(POLICY),
        "recorded_at_utc": recorded_at_utc,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "workloads": {
            "graph_64_tasks_512_edges": graph,
            "replay_100000_events": replay,
        },
    }


def validate_evidence(value: object) -> tuple[str, ...]:
    errors: list[str] = []
    if type(value) is not dict:
        return ("evidence_not_an_object",)
    if set(value) != {
        "benchmark_suite",
        "environment",
        "policy",
        "policy_digest",
        "recorded_at_utc",
        "schema_version",
        "workloads",
    }:
        errors.append("evidence_fields_invalid")
    if value.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        errors.append("evidence_schema_unsupported")
    if value.get("benchmark_suite") != BENCHMARK_SUITE:
        errors.append("benchmark_suite_mismatch")
    recorded_at = value.get("recorded_at_utc")
    if type(recorded_at) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", recorded_at
    ):
        errors.append("recorded_at_utc_invalid")
    if value.get("policy") != POLICY or value.get("policy_digest") != digest(POLICY):
        errors.append("gate_policy_mismatch")

    environment = value.get("environment")
    if type(environment) is not dict or set(environment) != {
        "identity",
        "identity_digest",
    }:
        errors.append("environment_invalid")
    elif environment.get("identity_digest") != digest(environment.get("identity")):
        errors.append("environment_digest_mismatch")
    else:
        errors.extend(_validate_environment_identity(environment.get("identity")))

    workloads = value.get("workloads")
    if type(workloads) is not dict or set(workloads) != {
        "graph_64_tasks_512_edges",
        "replay_100000_events",
    }:
        errors.append("workloads_invalid")
        return tuple(dict.fromkeys(errors))
    errors.extend(_validate_replay(workloads.get("replay_100000_events")))
    errors.extend(_validate_graph(workloads.get("graph_64_tasks_512_edges")))
    return tuple(dict.fromkeys(errors))


def evaluate_gate(
    current: object,
    baseline: object | None = None,
    *,
    controlled: bool = False,
    require_baseline: bool = False,
) -> GateReport:
    findings = list(validate_evidence(current))
    warnings: list[str] = []
    if findings or type(current) is not dict:
        return GateReport(False, tuple(findings), tuple(warnings))

    workloads = current["workloads"]
    replay = workloads["replay_100000_events"]
    graph = workloads["graph_64_tasks_512_edges"]
    absolute = POLICY["absolute_limits"]
    minimum_samples = int(POLICY["minimum_samples"])
    assert type(absolute) is dict
    cold = replay["summaries"]["cold_replay"]
    checkpoint = replay["summaries"]["checkpoint_tail"]
    graph_batch = graph["summaries"]["batch"]
    graph_iterations = graph["identity"]["iterations_per_sample"]

    _append_limit(
        findings,
        "replay:peak_rss_limit_exceeded",
        replay["measurements"]["peak_rss_bytes"],
        absolute["coordinator_peak_rss_bytes"],
    )
    if not controlled:
        if baseline is not None or require_baseline:
            findings.append("controlled_mode_required_for_baseline")
        warnings.append("wall_clock_diagnostic_only")
        return GateReport(not findings, tuple(findings), tuple(warnings))

    environment_identity = current["environment"]["identity"]
    storage_identity = environment_identity["storage"]
    if not environment_identity["clock"]["monotonic"]:
        findings.append("environment:performance_clock_not_monotonic")
    if storage_identity["kind"] == "stat_device" or any(
        storage_identity[name] in {"", "unknown"}
        for name in ("device_id", "filesystem", "mount_point", "mount_source")
    ):
        findings.append("environment:storage_identity_incomplete")

    for label, summary in (
        ("cold_replay", cold),
        ("checkpoint_tail", checkpoint),
        ("graph_batch", graph_batch),
    ):
        if summary["sample_count"] < minimum_samples:
            findings.append(f"{label}:insufficient_samples")
    _append_limit(
        findings,
        "cold_replay:p95_limit_exceeded",
        cold["p95_ms"],
        absolute["cold_replay_p95_ms"],
    )
    _append_limit(
        findings,
        "cold_replay:p99_limit_exceeded",
        cold["p99_ms"],
        absolute["cold_replay_p99_ms"],
    )
    _append_limit(
        findings,
        "checkpoint_tail:p95_limit_exceeded",
        checkpoint["p95_ms"],
        absolute["checkpoint_tail_p95_ms"],
    )
    _append_limit(
        findings,
        "checkpoint_tail:p99_limit_exceeded",
        checkpoint["p99_ms"],
        absolute["checkpoint_tail_p99_ms"],
    )
    _append_limit(
        findings,
        "graph_batch:p99_limit_exceeded",
        graph_batch["p99_ms"],
        absolute["graph_batch_p99_ms_per_iteration"] * graph_iterations,
    )

    if baseline is None:
        if require_baseline:
            findings.append("relative_baseline_required")
        else:
            warnings.append("relative_baseline_not_checked")
        return GateReport(not findings, tuple(findings), tuple(warnings))

    baseline_errors = validate_evidence(baseline)
    if baseline_errors or type(baseline) is not dict:
        findings.extend(f"baseline:{item}" for item in baseline_errors)
        return GateReport(False, tuple(findings), tuple(warnings))
    baseline_absolute = evaluate_gate(baseline, controlled=True)
    if not baseline_absolute.passed:
        findings.extend(
            f"baseline:{item}" for item in baseline_absolute.findings
        )
        return GateReport(False, tuple(findings), tuple(warnings))
    if (
        current["environment"]["identity_digest"]
        != baseline["environment"]["identity_digest"]
    ):
        findings.append("baseline:environment_identity_mismatch")
    for workload_name in workloads:
        if (
            workloads[workload_name]["identity_digest"]
            != baseline["workloads"][workload_name]["identity_digest"]
        ):
            findings.append(f"baseline:{workload_name}:workload_identity_mismatch")
    if findings:
        return GateReport(False, tuple(findings), tuple(warnings))

    relative = POLICY["relative_limits"]
    assert type(relative) is dict
    percent = relative["p50_regression_percent"]
    floors = relative["noise_floor_ms"]
    assert type(floors) is dict
    baseline_replay = baseline["workloads"]["replay_100000_events"]
    baseline_graph = baseline["workloads"]["graph_64_tasks_512_edges"]
    _append_regression(
        findings,
        "cold_replay",
        cold["p50_ms"],
        baseline_replay["summaries"]["cold_replay"]["p50_ms"],
        percent,
        floors["cold_replay"],
    )
    _append_regression(
        findings,
        "checkpoint_tail",
        checkpoint["p50_ms"],
        baseline_replay["summaries"]["checkpoint_tail"]["p50_ms"],
        percent,
        floors["checkpoint_tail"],
    )
    _append_regression(
        findings,
        "graph_batch",
        graph_batch["p50_ms"],
        baseline_graph["summaries"]["batch"]["p50_ms"],
        percent,
        floors["graph_batch"],
    )
    _append_regression(
        findings,
        "peak_rss",
        replay["measurements"]["peak_rss_bytes"],
        baseline_replay["measurements"]["peak_rss_bytes"],
        percent,
        relative["peak_rss_noise_floor_bytes"],
    )
    return GateReport(not findings, tuple(findings), tuple(warnings))


def write_evidence(path: Path, evidence: object) -> None:
    errors = validate_evidence(evidence)
    if errors:
        raise ValueError("invalid performance evidence: " + ", ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(evidence))
    os.replace(temporary, path)


def read_evidence(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("performance evidence exceeds the 1 MiB limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("performance evidence is not valid JSON") from exc
    if type(value) is not dict:
        raise ValueError("performance evidence must be a JSON object")
    if raw != canonical_json_bytes(value):
        raise ValueError("performance evidence is not canonical JSON")
    errors = validate_evidence(value)
    if errors:
        raise ValueError("invalid performance evidence: " + ", ".join(errors))
    return value


def _validate_environment_identity(value: object) -> list[str]:
    if type(value) is not dict or set(value) != {
        "clock",
        "platform",
        "python",
        "storage",
    }:
        return ["environment_identity_invalid"]
    required = {
        "platform": {
            "architecture_bits",
            "cpu_identifier",
            "logical_cpu_count",
            "machine",
            "os_name",
            "release",
            "system",
            "version",
        },
        "python": {"abi_tag", "executable_sha256", "implementation", "version"},
        "clock": {"implementation", "monotonic", "resolution_ns"},
    }
    errors: list[str] = []
    for name, keys in required.items():
        if type(value.get(name)) is not dict or set(value[name]) != keys:
            errors.append(f"environment_{name}_invalid")
    platform_identity = value.get("platform")
    if type(platform_identity) is dict:
        for name in (
            "cpu_identifier",
            "machine",
            "os_name",
            "release",
            "system",
            "version",
        ):
            if type(platform_identity.get(name)) is not str:
                errors.append(f"environment_platform_{name}_invalid")
        if platform_identity.get("architecture_bits") not in {32, 64}:
            errors.append("environment_platform_architecture_invalid")
        if (
            platform_identity.get("logical_cpu_count") is not None
            and (
                type(platform_identity.get("logical_cpu_count")) is not int
                or platform_identity["logical_cpu_count"] < 1
            )
        ):
            errors.append("environment_platform_cpu_count_invalid")
    storage = value.get("storage")
    if type(storage) is not dict or set(storage) != {
        "block_size",
        "device_id",
        "filesystem",
        "kind",
        "mount_point",
        "mount_source",
        "read_only",
    }:
        errors.append("environment_storage_invalid")
    else:
        if type(storage.get("block_size")) is not int or storage["block_size"] < 0:
            errors.append("environment_storage_block_size_invalid")
        for name in (
            "device_id",
            "filesystem",
            "kind",
            "mount_point",
            "mount_source",
        ):
            if type(storage.get(name)) is not str:
                errors.append(f"environment_storage_{name}_invalid")
        if type(storage.get("read_only")) is not bool:
            errors.append("environment_storage_read_only_invalid")
    python = value.get("python")
    if type(python) is dict and not SHA256_PATTERN.fullmatch(
        str(python.get("executable_sha256"))
    ):
        errors.append("python_executable_digest_invalid")
    elif type(python) is dict:
        for name in ("implementation", "version"):
            if type(python.get(name)) is not str:
                errors.append(f"environment_python_{name}_invalid")
        if python.get("abi_tag") is not None and type(python.get("abi_tag")) is not str:
            errors.append("environment_python_abi_tag_invalid")
    clock = value.get("clock")
    if type(clock) is dict:
        if type(clock.get("implementation")) is not str:
            errors.append("environment_clock_implementation_invalid")
        if type(clock.get("monotonic")) is not bool:
            errors.append("environment_clock_monotonic_invalid")
        if type(clock.get("resolution_ns")) is not int or clock["resolution_ns"] < 1:
            errors.append("environment_clock_resolution_invalid")
    return errors


def _validate_replay(value: object) -> list[str]:
    errors: list[str] = []
    if type(value) is not dict or set(value) != {
        "identity",
        "identity_digest",
        "measurements",
        "summaries",
    }:
        return ["replay_evidence_invalid"]
    identity = value["identity"]
    if type(identity) is not dict or set(identity) != {
        "checkpoint_sequence",
        "event_count",
        "journal_bytes",
        "journal_head_hash",
        "manifest_hash",
        "tail_event_count",
        "workload_id",
    }:
        errors.append("replay_identity_invalid")
    else:
        if identity.get("event_count") != 100_000:
            errors.append("replay_event_count_invalid")
        if identity.get("checkpoint_sequence") != 99_990:
            errors.append("replay_checkpoint_sequence_invalid")
        if identity.get("tail_event_count") != 10:
            errors.append("replay_tail_count_invalid")
        if identity.get("workload_id") != "canonical-status-chain-v1":
            errors.append("replay_workload_id_invalid")
        if type(identity.get("journal_bytes")) is not int or identity[
            "journal_bytes"
        ] < 1:
            errors.append("replay_journal_bytes_invalid")
        for name in ("journal_head_hash", "manifest_hash"):
            if not SHA256_PATTERN.fullmatch(str(identity.get(name))):
                errors.append(f"replay_{name}_invalid")
    if value.get("identity_digest") != digest(identity):
        errors.append("replay_identity_digest_mismatch")
    measurements = value.get("measurements")
    summaries = value.get("summaries")
    if type(measurements) is not dict or set(measurements) != {
        "checkpoint_tail_elapsed_ms",
        "cold_replay_elapsed_ms",
        "peak_rss_bytes",
    }:
        errors.append("replay_measurements_invalid")
        return errors
    if type(summaries) is not dict or set(summaries) != {
        "checkpoint_tail",
        "cold_replay",
    }:
        errors.append("replay_summaries_invalid")
        return errors
    for samples_name, summary_name in (
        ("cold_replay_elapsed_ms", "cold_replay"),
        ("checkpoint_tail_elapsed_ms", "checkpoint_tail"),
    ):
        try:
            expected = integer_summary(measurements[samples_name])
        except (TypeError, ValueError):
            errors.append(f"replay_{samples_name}_invalid")
        else:
            if summaries.get(summary_name) != expected:
                errors.append(f"replay_{summary_name}_summary_mismatch")
    if type(measurements.get("peak_rss_bytes")) is not int or measurements[
        "peak_rss_bytes"
    ] < 0:
        errors.append("replay_peak_rss_invalid")
    return errors


def _validate_graph(value: object) -> list[str]:
    errors: list[str] = []
    if type(value) is not dict or set(value) != {
        "identity",
        "identity_digest",
        "measurements",
        "summaries",
    }:
        return ["graph_evidence_invalid"]
    identity = value["identity"]
    if type(identity) is not dict or set(identity) != {
        "edge_count",
        "graph_index_digest",
        "iterations_per_sample",
        "manifest_hash",
        "task_count",
        "workload_id",
    }:
        errors.append("graph_identity_invalid")
    else:
        if identity.get("task_count") != 64 or identity.get("edge_count") != 512:
            errors.append("graph_envelope_invalid")
        if (
            type(identity.get("iterations_per_sample")) is not int
            or identity["iterations_per_sample"] < 1
        ):
            errors.append("graph_iterations_invalid")
        if (
            identity.get("workload_id")
            != "canonical-and-reordered-compile-pair-v1"
        ):
            errors.append("graph_workload_id_invalid")
        for name in ("graph_index_digest", "manifest_hash"):
            if not SHA256_PATTERN.fullmatch(str(identity.get(name))):
                errors.append(f"graph_{name}_invalid")
    if value.get("identity_digest") != digest(identity):
        errors.append("graph_identity_digest_mismatch")
    measurements = value.get("measurements")
    summaries = value.get("summaries")
    if type(measurements) is not dict or set(measurements) != {"batch_elapsed_ms"}:
        errors.append("graph_measurements_invalid")
        return errors
    if type(summaries) is not dict or set(summaries) != {"batch"}:
        errors.append("graph_summaries_invalid")
        return errors
    try:
        expected = integer_summary(measurements["batch_elapsed_ms"])
    except (TypeError, ValueError):
        errors.append("graph_batch_elapsed_invalid")
    else:
        if summaries.get("batch") != expected:
            errors.append("graph_batch_summary_mismatch")
    return errors


def _append_limit(
    findings: list[str], code: str, observed: object, limit: object
) -> None:
    if type(observed) is int and type(limit) is int and observed > limit:
        findings.append(f"{code}:{observed}>{limit}")


def _append_regression(
    findings: list[str],
    name: str,
    current: int,
    baseline: int,
    percent: int,
    noise_floor: int,
) -> None:
    allowed_delta = max(noise_floor, (baseline * percent + 99) // 100)
    if current > baseline + allowed_delta:
        findings.append(
            f"{name}:relative_regression:{current}>{baseline}+{allowed_delta}"
        )


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _cpu_identifier() -> str:
    if os.name == "nt":
        return os.environ.get("PROCESSOR_IDENTIFIER", platform.processor())
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    return platform.processor()


def _storage_identity(path: Path) -> dict[str, object]:
    try:
        if os.name == "nt":
            return _windows_storage_identity(path)
        if Path("/proc/self/mountinfo").is_file():
            return _linux_storage_identity(path)
    except OSError:
        pass
    stat_result = path.stat()
    statvfs = os.statvfs(path) if hasattr(os, "statvfs") else None
    return {
        "block_size": 0 if statvfs is None else int(statvfs.f_frsize),
        "device_id": str(stat_result.st_dev),
        "filesystem": "unknown",
        "kind": "stat_device",
        "mount_point": path.anchor,
        "mount_source": "unknown",
        "read_only": False,
    }


def _windows_storage_identity(path: Path) -> dict[str, object]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    volume_path = ctypes.create_unicode_buffer(260)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    )
    get_volume_path.restype = ctypes.c_int
    if not get_volume_path(str(path), volume_path, len(volume_path)):
        raise ctypes.WinError(ctypes.get_last_error())

    serial = ctypes.c_ulong()
    flags = ctypes.c_ulong()
    max_component = ctypes.c_ulong()
    filesystem = ctypes.create_unicode_buffer(64)
    get_volume_information = kernel32.GetVolumeInformationW
    get_volume_information.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    )
    get_volume_information.restype = ctypes.c_int
    if not get_volume_information(
        volume_path.value,
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    sectors_per_cluster = ctypes.c_ulong()
    bytes_per_sector = ctypes.c_ulong()
    free_clusters = ctypes.c_ulong()
    total_clusters = ctypes.c_ulong()
    get_disk_free_space = kernel32.GetDiskFreeSpaceW
    get_disk_free_space.argtypes = (
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
    )
    get_disk_free_space.restype = ctypes.c_int
    if not get_disk_free_space(
        volume_path.value,
        ctypes.byref(sectors_per_cluster),
        ctypes.byref(bytes_per_sector),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "block_size": int(sectors_per_cluster.value * bytes_per_sector.value),
        "device_id": f"volume-serial:{serial.value:08x}",
        "filesystem": filesystem.value,
        "kind": "windows_volume",
        "mount_point": volume_path.value,
        "mount_source": volume_path.value,
        "read_only": bool(flags.value & 0x00080000),
    }


def _linux_storage_identity(path: Path) -> dict[str, object]:
    best: tuple[int, list[str], list[str]] | None = None
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 2:
            continue
        mount_point = Path(_unescape_mount_field(left_fields[4]))
        try:
            contains = os.path.commonpath((str(path), str(mount_point))) == str(
                mount_point
            )
        except ValueError:
            contains = False
        if contains and (best is None or len(str(mount_point)) > best[0]):
            best = (len(str(mount_point)), left_fields, right_fields)
    if best is None:
        raise OSError("no mountinfo entry contains benchmark path")
    _, left_fields, right_fields = best
    statvfs = os.statvfs(path)
    options = set(left_fields[5].split(","))
    return {
        "block_size": int(statvfs.f_frsize),
        "device_id": left_fields[2],
        "filesystem": _unescape_mount_field(right_fields[0]),
        "kind": "linux_mountinfo",
        "mount_point": _unescape_mount_field(left_fields[4]),
        "mount_source": _unescape_mount_field(right_fields[1]),
        "read_only": "ro" in options,
    }


def _unescape_mount_field(value: str) -> str:
    replacements = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: replacements[match.group(1)],
        value,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


__all__ = [
    "BENCHMARK_SUITE",
    "EVIDENCE_SCHEMA_VERSION",
    "MINIMUM_CONTROLLED_SAMPLES",
    "POLICY",
    "GateReport",
    "build_evidence",
    "canonical_json_bytes",
    "capture_environment",
    "digest",
    "evaluate_gate",
    "integer_summary",
    "read_evidence",
    "validate_evidence",
    "write_evidence",
]
