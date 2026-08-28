from __future__ import annotations

import hashlib
import io
import json
import tarfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.ci_coverage_gate import (
    DEFAULT_SOURCE_ROOT,
    discover_grouped_sources,
    discover_safety_sources,
    evaluate_report,
)
from scripts.ci_distribution_evidence import build_distribution_evidence
from scripts.ci_evidence_packet import (
    _REQUIRED_PLATFORM_SKIPS,
    EvidencePacketError,
    build_evidence_packet,
    canonical_json_bytes,
    main,
    record_raw_evidence,
    stamp_performance_gate,
)
from scripts.ci_mutation_gate import (
    DEFAULT_MUTATIONS,
    MINIMUM_MUTATION_SCORE,
    REPORT_SCHEMA_VERSION as MUTATION_SCHEMA_VERSION,
)
from scripts.ci_safety_evidence import (
    CHANGED_LINES_SCHEMA_VERSION,
    evaluate_safety_evidence,
)
from scripts.ci_test_suite import discover_suite, discover_test_ids, test_id_digest
from scripts.ci_trellis_integration import (
    EXPECTED_NODE_TEST_COUNT,
    NODE_TEST_FILES,
    PYTHON_TEST_MODULES,
    build_summary as build_trellis_summary,
    discover_python_test_ids,
)
from tests.performance.evidence import build_evidence, digest, evaluate_gate


def _zip_archive_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()


def _sdist_archive_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.mode = 0o644
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _synthetic_performance_evidence(platform_name: str) -> dict[str, object]:
    identity = {
        "clock": {
            "implementation": "test-clock",
            "monotonic": True,
            "resolution_ns": 100,
        },
        "platform": {
            "architecture_bits": 64,
            "cpu_identifier": f"test-cpu-{platform_name}",
            "logical_cpu_count": 8,
            "machine": "x86_64",
            "os_name": "nt" if platform_name == "windows-latest" else "posix",
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
        environment={"identity": identity, "identity_digest": digest(identity)},
        replay_identity=replay_identity,
        cold_samples_ms=[100, 101, 102, 103, 104],
        checkpoint_samples_ms=[10, 11, 12, 13, 14],
        peak_rss_bytes=128 * 1024 * 1024,
        graph_identity=graph_identity,
        graph_samples_ms=[20, 21, 22, 23, 24],
        recorded_at_utc="2026-08-19T00:00:00Z",
    )


def _diagnostic_gate(
    evidence: dict[str, object], evidence_path: str
) -> dict[str, object]:
    report = evaluate_gate(evidence, None, controlled=False, require_baseline=False)
    replay = evidence["workloads"]["replay_100000_events"]
    graph = evidence["workloads"]["graph_64_tasks_512_edges"]
    return {
        **report.to_primitive(),
        "environment_digest": evidence["environment"]["identity_digest"],
        "evidence": evidence_path,
        "summaries": {
            "checkpoint_tail": replay["summaries"]["checkpoint_tail"],
            "cold_replay": replay["summaries"]["cold_replay"],
            "graph_batch": graph["summaries"]["batch"],
            "peak_rss_bytes": replay["measurements"]["peak_rss_bytes"],
        },
    }


class EvidencePacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate = "a" * 40
        self.base = "b" * 40
        self.needs = {
            name: {"outputs": {}, "result": "success"}
            for name in (
                "coverage",
                "mutation",
                "performance-evidence",
                "python",
                "python-distribution",
                "python-distribution-install",
                "safety-evidence",
                "trellis-official-integration",
            )
        }
        self.matrix_test_ids = discover_test_ids(
            discover_suite(exclude_packages=frozenset({"performance"}))
        )
        self.matrix_test_digest = test_id_digest(self.matrix_test_ids)
        self._write_fixture()
        collector = patch(
            "scripts.ci_evidence_packet.collect_changed_lines",
            side_effect=lambda *args, **kwargs: deepcopy(self.changed_lines),
        )
        collector.start()
        self.addCleanup(collector.stop)

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(value))

    def _write_fixture(self) -> None:
        trellis_python_ids = discover_python_test_ids()
        trellis_python_digest = test_id_digest(trellis_python_ids)
        for platform in ("ubuntu-latest", "windows-latest"):
            trellis_root = self.root / f"trellis-{platform}"
            self._write_json(
                trellis_root / "trellis-integration-summary.json",
                build_trellis_summary(
                    revision=self.candidate,
                    platform=platform,
                    node={
                        "cancelled": 0,
                        "failed": 0,
                        "passed": EXPECTED_NODE_TEST_COUNT,
                        "skipped": 0,
                        "test_files": list(NODE_TEST_FILES),
                        "tests_run": EXPECTED_NODE_TEST_COUNT,
                        "todo": 0,
                    },
                    python={
                        "errors": 0,
                        "failures": 0,
                        "skipped": 0,
                        "test_ids_digest": trellis_python_digest,
                        "test_modules": list(PYTHON_TEST_MODULES),
                        "tests_run": len(trellis_python_ids),
                    },
                ),
            )
        for platform in ("ubuntu-latest", "windows-latest"):
            for version in ("3.11", "3.12", "3.13"):
                cell_root = self.root / f"python-{platform}-{version}"
                skipped_tests = [
                    {"reason": reason, "test_id": test_id}
                    for test_id, reason in sorted(_REQUIRED_PLATFORM_SKIPS[platform])
                ]
                self._write_json(
                    cell_root / "ci-summary.json",
                    {
                        "cell_id": f"{platform}-py{version}",
                        "discovered_test_count": len(self.matrix_test_ids),
                        "discovered_test_ids_digest": self.matrix_test_digest,
                        "errors": 0,
                        "executed_test_count": len(self.matrix_test_ids),
                        "executed_test_ids_digest": self.matrix_test_digest,
                        "failures": 0,
                        "github_sha": self.candidate,
                        "platform": platform,
                        "python_version": version,
                        "revision": self.candidate,
                        "schema_version": 2,
                        "skipped": len(skipped_tests),
                        "skipped_tests": skipped_tests,
                        "status": "passed",
                        "tests_run": len(self.matrix_test_ids),
                    },
                )

        coverage_root = self.root / "coverage"
        governed_sources = discover_grouped_sources(DEFAULT_SOURCE_ROOT)
        raw_coverage = {
            "files": {
                path: {
                    "excluded_lines": [],
                    "executed_branches": [[1, 2]],
                    "executed_lines": [1],
                    "missing_branches": [],
                    "missing_lines": [],
                    "summary": {
                        "covered_branches": 1,
                        "covered_lines": 1,
                        "missing_branches": 0,
                        "missing_lines": 0,
                        "num_branches": 1,
                        "num_statements": 1,
                    },
                }
                for path in governed_sources
            },
            "meta": {"branch_coverage": True},
        }
        coverage_gate = evaluate_report(
            raw_coverage,
            governed_sources,
            discover_safety_sources(DEFAULT_SOURCE_ROOT),
        )
        self._write_json(coverage_root / "coverage.json", raw_coverage)
        self._write_json(coverage_root / "coverage-gate.json", coverage_gate)
        self._write_inventory(
            "coverage",
            coverage_root,
            ("coverage.json", "coverage-gate.json"),
            "coverage-raw-evidence.json",
        )
        mutation_root = self.root / "mutation"
        baseline_test_ids = tuple(
            dict.fromkeys(
                test_id for spec in DEFAULT_MUTATIONS for test_id in spec.test_ids
            )
        )
        self._write_json(
            mutation_root / "mutation-report.json",
            {
                "baseline": {
                    "error_test_ids": [],
                    "errors": 0,
                    "failed_test_ids": [],
                    "failures": 0,
                    "infrastructure_error": None,
                    "skipped": 0,
                    "successful": True,
                    "tests_run": len(baseline_test_ids),
                },
                "minimum_score": float(MINIMUM_MUTATION_SCORE),
                "mutation_count": len(DEFAULT_MUTATIONS),
                "policy": {
                    "errors": 0,
                    "killed": len(DEFAULT_MUTATIONS),
                    "passed": True,
                    "reasons": [],
                    "score": 100.0,
                    "survived": 0,
                    "surviving_safety_mutations": [],
                },
                "results": [
                    {
                        "invariant": spec.invariant,
                        "mutation_id": spec.mutation_id,
                        "safety_invariant": spec.safety_invariant,
                        "source_path": spec.source_path,
                        "status": "killed",
                        "test_ids": list(spec.test_ids),
                        "test_run": {
                            "error_test_ids": [],
                            "errors": 0,
                            "failed_test_ids": list(spec.test_ids),
                            "failures": len(spec.test_ids),
                            "infrastructure_error": None,
                            "skipped": 0,
                            "successful": False,
                            "tests_run": len(spec.test_ids),
                        },
                    }
                    for spec in DEFAULT_MUTATIONS
                ],
                "schema_version": MUTATION_SCHEMA_VERSION,
                "status": "passed",
            },
        )
        self._write_inventory(
            "mutation",
            mutation_root,
            ("mutation-report.json",),
            "mutation-raw-evidence.json",
        )
        safety_root = self.root / "safety"
        self.changed_lines = {
            "base_ref": self.base,
            "files": [],
            "head": self.candidate,
            "merge_base": self.base,
            "schema_version": CHANGED_LINES_SCHEMA_VERSION,
        }
        mutation_report = json.loads(
            (mutation_root / "mutation-report.json").read_text(encoding="utf-8")
        )
        safety = evaluate_safety_evidence(
            raw_coverage,
            mutation_report,
            self.changed_lines,
            trusted_changed_lines_report=self.changed_lines,
        )
        self.assertEqual("pass", safety["status"])
        self._write_json(safety_root / "changed-lines.json", self.changed_lines)
        self._write_json(safety_root / "safety-evidence.json", safety)
        self._write_inventory(
            "safety",
            safety_root,
            ("changed-lines.json", "safety-evidence.json"),
            "safety-raw-evidence.json",
        )

        dist = self.root / "distribution"
        dist.mkdir()
        license_bytes = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()
        package_metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: wish-builder\n"
            b"Version: 0.1\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        (dist / "wish_builder-0.1-py3-none-any.whl").write_bytes(
            _zip_archive_bytes(
                {
                    "wish_builder/__init__.py": b"",
                    "wish_builder-0.1.dist-info/licenses/LICENSE": license_bytes,
                    "wish_builder-0.1.dist-info/METADATA": package_metadata,
                }
            )
        )
        (dist / "wish_builder-0.1.tar.gz").write_bytes(
            _sdist_archive_bytes(
                {
                    "wish_builder-0.1/README.md": b"fixture",
                    "wish_builder-0.1/LICENSE": license_bytes,
                    "wish_builder-0.1/PKG-INFO": package_metadata,
                }
            )
        )
        skill = dist / "wish-builder-skill.zip"
        repeat = dist / "wish-builder-skill.repeat.zip"
        skill_bytes = _zip_archive_bytes(
            {
                "wish-builder/SKILL.md": b"fixture",
                "wish-builder/LICENSE": license_bytes,
            }
        )
        skill.write_bytes(skill_bytes)
        repeat.write_bytes(skill_bytes)
        distribution = build_distribution_evidence(
            dist,
            skill,
            repeat,
            revision=self.candidate,
        )
        distribution_path = dist / "distribution-evidence.json"
        self._write_json(distribution_path, distribution)
        install_artifacts = sorted(
            (
                item
                for item in distribution["artifacts"]
                if item["kind"] in {"wheel", "sdist"}
            ),
            key=lambda item: str(item["kind"]),
        )
        distribution_sha256 = "sha256:" + hashlib.sha256(
            distribution_path.read_bytes()
        ).hexdigest()
        for platform in ("ubuntu-latest", "windows-latest"):
            for version in ("3.11", "3.12", "3.13"):
                cell_id = f"{platform}-py{version}"
                install_evidence = {
                    "artifacts": install_artifacts,
                    "cell_id": cell_id,
                    "distribution_evidence_digest": distribution["evidence_digest"],
                    "distribution_evidence_sha256": distribution_sha256,
                    "github_sha": self.candidate,
                    "installations": [
                        {
                            "artifact_kind": item["kind"],
                            "artifact_sha256": item["sha256"],
                            "status": "passed",
                        }
                        for item in install_artifacts
                    ],
                    "platform": platform,
                    "python_version": version,
                    "revision": self.candidate,
                    "runtime": {
                        "implementation": "cpython",
                        "python_full_version": f"{version}.0",
                        "sys_platform": (
                            "win32" if platform == "windows-latest" else "linux"
                        ),
                    },
                    "schema_version": 1,
                    "status": "passed",
                }
                install_evidence["evidence_digest"] = "sha256:" + hashlib.sha256(
                    canonical_json_bytes(install_evidence)
                ).hexdigest()
                self._write_json(
                    self.root
                    / f"distribution-install-{platform}-{version}"
                    / "distribution-install-evidence.json",
                    install_evidence,
                )

        for platform in ("ubuntu-latest", "windows-latest"):
            performance_root = self.root / f"performance-{platform}"
            evidence = _synthetic_performance_evidence(platform)
            gate = _diagnostic_gate(
                evidence, str((Path("/runner") / "performance-evidence.json"))
            )
            self._write_json(performance_root / "performance-evidence.json", evidence)
            self._write_json(performance_root / "performance-gate.raw.json", gate)
            self._write_json(
                performance_root / "performance-gate.json",
                stamp_performance_gate(
                    gate,
                    platform=platform,
                    revision=self.candidate,
                ),
            )
            self._write_inventory(
                "performance",
                performance_root,
                (
                    "performance-evidence.json",
                    "performance-gate.raw.json",
                    "performance-gate.json",
                ),
                "performance-raw-evidence.json",
                cell_id=platform,
            )

    def _write_inventory(
        self,
        kind: str,
        directory: Path,
        filenames: tuple[str, ...],
        output: str,
        *,
        cell_id: str | None = None,
    ) -> None:
        inventory = record_raw_evidence(
            [directory / filename for filename in filenames],
            kind=kind,
            revision=self.candidate,
            cell_id=cell_id,
        )
        self._write_json(directory / output, inventory)

    def build(self) -> dict[str, object]:
        return build_evidence_packet(
            self.root,
            candidate_revision=self.candidate,
            needs=self.needs,
            safety_base_ref=self.base,
            workflow_run_id="1234",
            workflow_run_attempt="2",
        )

    def _reseal_distribution_claims(self) -> None:
        dist = self.root / "distribution"
        evidence_path = dist / "distribution-evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        for artifact in evidence["artifacts"]:
            raw = dist / artifact["path"]
            artifact["sha256"] = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
            artifact["size_bytes"] = raw.stat().st_size
        digest_input = dict(evidence)
        digest_input.pop("evidence_digest")
        evidence["evidence_digest"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes(digest_input)
        ).hexdigest()
        self._write_json(evidence_path, evidence)

    def build_cli_arguments(self, output: Path, digest_output: Path) -> list[str]:
        return [
            "build",
            "--evidence-root",
            str(self.root),
            "--candidate-revision",
            self.candidate,
            "--needs-json",
            json.dumps(self.needs),
            "--workflow-run-id",
            "1234",
            "--workflow-run-attempt",
            "1",
            "--safety-base-ref",
            self.base,
            "--output",
            str(output),
            "--digest-output",
            str(digest_output),
        ]

    def assert_packet_digests(
        self, output: Path, digest_output: Path, packet: dict[str, object]
    ) -> None:
        digest_input = dict(packet)
        declared_digest = digest_input.pop("packet_digest")
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            declared_digest,
        )
        encoded = output.read_bytes()
        self.assertEqual(
            "sha256:" + hashlib.sha256(encoded).hexdigest() + "\n",
            digest_output.read_text(encoding="ascii"),
        )

    def test_builds_one_canonical_revision_bound_packet(self) -> None:
        packet = self.build()

        digest_input = dict(packet)
        packet_digest = digest_input.pop("packet_digest")
        self.assertEqual("passed", packet["status"])
        self.assertEqual(self.candidate, packet["candidate_revision"])
        self.assertEqual(6, len(packet["python_matrix"]))
        self.assertEqual(6, len(packet["distribution_matrix"]))
        self.assertEqual(2, len(packet["trellis_matrix"]))
        self.assertEqual(2, len(packet["gates"]["performance"]))
        self.assertEqual(2, len(packet["raw_evidence"]["coverage"]["files"]))
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            packet_digest,
        )

    def test_build_reuses_the_single_trusted_git_collection(self) -> None:
        with patch(
            "scripts.ci_safety_evidence.collect_changed_lines",
            side_effect=AssertionError("safety evaluator recollected Git evidence"),
        ) as safety_collector:
            packet = self.build()

        self.assertEqual("passed", packet["status"])
        safety_collector.assert_not_called()

    def test_missing_or_duplicate_matrix_cell_is_rejected(self) -> None:
        missing = self.root / "python-ubuntu-latest-3.11" / "ci-summary.json"
        missing.unlink()
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("evidence_count_invalid", raised.exception.code)

    def test_distribution_install_matrix_requires_six_unique_cells(self) -> None:
        missing = (
            self.root
            / "distribution-install-ubuntu-latest-3.11"
            / "distribution-install-evidence.json"
        )
        missing.unlink()
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("evidence_count_invalid", raised.exception.code)

        source = (
            self.root
            / "distribution-install-ubuntu-latest-3.12"
            / "distribution-install-evidence.json"
        )
        missing.parent.mkdir(parents=True, exist_ok=True)
        duplicate = json.loads(source.read_text(encoding="utf-8"))
        self._write_json(missing, duplicate)
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_install_matrix_incomplete", raised.exception.code)

    def test_distribution_install_cell_must_cite_canonical_distribution_bytes(self) -> None:
        cell_path = (
            self.root
            / "distribution-install-windows-latest-3.13"
            / "distribution-install-evidence.json"
        )
        cell = json.loads(cell_path.read_text(encoding="utf-8"))
        cell["artifacts"][0]["sha256"] = "sha256:" + "f" * 64
        digest_input = dict(cell)
        digest_input.pop("evidence_digest")
        cell["evidence_digest"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes(digest_input)
        ).hexdigest()
        self._write_json(cell_path, cell)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_install_artifact_mismatch", raised.exception.code)

    def test_failed_distribution_install_matrix_job_cannot_seal_packet(self) -> None:
        self.needs["python-distribution-install"]["result"] = "failure"

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("job_not_passed", raised.exception.code)
        self.assertIn("python-distribution-install", str(raised.exception))

    def test_trellis_matrix_is_revision_bound_and_revalidated(self) -> None:
        summary_path = (
            self.root
            / "trellis-windows-latest"
            / "trellis-integration-summary.json"
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["node"]["passed"] = 20
        digest_input = dict(summary)
        digest_input.pop("summary_digest")
        summary["summary_digest"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes(digest_input)
        ).hexdigest()
        self._write_json(summary_path, summary)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("trellis_node_result_invalid", raised.exception.code)

    def test_one_of_one_skipped_matrix_forgery_is_rejected(self) -> None:
        summary_path = self.root / "python-windows-latest-3.13" / "ci-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        only_skip = next(iter(_REQUIRED_PLATFORM_SKIPS["windows-latest"]))
        one_test_digest = test_id_digest((only_skip[0],))
        summary.update(
            {
                "discovered_test_count": 1,
                "discovered_test_ids_digest": one_test_digest,
                "executed_test_count": 1,
                "executed_test_ids_digest": one_test_digest,
                "skipped": 1,
                "skipped_tests": [
                    {"reason": only_skip[1], "test_id": only_skip[0]}
                ],
                "tests_run": 1,
            }
        )
        self._write_json(summary_path, summary)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("test_cell_counts_invalid", raised.exception.code)

    def test_unapproved_matrix_skip_identity_is_rejected(self) -> None:
        summary_path = self.root / "python-ubuntu-latest-3.13" / "ci-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        existing_ids = {item["test_id"] for item in summary["skipped_tests"]}
        test_id = next(item for item in self.matrix_test_ids if item not in existing_ids)
        summary["skipped"] += 1
        summary["skipped_tests"].append(
            {"reason": "forged environmental skip", "test_id": test_id}
        )
        self._write_json(summary_path, summary)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("test_cell_skip_not_allowed", raised.exception.code)

    def test_missing_unpublished_trellis_runtime_is_an_allowed_skip(self) -> None:
        summary_path = self.root / "python-ubuntu-latest-3.13" / "ci-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        test_id = (
            "tests.adapters.test_trellis_projection."
            "TrellisProjectionAdapterIntegrationTests."
            "test_authoritative_projection_updates_task_and_replay_is_idempotent"
        )
        self.assertIn(test_id, self.matrix_test_ids)
        summary["skipped"] += 1
        summary["skipped_tests"].append(
            {
                "reason": "pinned local Trellis runtime is unavailable",
                "test_id": test_id,
            }
        )
        self._write_json(summary_path, summary)

        packet = self.build()
        cell = next(
            item
            for item in packet["python_matrix"]
            if item["cell_id"] == "ubuntu-latest-py3.13"
        )
        self.assertIn(
            {
                "reason": "pinned local Trellis runtime is unavailable",
                "test_id": test_id,
            },
            cell["skipped_tests"],
        )

    def test_duplicate_json_key_is_rejected(self) -> None:
        coverage = self.root / "coverage" / "coverage-gate.json"
        coverage.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
        self._write_inventory(
            "coverage",
            coverage.parent,
            ("coverage.json", "coverage-gate.json"),
            "coverage-raw-evidence.json",
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("duplicate_json_key", raised.exception.code)

    def test_changed_raw_distribution_file_is_rejected(self) -> None:
        (self.root / "distribution" / "wish_builder-0.1.tar.gz").write_bytes(
            b"tampered"
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_hash_mismatch", raised.exception.code)

    def test_packet_revalidates_skill_zip_bytes_after_claims_are_resealed(self) -> None:
        invalid = _zip_archive_bytes(
            {
                "wish-builder/LICENSE": b"not the repository license\n",
                "wish-builder/SKILL.md": b"fixture",
            }
        )
        dist = self.root / "distribution"
        (dist / "wish-builder-skill.zip").write_bytes(invalid)
        (dist / "wish-builder-skill.repeat.zip").write_bytes(invalid)
        self._reseal_distribution_claims()

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_content_invalid", raised.exception.code)
        self.assertIn("skill_zip archive LICENSE does not match", str(raised.exception))

    def test_packet_revalidates_wheel_metadata_after_claims_are_resealed(self) -> None:
        license_bytes = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()
        invalid_metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: another-project\n"
            b"Version: 0.1\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        wheel = self.root / "distribution" / "wish_builder-0.1-py3-none-any.whl"
        wheel.write_bytes(
            _zip_archive_bytes(
                {
                    "wish_builder/__init__.py": b"",
                    "wish_builder-0.1.dist-info/METADATA": invalid_metadata,
                    "wish_builder-0.1.dist-info/licenses/LICENSE": license_bytes,
                }
            )
        )
        self._reseal_distribution_claims()

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_content_invalid", raised.exception.code)
        self.assertIn("Name: wish-builder", str(raised.exception))

    def test_packet_revalidates_sdist_metadata_after_claims_are_resealed(self) -> None:
        license_bytes = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()
        invalid_metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: another-project\n"
            b"Version: 0.1\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        sdist = self.root / "distribution" / "wish_builder-0.1.tar.gz"
        sdist.write_bytes(
            _sdist_archive_bytes(
                {
                    "wish_builder-0.1/LICENSE": license_bytes,
                    "wish_builder-0.1/PKG-INFO": invalid_metadata,
                    "wish_builder-0.1/README.md": b"fixture",
                }
            )
        )
        self._reseal_distribution_claims()

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("distribution_content_invalid", raised.exception.code)
        self.assertIn("Name: wish-builder", str(raised.exception))

    def test_rewritten_safety_provenance_is_rejected(self) -> None:
        safety_path = self.root / "safety" / "safety-evidence.json"
        safety = json.loads(safety_path.read_text(encoding="utf-8"))
        safety["provenance"]["merge_base"] = self.candidate
        digest_input = {
            "changed_branches": safety["changed_branches"],
            "changed_files": safety["changed_files"],
            "invariants": safety["invariants"],
            "provenance": safety["provenance"],
        }
        safety["evidence_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                digest_input,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        self._write_json(safety_path, safety)
        self._write_inventory(
            "safety",
            safety_path.parent,
            ("changed-lines.json", "safety-evidence.json"),
            "safety-raw-evidence.json",
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("safety_gate_mismatch", raised.exception.code)

    def test_raw_gate_tampering_is_rejected_before_normalization(self) -> None:
        coverage = self.root / "coverage" / "coverage.json"
        coverage.write_bytes(b'{"tampered":true}\n')
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("raw_evidence_hash_mismatch", raised.exception.code)

    def test_rewritten_inventory_cannot_hide_invalid_raw_coverage(self) -> None:
        coverage = self.root / "coverage" / "coverage.json"
        self._write_json(coverage, {"raw": "not a coverage.py report"})
        self._write_inventory(
            "coverage",
            coverage.parent,
            ("coverage.json", "coverage-gate.json"),
            "coverage-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("coverage_raw_invalid", raised.exception.code)

    def test_rewritten_inventory_cannot_hide_coverage_gate_drift(self) -> None:
        coverage_path = self.root / "coverage" / "coverage.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        first_file = next(iter(coverage["files"].values()))
        first_file["summary"].update(
            {
                "covered_branches": 0,
                "missing_branches": 1,
                "num_branches": 1,
            }
        )
        self._write_json(coverage_path, coverage)
        self._write_inventory(
            "coverage",
            coverage_path.parent,
            ("coverage.json", "coverage-gate.json"),
            "coverage-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("coverage_gate_mismatch", raised.exception.code)

    def test_gate_cannot_remove_a_fixed_safety_file_to_hide_zero_branches(self) -> None:
        coverage_path = self.root / "coverage" / "coverage.json"
        gate_path = self.root / "coverage" / "coverage-gate.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        safety_path = discover_safety_sources(DEFAULT_SOURCE_ROOT)[0]
        payload = coverage["files"][safety_path]
        payload["executed_branches"] = []
        payload["summary"].update(
            {
                "covered_branches": 0,
                "missing_branches": 0,
                "num_branches": 0,
            }
        )
        forged_gate = evaluate_report(
            coverage,
            discover_grouped_sources(DEFAULT_SOURCE_ROOT),
            (),
        )
        self.assertEqual("pass", forged_gate["status"])
        self._write_json(coverage_path, coverage)
        self._write_json(gate_path, forged_gate)
        self._write_inventory(
            "coverage",
            coverage_path.parent,
            ("coverage.json", "coverage-gate.json"),
            "coverage-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("coverage_registry_mismatch", raised.exception.code)

    def test_incomplete_fixed_mutation_registry_is_rejected(self) -> None:
        mutation_path = self.root / "mutation" / "mutation-report.json"
        mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
        mutation["results"].pop()
        mutation["mutation_count"] -= 1
        mutation["policy"]["killed"] -= 1
        self._write_json(mutation_path, mutation)
        self._write_inventory(
            "mutation",
            mutation_path.parent,
            ("mutation-report.json",),
            "mutation-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("mutation_registry_mismatch", raised.exception.code)

    def test_mutation_registry_metadata_drift_is_rejected(self) -> None:
        mutation_path = self.root / "mutation" / "mutation-report.json"
        mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
        mutation["results"][0]["invariant"] = "rewritten invariant"
        self._write_json(mutation_path, mutation)
        self._write_inventory(
            "mutation",
            mutation_path.parent,
            ("mutation-report.json",),
            "mutation-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("mutation_registry_mismatch", raised.exception.code)

    def test_mutation_source_and_direct_test_run_are_fully_bound(self) -> None:
        mutation_path = self.root / "mutation" / "mutation-report.json"
        mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
        result = mutation["results"][0]
        result["source_path"] = "wish_builder/adapters/forged.py"
        self._write_json(mutation_path, mutation)
        self._write_inventory(
            "mutation",
            mutation_path.parent,
            ("mutation-report.json",),
            "mutation-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("mutation_registry_mismatch", raised.exception.code)

        result["source_path"] = DEFAULT_MUTATIONS[0].source_path
        result["test_run"].update(
            {
                "failed_test_ids": [],
                "failures": 0,
                "successful": True,
                "tests_run": 0,
            }
        )
        self._write_json(mutation_path, mutation)
        self._write_inventory(
            "mutation",
            mutation_path.parent,
            ("mutation-report.json",),
            "mutation-raw-evidence.json",
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("mutation_malformed", raised.exception.code)

    def test_raw_inventory_from_another_candidate_is_rejected(self) -> None:
        inventory_path = self.root / "coverage" / "coverage-raw-evidence.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory["github_sha"] = self.base
        inventory["revision"] = self.base
        digest_input = dict(inventory)
        digest_input.pop("inventory_digest")
        inventory["inventory_digest"] = "sha256:" + hashlib.sha256(
            canonical_json_bytes(digest_input)
        ).hexdigest()
        self._write_json(inventory_path, inventory)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("revision_mismatch", raised.exception.code)

    def test_performance_raw_gate_tamper_and_rewritten_inventory_are_rejected(self) -> None:
        performance_root = self.root / "performance-ubuntu-latest"
        raw_gate_path = performance_root / "performance-gate.raw.json"
        raw_gate = json.loads(raw_gate_path.read_text(encoding="utf-8"))
        raw_gate["warnings"].append("tampered")
        self._write_json(raw_gate_path, raw_gate)

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("raw_evidence_hash_mismatch", raised.exception.code)

        self._write_inventory(
            "performance",
            performance_root,
            (
                "performance-evidence.json",
                "performance-gate.raw.json",
                "performance-gate.json",
            ),
            "performance-raw-evidence.json",
            cell_id="ubuntu-latest",
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("performance_gate_mismatch", raised.exception.code)

    def test_synchronized_hollow_performance_forgery_is_rejected(self) -> None:
        performance_root = self.root / "performance-ubuntu-latest"
        raw_gate_path = performance_root / "performance-gate.raw.json"
        wrapper_path = performance_root / "performance-gate.json"
        evidence_path = performance_root / "performance-evidence.json"
        gate = json.loads(raw_gate_path.read_text(encoding="utf-8"))
        hollow = {
            "environment": {"identity_digest": gate["environment_digest"]},
            "schema_version": 2,
            "workloads": {
                "graph_64_tasks_512_edges": {
                    "summaries": {"batch": gate["summaries"]["graph_batch"]}
                },
                "replay_100000_events": {
                    "measurements": {
                        "peak_rss_bytes": gate["summaries"]["peak_rss_bytes"]
                    },
                    "summaries": {
                        "checkpoint_tail": gate["summaries"]["checkpoint_tail"],
                        "cold_replay": gate["summaries"]["cold_replay"],
                    },
                },
            },
        }
        self._write_json(evidence_path, hollow)
        self._write_json(raw_gate_path, gate)
        self._write_json(
            wrapper_path,
            stamp_performance_gate(
                gate, platform="ubuntu-latest", revision=self.candidate
            ),
        )
        self._write_inventory(
            "performance",
            performance_root,
            (
                "performance-evidence.json",
                "performance-gate.raw.json",
                "performance-gate.json",
            ),
            "performance-raw-evidence.json",
            cell_id="ubuntu-latest",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("performance_raw_invalid", raised.exception.code)

    def test_rewritten_changed_lines_inventory_cannot_hide_git_diff(self) -> None:
        changed_path = self.root / "safety" / "changed-lines.json"
        changed = json.loads(changed_path.read_text(encoding="utf-8"))
        changed["files"] = [
            {
                "hunks": [
                    {
                        "new_count": 1,
                        "new_first": 1,
                        "old_count": 0,
                        "old_first": 0,
                    }
                ],
                "new_path": "wish_builder/adapters/git_identity.py",
                "old_path": None,
                "status": "A",
            }
        ]
        self._write_json(changed_path, changed)
        self._write_inventory(
            "safety",
            changed_path.parent,
            ("changed-lines.json", "safety-evidence.json"),
            "safety-raw-evidence.json",
        )

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("safety_changed_lines_mismatch", raised.exception.code)

    def test_safety_report_cannot_select_a_different_trusted_base(self) -> None:
        with self.assertRaises(EvidencePacketError) as raised:
            build_evidence_packet(
                self.root,
                candidate_revision=self.candidate,
                needs=self.needs,
                safety_base_ref="c" * 40,
                workflow_run_id="1234",
                workflow_run_attempt="2",
            )
        self.assertEqual("safety_base_mismatch", raised.exception.code)

    def test_hollow_rewritten_safety_invariant_is_rejected(self) -> None:
        safety_path = self.root / "safety" / "safety-evidence.json"
        safety = json.loads(safety_path.read_text(encoding="utf-8"))
        safety["invariants"][0]["mutation_id"] = "UNREGISTERED-MUTATION"
        digest_input = {
            "changed_branches": safety["changed_branches"],
            "changed_files": safety["changed_files"],
            "invariants": safety["invariants"],
            "provenance": safety["provenance"],
        }
        safety["evidence_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                digest_input,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        self._write_json(safety_path, safety)
        self._write_inventory(
            "safety",
            safety_path.parent,
            ("changed-lines.json", "safety-evidence.json"),
            "safety-raw-evidence.json",
        )
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("safety_gate_mismatch", raised.exception.code)

    def test_revision_and_required_job_mismatches_are_rejected(self) -> None:
        summary_path = self.root / "python-windows-latest-3.13" / "ci-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["revision"] = self.base
        self._write_json(summary_path, summary)
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("revision_mismatch", raised.exception.code)

        summary["revision"] = self.candidate
        self._write_json(summary_path, summary)
        self.needs["coverage"]["result"] = "failure"
        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("job_not_passed", raised.exception.code)

    def test_failed_official_trellis_job_cannot_seal_a_passing_packet(self) -> None:
        self.needs["trellis-official-integration"]["result"] = "failure"

        with self.assertRaises(EvidencePacketError) as raised:
            self.build()
        self.assertEqual("job_not_passed", raised.exception.code)
        self.assertIn("trellis-official-integration", str(raised.exception))

    def test_cli_writes_a_canonical_failure_packet_and_raw_digest(self) -> None:
        output = self.root / "active-m1-evidence-packet.json"
        digest_output = self.root / "active-m1-evidence-packet.sha256"

        captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch("sys.stdout", captured):
            exit_code = main(
                [
                    "build",
                    "--evidence-root",
                    str(self.root),
                    "--candidate-revision",
                    self.candidate,
                    "--needs-json",
                    "{}",
                    "--workflow-run-id",
                    "1234",
                    "--workflow-run-attempt",
                    "1",
                    "--safety-base-ref",
                    self.base,
                    "--output",
                    str(output),
                    "--digest-output",
                    str(digest_output),
                ]
            )

        encoded = output.read_bytes()
        packet = json.loads(encoded)
        self.assertEqual(1, exit_code)
        self.assertEqual("failed", packet["status"])
        self.assertEqual("job_set_incomplete", packet["errors"][0]["code"])
        self.assert_packet_digests(output, digest_output, packet)

    def test_cli_writes_failure_packet_for_compatibility_loader_error(self) -> None:
        output = self.root / "unexpected-error-evidence-packet.json"
        digest_output = self.root / "unexpected-error-evidence-packet.sha256"

        captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with (
            patch("sys.stdout", captured),
            patch(
                "scripts.ci_evidence_packet.load_bundled_trellis_compatibility",
                side_effect=RuntimeError("compatibility loader failed"),
            ),
        ):
            exit_code = main(self.build_cli_arguments(output, digest_output))

        packet = json.loads(output.read_bytes())
        self.assertEqual(1, exit_code)
        self.assertEqual("failed", packet["status"])
        self.assertEqual("unexpected_error", packet["errors"][0]["code"])
        self.assertEqual("compatibility loader failed", packet["errors"][0]["message"])
        self.assert_packet_digests(output, digest_output, packet)

    def test_cli_does_not_catch_base_exceptions(self) -> None:
        for name, failure in (
            ("keyboard-interrupt", KeyboardInterrupt()),
            ("system-exit", SystemExit(7)),
        ):
            with self.subTest(failure=name):
                output = self.root / f"{name}-evidence-packet.json"
                digest_output = self.root / f"{name}-evidence-packet.sha256"
                with patch(
                    "scripts.ci_evidence_packet.load_bundled_trellis_compatibility",
                    side_effect=failure,
                ):
                    with self.assertRaises(type(failure)):
                        main(self.build_cli_arguments(output, digest_output))
                self.assertFalse(output.exists())
                self.assertFalse(digest_output.exists())


if __name__ == "__main__":
    unittest.main()
