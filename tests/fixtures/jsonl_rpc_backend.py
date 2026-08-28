#!/usr/bin/env python3
"""Credential-free JSONL RPC process used by provider adapter tests."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path


def argument(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


provider = os.environ.get("FAKE_RPC_PROVIDER", "pi")
delay = float(os.environ.get("FAKE_RPC_DELAY", "0.05"))
session_directory = Path(argument("--session-dir") or ".").resolve()
session_directory.mkdir(parents=True, exist_ok=True)
session_file = argument("--session") or argument("--resume")
session_path = (
    Path(session_file).resolve()
    if session_file is not None
    else session_directory / "session.jsonl"
)
output_lock = threading.Lock()
session_lock = threading.Lock()
abort_event = threading.Event()
streaming = threading.Event()


def output(value: object) -> None:
    with output_lock:
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def append_message(message: dict[str, object]) -> None:
    with session_lock:
        with session_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    {"type": "message", "message": message},
                    separators=(",", ":"),
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())


def finish_turn() -> None:
    cancelled = abort_event.wait(delay)
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "cancelled" if cancelled else "done"}],
        "stopReason": "aborted" if cancelled else "stop",
    }
    append_message(assistant)
    terminal = {
        "type": "agent_end",
        "messages": [assistant],
        **({"willContinue": False} if provider == "omp" else {"willRetry": False}),
    }
    output(terminal)
    if provider == "pi":
        output({"type": "agent_settled"})
    streaming.clear()


if provider == "omp":
    output(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": 1_048_576,
            "maxReassembledFrameBytes": 64 * 1_048_576,
        }
    )

for raw in sys.stdin.buffer:
    command = json.loads(raw.decode("utf-8"))
    command_type = command.get("type")
    request_id = command.get("id")
    if command_type == "negotiate_protocol":
        output(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
                "data": {"protocolVersion": 2},
            }
        )
    elif command_type == "get_state":
        output(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
                "data": {
                    "sessionId": "fixture-session",
                    "sessionFile": str(session_path),
                    "isStreaming": streaming.is_set(),
                    "messageCount": 0,
                },
            }
        )
    elif command_type == "prompt":
        abort_event.clear()
        append_message(
            {
                "role": "user",
                "content": [{"type": "text", "text": command["message"]}],
            }
        )
        streaming.set()
        output(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
            }
        )
        output({"type": "agent_start"})
        threading.Thread(target=finish_turn, daemon=True).start()
    elif command_type == "abort":
        abort_event.set()
        output(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
            }
        )
    else:
        output(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": False,
                "error": "unsupported fixture command",
            }
        )
