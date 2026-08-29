from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from wish_builder.processes import open_result_channel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("relative_path")
    parser.add_argument("content")
    parser.add_argument("marker")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--barrier")
    parser.add_argument("--barrier-count", type=int, default=0)
    args = parser.parse_args()

    started = time.time_ns()
    if args.barrier is not None:
        if args.barrier_count < 2:
            parser.error("--barrier-count must be at least two")
        barrier = Path(args.barrier)
        barrier.mkdir(parents=True, exist_ok=True)
        ready = barrier / f"{Path(args.marker).name}.ready"
        ready.write_text("ready\n", encoding="ascii", newline="\n")
        deadline = time.monotonic() + 5.0
        while len(tuple(barrier.glob("*.ready"))) < args.barrier_count:
            if time.monotonic() >= deadline:
                raise TimeoutError("worker barrier did not reach its participant count")
            time.sleep(0.01)
    elif args.barrier_count:
        parser.error("--barrier-count requires --barrier")
    if args.delay:
        time.sleep(args.delay)
    destination = Path(args.relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(args.content + "\n", encoding="utf-8", newline="\n")
    finished = time.time_ns()
    result = {
        "finished_ns": finished,
        "path": args.relative_path,
        "pid": os.getpid(),
        "started_ns": started,
    }
    marker = Path(args.marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )
    with open_result_channel() as channel:
        channel.write(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
