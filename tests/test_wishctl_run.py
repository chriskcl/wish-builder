from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.e2e.support import serial_parallel_manifest
from tests.test_wishctl import valid_manifest
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.cli import wishctl
from wish_builder.processes.foreground import ForegroundRunStatus
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    BackendAdmissionResult,
    current_platform,
)


def invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = wishctl.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class WishCtlRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bundled_disabled_backend_rejects_before_any_fake_port(self) -> None:
        platform = current_platform()
        self.assertIn(platform, {Platform.WINDOWS, Platform.LINUX})
        assert platform is not None
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.PI, platform)
        manifest = dataclasses.replace(
            serial_parallel_manifest(),
            provider=WorkerProvider.PI,
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=bundle.policy_digest,
        )
        manifest_path = self.root / "execution-manifest.json"
        manifest_path.write_bytes(manifest.canonical_json_bytes())
        runtime_root = self.root / "runtime"

        with (
            mock.patch("wish_builder.adapters.fake.FakeTaskPort") as fake_port,
            mock.patch.object(
                wishctl,
                "_build_production_components",
            ) as build_components,
        ):
            code, stdout, stderr = invoke(
                [
                    "run",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--workspace-root",
                    str(self.root),
                ]
            )

        self.assertEqual((1, ""), (code, stderr))
        payload = json.loads(stdout)
        self.assertEqual("rejected", payload["status"])
        self.assertEqual("backend_admission", payload["stage"])
        self.assertEqual("dispatch_not_qualified", payload["reason"])
        self.assertEqual(
            "dispatch_not_qualified",
            payload["backend_admission_reason"],
        )
        self.assertEqual([], payload["completed_task_ids"])
        self.assertEqual(0, payload["batch_count"])
        fake_port.assert_not_called()
        build_components.assert_not_called()
        self.assertFalse(runtime_root.exists())

    def test_qualified_backend_without_sdk_root_rejects_before_composition(self) -> None:
        platform = current_platform()
        self.assertIn(platform, {Platform.WINDOWS, Platform.LINUX})
        assert platform is not None
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.CODEX, platform)
        manifest = dataclasses.replace(
            serial_parallel_manifest(),
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=bundle.policy_digest,
        )
        manifest_path = self.root / "execution-manifest.json"
        manifest_path.write_bytes(manifest.canonical_json_bytes())
        preliminary = BackendAdmissionResult(
            True,
            BackendAdmissionReason.NONE,
            cell,
        )

        with (
            mock.patch.object(wishctl, "admit_backend", return_value=preliminary),
            mock.patch.object(wishctl, "_build_production_components") as build_components,
        ):
            code, stdout, stderr = invoke(
                [
                    "run",
                    str(manifest_path),
                    "--runtime-root",
                    str(self.root / "runtime"),
                    "--workspace-root",
                    str(self.root),
                ]
            )

        self.assertEqual((1, ""), (code, stderr))
        payload = json.loads(stdout)
        self.assertEqual("rejected", payload["status"])
        self.assertEqual("backend_admission", payload["stage"])
        self.assertEqual(
            "dispatch_not_qualified",
            payload["backend_admission_reason"],
        )
        build_components.assert_not_called()

    def test_legacy_manifest_is_not_an_execution_target(self) -> None:
        manifest_path = self.root / "legacy-manifest.json"
        manifest_path.write_text(
            json.dumps(valid_manifest()),
            encoding="utf-8",
        )

        code, stdout, stderr = invoke(["run", str(manifest_path)])

        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("run requires an execution manifest v2", stderr)

    def test_completed_service_result_maps_to_success_exit(self) -> None:
        manifest = serial_parallel_manifest()
        manifest_path = self.root / "completed-manifest.json"
        manifest_path.write_bytes(manifest.canonical_json_bytes())
        result = SimpleNamespace(
            backend_admission=SimpleNamespace(reason=SimpleNamespace(value="none")),
            batch_count=3,
            completed_task_ids=tuple(task.id for task in manifest.tasks),
            reason=SimpleNamespace(value="none"),
            stage=SimpleNamespace(value="terminal_lifecycle"),
            status=ForegroundRunStatus.COMPLETED,
        )
        service = mock.Mock()
        service.run.return_value = result
        runtime_root = self.root / "runtime" / ".." / "runtime"
        workspace_root = self.root / "workspace" / ".." / "workspace"
        component = object()
        with (
            mock.patch.object(
                wishctl,
                "ForegroundRunService",
                return_value=service,
            ) as service_type,
            mock.patch.object(
                wishctl,
                "_build_production_components",
                return_value=component,
            ) as build_components,
        ):
            code, stdout, stderr = invoke(
                [
                    "run",
                    str(manifest_path),
                    "--runtime-root",
                    str(runtime_root),
                    "--workspace-root",
                    str(workspace_root),
                ]
            )
            build_components.assert_not_called()
            service_call = service_type.call_args
            components_factory = service_call.kwargs["components_factory"]
            built_component = components_factory()

        self.assertEqual((0, ""), (code, stderr))
        payload = json.loads(stdout)
        self.assertEqual("completed", payload["status"])
        self.assertEqual(3, payload["batch_count"])
        service_type.assert_called_once()
        self.assertEqual((manifest,), service_call.args)
        self.assertIs(component, built_component)
        build_components.assert_called_once_with(
            manifest,
            runtime_root=runtime_root.expanduser().absolute(),
            workspace_root=workspace_root.expanduser().absolute(),
        )

    def test_missing_runtime_root_blocks_only_after_backend_admission(self) -> None:
        platform = current_platform()
        self.assertIn(platform, {Platform.WINDOWS, Platform.LINUX})
        assert platform is not None
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.CODEX, platform)
        manifest = dataclasses.replace(
            serial_parallel_manifest(),
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=bundle.policy_digest,
        )
        manifest_path = self.root / "admitted-manifest.json"
        manifest_path.write_bytes(manifest.canonical_json_bytes())
        admission = BackendAdmissionResult(
            True,
            BackendAdmissionReason.NONE,
            cell,
        )
        real_service = wishctl.ForegroundRunService

        def admitted_service(*args, **kwargs):
            kwargs["backend_admitter"] = lambda _: admission
            return real_service(*args, **kwargs)

        with (
            mock.patch.object(
                wishctl,
                "ForegroundRunService",
                side_effect=admitted_service,
            ),
            mock.patch.object(
                wishctl,
                "_build_production_components",
                side_effect=ValueError("runtime_root is required"),
            ) as build_components,
        ):
            code, stdout, stderr = invoke(
                ["run", str(manifest_path), "--workspace-root", str(self.root)]
            )

        self.assertEqual((1, ""), (code, stderr))
        payload = json.loads(stdout)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("manifest_validation", payload["stage"])
        self.assertEqual("composition_unavailable", payload["reason"])
        self.assertEqual("none", payload["backend_admission_reason"])
        build_components.assert_called_once_with(
            manifest,
            runtime_root=None,
            workspace_root=self.root.absolute(),
        )

    def test_run_help_documents_production_roots(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            wishctl.main(["run", "--help"])

        self.assertEqual(0, caught.exception.code)
        self.assertEqual("", stderr.getvalue())
        help_text = stdout.getvalue()
        self.assertIn("--runtime-root RUNTIME_ROOT", help_text)
        self.assertIn("--workspace-root WORKSPACE_ROOT", help_text)
        self.assertIn("--provider-sdk-root PROVIDER_SDK_ROOT", help_text)

    def test_run_rejects_relative_provider_sdk_root(self) -> None:
        manifest_path = self.root / "relative-sdk-manifest.json"
        manifest_path.write_bytes(serial_parallel_manifest().canonical_json_bytes())

        code, stdout, stderr = invoke(
            ["run", str(manifest_path), "--provider-sdk-root", "relative-sdk"]
        )

        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("--provider-sdk-root must be an absolute path", stderr)

    def test_run_roots_keep_admission_first_defaults(self) -> None:
        args = wishctl.build_parser().parse_args(["run", "manifest.json"])

        self.assertIsNone(args.runtime_root)
        self.assertIsNone(args.provider_sdk_root)
        self.assertEqual(".", args.workspace_root)


if __name__ == "__main__":
    unittest.main()
