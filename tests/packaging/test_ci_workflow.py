from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class CiWorkflowTests(unittest.TestCase):
    def test_official_trellis_job_emits_revision_bound_cross_platform_evidence(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        trellis_job = workflow.split("  trellis-official-integration:", 1)[1].split(
            "\n  python:", 1
        )[0]

        self.assertIn("os: [ubuntu-latest, windows-latest]", trellis_job)
        self.assertIn("python scripts/ci_trellis_integration.py", trellis_job)
        self.assertIn('--revision "${{ github.sha }}"', trellis_job)
        self.assertIn('--platform "${{ matrix.os }}"', trellis_job)
        self.assertIn("trellis-integration-summary.json", trellis_job)
        self.assertIn("active-m1-trellis-${{ matrix.os }}", trellis_job)
        self.assertIn("$registry = 'https://registry.npmjs.org'", trellis_job)
        self.assertEqual(4, trellis_job.count("--registry $registry"))
        self.assertEqual(2, trellis_job.count("--pack-destination $artifactRoot"))
        self.assertIn(
            "7b97e4247f54e71f22ff80caa328d9e68fb81908f984f15d70a4d81cc2a0306c",
            trellis_job,
        )
        self.assertIn(
            "3af3e71fbaba3b4e7f081ca7df39dc9d00f9c527d855dc159263f4a34cf8587a",
            trellis_job,
        )
        self.assertNotIn("@latest", trellis_job)

    def test_first_party_actions_are_pinned_to_immutable_commits(self) -> None:
        workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml"))
        )
        action_references = re.findall(
            r"uses:\s+(actions/[^@\s]+)@([^\s#]+)", workflows
        )

        self.assertGreater(len(action_references), 0)
        for action, reference in action_references:
            with self.subTest(action=action):
                self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_python_distributions_are_built_once_as_one_canonical_artifact(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        distribution_job = workflow.split("  python-distribution:", 1)[1].split(
            "\n  python-distribution-install:", 1
        )[0]

        self.assertIn("python scripts/build_distributions.py --outdir dist", distribution_job)
        self.assertEqual(1, workflow.count("python scripts/build_distributions.py --outdir dist"))
        self.assertNotIn("python -m build", distribution_job)
        self.assertIn("Install hash-locked build toolchain", distribution_job)
        self.assertIn("--require-hashes --only-binary=:all:", distribution_job)
        self.assertIn("-r requirements/build.txt", distribution_job)
        self.assertNotIn("pip install build==", distribution_job)
        self.assertIn("Build the standalone Skill ZIP twice", distribution_job)
        self.assertIn("ci_distribution_evidence.py", distribution_job)
        self.assertIn("--mode build", distribution_job)
        self.assertIn("distribution-evidence.json", distribution_job)
        self.assertIn("name: active-m1-distribution", distribution_job)
        self.assertIn("if: success()", distribution_job)
        self.assertNotIn("if: always()", distribution_job)
        self.assertNotIn("            dist/\n", distribution_job)
        for artifact in (
            "dist/*.whl",
            "dist/*.tar.gz",
            "dist/wish-builder-skill.zip",
            "dist/wish-builder-skill.repeat.zip",
        ):
            self.assertIn(artifact, distribution_job)

    def test_build_toolchain_lock_contains_every_resolved_wheel_hash(self) -> None:
        lock = (REPOSITORY_ROOT / "requirements" / "build.txt").read_text(
            encoding="utf-8"
        )
        expected = {
            "build==1.3.0": "7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4",
            "colorama==0.4.6": "4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6",
            "packaging==25.0": "29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
            "pyproject_hooks==1.2.0": "9e5c6bfa8dcc30091c74b0cf803c81fdd29d94f01992a7707bc97babb1141913",
            "setuptools==83.0.0": "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
            "wheel==0.47.0": "212281cab4dff978f6cedd499cd893e1f620791ca6ff7107cf270781e587eced",
        }
        for package, digest in expected.items():
            with self.subTest(package=package):
                self.assertIn(package, lock)
                self.assertIn(f"--hash=sha256:{digest}", lock)

    def test_six_clean_install_cells_consume_the_exact_same_artifact(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        install_job = workflow.split("  python-distribution-install:", 1)[1].split(
            "\n  coverage:", 1
        )[0]

        self.assertIn("needs: [python-distribution]", install_job)
        self.assertIn("os: [ubuntu-latest, windows-latest]", install_job)
        self.assertIn('python: ["3.11", "3.12", "3.13"]', install_job)
        self.assertIn("name: active-m1-distribution", install_job)
        self.assertIn("--mode verify-cell", install_job)
        self.assertIn("--dist-dir built-distribution/dist", install_job)
        self.assertIn(
            "--build-evidence built-distribution/distribution-evidence.json",
            install_job,
        )
        self.assertIn('--revision "${{ github.sha }}"', install_job)
        self.assertIn('--platform "${{ matrix.os }}"', install_job)
        self.assertIn('--python-version "${{ matrix.python }}"', install_job)
        self.assertIn(
            '--cell-id "${{ matrix.os }}-py${{ matrix.python }}"', install_job
        )
        self.assertIn(
            "active-m1-distribution-install-${{ matrix.os }}-${{ matrix.python }}",
            install_job,
        )
        self.assertNotIn("python -m build", install_job)

    def test_six_python_cells_upload_revision_bound_summaries(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        python_job = workflow.split("  python:", 1)[1].split(
            "\n  python-distribution:", 1
        )[0]

        self.assertIn('os: [ubuntu-latest, windows-latest]', python_job)
        self.assertIn('python: ["3.11", "3.12", "3.13"]', python_job)
        self.assertIn("--summary-output ci-summary.json", python_job)
        self.assertIn('--revision "${{ github.sha }}"', python_job)
        self.assertIn("CI_CELL_ID: ${{ matrix.os }}-py${{ matrix.python }}", python_job)
        self.assertIn("active-m1-python-${{ matrix.os }}-${{ matrix.python }}", python_job)

    def test_shared_runner_records_diagnostic_performance_only(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        performance_job = workflow.split("  performance-evidence:", 1)[1]
        gate_step = performance_job.split(
            "- name: Record shared-runner envelope evidence", 1
        )[1]

        self.assertIn(
            "python scripts/ci_performance_gate.py run",
            gate_step,
        )
        self.assertNotIn("--controlled", gate_step)

    def test_safety_evidence_uses_full_history_and_explicit_changed_base(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        safety_job = workflow.split("  safety-evidence:", 1)[1].split(
            "\n  performance-evidence:", 1
        )[0]

        self.assertIn("fetch-depth: 0", safety_job)
        self.assertIn("Resolve changed-safety comparison base", safety_job)
        self.assertIn("github.event.pull_request.base.sha", safety_job)
        self.assertIn("github.event.before", safety_job)
        self.assertIn("refs/remotes/origin/$DEFAULT_BRANCH", safety_job)
        self.assertIn("git rev-parse --verify --end-of-options", safety_job)
        self.assertIn(
            '--base-ref "${{ steps.safety-base.outputs.base_ref }}"',
            safety_job,
        )

    def test_safety_base_resolution_is_specific_to_each_event_shape(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        safety_job = workflow.split("  safety-evidence:", 1)[1].split(
            "\n  performance-evidence:", 1
        )[0]
        resolver = safety_job.split(
            "- name: Resolve changed-safety comparison base", 1
        )[1].split("- name: Join fixed and changed-branch safety evidence", 1)[0]

        pull_request_case = (
            'if [[ "$EVENT_NAME" == "pull_request" && -n "$PR_BASE_SHA" ]]; then'
        )
        feature_push_case = (
            'elif [[ "$EVENT_NAME" == "push" && -n "$DEFAULT_BRANCH" '
            '&& "$PUSH_REF_NAME" != "$DEFAULT_BRANCH" ]]; then'
        )
        default_push_case = (
            'elif [[ "$EVENT_NAME" == "push" && -n "$DEFAULT_BRANCH" '
            '&& "$PUSH_REF_NAME" == "$DEFAULT_BRANCH" '
            '&& -n "$PUSH_BEFORE_SHA" && "$PUSH_BEFORE_SHA" != "$zero_sha" ]]; then'
        )

        self.assertLess(resolver.index(pull_request_case), resolver.index(feature_push_case))
        self.assertLess(resolver.index(feature_push_case), resolver.index(default_push_case))

        pull_request_clause = resolver.split(pull_request_case, 1)[1].split(
            feature_push_case, 1
        )[0]
        self.assertIn('base_ref="$PR_BASE_SHA"', pull_request_clause)

        feature_push_clause = resolver.split(feature_push_case, 1)[1].split(
            default_push_case, 1
        )[0]
        self.assertNotIn("PUSH_BEFORE_SHA", feature_push_clause)
        self.assertIn('default_ref="refs/remotes/origin/$DEFAULT_BRANCH"', feature_push_clause)
        self.assertIn('git merge-base --all HEAD "$default_ref"', feature_push_clause)
        self.assertIn('"${#merge_bases[@]}" -ne 1', feature_push_clause)
        self.assertIn('base_ref="${merge_bases[0]}"', feature_push_clause)

        default_push_clause = resolver.split(default_push_case, 1)[1].split("else", 1)[0]
        self.assertIn('base_ref="$PUSH_BEFORE_SHA"', default_push_clause)

    def test_final_job_downloads_current_run_artifacts_and_fails_closed(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        final_job = workflow.split("  active-m1-evidence-packet:", 1)[1]

        for required in (
            "trellis-official-integration",
            "python",
            "python-distribution",
            "python-distribution-install",
            "coverage",
            "mutation",
            "safety-evidence",
            "performance-evidence",
        ):
            self.assertIn(f"      - {required}\n", final_job)
        self.assertIn("if: ${{ always() }}", final_job)
        self.assertIn("pattern: active-m1-python-*", final_job)
        self.assertIn("pattern: active-m1-trellis-*", final_job)
        self.assertIn("pattern: active-m1-performance-*", final_job)
        self.assertIn("pattern: active-m1-distribution-install-*", final_job)
        self.assertNotIn("run-id:", final_job)
        self.assertIn("ci_evidence_packet.py build", final_job)
        self.assertIn('--candidate-revision "${{ github.sha }}"', final_job)
        self.assertIn("fetch-depth: 0", final_job)
        self.assertIn("Resolve trusted packet safety base", final_job)
        self.assertIn(
            '--safety-base-ref "${{ steps.packet-safety-base.outputs.base_ref }}"',
            final_job,
        )
        self.assertIn("active-m1-evidence-packet.sha256", final_job)

    def test_each_gate_uploads_a_revision_bound_raw_inventory(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        for kind, manifest in (
            ("coverage", "coverage-raw-evidence.json"),
            ("mutation", "mutation-raw-evidence.json"),
            ("safety", "safety-raw-evidence.json"),
            ("performance", "performance-raw-evidence.json"),
        ):
            self.assertIn(f"--kind {kind}", workflow)
            self.assertIn(manifest, workflow)
        self.assertIn("--file coverage.json", workflow)
        self.assertIn("--changed-lines-output changed-lines.json", workflow)
        self.assertIn("--file changed-lines.json", workflow)
        self.assertIn("--file performance-evidence.json", workflow)
        self.assertIn("--file performance-gate.raw.json", workflow)


if __name__ == "__main__":
    unittest.main()
