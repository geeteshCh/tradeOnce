# TradeOnce: System Architecture

**Pitch:** TradeOnce wraps any trading agent and keeps making it safer. Every bug the red team finds becomes a code change, and it ships only if an independent judge, running a different model, can't break it on unseen tests.

**Hackathon tracks:** *The System That Gets Better* (Builder) + *Trust but Verify* (Judge).

TradeOnce does **not** predict prices or pick stocks. It is a **wrapper around an existing open-source trading agent** that improves that agent's code, and the code that places its trades, so its trades are **correct and safe**. It does not try to make the agent more profitable.

---

## What TradeOnce wraps

We do **not** build the trading agent. We fork an existing open-source one (default: **virattt/ai-hedge-fund**; check its license and code layout first) and wrap it:

```
            ┌─────────── Builder (Aider + LLM) edits both purple parts ───────────┐
            ▼                                                                     ▼
 External agent (our fork)                                                        
   LLM analysts ──signals──▶ [risk + portfolio code] ──intents──▶ [Executor] ──orders──▶ Arena
   (frozen, cached)           purple: improved                     purple: improved      (true record)
                                     │                                                    │
                                     └──────── decisions ──────▶ Judge ◀──── ledger ──────┘
                                                                   │
                                                  verdict ──▶ Loop ──▶ promote / roll back (git tag)
```

**What improves** (versioned; promoted or rolled back):
1. **The external agent's risk and portfolio code** (in our fork): the code that turns "bullish AAPL" into "buy 3 AAPL".
   - Fixes it should learn: size from current holdings (don't buy 10 more when already at target), never exceed cash, respect position limits, hold when a price is missing or stale, only trade allowed tickers.
2. **The Executor**: the code that turns "buy 3 AAPL" into broker calls.
   - Fixes it should learn: `client_order_id` plus lookup before retry (no duplicates), don't trust stale status, finish partial fills, cancel or wait before replacing a late order.
3. **The Builder's own lessons and tools** (`LESSONS.md`, `executor/tools/`): it fixes the next failure faster. This is the "improves itself" part of the track.

**What never changes during a run:** the agent's LLM analysts (their signals are run once and cached), the Arena, the Judge's checks, the hidden set. If the system could change its own tests, it could grade itself a pass.

**Who wrote what** (say this in the README and the pitch; the rules disqualify unclear contributions):

| Piece | Written by | Changed by |
|---|---|---|
| Trading agent, LLM analysts | Its authors (third-party) | Nobody: frozen, cached signals |
| Trading agent, risk + portfolio code | Its authors (third-party) | **Our Builder**, in our fork; every change in the fork's git history was made by TradeOnce and verified by our Judge |
| Arena, Executor, Builder, Judge, loop, adapter | **Us, during the hackathon** | The Executor by our Builder; the rest is fixed |

**Why the analysts are frozen:** re-running them means many LLM calls per ticker per day for every candidate. That takes minutes, costs money and gives different answers each time. Cached signals make each evaluation take seconds and give the same result every run. Letting the Builder edit analyst prompts is a stretch goal only.

**Priority:** the **Executor loop is the core demo** (half built). The agent-improvement loop is **round 2**, using decision invariants on cached signals (about 1.5 hours for one person). A held-out backtest score is only added if everything else works by 3:30.

---

## The 4 parts (one owner each)

### 1. The Arena (the world) — **built, see [Arena reference](#arena-reference)**
*Simulated broker + fault engine*
- A fake broker with Alpaca's API shape (`POST /v2/orders`, `GET /v2/orders`, `GET /v2/orders/{id}`, `DELETE /v2/orders/{id}`, `GET /v2/orders:by_client_order_id`, `GET /v2/positions`, `GET /v2/account`, `GET /v2/account/activities/FILL`).
- Holds the **true record** of orders, fills and cash. The Judge reads this; it never trusts the Builder's claims.
- Injects faults from JSON fault scripts, seeded so runs are reproducible.
  - Example: `{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout"}`
  - Fault types: see the [fault table](#fault-types).
- **Output:** a place where trades can go wrong in ways you can reproduce.

### 2. The Builder (gets better)
*Executor + evolution engine. Improves two targets, in separate rounds: the Executor (core) and the external agent's risk + portfolio code (round 2).*
- **Target 2, the external agent's decision code** (`agent/`, our fork): the Builder may edit only the fork's risk-manager and portfolio-manager files (exact paths: fill in after checking the repo; if the portfolio manager is an LLM call, the Builder edits its prompt and rules instead). It is driven by cached signals in `agent/signals/` and judged on decision invariants (see the Judge).
- **Executor** (target 1, the code the agent rewrites): `execute(intent) -> result`, where an intent is `{intent_id, symbol, side, qty}`.
  - `tools/` holds agent-created tools (e.g. `reconcile_order.py`), loaded through a registry.
  - v0 is deliberately naive: it retries on timeout, which causes duplicate orders.
  - The same code runs against the simulator and against the Alpaca paper account; only the base URL changes.
- **Evolution engine**: takes failure traces from the training set and produces a candidate patch (an edited file or a new tool) on its own git branch. Built on **Aider** (see *Agent stack*).
- **Memory** is `executor/LESSONS.md`, which the Builder updates each round. It is versioned in git and promoted or rolled back with the code.
- **Tournament:** 2 candidates per round (different models or prompts).
- **Output:** candidate Executor versions.

### 3. The Judge (verifies and attacks)
*Auditor + Adversary, on a different model family from the Builder*
- **Auditor**: locked code the agents cannot write to.
  - **Invariants**, checked on every run against the broker's true record:
    - At most one fill per `intent_id`
    - Final position equals the intended position
    - Cash is conserved
    - No orphan or unknown orders
    - No retry while an order's status is still unknown
  - **Test layers:**
    1. *Training set*: visible to the Builder, includes failure traces for debugging.
    2. *Regression suite*: every scenario passed in earlier rounds; it must still pass.
    3. *Hidden held-out set*: only the Auditor can read it. Variants with different timing, symbols and order sizes, plus counter-cases (e.g. "the order really was rejected, so retrying is correct") that catch overfit fixes.
  - **Tournament rule:** a candidate must pass every invariant to be eligible. The winner has the most hidden-set passes, with fewer broker API calls as the tiebreaker. If neither qualifies, the current version stays.
  - **Decision invariants** (round 2, checked on the intents the agent's decision code produces from cached signals; deterministic, no LLM):
    - Never exceed available cash
    - Never exceed the per-ticker position limit or the daily risk cap
    - Only trade tickers in the allowed universe (catches hallucinated tickers)
    - Size from current holdings: already at target means 0 shares, not another full buy
    - Hold when a price is missing or stale, or the ticker is halted
    - Each intent has a stable `intent_id` (run ID + date + ticker), so the same decision can't become two trades
  - **Decision hidden set:** held-out **dates and tickers** the Builder never saw, plus market-condition variants. Optional secondary score: risk-adjusted return on held-out date windows; it may not get worse.
- **Adversary**: a red-team LLM that writes new fault scripts, checked against the format before use. Its score is how many invariant violations it causes against the current promoted version. Its scenarios go into training, then into regression once the Builder beats them.
  - Round 2: it also writes **market conditions** for the decision code: a crash day, a price gap, a missing or stale price, a halted ticker, an oversized signal.
- **Output:** new attacks, and a promote/reject verdict with reasons for every candidate.

### 4. The Loop & Stage (runs it and shows it)
*Orchestrator + versioning + demo*
- **One round:** attack → fail → patch → judge → promote (`git tag vN`) or roll back (`git revert`).
- Writes everything to `events.jsonl`, with a replay mode for the demo (live LLM output varies).
- A Rich live terminal view of rounds, attacks, failures, patches, verdicts and the version timeline. No Streamlit; the dashboard is not the main feature.
- The final Alpaca paper trade through the promoted Executor.
- **The external agent:** fork it into `agent/`, run its LLM analysts **once** and save the signals to `agent/signals/` (per ticker per day), and write the **adapter** that turns the agent's decisions into our intents (skip holds; `intent_id` = run ID + date + ticker).
- README ("built during the hackathon" section, plus the who-wrote-what table), 1-minute video, pitch.

---

## How they connect

```
Judge (Adversary) ──fault scripts / market conditions──▶ Arena ◀──orders── Executor ◀──intents── agent decision code
                                       │                                      ▲                    ▲
                                 true records                          Builder edits         Builder edits
                                       ▼                                      │                    │
     Judge (Auditor: execution + decision invariants) ──verdict──▶ Loop & Stage ──traces──▶ Builder
                                                                         └──▶ promote / rollback (git tag)
```

The Arena is ready: code against it directly (`from arena import arena_server, run_scenario`).

---

## Shared interfaces

The Pydantic models live in **`arena/models.py`**; every part imports them (`from arena.models import Scenario, Intent, IntentResult, FaultRule`). Tell the team before changing a field name.

1. **Broker API subset:** endpoints and JSON fields copied from Alpaca (see [Arena reference](#arena-reference)).
2. **Intent / result** for `execute()`: `{intent_id, symbol, side, qty}` → `{intent_id, status, broker_order_id, filled_qty}`
   - `status` must be one of `filled | partially_filled | rejected | unknown | failed`. **Do not pass the broker's status through** (`new`, `canceled` are invalid here).
3. **Fault script:** `{match, nth, fault, params?}`. `match` is an endpoint key or an fnmatch glob (`"GET /v2/orders*"`); `nth` is an int or a list of ints (1-based count of matching calls).
4. **Event:** `{ts, round, actor, type, data}`
5. **Verdict:** `{candidate, decision: "promote" | "reject", invariant_failures[], hidden_pass_rate, reasons}`
6. **Scenario** (what the Adversary produces and the runner consumes):
   ```json
   {"id": "adv-r3-01", "seed": 42, "description": "why this should break the executor",
    "intents": [{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 1}],
    "faults":  [{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 1.5}}],
    "prices": {"AAPL": 200.0}, "starting_cash": 100000,
    "expected_positions": null}
   ```
   `expected_positions: null` means every intent should end fully filled. Set it when the correct outcome is different, e.g. `{}` when every order is really rejected. Use `Scenario.intended_positions()` rather than recomputing it.
7. **Executor process protocol** (how the runner calls every Executor version):
   - **stdin:** JSON list of intents.
   - **env:** `BROKER_URL` (also `APCA_API_BASE_URL`, plus dummy `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`).
   - **stdout:** the **last line** is a JSON list of results. Log anything else to stderr.
   - Default command: `python -m executor.run`, run with the candidate's directory as cwd and on `PYTHONPATH`.
8. **Decision scenario** (round 2; proposed, not built yet; the Judge owner finalizes it in `arena/models.py`):
   ```json
   {"id": "d-2026-03-04", "date": "2026-03-04",
    "signals":  {"AAPL": {"signal": "bullish", "confidence": 0.7}, "MSFT": {"signal": "bearish", "confidence": 0.6}},
    "prices":   {"AAPL": 200.0, "MSFT": null},
    "holdings": {"AAPL": 10}, "cash": 5000,
    "limits":   {"max_position_pct": 0.2, "universe": ["AAPL", "MSFT", "NVDA"]},
    "conditions": ["MSFT price missing"]}
   ```
   The agent's decision code reads this and prints intents (same format as item 2). The Judge checks them against the decision invariants. Signals come from `agent/signals/`; prices, holdings and conditions are set by the scenario, so the Adversary can attack them.

---

## How the agents interact

The agents **do not chat with each other.** They take turns through the environment, like a turn-based game. Every move is structured JSON saved to disk, so runs are reproducible, replayable and hard to game.

- **Adversary's move:** a scenario (`rounds/NN/attack.json`).
- **Arena:** runs the promoted Executor against it and records what happened (`rounds/NN/trace.json`).
- **Builder's move:** a patch per candidate (`rounds/NN/candidate_a/`, `candidate_b/`).
- **Auditor:** scores both and writes `rounds/NN/verdict.json`.
- Every step is also appended to `events.jsonl` for the live view and replay.

**Optional layers on top of the same JSON moves** (only after a full plain-Python round works; drop them if not working by 3:30):
- **Fetch.ai uAgents** as the agent-to-agent channel: Adversary, Builder and Auditor are uAgents that send each other the attack / patch / verdict messages above. Same payloads, new transport; this is what qualifies for the Fetch.ai prize.
- **Arena as an MCP server** (tools like `place_order`, `submit_fault_script`, `get_results`) if a remote agent such as an Antigravity sandbox needs to act in the Arena over a tunnel (cloudflared / ngrok). Not needed for Aider, which runs locally.

---

## Arena reference

Code: `arena/` (broker, fault engine, true record, runner). Tests: `tests/test_arena.py` (15 passing). Reference v0 Executor: `examples/naive_executor.py`.

### Running it

```bash
uv sync                                            # install (fastapi, uvicorn, httpx, pydantic, pytest)
uv run pytest -q                                   # Arena tests
uv run python -m arena.run_training --executor-cmd "python examples/naive_executor.py"   # v0: 4/9 pass
uv run python -m arena.run_training                # default: python -m executor.run (exit 1 if any fail)
uv run python -m arena --scenario arena/scenarios/training/t02_accept_then_timeout.json \
       --run "python examples/naive_executor.py"   # one scenario, prints the full trace JSON
uv run python -m arena --port 8765                 # standalone server; prints its admin token on stderr
```

From Python (Auditor, orchestrator):

```python
from arena import arena_server, run_scenario
with arena_server() as arena:                      # free port, own state; one per parallel candidate
    trace = run_scenario(scenario, arena.base_url, arena.admin_token,
                         executor_dir="../cand_a", executor_cmd=["python", "-m", "executor.run"])
```

`trace` = `{scenario, executor_claims, claims_error, exit_code, timed_out, elapsed_s, stderr_tail, ledger}`.
`ledger` = `{scenario_id, starting_cash, cash, positions, orders[], fills[], pending_fills[], calls[], prices}`; each call is `{seq, t, method, path, endpoint, body, fault, status_code, order_id?}`. One true fill per execution in `fills`; the FILL activity feed the Executor sees can differ (`duplicate_fill`).

### Endpoints

| Endpoint | Who | Notes |
|---|---|---|
| `POST /v2/orders` | Executor | Body `{symbol, qty, side, type="market", time_in_force="day", client_order_id?}`. Fills immediately at the scenario price unless a fault says otherwise. Duplicate `client_order_id` → 422 `client_order_id must be unique` (real Alpaca behavior). |
| `GET /v2/orders?status=open\|closed\|all` | Executor | List, newest first. |
| `GET /v2/orders/{id}` | Executor | 404 if unknown. |
| `GET /v2/orders:by_client_order_id?client_order_id=…` | Executor | The reconciliation lookup. 404 if the broker never recorded it. |
| `DELETE /v2/orders/{id}` | Executor | 204; 422 if already filled/canceled. Canceling a `delayed_fill` order stops it from filling. |
| `GET /v2/positions`, `GET /v2/account`, `GET /v2/account/activities/FILL` | Executor | Alpaca-shaped. |
| `POST /_arena/load`, `POST /_arena/settle`, `GET /_arena/ledger` | Orchestrator / Auditor only | Need header `X-Arena-Admin: <token>`. The runner never gives the token to the Executor. `settle` = time passes after the Executor exits: every delayed fill completes. `run_scenario` calls it before reading the ledger. |
| `GET /_arena/health` | anyone | |

Errors use Alpaca's shape: `{"code": 40010001, "message": "..."}`.

### Fault types

| Fault | On | What the broker does | Params (default) |
|---|---|---|---|
| `accept_then_timeout` | POST (GET: just delays) | Records **and fills** the order, then hangs past the client timeout. | `delay_s` (2.0) |
| `accept_then_500` | POST | Records and fills, then answers 500. | |
| `transient_500` | POST, GET | 500, records nothing. Retrying is correct. | |
| `rate_limit_429` | POST, GET | 429, records nothing. Retry after a pause is correct. | |
| `real_reject` | POST | 403 insufficient buying power, records nothing. | |
| `stale_status` | GET order endpoints | Reports `status: "new"`, `filled_qty: "0"` for the next `calls` matching responses, though it filled. | `calls` (2) |
| `delayed_fill` | POST | Accepts as `new`; fills after `after_calls` more broker calls, or at `settle`. A replacement order double-fills. | `after_calls` (3) |
| `partial_fill_then_cancel` | POST | Fills `floor(qty × fill_ratio)`, cancels the rest. The intent still wants the full qty. | `fill_ratio` (0.5) |
| `duplicate_fill` | POST | Normal fill, but it appears twice in the FILL activity feed. The true ledger has one. | |

Rules: the order is committed to the true record **before** a fault fires. When several rules match one call, the first listed wins (all of them still count the call). Same `seed` → same order IDs.

### Training scenarios (`arena/scenarios/training/`)

| Scenario | Naive v0 |
|---|---|
| `t01_clean` | pass |
| `t02_accept_then_timeout` | **fail**: duplicate order (the demo's opening bug) |
| `t03_accept_then_500` | **fail**: duplicate order |
| `t04_transient_500` | pass (counter-case: retry is correct) |
| `t05_timeout_then_stale` | **fail**: duplicate; a lookup-based fix must also survive stale status |
| `t06_partial_fill` | **fail**: only half filled, invalid status |
| `t07_delayed_fill` | **fail**: invalid status, claims don't match broker |
| `t08_duplicate_fill_activity` | pass (traps executors that sum the activity feed) |
| `t09_real_reject` | pass (counter-case: correct answer is "rejected", position 0) |

`python -m arena.run_training` checks each against the ledger: exit code / parsable results, valid `IntentResult` per intent, final positions == `intended_positions()`, claimed positions == broker positions, no orders left open. **It is the Builder's training signal, not the Auditor.** The Auditor owns the full invariants and the hidden set in `auditor/`.

### For the Builder
- Copy `examples/naive_executor.py` to `executor/run.py` as v0 (keep the stdin/stdout protocol). Aider's `--test-cmd` is `python -m arena.run_training`; add `--trace-out traces/latest.json` to hand the agent full traces.
- Client timeout comes from `EXECUTOR_TIMEOUT_S` (0.5s in v0). Keep it short so scenarios run in under 2s.
- The real fix the demo expects: a deterministic `client_order_id` per intent, and on timeout/5xx, look it up with `GET /v2/orders:by_client_order_id` before re-posting. Stale status, delayed fills and partial fills then push it further.

### For the Judge
- **Auditor:** call `run_scenario` per scenario and check invariants against `trace["ledger"]` only. Use `calls[]` for "no retry while status unknown" (e.g. a second `POST /v2/orders` for an intent after a timed-out one, with no lookup in between).
- **Hidden set:** write variants in `auditor/hidden/` (gitignored). Change timing (`delay_s`, `after_calls`, `calls`), `nth`, symbols, qty and multi-intent mixes, and include counter-cases where retrying is right.
- **Adversary:** output must validate as `arena.models.Scenario`; unknown faults are rejected by Pydantic, so feed the validation error back and retry. The fault catalog to give it is the table above (`arena.models.FAULT_TYPES`, `ENDPOINT_KEYS`).

---

## Agent stack (use existing tools, build only what's ours)

| Part | Tool | Why |
|---|---|---|
| **Builder** | **Aider** (CLI, scripted) | Edits files, auto-commits each change to git, and `--test-cmd` + `--auto-test` runs the training scenarios and fixes failures on its own. Works with any model (uses LiteLLM), so it can run on sponsor models. |
| **Adversary** | **PydanticAI**, one call returning a `Scenario` | One structured move per round. PydanticAI validates the output and retries on bad JSON. |
| **Auditor** | Plain Python (deterministic) | The invariant checks must not depend on an LLM. Optionally an LLM (Adversary's model family) writes hidden-set variants ahead of time and plain-language reasons. |
| **Orchestration** | Plain Python round loop (~50 lines) | Our loop is fixed; a framework adds nothing. |
| **Memory** | Files in git (`executor/`, `executor/tools/`, `executor/LESSONS.md`) | The track wants *versioned* changes proven on held-out tests. Framework memory (vector stores, chat history) is neither. |

**Builder alternative: Google Antigravity managed agent** (`agent="antigravity-preview-09-2026"`, `agent_config={"type": "antigravity", "model": "gemini-4-argon"}`, `environment="remote"`, `background=True`). A hosted sandbox where Argon edits, runs tests and iterates. Use it if Aider + sponsor models struggle: send it `executor/`, `arena/` and the training traces, let it run `python -m arena.run_training`, take back the changed files, and re-run everything locally (never trust its "passed"). It is one agent per call, not a multi-agent environment. Preview API and calls take minutes, so keep the Aider path working as the fallback.

**Not using:** Mastra (TypeScript; we're Python), LangGraph (unneeded orchestration), CrewAI/AutoGen (built for agents chatting, which we avoid), OpenHands (too heavy), Letta (overkill).

**Builder run per candidate**, each in its own git worktree (one branch per candidate = the tournament):

```bash
git worktree add ../cand_a -b r3-cand-a v2
cd ../cand_a && aider \
  --model openai/<meta-model> --openai-api-base <meta-url> \
  --yes-always --auto-test \
  --test-cmd "python -m arena.run_training" \
  --read traces/r3.json --read executor/LESSONS.md \
  --message "These training scenarios violate invariants. Fix the executor. You may add tools under executor/tools/. Update LESSONS.md with what you learned." \
  executor/executor.py executor/tools/
```

Aider only sees the files listed, so it cannot read `auditor/` or the hidden set.

**Models (different families for independence and sponsor prizes):**
- Builder candidate A: Meta Model API. Candidate B: a second Meta prompt, or Gemini.
- Adversary + Auditor helpers: Subquadratic SubQ (or Gemini).
- Confirm early that each API is OpenAI-compatible; if not, put LiteLLM in front of it.

**Optional (Fetch.ai prize):** once the loop works, wrap the Adversary and Builder as Fetch.ai uAgents that exchange the same JSON messages.

**Arena status:** broker, all fault types, runner, training check and 9 training scenarios are done. Next for the Arena owner: help the Judge with hidden-set variants, then the optional MCP wrapper.

---

## Demo story

1. v0 retries on timeout and places a **duplicate order**. The Auditor catches it.
2. The Builder writes reconciliation using `client_order_id`, passes the hidden set, and is **promoted to v1**.
3. The Adversary attacks with a **stale order status**.
4. One candidate "fixes" it by never retrying. That passes training but fails the hidden real-rejection case, so it is **rolled back automatically**.
5. The better candidate wins the tournament and is **promoted to v2**.
6. **Round 2, the external agent:** on a held-out day the agent already holds its target in AAPL but buys the full amount again (or trades on a missing price). The Judge catches it. The Builder fixes the fork's portfolio code to size from current holdings, and the fork gets its own promoted version.
7. The improved agent's decisions run through the promoted Executor and place a small trade on the **Alpaca paper account**.

Closing line: "ai-hedge-fund is someone else's agent. Every change to it in our fork was written by TradeOnce and verified by our Judge."

## Timeline

| Time | Milestone |
|---|---|
| 12:35 | Shared interfaces agreed |
| 1:30 | Arena working; Executor v0; invariants; event log |
| 1:45 | First integration: v0 duplicate order caught by the Auditor (fallback demo) |
| 3:15 | Full loop: promote → attack → rollback → promote |
| 3:30 | Round 2 working: agent fork, cached signals, decision invariants, one promoted fix. Otherwise cut it and demo the Executor loop only |
| 4:00 | Feature freeze; Alpaca paper trade |
| 4:40 | Video recorded from replay, README done, repo public |
| 5:00 | Submitted |

## Rules

- Chaos tests run only against the simulated broker, never against real funds.
- Agents cannot write to `auditor/` or the hidden set. The Builder may only write under `executor/` and the fork's risk + portfolio files in `agent/`. Never the cached signals or the analysts.
- The external agent is third-party: keep its license file, credit it in the README, and never present it as our work.
- The repo is public and a sandboxed Builder can search the web, so **the hidden set is never committed** before judging. `auditor/hidden/` and `hidden_set/` are gitignored; generate or store it locally.
- Agents never get the Arena admin token.
- Meta credit claims close at 3:00 PM.
