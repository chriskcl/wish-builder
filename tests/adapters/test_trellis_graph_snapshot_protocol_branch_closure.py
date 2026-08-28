from __future__ import annotations

import base64
import hashlib
import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from wish_builder.adapters.trellis import graph_snapshot

PARENT_TASK_ID = "parent-wish"
OBSERVED_AT = "2026-08-20T01:00:00.000Z"
SNAPSHOT_BYTES = b'{"schema_version":1}\n'


def _sha256(raw: bytes = b"qualified") -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bridge_metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "bridgeProtocolVersion": graph_snapshot.BRIDGE_PROTOCOL_VERSION,
        "corePackageName": "@mindfoldhq/trellis-core",
        "corePackageVersion": graph_snapshot.SUPPORTED_TRELLIS_VERSION,
        "coreArchiveSha256": _sha256(b"archive"),
        "coreArchiveVerified": True,
        "corePackageTreeSha256": _sha256(b"tree"),
        "operationSchemaVersion": None,
        "capabilitySchemaVersion": None,
        "operationKinds": [],
    }
    value.update(changes)
    return value


def _snapshot_payload(
    raw: bytes = SNAPSHOT_BYTES,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "exportVersion": graph_snapshot.SUPPORTED_TRELLIS_EXPORT_VERSION,
        "trellisVersion": graph_snapshot.SUPPORTED_TRELLIS_VERSION,
        "parentTaskId": PARENT_TASK_ID,
        "revision": _sha256(b"revision"),
        "observedAt": OBSERVED_AT,
        "snapshotBase64": base64.b64encode(raw).decode("ascii"),
        "sourceSha256": _sha256(raw),
        "byteLength": len(raw),
        "complete": True,
    }
    value.update(changes)
    return value


def _success_response(
    *,
    snapshot: object | None = None,
    bridge: object | None = None,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "protocolVersion": graph_snapshot.BRIDGE_PROTOCOL_VERSION,
        "ok": True,
        "action": "graph_snapshot",
        "snapshot": _snapshot_payload() if snapshot is None else snapshot,
        "bridge": _bridge_metadata() if bridge is None else bridge,
    }
    value.update(changes)
    return value


def _error_response(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "protocolVersion": graph_snapshot.BRIDGE_PROTOCOL_VERSION,
        "ok": False,
        "action": "graph_snapshot",
        "error": {
            "code": "fallback_code",
            "message": "bridge failed",
            "details": {"reason": "graph_parent_missing"},
        },
    }
    value.update(changes)
    return value


class TrellisGraphSnapshotProtocolBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.working = self.root / "working"
        self.checkout.mkdir()
        self.working.mkdir()
        self.node = self.root / "node.exe"
        self.bridge = self.root / "bridge.mjs"
        self.node.write_bytes(b"test executable placeholder")
        self.bridge.write_bytes(b"test bridge placeholder")

    def port(self, **changes: object) -> graph_snapshot.TrellisCoreGraphPort:
        options: dict[str, object] = {
            "bridge_command": (str(self.node), str(self.bridge)),
            "checkout_root": self.checkout,
            "working_directory": self.working,
            "environment": {"WISH_BUILDER_TEST": "graph"},
            "clock": lambda: OBSERVED_AT,
        }
        options.update(changes)
        return graph_snapshot.TrellisCoreGraphPort(**options)

    def assert_reason(self, expected: str, operation) -> None:
        with self.assertRaises(graph_snapshot.TrellisGraphAdapterError) as raised:
            operation()
        self.assertEqual(expected, raised.exception.reason)

    def test_success_uses_bounded_shared_transport_and_returns_snapshot(self) -> None:
        port = self.port(
            timeout_seconds=12,
            max_request_bytes=1024,
            max_stdout_bytes=2048,
            max_stderr_bytes=512,
        )
        response = _success_response()
        with mock.patch.object(
            graph_snapshot, "_invoke_bridge", return_value=(response, 0)
        ) as invoke:
            result = port.export_snapshot(PARENT_TASK_ID)

        request = json.loads(invoke.call_args.kwargs["raw"])
        self.assertEqual(
            {
                "protocolVersion": graph_snapshot.BRIDGE_PROTOCOL_VERSION,
                "action": "graph_snapshot",
                "checkoutRoot": str(self.checkout),
                "parentTaskId": PARENT_TASK_ID,
                "observedAt": OBSERVED_AT,
            },
            request,
        )
        self.assertEqual(
            (str(self.node), str(self.bridge)),
            invoke.call_args.kwargs["bridge_command"],
        )
        self.assertEqual(
            str(self.working), invoke.call_args.kwargs["working_directory"]
        )
        self.assertEqual(
            "graph", invoke.call_args.kwargs["environment"]["WISH_BUILDER_TEST"]
        )
        self.assertEqual(12.0, invoke.call_args.kwargs["timeout_seconds"])
        self.assertEqual(1024, invoke.call_args.kwargs["max_request_bytes"])
        self.assertEqual(2048, invoke.call_args.kwargs["max_stdout_bytes"])
        self.assertEqual(512, invoke.call_args.kwargs["max_stderr_bytes"])
        self.assertEqual(PARENT_TASK_ID, result.parent_task_id)
        self.assertEqual(SNAPSHOT_BYTES, result.snapshot_bytes)
        self.assertEqual(_sha256(SNAPSHOT_BYTES), result.source_sha256)

    def test_constructor_rejects_commands_directories_timeout_limits_and_clock(
        self,
    ) -> None:
        invalid_commands: tuple[object, ...] = (
            "node",
            b"node",
            (),
            (str(self.node),),
            (1, str(self.bridge)),
            ("", str(self.bridge)),
            ("bad\x00path", str(self.bridge)),
            ("relative-node", str(self.bridge)),
            (str(self.root / "missing.exe"), str(self.bridge)),
        )
        for command in invalid_commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.port(bridge_command=command)

        for field, value in (
            ("checkout_root", Path("relative")),
            ("working_directory", self.root / "missing-directory"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.port(**{field: value})
        with self.assertRaises(TypeError):
            self.port(environment=[])

        for value in (None, "30", True, 0, -1, math.nan, math.inf, 301):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                self.port(timeout_seconds=value)
        self.port(timeout_seconds=graph_snapshot.MAX_GRAPH_TIMEOUT_SECONDS)

        for field, value in (
            ("max_request_bytes", 0),
            ("max_request_bytes", True),
            (
                "max_request_bytes",
                graph_snapshot.DEFAULT_GRAPH_REQUEST_BYTES * 8 + 1,
            ),
            ("max_stdout_bytes", 0),
            (
                "max_stdout_bytes",
                graph_snapshot.DEFAULT_GRAPH_OUTPUT_BYTES * 2 + 1,
            ),
            ("max_stderr_bytes", 0),
            (
                "max_stderr_bytes",
                graph_snapshot.DEFAULT_GRAPH_STDERR_BYTES * 8 + 1,
            ),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.port(**{field: value})
        with self.assertRaises(TypeError):
            self.port(clock=None)

    def test_parent_and_clock_values_fail_before_or_after_transport_as_required(
        self,
    ) -> None:
        port = self.port()
        with mock.patch.object(graph_snapshot, "_invoke_bridge") as invoke:
            for parent in (None, 1, ""):
                with self.subTest(parent=parent), self.assertRaises(ValueError):
                    port.export_snapshot(parent)
            invoke.assert_not_called()

        for observed_at in (None, 1, ""):
            port = self.port(clock=lambda observed_at=observed_at: observed_at)
            with (
                self.subTest(observed_at=observed_at),
                mock.patch.object(graph_snapshot, "_invoke_bridge") as invoke,
            ):
                self.assert_reason(
                    "graph_clock_invalid",
                    lambda port=port: port.export_snapshot(PARENT_TASK_ID),
                )
                invoke.assert_not_called()

        invalid_time = "not-a-timestamp"
        response = _success_response(
            snapshot=_snapshot_payload(observedAt=invalid_time)
        )
        with mock.patch.object(
            graph_snapshot, "_invoke_bridge", return_value=(response, 0)
        ):
            self.assert_reason(
                "graph_snapshot_invalid",
                lambda: self.port(clock=lambda: invalid_time).export_snapshot(
                    PARENT_TASK_ID
                ),
            )

        with (
            mock.patch.object(graph_snapshot, "_utc_now", return_value=OBSERVED_AT),
            mock.patch.object(
                graph_snapshot,
                "_invoke_bridge",
                return_value=(_success_response(), 0),
            ),
        ):
            default_clock_port = graph_snapshot.TrellisCoreGraphPort(
                bridge_command=(str(self.node), str(self.bridge)),
                checkout_root=self.checkout,
                working_directory=self.working,
            )
            self.assertEqual(
                OBSERVED_AT,
                default_clock_port.export_snapshot(PARENT_TASK_ID).observed_at,
            )

    def test_transport_exit_response_schema_and_identity_fail_closed(self) -> None:
        port = self.port()
        transport = graph_snapshot._BridgeTransportError("timeout")
        with (
            mock.patch.object(graph_snapshot, "_invoke_bridge", side_effect=transport),
            self.assertRaises(graph_snapshot.TrellisGraphAdapterError) as raised,
        ):
            port._call({})
        self.assertEqual("graph_timeout", raised.exception.reason)
        self.assertIs(transport, raised.exception.__cause__)

        with mock.patch.object(
            graph_snapshot,
            "_invoke_bridge",
            return_value=(_success_response(), 2),
        ):
            self.assert_reason("graph_exit_failure", lambda: port._call({}))

        for response in ({}, {**_success_response(), "extra": True}):
            with (
                self.subTest(response=response),
                mock.patch.object(
                    graph_snapshot, "_invoke_bridge", return_value=(response, 0)
                ),
            ):
                self.assert_reason("graph_response_schema", lambda: port._call({}))

        for changes in (
            {"protocolVersion": 2},
            {"ok": 1},
            {"action": "projection_inspect"},
        ):
            with (
                self.subTest(changes=changes),
                mock.patch.object(
                    graph_snapshot,
                    "_invoke_bridge",
                    return_value=(_success_response(**changes), 0),
                ),
            ):
                self.assert_reason("graph_response_identity", lambda: port._call({}))

    def test_error_envelope_schema_identity_and_reason_precedence_fail_closed(
        self,
    ) -> None:
        schema_cases = (
            {},
            {**_error_response(), "extra": True},
            {**_error_response(), "error": []},
            {
                **_error_response(),
                "error": {"code": "failed", "message": "missing details"},
            },
        )
        for document in schema_cases:
            with self.subTest(document=document):
                self.assert_reason(
                    "graph_error_schema",
                    lambda document=document: graph_snapshot._raise_bridge_error(
                        document, 1
                    ),
                )

        for document, return_code in (
            ({**_error_response(), "protocolVersion": 2}, 1),
            ({**_error_response(), "ok": True}, 1),
            ({**_error_response(), "action": "other"}, 1),
            (_error_response(), None),
            (_error_response(), 0),
        ):
            with self.subTest(document=document, return_code=return_code):
                self.assert_reason(
                    "graph_error_identity",
                    lambda document=document, return_code=return_code: (
                        graph_snapshot._raise_bridge_error(document, return_code)
                    ),
                )

        reasons = (
            (
                {
                    "code": "fallback",
                    "message": "x",
                    "details": {"reason": "BAD REASON!"},
                },
                "bad_reason_",
            ),
            (
                {"code": "FALLBACK CODE", "message": "x", "details": None},
                "fallback_code",
            ),
            (
                {"code": "fallback", "message": "x", "details": {}},
                "graph_unavailable",
            ),
        )
        for error, expected in reasons:
            document = _error_response(error=error)
            with (
                self.subTest(expected=expected),
                mock.patch.object(
                    graph_snapshot, "_invoke_bridge", return_value=(document, 1)
                ),
            ):
                self.assert_reason(expected, lambda: self.port()._call({}))

    def test_bridge_metadata_schema_identity_and_archive_digests_are_strict(
        self,
    ) -> None:
        for value in (None, [], {}, {**_bridge_metadata(), "extra": True}):
            with self.subTest(value=value):
                self.assert_reason(
                    "graph_bridge_schema",
                    lambda value=value: (
                        graph_snapshot.TrellisCoreGraphPort._validate_bridge(value)
                    ),
                )

        identity_changes = (
            {"bridgeProtocolVersion": 2},
            {"corePackageName": "trellis-core"},
            {"corePackageVersion": "0.6.14"},
            {"coreArchiveVerified": False},
            {"coreArchiveVerified": 1},
            {"operationSchemaVersion": 1},
            {"capabilitySchemaVersion": 1},
            {"operationKinds": ["read"]},
            {"operationKinds": ()},
        )
        for changes in identity_changes:
            with self.subTest(changes=changes):
                self.assert_reason(
                    "graph_bridge_identity",
                    lambda changes=changes: (
                        graph_snapshot.TrellisCoreGraphPort._validate_bridge(
                            _bridge_metadata(**changes)
                        )
                    ),
                )

        digest_cases = (
            ({"coreArchiveSha256": None}, "graph_corearchivesha256_invalid"),
            ({"coreArchiveSha256": "short"}, "graph_corearchivesha256_invalid"),
            (
                {"coreArchiveSha256": "sha256:" + "g" * 64},
                "graph_corearchivesha256_invalid",
            ),
            (
                {"corePackageTreeSha256": "sha256:" + "g" * 64},
                "graph_corepackagetreesha256_invalid",
            ),
        )
        for changes, expected in digest_cases:
            with self.subTest(changes=changes):
                self.assert_reason(
                    expected,
                    lambda changes=changes: (
                        graph_snapshot.TrellisCoreGraphPort._validate_bridge(
                            _bridge_metadata(**changes)
                        )
                    ),
                )
        graph_snapshot.TrellisCoreGraphPort._validate_bridge(_bridge_metadata())

    def test_snapshot_schema_and_identity_are_strict(self) -> None:
        for value in (None, [], {}, {**_snapshot_payload(), "extra": True}):
            with self.subTest(value=value):
                self.assert_reason(
                    "graph_snapshot_schema",
                    lambda value=value: graph_snapshot._snapshot(
                        value, PARENT_TASK_ID, OBSERVED_AT
                    ),
                )

        identity_changes = (
            {"exportVersion": "wish-builder.trellis-graph.v2"},
            {"trellisVersion": "0.6.14"},
            {"parentTaskId": "other-parent"},
            {"observedAt": "2026-08-20T02:00:00.000Z"},
            {"complete": False},
            {"complete": 1},
        )
        for changes in identity_changes:
            with self.subTest(changes=changes):
                self.assert_reason(
                    "graph_snapshot_identity",
                    lambda changes=changes: graph_snapshot._snapshot(
                        _snapshot_payload(**changes), PARENT_TASK_ID, OBSERVED_AT
                    ),
                )

    def test_snapshot_encoding_length_revision_digest_and_model_validation(
        self,
    ) -> None:
        for encoded in (1, "not-base64!", "YQ==\n"):
            with self.subTest(encoded=encoded):
                self.assert_reason(
                    "graph_snapshot_encoding",
                    lambda encoded=encoded: graph_snapshot._snapshot(
                        _snapshot_payload(snapshotBase64=encoded),
                        PARENT_TASK_ID,
                        OBSERVED_AT,
                    ),
                )

        length_cases = (
            _snapshot_payload(byteLength=True),
            _snapshot_payload(byteLength=len(SNAPSHOT_BYTES) + 1),
            _snapshot_payload(b""),
        )
        for value in length_cases:
            with self.subTest(value=value):
                self.assert_reason(
                    "graph_snapshot_length",
                    lambda value=value: graph_snapshot._snapshot(
                        value, PARENT_TASK_ID, OBSERVED_AT
                    ),
                )
        with mock.patch.object(graph_snapshot, "MAX_GRAPH_SNAPSHOT_BYTES", 1):
            self.assert_reason(
                "graph_snapshot_length",
                lambda: graph_snapshot._snapshot(
                    _snapshot_payload(b"xx"), PARENT_TASK_ID, OBSERVED_AT
                ),
            )

        for revision in (None, 1, ""):
            with self.subTest(revision=revision):
                self.assert_reason(
                    "graph_snapshot_revision",
                    lambda revision=revision: graph_snapshot._snapshot(
                        _snapshot_payload(revision=revision),
                        PARENT_TASK_ID,
                        OBSERVED_AT,
                    ),
                )

        digest_cases = (
            (None, "graph_sourcesha256_invalid"),
            ("short", "graph_sourcesha256_invalid"),
            ("xxxxxxx" + "a" * 64, "graph_sourcesha256_invalid"),
            ("sha256:" + "g" * 64, "graph_sourcesha256_invalid"),
        )
        for digest, expected in digest_cases:
            with self.subTest(digest=digest):
                self.assert_reason(
                    expected,
                    lambda digest=digest: graph_snapshot._snapshot(
                        _snapshot_payload(sourceSha256=digest),
                        PARENT_TASK_ID,
                        OBSERVED_AT,
                    ),
                )

        invalid_model_cases = (
            (
                _snapshot_payload(sourceSha256="sha256:" + "f" * 64),
                PARENT_TASK_ID,
                OBSERVED_AT,
            ),
            (
                _snapshot_payload(observedAt="not-a-time"),
                PARENT_TASK_ID,
                "not-a-time",
            ),
            (
                _snapshot_payload(parentTaskId="bad\x00parent"),
                "bad\x00parent",
                OBSERVED_AT,
            ),
            (
                _snapshot_payload(revision="r" * 513),
                PARENT_TASK_ID,
                OBSERVED_AT,
            ),
        )
        for value, parent_task_id, observed_at in invalid_model_cases:
            with self.subTest(value=value):
                self.assert_reason(
                    "graph_snapshot_invalid",
                    lambda value=value, parent_task_id=parent_task_id, observed_at=observed_at: (
                        graph_snapshot._snapshot(value, parent_task_id, observed_at)
                    ),
                )

        result = graph_snapshot._snapshot(
            _snapshot_payload(), PARENT_TASK_ID, OBSERVED_AT
        )
        self.assertEqual(SNAPSHOT_BYTES, result.snapshot_bytes)
        self.assertTrue(result.complete)

    def test_digest_reason_error_and_utc_helpers_normalize_stably(self) -> None:
        valid = "sha256:" + "a" * 64
        self.assertEqual(valid, graph_snapshot._digest(valid, "digest"))
        for value in (None, 1, "short", "xxxxxxx" + "a" * 64, "sha256:" + "g" * 64):
            with self.subTest(value=value):
                self.assert_reason(
                    "graph_digest_invalid",
                    lambda value=value: graph_snapshot._digest(value, "digest"),
                )

        self.assertEqual("graph_unavailable", graph_snapshot._reason(None))
        self.assertEqual("graph_unavailable", graph_snapshot._reason(""))
        self.assertEqual("bad_reason_-_é", graph_snapshot._reason("BAD REASON!-/É"))
        self.assertEqual("x" * 128, graph_snapshot._reason("X" * 200))
        normalized = graph_snapshot.TrellisGraphAdapterError("BAD REASON!")
        self.assertEqual("bad_reason_", normalized.reason)
        unavailable = graph_snapshot.TrellisGraphAdapterError(None)
        self.assertEqual("graph_unavailable", str(unavailable))

        now = graph_snapshot._utc_now()
        self.assertTrue(now.endswith("Z"))
        datetime.fromisoformat(now.removesuffix("Z") + "+00:00")


if __name__ == "__main__":
    unittest.main()
