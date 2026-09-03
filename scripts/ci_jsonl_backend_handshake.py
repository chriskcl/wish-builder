#!/usr/bin/env python3
"""Capture non-authorizing Pi/Oh My Pi startup and RPC handshake evidence.

This probe never sends a model prompt and can never enable dispatch.  It exists
to distinguish an installed, protocol-compatible SDK from a backend that has
completed the full qualification scenarios.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from wish_builder.adapters.providers import (
    JsonlRpcClient,
    JsonlRpcError,
    JsonlRpcLaunch,
    JsonlRpcProtocol,
)
from wish_builder.contracts import WorkerProvider, canonical_json_bytes

SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PINS = {
    WorkerProvider.PI: (
        "@earendil-works/pi-coding-agent",
        "0.84.2",
        "e4d4c1e769963c816959f5cea02a0a10ccc0495a",
        "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/tN4zbYH/wOwvx9pJpeLCA==",
    ),
    WorkerProvider.OH_MY_PI: (
        "@oh-my-pi/pi-coding-agent",
        "18.0.11",
        "bbb5bf3e89b4b6a2eb692976109578071369378d",
        "sha512-3H90cCc+3yLtvSKM2RooIvkhG+77OFFoXD6+9GPZDF3PQ3FF6uCnPP57OaUa8VZ8YwOm9Eio5ZmfdFuvwLn+VA==",
    ),
}


class HandshakeProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ResolvedSdk:
    provider: WorkerProvider
    name: str
    version: str
    shasum: str
    integrity: str
    cli: Path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise HandshakeProbeError(f"invalid JSON: {path}") from exc
    if type(value) is not dict:
        raise HandshakeProbeError(f"JSON root is not an object: {path}")
    return value


def _resolve_sdk(root: Path, provider: WorkerProvider) -> _ResolvedSdk:
    name, version, shasum, integrity = _PINS[provider]
    package_root = root.joinpath("node_modules", *name.split("/"))
    package_json = _load_object(package_root / "package.json")
    if package_json.get("name") != name or package_json.get("version") != version:
        raise HandshakeProbeError(f"installed package does not match {name}@{version}")
    lock = _load_object(root / "package-lock.json")
    packages = lock.get("packages")
    lock_key = f"node_modules/{name}"
    if type(packages) is not dict or type(packages.get(lock_key)) is not dict:
        raise HandshakeProbeError(f"package lock entry is missing: {lock_key}")
    entry = packages[lock_key]
    assert type(entry) is dict
    if entry.get("version") != version or entry.get("integrity") != integrity:
        raise HandshakeProbeError(f"package lock pin does not match {name}@{version}")
    cli = (package_root / "dist" / "cli.js").resolve(strict=True)
    if not cli.is_file():
        raise HandshakeProbeError(f"provider CLI is missing: {cli}")
    return _ResolvedSdk(provider, name, version, shasum, integrity, cli)


def _runtime_version(executable: Path) -> str:
    try:
        completed = subprocess.run(
            (str(executable), "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandshakeProbeError(f"runtime version probe failed: {executable}") from exc
    if completed.returncode != 0:
        raise HandshakeProbeError(f"runtime version probe failed: {executable}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _launch(sdk: _ResolvedSdk, node: Path, bun: Path) -> JsonlRpcLaunch:
    if sdk.provider is WorkerProvider.PI:
        return JsonlRpcLaunch(
            sdk.provider,
            JsonlRpcProtocol.PI,
            (str(node), str(sdk.cli)),
            sdk.name,
            sdk.version,
            ("--no-approve",),
        )
    return JsonlRpcLaunch(
        sdk.provider,
        JsonlRpcProtocol.OH_MY_PI_V2,
        (str(bun), str(sdk.cli)),
        sdk.name,
        sdk.version,
        ("--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-pty"),
    )


def _probe(
    sdk: _ResolvedSdk,
    launch: JsonlRpcLaunch,
    workspace: Path,
    session_root: Path,
) -> dict[str, object]:
    client = JsonlRpcClient(
        launch,
        working_directory=workspace,
        session_directory=session_root,
        environment={"NO_COLOR": "1"},
        handshake_timeout_seconds=30,
        response_timeout_seconds=30,
    )
    error_code: str | None = None
    state: dict[str, object] | None = None
    try:
        state = client.start()
    except JsonlRpcError as exc:
        error_code = exc.code
    finally:
        returncode = client.returncode
        stderr = client.stderr_text.encode("utf-8", errors="strict")
        client.close()
    handshake_completed = state is not None
    return {
        "dispatchQualified": False,
        "errorCode": error_code,
        "handshakeCompleted": handshake_completed,
        "isStreaming": None if state is None else state.get("isStreaming"),
        "modelTurnSent": False,
        "processExitCodeBeforeCleanup": returncode,
        "provider": sdk.provider.value,
        "protocol": launch.protocol.value,
        "sdk": {
            "integrity": sdk.integrity,
            "name": sdk.name,
            "shasum": sdk.shasum,
            "version": sdk.version,
        },
        "sessionFileReported": (
            False if state is None else type(state.get("sessionFile")) is str
        ),
        "sessionIdDigest": (
            None
            if state is None or type(state.get("sessionId")) is not str
            else _digest(state["sessionId"].encode("utf-8"))
        ),
        "stderrDigest": _digest(stderr),
        "stderrLength": len(stderr),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture non-authorizing Pi/Oh My Pi RPC handshake evidence."
    )
    parser.add_argument("--providers-root", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--bun", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.providers_root.resolve(strict=True)
        workspace = args.workspace.resolve(strict=True)
        if not root.is_dir() or not workspace.is_dir():
            raise HandshakeProbeError("providers root and workspace must be directories")
        if not _REVISION_RE.fullmatch(args.source_revision):
            raise HandshakeProbeError("source revision must be a full lowercase digest")
        node_raw = args.node or (Path(value) if (value := shutil.which("node")) else None)
        bun_raw = args.bun or (Path(value) if (value := shutil.which("bun")) else None)
        if node_raw is None or bun_raw is None:
            raise HandshakeProbeError("both Node.js and Bun runtimes are required")
        node = node_raw.resolve(strict=True)
        bun = bun_raw.resolve(strict=True)
        sdks = tuple(_resolve_sdk(root, provider) for provider in _PINS)
        with tempfile.TemporaryDirectory(prefix="wish-builder-handshake-") as temporary:
            temporary_root = Path(temporary).resolve()
            results = tuple(
                _probe(
                    sdk,
                    _launch(sdk, node, bun),
                    workspace,
                    temporary_root / sdk.provider.value,
                )
                for sdk in sdks
            )
        body = {
            "capturedAt": _utc_now(),
            "modelTurnSent": False,
            "platform": "windows" if os.name == "nt" else "linux",
            "providers": list(results),
            "python": platform.python_version(),
            "runtimes": {
                "bun": _runtime_version(bun),
                "node": _runtime_version(node),
            },
            "schemaVersion": SCHEMA_VERSION,
            "sourceRevision": args.source_revision,
        }
        document = {**body, "evidenceDigest": _digest(canonical_json_bytes(body))}
        raw = canonical_json_bytes(document)
        output = args.output.resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{output.name}-", suffix=".tmp", dir=output.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, output)
        finally:
            temporary_path.unlink(missing_ok=True)
    except (HandshakeProbeError, JsonlRpcError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
