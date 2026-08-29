"""Record and verify controlled active-M1 performance evidence."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tests.performance.benchmark import run_controlled_benchmarks
from tests.performance.evidence import (
    evaluate_gate,
    read_evidence,
    write_evidence,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="measure, write, and gate current evidence")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--baseline", type=Path)
    run.add_argument(
        "--controlled",
        action="store_true",
        help="enforce wall-clock limits on an identity-pinned controlled runner",
    )
    run.add_argument("--require-baseline", action="store_true")
    run.add_argument("--work-root", type=Path, default=Path(tempfile.gettempdir()))
    run.add_argument("--replay-samples", type=int, default=5)
    run.add_argument("--graph-samples", type=int, default=7)
    run.add_argument("--graph-iterations", type=int, default=100)

    verify = commands.add_parser("verify", help="validate and gate recorded evidence")
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--baseline", type=Path)
    verify.add_argument(
        "--controlled",
        action="store_true",
        help="enforce wall-clock limits on an identity-pinned controlled runner",
    )
    verify.add_argument("--require-baseline", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "run":
            evidence = run_controlled_benchmarks(
                arguments.work_root,
                replay_samples=arguments.replay_samples,
                graph_samples=arguments.graph_samples,
                graph_iterations=arguments.graph_iterations,
            )
            write_evidence(arguments.output, evidence)
            evidence_path = arguments.output
        else:
            evidence = read_evidence(arguments.evidence)
            evidence_path = arguments.evidence
        baseline = (
            None if arguments.baseline is None else read_evidence(arguments.baseline)
        )
        report = evaluate_gate(
            evidence,
            baseline,
            controlled=arguments.controlled,
            require_baseline=arguments.require_baseline,
        )
    except (OSError, ValueError, AssertionError) as exc:
        print(json.dumps({"error": str(exc), "passed": False}, sort_keys=True))
        return 1

    output = report.to_primitive()
    output["environment_digest"] = evidence["environment"]["identity_digest"]
    output["evidence"] = str(evidence_path.resolve())
    output["summaries"] = {
        "checkpoint_tail": evidence["workloads"]["replay_100000_events"][
            "summaries"
        ]["checkpoint_tail"],
        "cold_replay": evidence["workloads"]["replay_100000_events"][
            "summaries"
        ]["cold_replay"],
        "graph_batch": evidence["workloads"]["graph_64_tasks_512_edges"][
            "summaries"
        ]["batch"],
        "peak_rss_bytes": evidence["workloads"]["replay_100000_events"][
            "measurements"
        ]["peak_rss_bytes"],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
