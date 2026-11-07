# Judge brief (TradeOnce)

Hand this file to the Judge owner, or to their coding agent. It is self-contained. The full architecture is in `AGENTS.md`.

**Deadline: Adversary and hidden set working in the loop by 3:15 PM.** Feature freeze 4:00, submission 5:00.

---

## 1. The project in four lines

- TradeOnce wraps an **external trading agent** (virattt/ai-hedge-fund, unmodified) with a **software layer** whose Executor places the agent's trades at a broker, exactly once, even when the broker misbehaves.
- A **Builder** (an LLM, another team member) rewrites the Executor after every failure.
- **Your job: the Judge.** An independent verifier that decides whether each rewrite is promoted or rolled back (*Trust but Verify* track), plus the **Adversary**, a red-team LLM that invents new attacks.
- Independence is the point: run on a **different model family** from the Builder, keep your hidden tests where the Builder can't see them, and never trust what the Executor claims. Check the broker's true record.

## 2. What already exists (don't rebuild it)

| Piece | Where | Status |
|---|---|---|
| Your auditor (`audit_run(intent, snapshot)`) | `auditor/invariants.py` | **Already called on every run** by the loop |
| Adapter from the Arena's ledger to your snapshot format | `loop/judge.py` (`ledger_to_snapshot`) | Done |
| Tournament rule (eligible = passes everything; winner = most hidden passes, then fewer broker calls) | `loop/judge.py` (`decide`) | Done |
| Fake, unreliable broker with the true ledger (the Arena) | `arena/` | Done |
| Stand-in Adversary (replays fixed scenarios) | `loop/adversary.py` (`queue:`) | Done |
| Stand-in hidden set generator (10 scenarios) | `loop/make_hidden.py` → `hidden_set/` | Done; replace it with yours |
| Gemini and SubQ clients | `providers/` | Done |

Setup: `uv sync`, then `uv run pytest -q` (expect 50 passed, 5 skipped).

## 3. What's missing (your deliverables)

1. **A real Adversary**: an LLM that writes new attack scenarios.
2. **Your own hidden set** in `auditor/hidden/`.
3. Optionally, a stricter judge function.

## 4. The Adversary

```bash
uv run python -m loop.run --adversary yourpkg.adversary:attack --builder aider
```

```python
from arena.models import Scenario, FAULT_TYPES, ENDPOINT_KEYS

def attack(round: int, history: list[dict]) -> Scenario | None:
    """Return the next attack, or None when out of ideas.
    history = [{"round", "scenario_id", "faults", "broke_promoted": bool}, ...]  (what you tried and whether it worked)
    """
    # 1. Prompt the LLM with: the fault catalog (FAULT_TYPES, ENDPOINT_KEYS, the table below), the history,
    #    and the scenario JSON format. Ask for ONE scenario that breaks the current promoted Executor.
    # 2. Validate: Scenario.model_validate(json). On ValidationError, send the error back to the LLM and retry (2-3 times).
    # 3. Use a NEW id each round, e.g. f"adv-r{round:02d}".
```

- Use a **different model family from the Builder**. If the Builder runs on Gemini, use SubQ here, or a different Gemini model as a fallback. `providers.SubQClient.from_env()` reads `SUBQ_API_KEY`; `providers.GeminiClient.from_env()` has `generate_structured(prompt, Scenario)`, which returns a validated Pydantic model.
- **Your score** is how often you break the promoted version (`broke_promoted`). An attack that survives goes into the regression suite. One that breaks it goes into training, so the Builder can fix it.
- Escalate: single faults first, then combinations (timeout plus stale status, a fault on the 2nd of 3 intents, partial fills on sells).

**Scenario format** (`arena/models.py`):

```json
{"id": "adv-r03", "seed": 42, "description": "why this should break the executor",
 "intents": [{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 2}],
 "faults":  [{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 1.5}},
             {"match": "GET /v2/orders*", "nth": 1, "fault": "stale_status", "params": {"calls": 3}}],
 "prices": {"AAPL": 200.0}, "starting_cash": 100000,
 "expected_positions": null}
```

- `match`: an endpoint key or an fnmatch glob (`"GET /v2/orders*"`). `nth`: the 1-based count of matching calls, an int or a list.
- `expected_positions: null` means every intent should end fully filled. Set it when the right answer is different, e.g. `{}` when every order is really rejected.

**Fault catalog:**

| Fault | Effect | Param (default) |
|---|---|---|
| `accept_then_timeout` | Order recorded and filled, reply hangs past the client timeout | `delay_s` (2.0) |
| `accept_then_500` | Recorded and filled, then 500 | |
| `transient_500` / `rate_limit_429` | Nothing recorded (retrying is correct) | |
| `real_reject` | 403, nothing recorded (report rejected) | |
| `stale_status` | Lookups say `new` / `filled_qty 0` for N calls | `calls` (2) |
| `delayed_fill` | Accepted as `new`, fills N broker calls later | `after_calls` (3) |
| `partial_fill_then_cancel` | Fills part, cancels the rest | `fill_ratio` (0.5) |
| `duplicate_fill` | Fill shows twice in the activity feed; the ledger has one | |

## 5. The hidden set

- Put scenario JSON files (same format as above) in **`auditor/hidden/`**. The loop uses it automatically when it exists (`--hidden` overrides). **It's gitignored: never commit it.** The repo is public and a Builder can search the web.
- Make variants the Builder never sees: longer or shorter timings (`delay_s`, `after_calls`, `calls`), other symbols and sizes, faults on the 2nd intent of several, sells as well as buys.
- **Include counter-cases** that catch overfit fixes:
  - `transient_500` and `rate_limit_429`: retrying is correct, so a "never retry" fix fails.
  - `real_reject` on every attempt (`"nth": [1, ..., 10]`, `expected_positions: {}`): the right answer is report `rejected` with nothing placed, so "retry until it fills" fails.
  - `real_reject` on one of two intents (`expected_positions` set to what the other intent fills): one failure must not stop the other trade.
- The loop only activates hidden scenarios whose faults have already been attacked, plus the counter-case faults, so the Builder isn't judged on faults it has never seen.
- Look at `hidden_set/` (from `uv run python -m loop.make_hidden`) for 10 working examples.

## 6. Optional: your own judge function

The default judge is `auditor.audit_run` per intent, plus the Arena's completion checks. To replace it:

```bash
uv run python -m loop.run --judge yourpkg.judge:judge
```

```python
def judge(trace: dict) -> list[dict]:
    """trace = {scenario, executor_claims, claims_error, exit_code, timed_out, elapsed_s, stderr_tail, ledger}.
    Return failures as [{"code": "OVERFILL", "message": "..."}, ...]; an empty list means pass."""
```

Judge **only from `trace["ledger"]`** (the broker's true record: orders, fills, positions, cash, and every call with the fault that fired). Never trust `executor_claims` except to check that they match the ledger. Keep it **deterministic: no LLM in the pass/fail decision.** An LLM may write hidden variants ahead of time, or plain-language reasons.

## 7. Done when

- [ ] `uv run python -m loop.run --adversary yourpkg.adversary:attack --builder aider` runs 4 rounds, and your Adversary produces valid scenarios.
- [ ] At least one Adversary attack **breaks** the promoted version, and the Builder then fixes it.
- [ ] `auditor/hidden/` exists with your variants, including counter-cases, and **at least one candidate gets rolled back** because of it (the demo moment).
- [ ] Note the run id and tell the Loop & Stage owner.

## 8. Don'ts

- Don't commit keys (`.env` is gitignored) or `auditor/hidden/`.
- Don't let any agent write to `auditor/`.
- Don't put an LLM in the pass/fail decision.
