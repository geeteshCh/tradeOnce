# TradeOnce

TradeOnce wraps a trading agent and verifies code improvements against an
independent Judge. See [AGENTS.md](AGENTS.md) for the architecture and ownership
boundaries.

## Real coding demo: one timeout, two orders, one lesson

Run a real Gemini 4 Argon Builder through one sequential Harness² refinement
step, then transfer the approved Builder to a related accepted-then-500 failure:

```bash
uv sync --locked
direnv exec . uv run python -m builder.demo
# Use the printed report path for a reliable presentation of the actual live run:
uv run python -m builder.demo --replay runs/coding-demo/<run-id>/report.json
# Replay our checked-in actual Argon run, without keys or database access:
uv run python -m builder.demo --replay demo/recorded/argon-coding.json
```

Requires the private Gemini and MongoDB settings described below. Generated code
execution currently requires macOS with `/usr/bin/sandbox-exec`; other platforms
fail closed. The demo uses the local Arena and creates no real broker trades.

The first task reproduces the original Executor's duplicate order in the true
broker ledger. Harness² sees that reference execution, refines the Builder's
four instruction files, and calls the Argon coding Builder for each candidate.
Argon returns complete Python and `executor/LESSONS.md` contents through a
Pydantic model. A public failure can trigger one repair attempt, with at most
two generation calls per candidate. This path uses the official Google SDK;
the existing Aider Builder remains separately available.

Every candidate is a real commit in the run's isolated Git repository. The
candidate must pass training and regression scenarios, then the fixed auditor
and a runtime-private held-out suite. Held-out cases include ambiguous success,
transient failures where retrying is necessary, rate limits, and genuine
rejections. Merely disabling retries or increasing timeouts is insufficient.
Only independently approved code, lessons, Builder harness, and complete
Harness² library can advance MongoDB memory, using revision compare-and-swap.

The later task starts again from the original buggy Python source. A fresh
MongoDB connection reloads the approved Builder instructions and lessons, and
tries one repair before paying for another Harness² refinement. If the new
candidate fails any required check, it is rejected and refinement resumes.
The terminal reports actual Executor generation calls, including failed attempts.
First-task versus later-task counts are an observed transfer demonstration,
not a controlled causal estimate; no attempt reduction is hard-coded.

Verified live run `bf8959d802f7`: the timeout repair needed **3 patch proposals**
and 147 seconds; the later accepted-then-500 repair needed **1 proposal** and
36 seconds. Both passed **5/5 hidden cases**, the original timeout remained a
regression on the later task, Git advanced to `v1` then `v2`, and MongoDB memory
advanced from revision 0 to 1 to 2. The checked-in report is recorded live evidence,
not a script used by the code-generation loop.

The final report shows the actual Git diff, broker positions, public and private
pass counts, promotion tags, memory revisions, and proposal counts. Replay
re-presents that recorded live evidence without model calls. The original
runtime-private cases are retained locally under the ignored run's `private/`
directory with restricted permissions; verdicts record their SHA-256 digest.
They are never supplied to either Gemini proposer or code generator.

Source and responsibilities:

| Module | Responsibility |
| --- | --- |
| `builder/codegen.py` | Typed Argon patch generation from explicitly supplied public inputs |
| `builder/workflow.py` | Real Harness² run, Git versions, hidden gate, memory adoption, and transfer |
| `builder/evidence.py` | Trusted Arena ledger/auditor evidence and private variants |
| `builder/sandbox.py` | Disposable executor copies, clean environment, filesystem/network restrictions |
| `builder/demo.py` | Live command and replayable presentation |

The macOS sandbox admits only the copied Executor, Python runtime/dependencies,
and its exact local Arena port. It does not inherit API/database secrets, grant
access to the repository or hidden fixtures, or permit arbitrary network access.
No generated code runs directly in a developer checkout.

## MongoDB client

The shared `storage` package records Builder candidates and Judge verdicts in
three collections in the `tradeonce` database:

| Collection | Pydantic model | Contents |
| --- | --- | --- |
| `builder_candidates` | `BuilderCandidate` | Run, round, candidate name, git revision, and summary |
| `verification_results` | `VerificationResult` | Candidate ID, promote/reject decision, pass rate, failures, and reasons |
| `harness_memory` | `HarnessMemory` | Latest approved harness and Harness² lesson library per scope, with revision and proof IDs |

Both documents use string UUIDs as MongoDB `_id` values and UTC creation times.
`VerificationResult.candidate_id` references `BuilderCandidate.id`. Each evaluation
gets its own ID so a candidate can retain multiple verification results. The
client validates records when constructing them and when reading them from MongoDB.

### Setup

Requires Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), and a MongoDB connection.
The pinned Harness² dependency requires this Python range; `.python-version` selects 3.12.

```bash
uv sync
cp .env.example .env
# Set MONGODB_URI in .env; MONGODB_DATABASE defaults to tradeonce.
direnv allow
```

The committed `.envrc` loads your gitignored `.env`. Credentials are never part
of the example file. Without a direnv shell hook, prefix commands with
`direnv exec .`, or export the two variables in your existing environment.
Atlas connections must allow the public IP of the machine running the client.

Initialize the collections and their query indexes once:

```bash
direnv exec . uv run python -c \
  'from storage import MongoStore; s = MongoStore.from_env(); s.initialize(); print(s.ping()); s.close()'
```

### Write and read

```python
from storage import BuilderCandidate, MongoStore, VerificationResult

with MongoStore.from_env() as store:
    candidate = BuilderCandidate(
        run_id="demo-001",
        round=1,
        candidate="candidate_a",
        git_ref="abc1234",  # Use the candidate's actual git commit SHA.
        summary="Look up timed-out orders before retrying.",
    )
    store.save_candidate(candidate)

    verdict = VerificationResult(
        candidate_id=candidate.id,
        decision="promote",
        hidden_pass_rate=1.0,
        reasons=["All invariants and held-out scenarios passed."],
    )
    store.save_verification(verdict)

    print(store.get_candidate(candidate.id))
    print(store.get_verification(verdict.id))
    print(store.list_candidates("demo-001"))
    print(store.list_verifications(candidate.id))
```

`save_*` inserts a new ID or fully replaces the document with that ID. Reuse an
ID to update a record; create a new result to preserve a separate evaluation.
`get_*` returns `None` for a missing ID. Lists are ordered deterministically:
candidates by round, results by creation time, with IDs as tiebreakers.

Keep one store per process and close it when done; PyMongo manages connection
pooling. MongoDB and Pydantic errors propagate directly to the caller. To extend
the records, add typed fields in `storage/models.py`; the client serializes them
automatically. BSON stores timestamps to millisecond precision, which the models
normalize before writing.

MongoDB holds candidate metadata and approved Markdown harness/library snapshots.
Application code stays versioned in git; held-out inputs remain private to the Judge.
Saving a verdict does not promote or roll back code, and this shared client does
not enforce which agent can write a verdict. The orchestrator owns those rules.

### Validation

Run the existing auditor tests and offline storage tests without a database:

```bash
uv run python -m unittest discover -s tests -v
```

Run the live integration tests against your configured MongoDB:

```bash
direnv exec . env RUN_MONGODB_TESTS=1 uv run python -m unittest discover -s tests -v
```

The integration tests create unique test records and remove only their own
documents. They never drop a database or collection.

## Model API clients

The `providers` package exposes two small synchronous clients:

| Client | Default model | Use |
| --- | --- | --- |
| `GeminiClient` | `gemini-4-argon` | Builder text, code proposals, and Pydantic structured responses |
| `SubQClient` | `subq-snap-1.1` | Judge rubric assessments with a probability for each yes/no question |

Set `GEMINI_API_KEY` and `SUBQ_API_KEY` in your private `.env`. Optional
`GEMINI_MODEL` and `SUBQ_MODEL` override the defaults. Argon access is specific
to the provisioned hackathon account; teammates need credentials with that access.
Run `uv sync` after updating the checkout.

Gemini uses Google's official `google-genai` SDK. Snap uses its documented
Decisions endpoint through `httpx`; it is a scoring API, not the gated SubQ
Preview text-generation API. Both clients reuse connections, have a configurable
30-second timeout, and make one attempt per call. Provider errors propagate;
the caller controls retries and budget. Override `timeout_seconds` by constructing
`GeminiSettings` or `SubQSettings` explicitly. Use context managers to close clients.

### Builder: typed Gemini output

```python
from pydantic import BaseModel
from providers import GeminiClient

class ImprovementPlan(BaseModel):
    summary: str
    steps: list[str]

with GeminiClient.from_env() as gemini:
    plan = gemini.generate_structured(
        "Propose a minimal fix for an executor that retries a timed-out order "
        "and accidentally places it twice. Use client_order_id reconciliation.",
        ImprovementPlan,
        system_instruction="Use the training trace and editable code only.",
        max_output_tokens=2048,
    )
    print(plan.model_dump_json(indent=2))

    # For plain text or code instead:
    text = gemini.generate_text("Explain lookup-before-retry in one sentence.")
```

Your Builder can supply its own Pydantic response model and include the current
code and training traces in the prompt. It still owns patch application and
evaluation; the API client only returns model output.

### Judge: typed SubQ rubric scores

```python
from providers import BinaryCriteria, BinaryQuestion, SubQClient

with SubQClient.from_env() as subq:
    assessment = subq.evaluate(
        state={"intent_id": "buy-001", "fills": 2, "expected_fills": 1},
        questions={
            "needs_review": BinaryQuestion(
                instructions="Does this execution show a possible duplicate order?",
                criteria=BinaryCriteria(
                    true="More than one fill exists for one intended order.",
                    false="At most one fill exists for the intended order.",
                ),
            )
        },
    )
    score = assessment.answers["needs_review"].probability_yes
    print(score, assessment.usage)
```

SubQ accepts text or JSON state and 1–128 named questions. Scores remain
probabilities; the client does not threshold them into decisions. Keep `audit_run`
and held-out evaluation as the authority for invariants and promotion. Store
verification summaries only after those checks; a SubQ score is not a measured
`hidden_pass_rate`. The current SubQ access does not generate adversarial scenarios
or free-form explanations. Gemini can generate those when appropriate for the role.

Offline model-client tests use SDK mocks and HTTPX mock transports. Tiny live tests
exercise Gemini text, typed Gemini JSON, and SubQ scoring through these clients:

```bash
uv run python -m unittest discover -s tests -v
direnv exec . env RUN_MODEL_API_TESTS=1 uv run python -m unittest discover -s tests -v
```

## Built during the hackathon

The current code includes our deterministic auditor, MongoDB client, model API clients,
and both the real coding demo above and the lightweight Harness² policy fixture
below. The coding demo reuses the Arena, fixed auditor, and isolated Git versioning.
The Aider tournament and external-agent bridge remain separate entry points.
Any external trading agent remains its
authors' work; its license and attribution must accompany its integration.

## Harness² refinement and persistent memory

`harnessing` calls Google's real `harness2.core.schedule.run_task` with our
Gemini proposer, a TradeOnce benchmark adapter, and trusted application callbacks.
The upstream dependency is pinned to commit
[`6868817`](https://github.com/google-research/harness2/tree/6868817518876de8fd170c6a8354daca273b6191)
and remains Google's Apache-2.0-licensed work. Our adapter, proposer, verification
gate, MongoDB persistence, and retry-policy fixture are implemented here.

```text
approved MongoDB memory → Harness² + Gemini → candidate + lesson
                                             ↓
                         private verification versus the same baseline
                                             ↓
                         improvement → advance memory revision
                         rejection   → retain previous memory
```

The initial editable surface is four Markdown files: `systemprompt.md`,
`guardrails.md`, `memory.md`, and `toolguidance.md`. Gemini returns typed edits;
the proposer writes only these files. It receives public task inputs and execution
traces. The integration does not launch an agent CLI or execute generated scripts.
The adapter uses Harness²'s OpenCode prompt vocabulary for this text surface;
all model calls use the Google GenAI SDK.

### Try the complete loop

For the small declarative policy fixture, with MongoDB and Gemini configured above:
use `python -m builder.demo` above for actual Python code generation.

```bash
direnv exec . uv run python -m harnessing --scope demo-executor
# Include the optional public-trace SubQ assessment:
direnv exec . uv run python -m harnessing --scope demo-executor --subq
```

This fixture starts with a policy that blindly retries a timed-out submission,
creating duplicate orders. Gemini can propose lookup-before-retry. The existing
deterministic auditor checks broker ledger truth. Runtime-generated private cases
include submissions that were accepted and submissions that were never recorded;
simply disabling retries fails the latter. One verifier freezes the same cases for
baseline and candidate. Private inputs never enter the proposer context or git.
This proves the framework wiring with a declarative retry policy; connecting a
full coding Executor and Arena is the next application integration.

`--k 1` runs the base, invariant, domain, and composed policies, with a reflection
call for the lesson. `--mode sequential` is the default; `parallel` is also supported.
Artifacts go into ignored `runs/harness2/<run_id>/`. The final JSON reports the
candidate, verdict, loaded memory revision, and resulting memory revision.

### Use it from your Builder

```python
from harnessing import GeminiHarnessProposer, HarnessRunner, HarnessSettings
from harnessing.demo import DEFAULT_HARNESS, DEMO_TASK, demo_probe, make_demo_verifier
from providers import GeminiClient, GeminiSettings
from storage import MongoStore

settings = GeminiSettings.from_env()
with MongoStore.from_env() as store, GeminiClient(settings) as gemini:
    store.initialize()
    runner = HarnessRunner(
        store,
        GeminiHarnessProposer(gemini, settings.model),
        HarnessSettings(scope="demo-executor", git_ref="<actual source commit SHA>"),
    )
    result = runner.run(
        DEMO_TASK,
        initial_harness=DEFAULT_HARNESS,
        probe=demo_probe,
        verifier=make_demo_verifier(),
    )
```

For the application, replace the demo with a `HarnessTask`, a trusted
`Probe(task, harness) -> ProbeResult`, and a private
`Verifier(task, harness, probe) -> VerificationOutcome`. Use a separate scope per
compatible application/domain. These callbacks own execution isolation and the
actual regression/held-out checks. Never include private fixtures in task inputs,
public probe data, or harness files. SubQ can advise on public observations, but
the deterministic verification gate alone authorizes adoption.

Only a changed harness that passes every required check and improves on the
baseline may advance memory. Improvement means fixing a failing baseline, or
fewer measured broker calls when both pass. A supported lesson is required too.
Before saving `harness_memory`, the store checks the candidate's exact harness
contents, source revision, and passing verdict. Revision-based compare-and-swap
rejects concurrent stale writes. The next process reloads both the approved
harness and the complete upstream library (`INDEX.md`, adopted trees, and
`EXPERIENCE.md`). Rejected runs retain the prior memory snapshot. Candidate and
verdict history remains in the existing collections for inspection.

The orchestrator must own verifier and database-write authority. This shared
client is not an authorization boundary; direct database access can bypass its
proof checks. The fixture's source revision records provenance and does not create
a git commit for each generated policy.

Offline tests exercise the real upstream scheduler with a scripted proposer.
The opt-in MongoDB tests also prove adoption and reload across fresh runner/store
instances, and remove only their own records:

```bash
uv run python -m unittest discover -s tests -v
direnv exec . env RUN_MONGODB_TESTS=1 uv run python -m unittest discover -s tests -v
```

## Builder MVP

The `builder` package provides the controlled candidate loop: it creates isolated
Git worktrees, runs Aider against a failure trace, enforces the Builder's
`executor/` write boundary, runs the visible training command, and asks a locked
Judge callback for the promotion verdict. The Arena and Executor are intentionally
injected, so they can be added without changing the Builder.

```python
from pathlib import Path

from builder import BuilderRound

round_runner = BuilderRound(
    repo=Path("."),
    worktree_root=Path("../tradeonce-builder-worktrees"),
    base_ref="HEAD",
    training_command=["uv", "run", "python", "-m", "arena.run_training"],
    judge=judge_candidate,  # (worktree, git_ref) -> JudgeVerdict
)
winner, candidates, verdicts = round_runner.run(
    round_number=1,
    failure_trace="The timed-out order was submitted twice.",
)
```

Candidates are rejected if they modify anything outside `executor/`. Only
training-passing candidates are committed and sent to the Judge; a promoted
candidate receives a `vN` Git tag. Hidden tests and Judge implementation remain
outside the Builder worktree and are never included in its prompt.
