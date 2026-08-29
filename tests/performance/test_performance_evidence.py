from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.performance.evidence import (
    POLICY,
    build_evidence,
    canonical_json_bytes,
    capture_environment,
    digest,
    evaluate_gate,
    integer_summary,
    read_evidence,
    validate_evidence,
    write_evidence,
)


def synthetic_evidence(
    *,
    cold: list[int] | None = None,
    checkpoint: list[int] | None = None,
    graph: list[int] | None = None,
    peak_rss: int = 128 * 1024 * 1024,
) -> dict[str, object]:
    identity = {
        "clock": {
            "implementation": "test-clock",
            "monotonic": True,
            "resolution_ns": 100,
        },
        "platform": {
            "architecture_bits": 64,
            "cpu_identifier": "test-cpu",
            "logical_cpu_count": 8,
            "machine": "x86_64",
            "os_name": "posix",
            "release": "test-release",
            "system": "TestOS",
            "version": "test-version",
        },
        "python": {
            "abi_tag": "cpython-test",
            "executable_sha256": "sha256:" + "a" * 64,
            "implementation": "CPython",
            "version": "3.13.0",
        },
        "storage": {
            "block_size": 4096,
            "device_id": "8:1",
            "filesystem": "testfs",
            "kind": "test",
            "mount_point": "/test",
            "mount_source": "/dev/test",
            "read_only": False,
        },
    }
    environment = {"identity": identity, "identity_digest": digest(identity)}
    replay_identity = {
        "checkpoint_sequence": 99_990,
        "event_count": 100_000,
        "journal_bytes": 42_000_000,
        "journal_head_hash": "sha256:" + "b" * 64,
        "manifest_hash": "sha256:" + "c" * 64,
        "tail_event_count": 10,
        "workload_id": "canonical-status-chain-v1",
    }
    graph_identity = {
        "edge_count": 512,
        "graph_index_digest": "sha256:" + "d" * 64,
        "iterations_per_sample": 100,
        "manifest_hash": "sha256:" + "e" * 64,
        "task_count": 64,
        "workload_id": "canonical-and-reordered-compile-pair-v1",
    }
    return build_evidence(
        environment=environment,
        replay_identity=replay_identity,
        cold_samples_ms=(
            [10_000, 10_100, 10_200, 10_300, 10_400]
            if cold is None
            else cold
        ),
        checkpoint_samples_ms=(
            [80, 85, 90, 95, 100] if checkpoint is None else checkpoint
        ),
        peak_rss_bytes=peak_rss,
        graph_identity=graph_identity,
        graph_samples_ms=(
            [900, 950, 1_000, 1_050, 1_100] if graph is None else graph
        ),
        recorded_at_utc="2026-08-19T00:00:00Z",
    )


class EnvironmentEvidenceTests(unittest.TestCase):
    def test_environment_identity_is_stable_for_same_runtime_and_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first"
            second_path = Path(temporary) / "second"
            first_path.mkdir()
            second_path.mkdir()
            first = capture_environment(first_path)
            second = capture_environment(second_path)

        self.assertEqual(first, second)
        self.assertEqual(digest(first["identity"]), first["identity_digest"])
        identity = first["identity"]
        self.assertIn("system", identity["platform"])
        self.assertIn("executable_sha256", identity["python"])
        self.assertIn("filesystem", identity["storage"])
        self.assertIn("device_id", identity["storage"])

    def test_canonical_round_trip_rejects_noncanonical_and_duplicate_json(self) -> None:
        evidence = synthetic_evidence()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            write_evidence(path, evidence)
            self.assertEqual(evidence, read_evidence(path))
            self.assertEqual(canonical_json_bytes(evidence), path.read_bytes())

            path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not canonical"):
                read_evidence(path)
            path.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                read_evidence(path)

    def test_validation_detects_measurement_and_identity_tampering(self) -> None:
        evidence = synthetic_evidence()
        evidence["workloads"]["replay_100000_events"]["measurements"][
            "cold_replay_elapsed_ms"
        ][0] += 1
        evidence["environment"]["identity"]["storage"]["filesystem"] = "other"

        errors = validate_evidence(evidence)

        self.assertIn("replay_cold_replay_summary_mismatch", errors)
        self.assertIn("environment_digest_mismatch", errors)


class PerformanceGateTests(unittest.TestCase):
    def test_summary_uses_explicit_nearest_rank_percentiles(self) -> None:
        self.assertEqual(
            {
                "minimum_ms": 1,
                "p50_ms": 3,
                "p95_ms": 5,
                "p99_ms": 5,
                "sample_count": 5,
            },
            integer_summary([5, 1, 4, 2, 3]),
        )

    def test_absolute_gate_requires_controlled_samples_and_coarse_limits(self) -> None:
        passing = evaluate_gate(synthetic_evidence(), controlled=True)
        too_few = evaluate_gate(
            synthetic_evidence(
                cold=[10_000] * 4,
                checkpoint=[100] * 4,
                graph=[1_000] * 4,
            ),
            controlled=True,
        )
        slow = evaluate_gate(
            synthetic_evidence(cold=[31_000] * 5), controlled=True
        )

        self.assertTrue(passing.passed)
        self.assertEqual(("relative_baseline_not_checked",), passing.warnings)
        self.assertFalse(too_few.passed)
        self.assertIn("cold_replay:insufficient_samples", too_few.findings)
        self.assertFalse(slow.passed)
        self.assertTrue(
            any("cold_replay:p95_limit_exceeded" in item for item in slow.findings)
        )

    def test_absolute_gate_enforces_p95_before_the_looser_p99_limit(self) -> None:
        cold_tail = evaluate_gate(
            synthetic_evidence(cold=[10_000, 10_000, 10_000, 20_000, 20_000]),
            controlled=True,
        )
        checkpoint_tail = evaluate_gate(
            synthetic_evidence(checkpoint=[100, 100, 100, 5_000, 5_000]),
            controlled=True,
        )

        self.assertFalse(cold_tail.passed)
        self.assertIn(
            "cold_replay:p95_limit_exceeded:20000>15000",
            cold_tail.findings,
        )
        self.assertNotIn(
            "cold_replay:p99_limit_exceeded:20000>30000",
            cold_tail.findings,
        )
        self.assertFalse(checkpoint_tail.passed)
        self.assertIn(
            "checkpoint_tail:p95_limit_exceeded:5000>3000",
            checkpoint_tail.findings,
        )
        self.assertNotIn(
            "checkpoint_tail:p99_limit_exceeded:5000>10000",
            checkpoint_tail.findings,
        )

    def test_relative_gate_fails_real_regression_but_ignores_tiny_jitter(self) -> None:
        baseline = synthetic_evidence()
        regression = synthetic_evidence(
            cold=[13_000] * 5,
            checkpoint=[200] * 5,
            graph=[1_300] * 5,
        )
        tiny_fast_path_jitter = synthetic_evidence(checkpoint=[120] * 5)

        failed = evaluate_gate(
            regression,
            baseline,
            controlled=True,
            require_baseline=True,
        )
        stable = evaluate_gate(
            tiny_fast_path_jitter,
            baseline,
            controlled=True,
            require_baseline=True,
        )

        self.assertFalse(failed.passed)
        self.assertTrue(
            any(item.startswith("cold_replay:") for item in failed.findings)
        )
        self.assertTrue(
            any(item.startswith("graph_batch:") for item in failed.findings)
        )
        self.assertTrue(stable.passed, stable.findings)

    def test_relative_gate_refuses_cross_environment_or_workload_comparison(
        self,
    ) -> None:
        baseline = synthetic_evidence()
        current = copy.deepcopy(baseline)
        current["environment"]["identity"]["platform"]["release"] = "different"
        current["environment"]["identity_digest"] = digest(
            current["environment"]["identity"]
        )
        report = evaluate_gate(
            current, baseline, controlled=True, require_baseline=True
        )

        self.assertFalse(report.passed)
        self.assertIn("baseline:environment_identity_mismatch", report.findings)

        workload = copy.deepcopy(baseline)
        graph_identity = workload["workloads"]["graph_64_tasks_512_edges"][
            "identity"
        ]
        graph_identity["iterations_per_sample"] = 101
        workload["workloads"]["graph_64_tasks_512_edges"][
            "identity_digest"
        ] = digest(graph_identity)
        report = evaluate_gate(
            workload, baseline, controlled=True, require_baseline=True
        )
        self.assertIn(
            "baseline:graph_64_tasks_512_edges:workload_identity_mismatch",
            report.findings,
        )

    def test_relative_gate_rejects_an_under_sampled_baseline(self) -> None:
        baseline = synthetic_evidence(
            cold=[10_000] * 4,
            checkpoint=[100] * 4,
            graph=[1_000] * 4,
        )

        report = evaluate_gate(
            synthetic_evidence(),
            baseline,
            controlled=True,
            require_baseline=True,
        )

        self.assertFalse(report.passed)
        self.assertIn("baseline:cold_replay:insufficient_samples", report.findings)

    def test_policy_is_explicit_and_baseline_can_be_required(self) -> None:
        report = evaluate_gate(
            synthetic_evidence(), controlled=True, require_baseline=True
        )

        self.assertEqual("active-m1-controlled-v3", POLICY["policy_id"])
        self.assertFalse(report.passed)
        self.assertIn("relative_baseline_required", report.findings)

    def test_controlled_gate_requires_complete_storage_identity(self) -> None:
        evidence = synthetic_evidence()
        storage = evidence["environment"]["identity"]["storage"]
        storage["kind"] = "stat_device"
        storage["filesystem"] = "unknown"
        evidence["environment"]["identity_digest"] = digest(
            evidence["environment"]["identity"]
        )

        report = evaluate_gate(evidence, controlled=True)

        self.assertFalse(report.passed)
        self.assertIn("environment:storage_identity_incomplete", report.findings)

    def test_shared_runner_timing_is_diagnostic_but_resource_limit_still_fails(
        self,
    ) -> None:
        slow = evaluate_gate(synthetic_evidence(cold=[60_000] * 5))
        too_large = evaluate_gate(
            synthetic_evidence(peak_rss=600 * 1024 * 1024)
        )

        self.assertTrue(slow.passed)
        self.assertEqual(("wall_clock_diagnostic_only",), slow.warnings)
        self.assertFalse(too_large.passed)
        self.assertTrue(
            any("peak_rss_limit_exceeded" in item for item in too_large.findings)
        )


class PerformanceGateCliTests(unittest.TestCase):
    def test_controlled_cli_rejects_wall_clock_limit_violations(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = repository / "scripts" / "ci_performance_gate.py"
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "slow-evidence.json"
            write_evidence(
                evidence,
                synthetic_evidence(
                    cold=[60_000] * 5,
                    checkpoint=[20_000] * 5,
                    graph=[999_999] * 5,
                ),
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "verify",
                    "--evidence",
                    str(evidence),
                    "--controlled",
                ],
                cwd=repository,
                capture_output=True,
                check=False,
                text=True,
            )

        self.assertEqual(1, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertFalse(output["passed"])
        self.assertIn(
            "cold_replay:p95_limit_exceeded:60000>15000",
            output["findings"],
        )

    def test_cli_verifies_canonical_evidence_without_running_benchmark(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        script = repository / "scripts" / "ci_performance_gate.py"
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence.json"
            write_evidence(evidence, synthetic_evidence())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "verify",
                    "--evidence",
                    str(evidence),
                    "--baseline",
                    str(evidence),
                    "--controlled",
                    "--require-baseline",
                ],
                cwd=repository,
                capture_output=True,
                check=False,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertTrue(output["passed"])
        self.assertEqual([], output["findings"])


if __name__ == "__main__":
    unittest.main()
