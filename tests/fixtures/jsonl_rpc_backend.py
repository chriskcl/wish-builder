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
abort_response_after_terminal = (
    os.environ.get("FAKE_RPC_ABORT_RESPONSE_AFTER_TERMINAL") == "1"
)
prompt_response_after_start = (
    os.environ.get("FAKE_RPC_PROMPT_RESPONSE_AFTER_START") == "1"
)
startup_event_before_response = (
    os.environ.get("FAKE_RPC_STARTUP_EVENT_BEFORE_RESPONSE") == "1"
)
barrier_directory_text = os.environ.get("FAKE_RPC_BARRIER_DIRECTORY", "")
barrier_directory = (
    Path(barrier_directory_text).resolve() if barrier_directory_text else None
)
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


def wait_for_sibling_barrier() -> None:
    if barrier_directory is None:
        return
    barrier_directory.mkdir(parents=True, exist_ok=True)
    (barrier_directory / f"{os.getpid()}.ready").write_text(
        "ready\n",
        encoding="utf-8",
        newline="",
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(tuple(barrier_directory.glob("*.ready"))) >= 2:
            return
        time.sleep(0.001)
    raise RuntimeError("sibling provider did not reach prompt barrier")


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
    if startup_event_before_response:
        output({"type": "available_commands_update", "commands": []})

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
        wait_for_sibling_barrier()
        abort_event.clear()
        append_message(
            {
                "role": "user",
                "content": [{"type": "text", "text": command["message"]}],
            }
        )
        streaming.set()
        prompt_response = {
            "id": request_id,
            "type": "response",
            "command": command_type,
            "success": True,
        }
        if prompt_response_after_start:
            output({"type": "agent_start"})
            output(prompt_response)
        else:
            output(prompt_response)
            output({"type": "agent_start"})
        threading.Thread(target=finish_turn, daemon=True).start()
    elif command_type == "abort":
        abort_event.set()
        if abort_response_after_terminal:
            deadline = time.monotonic() + 5
            while streaming.is_set() and time.monotonic() < deadline:
                time.sleep(0.001)
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
