# TradeOnce

TradeOnce wraps a trading agent and verifies code improvements against an
independent Judge. See [AGENTS.md](AGENTS.md) for the architecture and ownership
boundaries.

## MongoDB client

The shared `storage` package records Builder candidates and Judge verdicts in
two collections in the `tradeonce` database:

| Collection | Pydantic model | Contents |
| --- | --- | --- |
| `builder_candidates` | `BuilderCandidate` | Run, round, candidate name, git revision, and summary |
| `verification_results` | `VerificationResult` | Candidate ID, promote/reject decision, pass rate, failures, and reasons |

Both documents use string UUIDs as MongoDB `_id` values and UTC creation times.
`VerificationResult.candidate_id` references `BuilderCandidate.id`. Each evaluation
gets its own ID so a candidate can retain multiple verification results. The
client validates records when constructing them and when reading them from MongoDB.

### Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a MongoDB connection.

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

MongoDB holds metadata, not executable code or hidden test fixtures. Code and
`LESSONS.md` stay versioned in git; held-out inputs remain private to the Judge.
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

The current code includes our deterministic auditor, MongoDB client, and model API clients.
The Arena, Builder, Judge orchestration, and external-agent integration described
in `AGENTS.md` are separate workstreams. Any external trading agent remains its
authors' work; its license and attribution must accompany its integration.
