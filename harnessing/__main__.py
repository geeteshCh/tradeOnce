"""Run the trading retry-policy fixture through Harness² and MongoDB memory."""

import argparse
import subprocess
from contextlib import ExitStack
from pathlib import Path

from harnessing.demo import DEFAULT_HARNESS, DEMO_TASK, demo_probe, make_demo_verifier
from harnessing.models import HarnessTask
from harnessing.proposer import GeminiHarnessProposer
from harnessing.runner import HarnessRunner, HarnessSettings
from providers import GeminiClient, GeminiSettings, SubQClient
from storage import MongoStore


def main() -> None:
    """Expose a real model-backed refinement run with an explicit demo scope."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="demo-executor")
    parser.add_argument("--task-id", default="retry-reconciliation")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/harness2"))
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument(
        "--mode", choices=("parallel", "sequential"), default="sequential"
    )
    parser.add_argument(
        "--git-ref", help="Source revision; defaults to this checkout's HEAD"
    )
    parser.add_argument(
        "--subq", action="store_true", help="Include advisory SubQ assessment"
    )
    args = parser.parse_args()
    revision = (
        args.git_ref
        or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    )
    settings = HarnessSettings(
        scope=args.scope,
        git_ref=revision,
        runs_dir=args.runs_dir,
        k=args.k,
        mode=args.mode,
    )
    task = HarnessTask(
        id=args.task_id,
        prompt=(
            "Execute the authorized trading intent exactly once, despite an ambiguous "
            "submission response. Observe the training trace and improve the retry policy. "
            "toolguidance.md must contain only a JSON object with retry_policy set to one "
            "of blind_retry, lookup_before_retry, or never_retry. The lookup operation "
            "resolves whether the broker accepted a submission; resubmit only if needed. "
            "Other Markdown files may hold instructions and evidence-supported lessons."
        ),
        domain=DEMO_TASK.domain,
        inputs=DEMO_TASK.inputs,
    )
    with ExitStack() as stack:
        store = stack.enter_context(MongoStore.from_env())
        store.initialize()
        gemini_settings = GeminiSettings.from_env()
        gemini = stack.enter_context(GeminiClient(gemini_settings))
        proposer = GeminiHarnessProposer(gemini, gemini_settings.model)
        advisor = stack.enter_context(SubQClient.from_env()) if args.subq else None
        runner = HarnessRunner(store, proposer, settings, advisor=advisor)
        result = runner.run(
            task,
            initial_harness=DEFAULT_HARNESS,
            probe=demo_probe,
            verifier=make_demo_verifier(),
        )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
