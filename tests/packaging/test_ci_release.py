from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.ci_distribution_evidence import (
    build_distribution_evidence,
    canonical_json_bytes,
)
from scripts.ci_evidence_packet import SCHEMA_VERSION as EVIDENCE_PACKET_SCHEMA_VERSION
from scripts.ci_local_release import _require_clean_candidate, prepare_local_release
from scripts.ci_release import (
    EVIDENCE_PACKET_SCHEMA_VERSION as RELEASE_ACCEPTED_PACKET_SCHEMA_VERSION,
    ReleasePromotionError,
    prepare_release,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.1.0.dev0"
REVISION = "a" * 40
CI_RUN_ID = 123456
CI_RUN_ATTEMPT = 2


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return output.getvalue()


def _sdist_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class LocalCandidateBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repository"
        self.repository.mkdir()
        self._git("init", "--quiet")
        self._git("config", "user.name", "Wish Builder Tests")
        self._git("config", "user.email", "wish-builder-tests@invalid.example")
        (self.repository / "source.txt").write_text("committed\n", encoding="utf-8")
        self._git("add", "source.txt")
        self._git("commit", "--quiet", "-m", "fixture")
        self.revision = self._git("rev-parse", "HEAD").strip()

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(self.repository), *arguments),
            check=True,
            capture_output=True,
            shell=False,
            text=True,
            timeout=30,
        ).stdout

    def test_clean_candidate_must_match_checked_out_head(self) -> None:
        _require_clean_candidate(self.repository, self.revision)
        with self.assertRaisesRegex(
            ReleasePromotionError, "is not the checked-out HEAD"
        ):
            _require_clean_candidate(self.repository, "b" * 40)

    def test_tracked_or_untracked_changes_are_rejected(self) -> None:
        (self.repository / "source.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleasePromotionError, "is not clean"):
            _require_clean_candidate(self.repository, self.revision)

        self._git("restore", "source.txt")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        with self.assertRaisesRegex(ReleasePromotionError, "is not clean"):
            _require_clean_candidate(self.repository, self.revision)


class ReleasePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.distribution = self.root / "distribution"
        self.repository.mkdir()
        self.distribution.mkdir()
        self._write_repository()
        self._write_distribution_and_packet()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_repository(self) -> None:
        package = self.repository / "wish_builder"
        package.mkdir()
        (self.repository / "pyproject.toml").write_text(
            """[project]\nname = "wish-builder"\nversion = "0.1.0.dev0"\nlicense = "GPL-3.0-only"\n""",
            encoding="utf-8",
        )
        (package / "__init__.py").write_text(
            '__version__ = "0.1.0.dev0"\n', encoding="utf-8"
        )
        (self.repository / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "wish-builder"\nversion = "0.1.0.dev0"\n',
            encoding="utf-8",
        )
        (self.repository / "LICENSE").write_bytes(
            (REPOSITORY_ROOT / "LICENSE").read_bytes()
        )
        (self.repository / "THIRD_PARTY_NOTICES.md").write_text(
            "# Third-Party Notices\n", encoding="utf-8"
        )

    def _write_distribution_and_packet(self) -> None:
        license_bytes = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: wish-builder\n"
            b"Version: 0.1.0.dev0\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        wheel = self.distribution / f"wish_builder-{VERSION}-py3-none-any.whl"
        wheel.write_bytes(
            _zip_bytes(
                {
                    "wish_builder/__init__.py": b"",
                    f"wish_builder-{VERSION}.dist-info/METADATA": metadata,
                    f"wish_builder-{VERSION}.dist-info/licenses/LICENSE": license_bytes,
                }
            )
        )
        sdist = self.distribution / f"wish_builder-{VERSION}.tar.gz"
        sdist.write_bytes(
            _sdist_bytes(
                {
                    f"wish_builder-{VERSION}/LICENSE": license_bytes,
                    f"wish_builder-{VERSION}/PKG-INFO": metadata,
                    f"wish_builder-{VERSION}/README.md": b"fixture",
                }
            )
        )
        skill = self.distribution / "wish-builder-skill.zip"
        skill_repeat = self.distribution / "wish-builder-skill.repeat.zip"
        skill_bytes = _zip_bytes(
            {
                "wish-builder/LICENSE": license_bytes,
                "wish-builder/SKILL.md": b"fixture",
            }
        )
        skill.write_bytes(skill_bytes)
        skill_repeat.write_bytes(skill_bytes)

        evidence = build_distribution_evidence(
            self.distribution,
            skill,
            skill_repeat,
            revision=REVISION,
        )
        evidence_raw = canonical_json_bytes(evidence)
        (self.distribution / "distribution-evidence.json").write_bytes(evidence_raw)
        packet: dict[str, object] = {
            "candidate_revision": REVISION,
            "distribution": {
                "artifacts": evidence["artifacts"],
                "evidence_digest": evidence["evidence_digest"],
                "evidence_sha256": _sha256(evidence_raw),
                "schema_version": 1,
                "status": "passed",
            },
            "schema_version": EVIDENCE_PACKET_SCHEMA_VERSION,
            "status": "passed",
            "workflow": {
                "run_attempt": CI_RUN_ATTEMPT,
                "run_id": CI_RUN_ID,
            },
        }
        packet["packet_digest"] = _sha256(canonical_json_bytes(packet))
        packet_raw = canonical_json_bytes(packet)
        self.packet = self.root / "active-m1-evidence-packet.json"
        self.packet_digest = self.root / "active-m1-evidence-packet.sha256"
        self.packet.write_bytes(packet_raw)
        self.packet_digest.write_text(_sha256(packet_raw) + "\n", encoding="ascii")

    def _prepare(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "repository_root": self.repository,
            "distribution_root": self.distribution,
            "packet_path": self.packet,
            "packet_digest_path": self.packet_digest,
            "output_dir": self.root / "release-assets",
            "revision": REVISION,
            "version": VERSION,
            "tag": f"v{VERSION}",
            "ci_run_id": CI_RUN_ID,
            "ci_run_attempt": CI_RUN_ATTEMPT,
        }
        arguments.update(overrides)
        return prepare_release(**arguments)  # type: ignore[arg-type]

    def _write_local_manifest(self, **extra: object) -> dict[str, object]:
        manifest = json.loads(self.packet.read_text(encoding="ascii"))
        manifest.pop("packet_digest")
        manifest.pop("workflow")
        manifest.update(
            {
                "provenance_kind": "local",
                "schema_version": 1,
                "status": "passed",
                **extra,
            }
        )
        manifest["evidence_digest"] = _sha256(canonical_json_bytes(manifest))
        raw = canonical_json_bytes(manifest)
        self.local_manifest = self.root / "local-m1-evidence-manifest.json"
        self.local_manifest_digest = self.root / "local-m1-evidence-manifest.sha256"
        self.local_manifest.write_bytes(raw)
        self.local_manifest_digest.write_text(_sha256(raw) + "\n", encoding="ascii")
        return manifest

    def _prepare_local(self, **overrides: object) -> dict[str, object]:
        manifest = self._write_local_manifest()
        arguments: dict[str, object] = {
            "repository_root": self.repository,
            "evidence_root": self.root,
            "safety_base_ref": "b" * 40,
            "distribution_root": self.distribution,
            "manifest_path": self.local_manifest,
            "manifest_digest_path": self.local_manifest_digest,
            "output_dir": self.root / "local-release-assets",
            "revision": REVISION,
            "version": VERSION,
            "tag": f"v{VERSION}",
        }
        arguments.update(overrides)
        with patch(
            "scripts.ci_local_release.build_local_evidence_manifest",
            return_value=manifest,
        ), patch("scripts.ci_local_release._require_clean_candidate"):
            return prepare_local_release(**arguments)  # type: ignore[arg-type]

    def _reseal_distribution_claims_without_validation(self) -> None:
        evidence_path = self.distribution / "distribution-evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="ascii"))
        raw_paths = {
            "wheel": next(self.distribution.glob("*.whl")),
            "sdist": next(self.distribution.glob("*.tar.gz")),
            "skill_zip": self.distribution / "wish-builder-skill.zip",
            "skill_zip_repeat": self.distribution / "wish-builder-skill.repeat.zip",
        }
        for artifact in evidence["artifacts"]:
            raw = raw_paths[artifact["kind"]]
            artifact["path"] = raw.name
            artifact["sha256"] = _sha256(raw.read_bytes())
            artifact["size_bytes"] = raw.stat().st_size
        digest_input = dict(evidence)
        digest_input.pop("evidence_digest")
        evidence["evidence_digest"] = _sha256(canonical_json_bytes(digest_input))
        evidence_raw = canonical_json_bytes(evidence)
        evidence_path.write_bytes(evidence_raw)

        packet = json.loads(self.packet.read_text(encoding="ascii"))
        packet["distribution"]["artifacts"] = evidence["artifacts"]
        packet["distribution"]["evidence_digest"] = evidence["evidence_digest"]
        packet["distribution"]["evidence_sha256"] = _sha256(evidence_raw)
        packet.pop("packet_digest")
        packet["packet_digest"] = _sha256(canonical_json_bytes(packet))
        packet_raw = canonical_json_bytes(packet)
        self.packet.write_bytes(packet_raw)
        self.packet_digest.write_text(_sha256(packet_raw) + "\n", encoding="ascii")

    def test_promotes_exact_artifacts_and_writes_verifiable_checksums(self) -> None:
        manifest = self._prepare()
        output = self.root / "release-assets"

        self.assertEqual(REVISION, manifest["candidate_revision"])
        self.assertEqual(CI_RUN_ATTEMPT, manifest["ci_run_attempt"])
        self.assertEqual(8, manifest["artifact_count"])
        self.assertTrue(manifest["prerelease"])
        self.assertTrue((output / f"wish-builder-skill-{VERSION}.zip").is_file())
        self.assertEqual(
            (self.repository / "THIRD_PARTY_NOTICES.md").read_bytes(),
            (output / "THIRD_PARTY_NOTICES.md").read_bytes(),
        )

        checksum_lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        self.assertEqual(9, len(checksum_lines))
        for line in checksum_lines:
            digest, filename = line.split("  ", 1)
            self.assertEqual(
                digest,
                hashlib.sha256((output / filename).read_bytes()).hexdigest(),
            )

    def test_release_accepts_the_packet_producers_current_schema(self) -> None:
        self.assertEqual(
            EVIDENCE_PACKET_SCHEMA_VERSION,
            RELEASE_ACCEPTED_PACKET_SCHEMA_VERSION,
        )
        self.assertEqual(
            EVIDENCE_PACKET_SCHEMA_VERSION,
            json.loads(self.packet.read_text(encoding="ascii"))["schema_version"],
        )

    def test_local_release_uses_local_provenance_without_ci_identity(self) -> None:
        manifest = self._prepare_local()
        output = self.root / "local-release-assets"

        self.assertEqual("local", manifest["provenance_kind"])
        self.assertNotIn("ci_run_id", manifest)
        self.assertNotIn("ci_run_attempt", manifest)
        self.assertTrue((output / "local-m1-evidence-manifest.json").is_file())
        self.assertTrue((output / "local-m1-evidence-manifest.sha256").is_file())
        self.assertFalse((output / "active-m1-evidence-packet.json").exists())

    def test_local_release_rejects_ci_provenance(self) -> None:
        manifest = self._write_local_manifest(workflow={"run_id": 1})
        with patch(
            "scripts.ci_local_release.build_local_evidence_manifest",
            return_value=manifest,
        ), patch("scripts.ci_local_release._require_clean_candidate"):
            with self.assertRaisesRegex(
                ReleasePromotionError, "contains CI provenance"
            ):
                prepare_local_release(
                    repository_root=self.repository,
                    evidence_root=self.root,
                    safety_base_ref="b" * 40,
                    distribution_root=self.distribution,
                    manifest_path=self.local_manifest,
                    manifest_digest_path=self.local_manifest_digest,
                    output_dir=self.root / "local-release-assets",
                    revision=REVISION,
                    version=VERSION,
                    tag=f"v{VERSION}",
                )
        self.assertFalse((self.root / "local-release-assets").exists())

    def test_local_release_rejects_unreconstructable_evidence(self) -> None:
        manifest = self._write_local_manifest()
        rebuilt = dict(manifest)
        rebuilt["status"] = "failed"
        with patch(
            "scripts.ci_local_release.build_local_evidence_manifest",
            return_value=rebuilt,
        ), patch("scripts.ci_local_release._require_clean_candidate"):
            with self.assertRaisesRegex(
                ReleasePromotionError, "cannot be reconstructed"
            ):
                prepare_local_release(
                    repository_root=self.repository,
                    evidence_root=self.root,
                    safety_base_ref="b" * 40,
                    distribution_root=self.distribution,
                    manifest_path=self.local_manifest,
                    manifest_digest_path=self.local_manifest_digest,
                    output_dir=self.root / "local-release-assets",
                    revision=REVISION,
                    version=VERSION,
                    tag=f"v{VERSION}",
                )
        self.assertFalse((self.root / "local-release-assets").exists())

    def test_local_release_rejects_a_dirty_candidate_before_promotion(self) -> None:
        manifest = self._write_local_manifest()
        with patch(
            "scripts.ci_local_release.build_local_evidence_manifest",
            return_value=manifest,
        ), patch(
            "scripts.ci_local_release._require_clean_candidate",
            side_effect=ReleasePromotionError("candidate repository is not clean"),
        ):
            with self.assertRaisesRegex(ReleasePromotionError, "is not clean"):
                prepare_local_release(
                    repository_root=self.repository,
                    evidence_root=self.root,
                    safety_base_ref="b" * 40,
                    distribution_root=self.distribution,
                    manifest_path=self.local_manifest,
                    manifest_digest_path=self.local_manifest_digest,
                    output_dir=self.root / "local-release-assets",
                    revision=REVISION,
                    version=VERSION,
                    tag=f"v{VERSION}",
                )
        self.assertFalse((self.root / "local-release-assets").exists())

    def test_rejects_artifact_tampering_and_does_not_create_output(self) -> None:
        wheel = next(self.distribution.glob("*.whl"))
        wheel.write_bytes(wheel.read_bytes() + b"tampered")

        with self.assertRaisesRegex(ReleasePromotionError, "size does not match"):
            self._prepare()
        self.assertFalse((self.root / "release-assets").exists())

    def test_release_revalidates_malicious_archive_members(self) -> None:
        license_bytes = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        malicious_skill = _zip_bytes(
            {
                "wish-builder/LICENSE": license_bytes,
                "wish-builder/SKILL.md": b"fixture",
                "../outside.txt": b"unsafe",
            }
        )
        (self.distribution / "wish-builder-skill.zip").write_bytes(malicious_skill)
        (self.distribution / "wish-builder-skill.repeat.zip").write_bytes(
            malicious_skill
        )
        self._reseal_distribution_claims_without_validation()

        with self.assertRaisesRegex(
            ReleasePromotionError,
            "skill_zip archive contains an unsafe member path",
        ):
            self._prepare()
        self.assertFalse((self.root / "release-assets").exists())

    def test_rejects_resealed_wheel_with_the_wrong_package_name(self) -> None:
        license_bytes = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: another-project\n"
            b"Version: 0.1.0.dev0\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        wheel = next(self.distribution.glob("*.whl"))
        wheel.write_bytes(
            _zip_bytes(
                {
                    "wish_builder/__init__.py": b"",
                    f"wish_builder-{VERSION}.dist-info/METADATA": metadata,
                    f"wish_builder-{VERSION}.dist-info/licenses/LICENSE": license_bytes,
                }
            )
        )
        self._reseal_distribution_claims_without_validation()

        with self.assertRaisesRegex(
            ReleasePromotionError,
            "wheel metadata must declare exactly one Name: wish-builder",
        ):
            self._prepare()

    def test_rejects_resealed_sdist_with_the_wrong_package_name(self) -> None:
        license_bytes = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: another-project\n"
            b"Version: 0.1.0.dev0\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        sdist = next(self.distribution.glob("*.tar.gz"))
        sdist.write_bytes(
            _sdist_bytes(
                {
                    f"wish_builder-{VERSION}/LICENSE": license_bytes,
                    f"wish_builder-{VERSION}/PKG-INFO": metadata,
                    f"wish_builder-{VERSION}/README.md": b"fixture",
                }
            )
        )
        self._reseal_distribution_claims_without_validation()

        with self.assertRaisesRegex(
            ReleasePromotionError,
            "sdist metadata must declare exactly one Name: wish-builder",
        ):
            self._prepare()

    def test_rejects_resealed_distribution_with_the_wrong_version(self) -> None:
        license_bytes = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        metadata = (
            b"Metadata-Version: 2.4\n"
            b"Name: wish-builder\n"
            b"Version: 0.1.0.dev1\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )
        sdist = next(self.distribution.glob("*.tar.gz"))
        sdist.write_bytes(
            _sdist_bytes(
                {
                    f"wish_builder-{VERSION}/LICENSE": license_bytes,
                    f"wish_builder-{VERSION}/PKG-INFO": metadata,
                    f"wish_builder-{VERSION}/README.md": b"fixture",
                }
            )
        )
        self._reseal_distribution_claims_without_validation()

        with self.assertRaisesRegex(
            ReleasePromotionError,
            f"sdist metadata Version must equal {re.escape(VERSION)}",
        ):
            self._prepare()

    def test_rejects_resealed_noncanonical_distribution_filename(self) -> None:
        wheel = next(self.distribution.glob("*.whl"))
        wheel.rename(self.distribution / f"wish-builder-{VERSION}-py3-none-any.whl")
        self._reseal_distribution_claims_without_validation()

        with self.assertRaisesRegex(
            ReleasePromotionError,
            "wheel filename must be canonical",
        ):
            self._prepare()

    def test_rejects_wrong_revision_run_tag_and_project_version(self) -> None:
        cases = (
            {"revision": "b" * 40},
            {"ci_run_id": CI_RUN_ID + 1},
            {"ci_run_attempt": CI_RUN_ATTEMPT + 1},
            {"tag": "v0.1.0.dev1"},
            {"version": "0.1.0.dev1", "tag": "v0.1.0.dev1"},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ReleasePromotionError):
                    self._prepare(
                        output_dir=self.root / f"release-assets-{index}",
                        **overrides,
                    )

    def test_rejects_a_non_development_release_version(self) -> None:
        with self.assertRaisesRegex(
            ReleasePromotionError,
            "three-part development prerelease",
        ):
            self._prepare(version="0.1.0", tag="v0.1.0")

    def test_rejects_noncanonical_packet_and_existing_output(self) -> None:
        value = json.loads(self.packet.read_text(encoding="ascii"))
        self.packet.write_text(json.dumps(value, indent=2), encoding="ascii")
        self.packet_digest.write_text(
            _sha256(self.packet.read_bytes()) + "\n", encoding="ascii"
        )
        with self.assertRaisesRegex(ReleasePromotionError, "not canonical JSON"):
            self._prepare()

        self._write_distribution_and_packet()
        output = self.root / "already-there"
        output.mkdir()
        with self.assertRaisesRegex(ReleasePromotionError, "already exists"):
            self._prepare(output_dir=output)

    def test_requires_third_party_notices(self) -> None:
        (self.repository / "THIRD_PARTY_NOTICES.md").unlink()
        with self.assertRaisesRegex(ReleasePromotionError, "third-party notices is missing"):
            self._prepare()

    def test_requires_the_canonical_gpl_v3_license_text(self) -> None:
        (self.repository / "LICENSE").write_text(
            "GNU GENERAL PUBLIC LICENSE Version 3\nnot the license\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ReleasePromotionError,
            "canonical GNU GPL version 3 text",
        ):
            self._prepare()


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_script_direct_entrypoint_loads_from_the_repository_root(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/ci_release.py", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=30,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--packet-digest", completed.stdout)

    def test_local_release_script_has_no_ci_identity_arguments(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/ci_local_release.py", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=30,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--evidence-root", completed.stdout)
        self.assertIn("--safety-base-ref", completed.stdout)
        self.assertNotIn("--ci-run-id", completed.stdout)
        self.assertNotIn("--ci-run-attempt", completed.stdout)

    def test_workflow_rebuilds_trusted_source_before_promoting_ci_bytes(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("version:", workflow)
        self.assertIn("target_sha:", workflow)
        self.assertIn("ci_run_id:", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertEqual(2, workflow.count("run-id: ${{ inputs.ci_run_id }}"))
        self.assertIn("name: active-m1-distribution", workflow)
        self.assertIn("name: active-m1-evidence-packet", workflow)
        self.assertIn("python scripts/ci_release.py", workflow)
        self.assertIn(
            '--ci-run-attempt "${{ steps.selected-ci.outputs.run_attempt }}"',
            workflow,
        )
        self.assertIn(
            '[[ "$TARGET_SHA" == "$TRUSTED_WORKFLOW_SHA" ]]', workflow
        )
        self.assertIn(
            "python scripts/build_distributions.py --outdir dist-rebuilt",
            workflow,
        )
        self.assertIn("dist-rebuilt/wish-builder-skill.repeat.zip", workflow)
        self.assertIn("dist-rebuilt/distribution-evidence.json", workflow)
        self.assertIn("cmp --silent", workflow)
        self.assertIn("--repository-root .", workflow)
        self.assertNotIn("candidate-source", workflow)
        self.assertNotIn("python -m build", workflow)
        self.assertNotIn("pip wheel", workflow)
        self.assertNotIn("npm pack", workflow)
        self.assertNotIn("sync_skill_runtime", workflow)

    def test_workflow_verifies_before_and_after_upload_then_publishes(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        local_check = workflow.index("Verify local release checksums")
        draft = workflow.index("Create a draft prerelease")
        download = workflow.index("Download the draft and verify every published byte")
        publish = workflow.index("Publish the verified prerelease")
        self.assertLess(local_check, draft)
        self.assertLess(draft, download)
        self.assertLess(download, publish)
        self.assertIn("sha256sum --check SHA256SUMS", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("gh release download", workflow)
        self.assertIn("--draft=false", workflow)
        self.assertIn('--latest=false', workflow)
        self.assertIn('minimum_gh_version="2.96.0"', workflow)
        self.assertIn("--json isDraft,isPrerelease,targetCommitish", workflow)
        self.assertIn("cmp --silent release-assets/SHA256SUMS", workflow)
        self.assertIn("expected-release-assets.sha256", workflow)
        self.assertIn("downloaded-release-assets.sha256", workflow)
        self.assertIn(
            "diff -u expected-release-assets.sha256 downloaded-release-assets.sha256",
            workflow,
        )
        self.assertIn('[[ "$(git rev-parse HEAD)" == "$TARGET_SHA" ]]', workflow)
        self.assertIn('[[ "$(jq -r .head_sha <<<"$run_json")" == "$TARGET_SHA" ]]', workflow)
        self.assertIn(
            '[[ "$(jq -r .path <<<"$run_json")" == ".github/workflows/ci.yml" ]]',
            workflow,
        )
        self.assertIn('[[ "$(jq -r .event <<<"$run_json")" == "push" ]]', workflow)
        self.assertIn('run_attempt="$(jq -r .run_attempt <<<"$run_json")"', workflow)
        self.assertIn('echo "run_attempt=$run_attempt" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn(
            'test "$(jq -r .ci_run_attempt release-assets/release-manifest.json)"',
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
