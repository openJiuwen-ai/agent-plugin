"""Command-line entry point.

Replaces runner.py + usecases/AG.py. Differences that matter:

* Non-interactive by default. Every previous `input()` prompt is a flag; the
  prompt survives only as a TTY fallback when the flag is omitted.
* Returns a real exit code. The old runner caught every exception, logged it,
  printed "All N log runs completed", and exited 0 — a run that made zero
  successful API calls looked identical to a successful one.
* No `--framework`: it had one legal value and wrapped a single chat completion.
* No mandatory HTTPS_PROXY check: it hard-raised before doing anything, on a
  machine-specific assumption.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from hashlib import sha256
from uuid import uuid4
from pathlib import Path

from .core import artifacts as artifacts_mod
from .core.config import DEFAULT_PARAMETERS, TAXONOMIES
from .dependencies import EvaluationDependencyError, require_evaluation_dependencies
from .core.llm import ModelOutputError
from .core.logging import TerminalCapture, setup_logging
from .core.providers import (
    PROVIDERS,
    build_client,
    request_extras,
    sampling_params,
)
from .domain import taxonomy as taxonomy_mod
from .domain import trajectories
from .modules import failure_detector
from .state import read_json, write_json

MODULES = {failure_detector.NAME: failure_detector}

logger = logging.getLogger("ojw_evaluation")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ojw_evaluation",
        description="Evaluation modules over agentic structured trajectories.",
    )
    p.add_argument("command", choices=["run"], help="What to do")
    p.add_argument(
        "module", nargs="?", default=failure_detector.NAME, choices=list(MODULES)
    )
    p.add_argument(
        "--strategy",
        default=DEFAULT_PARAMETERS["strategy"],
        help="Detection strategy (few-shot is currently deferred)",
    )
    p.add_argument("--taxonomy", choices=list(TAXONOMIES), default=DEFAULT_PARAMETERS["taxonomy"])
    p.add_argument("--client", choices=list(PROVIDERS), default=DEFAULT_PARAMETERS["client"])
    # --model-id matches the runner's flag (ojw_agentic_applications) so the two
    # CLIs share one vocabulary; --model is kept as an alias.
    p.add_argument("--model-id", "--model", dest="model", default=None,
                   help="Defaults to the provider's model")
    p.add_argument("--trajectory", default=None, type=str)
    p.add_argument("--temperature", default=DEFAULT_PARAMETERS["temperature"], type=float)
    p.add_argument("--top-p", default=DEFAULT_PARAMETERS["top_p"], type=float)
    p.add_argument("--output-dir", default=None, type=str,
                   help="Evaluation parent directory (default: beside the trajectory in evaluation/). "
                        "Each run creates a YYYY-MM-DD_HH-MM-SS_terminal subdirectory (UTC).")
    p.add_argument("--print-traces", action="store_true")
    p.add_argument("--dedup", choices=["none", "exact", "by_code"], default=DEFAULT_PARAMETERS["dedup"])
    return p


def resolve_trajectory(value: str | None) -> Path:
    """Resolve --trajectory, falling back to a prompt only on an interactive TTY."""
    if value:
        path = Path(value).expanduser()
        if not path.is_file():
            raise SystemExit(f"No trajectory file at {path}")
        return path

    if not sys.stdin.isatty():
        raise SystemExit("--trajectory is required in non-interactive use")
    choice = input("Trajectory file path: ").strip()
    if not choice:
        raise SystemExit("A trajectory file path is required")
    return resolve_trajectory(choice)


async def run_failure_detector(args, obs, run_dir, *, client_config=None) -> int:
    client, default_model = client_config if client_config is not None else build_client(args.client)
    model = args.model or default_model
    params = sampling_params(model, args.temperature, args.top_p)
    extra_body = request_extras(args.client)

    if args.temperature is not None and not params:
        logger.warning(
            "Model %s only accepts default sampling parameters; "
            "ignoring --temperature/--top-p",
            model,
        )

    traj_path = resolve_trajectory(args.trajectory)
    parsed, trajectory_text = trajectories.load(traj_path)
    logger.info("Trajectory: %s (%s chars)", traj_path.name, f"{len(trajectory_text):,}")

    if args.strategy in failure_detector.DEFERRED:
        raise SystemExit(
            f"Strategy {args.strategy!r} is {failure_detector.DEFERRED[args.strategy]}"
        )
    if args.strategy not in failure_detector.STRATEGIES:
        raise SystemExit(
            f"Unknown strategy {args.strategy!r}. "
            f"Available: {', '.join(failure_detector.STRATEGIES)}"
        )

    with obs.run_span(1):
        if args.strategy == "zero-shot":
            result, _ = await failure_detector.zero_shot(
                client, model, trajectory_text,
                params=params, artifacts=run_dir, extra_body=extra_body,
            )
            run_dir.write_json("annotations.json", result.model_dump())
            logger.info("Steps annotated: %d", len(result.annotations))
            return 0

        tax = taxonomy_mod.load(TAXONOMIES[args.taxonomy], name=args.taxonomy)
        logger.info("Taxonomy %s: %d categories", tax.name, len(tax))

        results = await failure_detector.taxonomy_guided(
            client, model, trajectory_text, tax,
            params=params, artifacts=run_dir, extra_body=extra_body,
        )
        if not results:
            logger.error("No category returned usable output")
            return 1

        steps = failure_detector.aggregate(
            results, step_order=trajectories.step_ids(parsed)
        )
        if args.dedup != "none":
            steps = failure_detector.dedup(steps, policy=args.dedup)

        run_dir.write_json(
            "annotations.json", [s.model_dump() for s in steps]
        )
        total = sum(len(s.failures) for s in steps)
        logger.info(
            "Aggregated %d categories -> %d steps, %d failures (%d with >=1)",
            len(results),
            len(steps),
            total,
            sum(1 for s in steps if s.failures),
        )
        if len(results) < len(tax):
            logger.warning(
                "Only %d of %d categories produced output", len(results), len(tax)
            )
            return 1
        return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        require_evaluation_dependencies()
    except EvaluationDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    from .core.tracing import Observability

    # Resolve before creating output directories, including interactive selection.
    trajectory = resolve_trajectory(args.trajectory).resolve()
    args.trajectory = str(trajectory)
    output_parent = Path(args.output_dir) if args.output_dir else trajectory.parent / "evaluation"
    job_dir = artifacts_mod.create_evaluation_directory(output_parent, "terminal")
    module = MODULES[args.module]
    run_dir = artifacts_mod.RunDir(
        job_dir,
        module.NAME,
        args.strategy,
        {
            "taxonomy": args.taxonomy if args.strategy != "zero-shot" else "",
            "model": args.model or args.client,
            "trajectory": Path(args.trajectory).stem if args.trajectory else "",
        },
    )

    # Keep the same record names and result fields as UI evaluations. These
    # records are independent of a running UI service and contain no credentials.
    def save_parameters(model):
        sampling = sampling_params(model, args.temperature, args.top_p)
        parameters = {
            "client": args.client, "model": model,
            "base_url": PROVIDERS[args.client]["base_url"],
            "strategy": args.strategy,
            "taxonomy": args.taxonomy if args.strategy == "taxonomy-guided" else None,
            "dedup": args.dedup if args.strategy == "taxonomy-guided" else "none",
            "temperature": sampling.get("temperature"), "top_p": sampling.get("top_p"),
        }
        write_json(job_dir / "parameters.json", parameters)
        return parameters

    job = {
        "id": uuid4().hex, "origin": "terminal", "module": module.NAME,
        "status": "running", "created_at": time.time(), "context_id": None,
        "output_dir": str(job_dir),
        "input": {"name": trajectory.name, "source_path": str(trajectory)},
        "parameters": save_parameters(args.model or PROVIDERS[args.client]["default_model"]),
    }
    write_json(job_dir / "evaluation.json", job)
    obs = Observability(print_traces=args.print_traces)
    code, error, interrupted = 1, None, False
    try:
        snapshot = trajectory.read_bytes()
        (job_dir / "trajectory.json").write_bytes(snapshot)
        digest = sha256(snapshot).hexdigest()
        write_json(job_dir / "source.json", {
            "source_path": str(trajectory), "source_kind": "server",
            "source_sha256": digest, "snapshot": "trajectory.json",
            "snapshot_sha256": digest,
        })
        # Evaluate exactly the bytes saved with this run, even if the source changes.
        args.trajectory = str(job_dir / "trajectory.json")
        obs.init()
        # setup_logging() runs inside capture so its handler writes to run.log.
        with TerminalCapture(run_dir.terminal_log):
            setup_logging(logging.INFO)
            try:
                client_config = build_client(args.client)
                args.model = args.model or client_config[1]
                job["parameters"] = save_parameters(args.model)
                write_json(job_dir / "evaluation.json", job)
                code = asyncio.run(run_failure_detector(args, obs, run_dir, client_config=client_config))
            except ModelOutputError as exc:
                logger.error("The model did not return the requested JSON. %s", exc)
                error = "The model did not return valid evaluation output. Check run.log."
                code = 1
            except Exception as exc:
                # Handle provider failures before TerminalCapture can save their
                # traceback: response bodies can echo the submitted credential.
                logger.error("Run failed (%s). Check provider credentials, model access, and network settings.",
                             type(exc).__name__)
                error = "Evaluation failed. Check run.log and the terminal output."
                code = 1
    except KeyboardInterrupt:
        interrupted, code = True, 130
        error = "Evaluation interrupted from the terminal."
        raise
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        error = "Evaluation exited before completion. Check the terminal output."
        raise
    except Exception as exc:
        # The capture may already be closed. Avoid its logging handlers and do
        # not expose arbitrary exception text from setup or filesystem failures.
        print(f"Run failed ({type(exc).__name__}). Check input and output paths.", file=sys.stderr)
        error = "Evaluation failed. Check run.log and the terminal output."
        code = 1
    finally:
        # Never persist exception text: provider errors may contain credentials.
        annotations_path = run_dir.cell / "annotations.json"
        annotations = read_json(annotations_path)
        status = ("interrupted" if interrupted else
                  "completed" if code == 0 and error is None else
                  "partial" if annotations is not None else "failed")
        result = {
            "status": status, "exit_code": code, "annotations": annotations,
            "annotations_file": str(annotations_path) if annotations is not None else None,
            "artifacts_dir": str(run_dir.cell),
            "message": {"completed": "Evaluation completed", "partial": "Evaluation finished with partial results",
                        "failed": "Evaluation failed", "interrupted": "Evaluation interrupted"}[status],
        }
        if error:
            result["error"] = error
        write_json(job_dir / artifacts_mod.FAILURE_DETECTOR_RESULT, result)
        job.update(result, finished_at=time.time())
        write_json(job_dir / "evaluation.json", job)
        obs.shutdown()

    print(f"\nOutput: {run_dir.cell}")
    return code


if __name__ == "__main__":
    sys.exit(main())
