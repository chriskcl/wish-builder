from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.adapters.test_trellis_projection import _projection
from wish_builder.adapters.trellis import projection
from wish_builder.services.ports import (
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionReason,
)


def _projection_payload() -> dict[str, object]:
    value = _projection(sequence=11).to_primitive()
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    value["projectionDigest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return value


def _observation_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "disposition": "inspected",
        "reason": "none",
        "recordRevision": "sha256:" + "a" * 64,
        "byteLength": 100,
        "taskStatus": "planning",
        "projection": _projection_payload(),
    }
    value.update(changes)
    return value


def _bridge_metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "bridgeProtocolVersion": 1,
        "corePackageName": "@mindfoldhq/trellis-core",
        "corePackageVersion": "0.6.15",
        "coreArchiveSha256": "sha256:" + "1" * 64,
        "coreArchiveVerified": True,
        "corePackageTreeSha256": "sha256:" + "2" * 64,
        "operationSchemaVersion": None,
        "capabilitySchemaVersion": None,
        "operationKinds": [],
    }
    value.update(changes)
    return value


class _ReadStream:
    def __init__(self, chunks: list[bytes] | None = None, error: Exception | None = None):
        self.chunks = list(chunks or [])
        self.error = error

    def read(self, _size: int) -> bytes:
        if self.error is not None:
            raise self.error
        return self.chunks.pop(0) if self.chunks else b""


class _WriteStream:
    def __init__(self, *, write_error: bool = False, close_error: bool = False):
        self.write_error = write_error
        self.close_error = close_error
        self.value = b""
        self.flushed = False
        self.closed = False

    def write(self, raw: bytes) -> None:
        if self.write_error:
            raise BrokenPipeError
        self.value += raw

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise OSError


class _TerminateProcess:
    def __init__(self, *, kill_error: bool = False, wait_error: bool = False):
        self.kill_error = kill_error
        self.wait_error = wait_error
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        if self.kill_error:
            raise ProcessLookupError

    def wait(self, timeout: float) -> None:
        self.waited = True
        if self.wait_error:
            raise subprocess.TimeoutExpired("bridge", timeout)


class _CallStream:
    def __init__(self, chunks: list[bytes] | None = None, *, close_error: bool = False):
        self.chunks = list(chunks or [])
        self.close_error = close_error
        self.written = b""

    def read(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, raw: bytes) -> None:
        self.written += raw

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self.close_error:
            raise OSError


class _CallProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        poll_values: list[int | None] | None = None,
        wait_error: bool = False,
        close_error: bool = False,
    ) -> None:
        self.stdin = _CallStream(close_error=close_error)
        self.stdout = _CallStream([stdout] if stdout else [], close_error=close_error)
        self.stderr = _CallStream([stderr] if stderr else [], close_error=close_error)
        self.poll_values = list(poll_values or [0])
        self.wait_error = wait_error
        self.returncode = 0

    def poll(self) -> int | None:
        return self.poll_values.pop(0) if self.poll_values else 0

    def wait(self, timeout: float) -> int:
        if self.wait_error:
            raise subprocess.TimeoutExpired("bridge", timeout)
        return 0

    def kill(self) -> None:
        self.returncode = -9


class _SyncThread:
    def __init__(self, *, target, args, daemon: bool, alive: bool = False) -> None:
        self.target = target
        self.args = args
        self.alive = alive

    def start(self) -> None:
        self.target(*self.args)

    def join(self, timeout: float) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive


class TrellisProjectionProtocolBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.python = Path(sys.executable).resolve()

    def port(self, code: str = "pass", **kwargs: object) -> projection.TrellisCoreProjectionPort:
        return projection.TrellisCoreProjectionPort(
            bridge_command=(str(self.python), "-c", code),
            working_directory=self.root,
            **kwargs,
        )

    def call_script(self, code: str) -> dict[str, object]:
        return self.port(code)._call({}, "projection_inspect")

    def assert_adapter_reason(self, expected: str, invoke) -> None:
        with self.assertRaises(projection.TrellisProjectionAdapterError) as raised:
            invoke()
        self.assertEqual(expected, raised.exception.reason)

    def test_constructor_rejects_timeout_and_limit_boundaries(self) -> None:
        for value in (True, 0, -1, projection.MAX_PROJECTION_TIMEOUT_SECONDS + 1):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                self.port(timeout_seconds=value)
        for field, value in (
            ("max_request_bytes", 0),
            ("max_stdout_bytes", True),
            ("max_stderr_bytes", projection.DEFAULT_PROJECTION_STDERR_BYTES * 8 + 1),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.port(**{field: value})

    def test_inspect_and_apply_turn_bridge_failures_into_unavailable_observations(self) -> None:
        port = self.port()
        with mock.patch.object(port, "_call", side_effect=ValueError("bad response")):
            observed = port.inspect(self.root, "trellis/task-a")
        self.assertIs(observed.disposition, TrellisProjectionDisposition.UNAVAILABLE)
        self.assertIs(observed.reason, TrellisProjectionReason.UNAVAILABLE)

        request = TrellisProjectionApplyRequest(
            self.root,
            "trellis/task-a",
            "sha256:" + "a" * 64,
            _projection(sequence=11),
        )
        with mock.patch.object(
            port,
            "_call",
            side_effect=projection.TrellisProjectionAdapterError("status_mismatch"),
        ):
            observed = port.apply(request)
        self.assertIs(observed.disposition, TrellisProjectionDisposition.UNAVAILABLE)
        self.assertIs(observed.reason, TrellisProjectionReason.STATUS_MISMATCH)

        with self.assertRaises(TypeError):
            port.apply(object())  # type: ignore[arg-type]

    def test_request_limit_and_spawn_failure_are_stable(self) -> None:
        self.assert_adapter_reason(
            "projection_request_limit",
            lambda: self.port(max_request_bytes=1)._call({"value": "large"}, "x"),
        )
        missing = self.root / "missing-python.exe"
        port = projection.TrellisCoreProjectionPort(
            bridge_command=(str(missing),),
            working_directory=self.root,
        )
        self.assert_adapter_reason(
            "projection_spawn_failed",
            lambda: port._call({}, "projection_inspect"),
        )

    def test_subprocess_timeout_and_output_limits_are_enforced(self) -> None:
        self.assert_adapter_reason(
            "projection_timeout",
            lambda: self.port("import time; time.sleep(1)", timeout_seconds=0.02)._call(
                {}, "projection_inspect"
            ),
        )
        self.assert_adapter_reason(
            "projection_stdout_limit",
            lambda: self.port(
                "import sys; sys.stdout.buffer.write(b'x' * 128)",
                max_stdout_bytes=8,
            )._call({}, "projection_inspect"),
        )
        self.assert_adapter_reason(
            "projection_stderr_limit",
            lambda: self.port(
                "import sys; sys.stderr.buffer.write(b'x' * 128)",
                max_stderr_bytes=8,
            )._call({}, "projection_inspect"),
        )

    def test_call_detects_live_stderr_wait_timeout_and_unclosed_pipes(self) -> None:
        def thread_factory(*, target, args, daemon):
            return _SyncThread(target=target, args=args, daemon=daemon)

        stdout_process = _CallProcess(stdout=b"overflow", poll_values=[None])
        port = self.port(max_stdout_bytes=1)
        with (
            mock.patch.object(projection.subprocess, "Popen", return_value=stdout_process),
            mock.patch.object(projection.threading, "Thread", side_effect=thread_factory),
        ):
            self.assert_adapter_reason(
                "projection_stdout_limit",
                lambda: port._call({}, "projection_inspect"),
            )

        stderr_process = _CallProcess(stderr=b"overflow", poll_values=[None])
        port = self.port(max_stderr_bytes=1)
        with (
            mock.patch.object(projection.subprocess, "Popen", return_value=stderr_process),
            mock.patch.object(projection.threading, "Thread", side_effect=thread_factory),
        ):
            self.assert_adapter_reason(
                "projection_stderr_limit",
                lambda: port._call({}, "projection_inspect"),
            )

        wait_process = _CallProcess(stdout=b"{}", wait_error=True)
        with (
            mock.patch.object(projection.subprocess, "Popen", return_value=wait_process),
            mock.patch.object(projection.threading, "Thread", side_effect=thread_factory),
        ):
            self.assert_adapter_reason(
                "projection_timeout",
                lambda: self.port()._call({}, "projection_inspect"),
            )

        def alive_thread_factory(*, target, args, daemon):
            return _SyncThread(target=target, args=args, daemon=daemon, alive=True)

        pipe_process = _CallProcess(
            stdout=b"{}",
            close_error=True,
        )
        with (
            mock.patch.object(projection.subprocess, "Popen", return_value=pipe_process),
            mock.patch.object(
                projection.threading, "Thread", side_effect=alive_thread_factory
            ),
        ):
            self.assert_adapter_reason(
                "projection_pipe_not_closed",
                lambda: self.port()._call({}, "projection_inspect"),
            )

    def test_subprocess_protocol_rejects_stderr_exit_schema_and_identity(self) -> None:
        valid = {
            "protocolVersion": 1,
            "ok": True,
            "action": "projection_inspect",
            "projection": None,
            "bridge": _bridge_metadata(),
        }
        cases = (
            ("projection_json_invalid", "print('{')"),
            (
                "projection_unexpected_stderr",
                "import sys; sys.stderr.write('warning'); print('{}')",
            ),
            ("projection_exit_failure", "import sys; print('{}'); sys.exit(2)"),
            ("projection_response_schema", "print('{}')"),
            (
                "projection_response_identity",
                "print(" + repr(json.dumps({**valid, "protocolVersion": 2})) + ")",
            ),
            (
                "fallback_code",
                "print("
                + repr(
                    json.dumps(
                        {
                            "protocolVersion": 1,
                            "ok": False,
                            "action": "projection_inspect",
                            "error": {
                                "code": "fallback_code",
                                "message": "failed",
                                "details": {},
                            },
                        }
                    )
                )
                + ")",
            ),
        )
        for reason, code in cases:
            with self.subTest(reason=reason):
                self.assert_adapter_reason(reason, lambda code=code: self.call_script(code))

        result = self.call_script("print(" + repr(json.dumps(valid)) + ")")
        self.assertEqual(valid, result)

        for metadata, reason in (
            ({}, "projection_bridge_schema"),
            (
                _bridge_metadata(corePackageVersion="0.6.14"),
                "projection_bridge_identity",
            ),
            (
                _bridge_metadata(coreArchiveVerified=False),
                "projection_bridge_identity",
            ),
            (
                _bridge_metadata(coreArchiveSha256="bad"),
                "projection_bridge_identity",
            ),
        ):
            with self.subTest(reason=reason):
                response = {**valid, "bridge": metadata}
                self.assert_adapter_reason(
                    reason,
                    lambda response=response: self.call_script(
                        "print(" + repr(json.dumps(response)) + ")"
                    ),
                )

    def test_bridge_error_envelope_validation_and_reason_precedence(self) -> None:
        valid_error = {
            "protocolVersion": 1,
            "ok": False,
            "action": "projection_inspect",
            "error": {"code": "fallback_code", "message": "failed", "details": {}},
        }
        cases = (
            ({}, "projection_error_schema"),
            ({**valid_error, "protocolVersion": 2}, "projection_error_identity"),
            ({**valid_error, "ok": True}, "projection_error_identity"),
            ({**valid_error, "action": "projection_apply"}, "projection_error_action"),
            ({**valid_error, "error": []}, "projection_error_schema"),
            (
                {**valid_error, "error": {"code": "x", "message": "m"}},
                "projection_error_schema",
            ),
        )
        for document, reason in cases:
            with self.subTest(reason=reason):
                self.assert_adapter_reason(
                    reason,
                    lambda document=document: projection._raise_bridge_error(
                        document, "projection_inspect", 1
                    ),
                )

        for details, code, expected in (
            ({"reason": "status_mismatch"}, "fallback", "status_mismatch"),
            ({"reason": 1}, "fallback_code", "fallback_code"),
            (None, None, "projection_failure"),
        ):
            document = {
                **valid_error,
                "error": {"code": code, "message": "failed", "details": details},
            }
            self.assert_adapter_reason(
                expected,
                lambda document=document: projection._raise_bridge_error(
                    document, "projection_inspect", 1
                ),
            )

    def test_observation_schema_and_values_are_strict(self) -> None:
        cases = (
            ([], "object"),
            ({}, "schema"),
            (_observation_payload(disposition="unknown"), "disposition"),
            (_observation_payload(recordRevision="bad"), "sha256"),
            (_observation_payload(byteLength=True), "byteLength"),
            (_observation_payload(byteLength=-1), "byteLength"),
            (_observation_payload(taskStatus=""), "taskStatus"),
            (_observation_payload(taskStatus=1), "taskStatus"),
            (_observation_payload(disposition="unavailable"), "cannot return unavailable"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                projection._observation(value)

        with self.assertRaises(ValueError):
            projection._observation(_observation_payload(reason="unknown"))

        observed = projection._observation(
            _observation_payload(recordRevision=None, taskStatus=None, projection=None)
        )
        self.assertIsNone(observed.record_revision)
        self.assertIsNone(observed.projection)

    def test_projection_payload_schema_and_digest_are_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, "object"):
            projection._decode_projection([])
        with self.assertRaisesRegex(ValueError, "schema"):
            projection._decode_projection({})
        mismatched = _projection_payload()
        mismatched["projectionDigest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            projection._decode_projection(mismatched)
        self.assertEqual(_projection(sequence=11), projection._decode_projection(_projection_payload()))

    def test_reason_json_command_path_environment_and_digest_helpers(self) -> None:
        self.assertEqual("projection_unavailable", projection._reason_text(None))
        self.assertEqual("bad_reason", projection._reason_text("BAD REASON"))
        self.assertEqual("projection_unavailable", projection._reason_enum("not-known"))
        self.assertEqual(
            "projection_response_invalid",
            projection._failure_reason(ValueError("bad")),
        )
        adapter_error = projection.TrellisProjectionAdapterError("BAD REASON")
        self.assertEqual("bad_reason", projection._failure_reason(adapter_error))

        for raw, reason in (
            (b"", "projection_stdout_limit"),
            (b"\xff", "projection_json_invalid"),
            (b"{", "projection_json_invalid"),
            (b'{"a":1,"a":2}', "projection_json_invalid"),
            (b'{"value":NaN}', "projection_json_invalid"),
            (b"[]", "projection_response_not_object"),
        ):
            with self.subTest(reason=reason):
                self.assert_adapter_reason(reason, lambda raw=raw: projection._strict_object(raw))
        with mock.patch.object(projection, "DEFAULT_PROJECTION_OUTPUT_BYTES", 1):
            self.assert_adapter_reason(
                "projection_stdout_limit", lambda: projection._strict_object(b"123456789")
            )

        with self.assertRaises(ValueError):
            projection._command(())
        with self.assertRaises(ValueError):
            projection._command((str(self.python), ""))
        with self.assertRaises(ValueError):
            projection._command(("python",))
        with self.assertRaises(ValueError):
            projection._directory("relative", "working_directory")
        with self.assertRaises(TypeError):
            projection._checkout(str(self.root))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            projection._checkout(Path("relative"))
        with self.assertRaises(TypeError):
            projection._environment([])  # type: ignore[arg-type]
        for value in ({1: "x"}, {"": "x"}, {"A": 1}, {"A": "bad\x00value"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                projection._environment(value)  # type: ignore[arg-type]
        self.assertEqual("x", projection._environment({"A": "x"})["A"])
        for value in (None, "short", "sha256:" + "g" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                projection._digest(value, "digest")

    def test_bounded_capture_write_and_termination_helpers_cover_failures(self) -> None:
        capture = projection._BoundedCapture(2)
        capture.drain(_ReadStream([b"abc"]))
        self.assertEqual(b"ab", bytes(capture.data))
        self.assertTrue(capture.overflow.is_set())

        capture = projection._BoundedCapture(0)
        capture.drain(_ReadStream([b"x"]))
        self.assertEqual(b"", bytes(capture.data))
        self.assertTrue(capture.overflow.is_set())

        capture = projection._BoundedCapture(2)
        capture.drain(_ReadStream(error=OSError()))
        self.assertTrue(capture.overflow.is_set())

        stream = _WriteStream()
        projection._write_request(stream, b"request")
        self.assertEqual(b"request", stream.value)
        self.assertTrue(stream.flushed)
        self.assertTrue(stream.closed)
        projection._write_request(_WriteStream(write_error=True, close_error=True), b"x")

        normal = _TerminateProcess()
        projection._terminate(normal)  # type: ignore[arg-type]
        self.assertTrue(normal.killed)
        self.assertTrue(normal.waited)
        projection._terminate(
            _TerminateProcess(kill_error=True, wait_error=True)  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    unittest.main()
