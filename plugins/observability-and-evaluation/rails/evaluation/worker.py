"""Isolated evaluation process. Receives credentials through stdin, never argv."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

# A crashed Linux UI process must not leave an untracked paid worker running.
# Set this before importing the evaluator; explicit CLI execution has no guard.
if __name__ == "__main__" and len(sys.argv) > 1 and sys.platform == "linux":
    import ctypes
    import os
    import signal
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != int(sys.argv[1]):
        raise SystemExit(1)

# Direct execution also works from a plugin imported under a loader alias.
if not __package__:
    import importlib.util
    directory = Path(__file__).resolve().parent
    __package__ = "_ojw_evaluation_worker"
    spec = importlib.util.spec_from_file_location(
        __package__, directory / "__init__.py", submodule_search_locations=[str(directory)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[__package__] = package
    spec.loader.exec_module(package)

from .cli import run_failure_detector
from .core.artifacts import FAILURE_DETECTOR_RESULT, RunDir
from .core.providers import PROVIDERS
from .core.tracing import Observability
from openai import AsyncOpenAI, AuthenticationError, PermissionDeniedError, RateLimitError, APIConnectionError, APITimeoutError, BadRequestError
from .core.llm import ModelOutputError, reset_usage, usage_snapshot


async def evaluate(request):
    job_dir = Path(request["job_dir"])
    api_key = request.pop("api_key")
    base_url = request.pop("base_url")
    args = SimpleNamespace(**request, trajectory=str(job_dir / "trajectory.json"))
    run_dir = RunDir(job_dir, "failure-detector", args.strategy,
                     {"taxonomy": args.taxonomy, "model": args.model, "trajectory": "trajectory"})
    # Never persist the request object: it contains the key until it is popped.
    (job_dir / "parameters.json").write_text(json.dumps({
        k: getattr(args, k) for k in ("client", "model", "strategy", "taxonomy", "dedup", "temperature", "top_p")
    }, indent=2))
    obs = Observability(print_traces=False)
    obs.init()
    try:
        async with AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=1) as client:
            code = await run_failure_detector(args, obs, run_dir,
                client_config=(client, PROVIDERS[args.client]["default_model"]))
        annotations_path = run_dir.cell / "annotations.json"
        annotations = json.loads(annotations_path.read_text()) if annotations_path.is_file() else None
        return {"status": "completed" if code == 0 else ("partial" if annotations is not None else "failed"),
                "exit_code": code, "annotations": annotations,
                "annotations_file": str(annotations_path) if annotations_path.is_file() else None,
                "artifacts_dir": str(run_dir.cell),
                "message": "Evaluation completed" if code == 0 else "Some categories failed; results are incomplete"}
    finally:
        obs.shutdown()


def main():
    request = json.load(sys.stdin)
    job_dir = Path(request["job_dir"])
    reset_usage()
    try:
        result = asyncio.run(evaluate(request))
    except (AuthenticationError, PermissionDeniedError):
        result = {"status": "failed", "error": "Provider rejected the API key or access to this model."}
    except RateLimitError:
        result = {"status": "failed", "error": "Provider rate or quota limit reached. Retry later or check your quota."}
    except APITimeoutError:
        result = {"status": "failed", "error": "The model provider timed out."}
    except APIConnectionError:
        result = {"status": "failed", "error": "Cannot connect to the model provider. Check the API base URL and server network access."}
    except BadRequestError:
        result = {"status": "failed", "error": "Provider rejected the model request. Check model ID, supported parameters and input size."}
    except ModelOutputError:
        result = {"status": "failed", "error": "The model returned invalid evaluation output. Try another judge model."}
    except OSError:
        result = {"status": "failed", "error": "Cannot read the trajectory or write evaluation artifacts. Check folder permissions and disk space."}
    except Exception:
        result = {"status": "failed", "error": "Evaluation failed while processing model output. Check the selected trajectory and evaluation dependencies."}
    # Failed and partial runs still consumed tokens; record whatever was used.
    usage = usage_snapshot()
    if usage["calls"]:
        result["judge_usage"] = usage
    (job_dir / FAILURE_DETECTOR_RESULT).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
