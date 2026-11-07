# TradeOnce: System Architecture

**Pitch:** TradeOnce wraps any trading agent and keeps making it safer. Every bug the red team finds becomes a code change, and it ships only if an independent judge, running a different model, can't break it on unseen tests.

**Hackathon tracks:** *The System That Gets Better* (Builder) + *Trust but Verify* (Judge).

TradeOnce does **not** predict prices or pick stocks. It is a **wrapper around an existing open-source trading agent** that improves that agent's code, and the code that places its trades, so its trades are **correct and safe**. It does not try to make the agent more profitable.

---

## Status and next steps (read this first)

*Updated Saturday 2026-11-07, about 2:30 PM. Submissions close at 5:00 PM.*

### Done and verified

| Piece | Where | Proof |
|---|---|---|
| Arena (fake, unreliable broker) | `arena/` | `uv run pytest -q`: 50 passed, 5 skipped (integration tests that need MongoDB or API keys) |
| Round loop, versioning, Stage view | `loop/`, `stage/` | `uv run python -m loop.run` promotes v0 → v3 with **scripted** candidates (hand-written stand-ins) |
| Judge's auditor wired into the loop | `loop/judge.py` → `auditor.audit_run` | called on every run |
| External agent through our layer | `agent_bridge/` | ai-hedge-fund (unmodified), naive v0: **4/9 safe**; a timeout makes it hold 50% AAPL against its own 25% limit |
| Real broker with injected faults | `arena/alpaca_proxy.py`, `arena/live_soak.py` | Alpaca **paper**, same 10 seeded faults: v0 sent **14 orders (4 duplicates)**, scripted v3 sent **10 (0)** |
| The agent alone, live | `aihf` CLI | made a real decision on Friday's session: buy AAPL on an earnings beat, capped by its risk limit |

All of this is on branch `feature/arena-loop`. **Open and merge its PR into `main` first**, so everyone builds on it: https://github.com/geeteshCh/tradeOnce/pull/new/feature/arena-loop

### The one thing still missing

A promoted Executor **written by an LLM**. Every promotion so far uses scripted candidates. That is the heart of *The System That Gets Better*, so it comes first.

### Next steps by owner

Handoff briefs (self-contained, give one to each owner or their coding agent): [`docs/handoff/BUILDER.md`](docs/handoff/BUILDER.md), [`docs/handoff/JUDGE.md`](docs/handoff/JUDGE.md).

**Builder owner** (by 3:15)
- [ ] Plug in a real Builder: `uv run python -m loop.run --builder aider` (models from `TRADEONCE_AIDER_MODELS=m1,m2`) or `--builder yourmodule:build` (signature in [Plug in your part](#plug-in-your-part-no-loop-changes-needed)). The Gemini and SubQ clients are in `providers/`.
- [ ] The Builder edits only `executor/` in its worktree and keeps the stdin/stdout protocol (see Shared interfaces item 7).
- [ ] Generated Executors must read `BROKER_URL` and never hard-code a host or keys. The proxy adds Alpaca's auth headers, so the same code runs on the Arena and on Alpaca paper.
- [ ] Use `client_order_id` = `intent_id` (or `"<intent_id>-<n>"` for follow-up orders). The Judge maps orders to intents that way.
- [ ] **Done when** one loop run promotes at least one LLM-written version that passes the hidden set. Copy the run id: `ls runs/`.

**Judge owner** (by 3:15)
- [ ] The auditor already runs every round. Next: the **Adversary** as `--adversary yourmodule:attack`, with signature `attack(round, history) -> arena.models.Scenario | None`. Validate with `Scenario.model_validate`; if it fails, feed the error back to the LLM and retry. Use only faults in `arena.models.FAULT_TYPES`.
- [ ] Your own hidden set in `auditor/hidden/` (gitignored, **never commit it**). Include counter-cases where retrying is correct (`transient_500`, `rate_limit_429`, `real_reject`). Until it exists, `python -m loop.make_hidden` writes a stand-in to `hidden_set/`.
- [ ] Run the Adversary on a different model family from the Builder (the independence story).

**Loop & Stage owner** (by 4:00)
- [ ] Merge the `feature/arena-loop` PR.
- [ ] Add `--executor-dir` to `agent_bridge/run_baseline.py`, so ai-hedge-fund can run through a promoted version in `runs/<run>/evo` (today it runs from the repo root).
- [ ] Final runs, once the Builder's version exists (commands below).
- [ ] README: the pitch, the who-wrote-what table, before/after tables, and what is scripted vs LLM-written.
- [ ] Record the 1-minute video from `uv run python -m stage.view runs/<run>/events.jsonl --replay`.

**Everyone**
- [ ] 4:00 feature freeze. 4:40 video done, repo public. 5:00 submitted with every member added.
- [ ] Say "scripted" wherever a hand-written candidate appears in the demo.

### Final checks (after the LLM Builder promotes a version)

```bash
uv run python -m loop.run --builder aider --rounds 4                        # 1. LLM-written promotion; note the run id
cd agent_bridge && uv run python run_baseline.py --executor-cmd "python -m executor.run" --label after   # 2. needs --executor-dir first (see above)
cd .. && set -a && source .env && set +a && uv run python -m arena.live_soak --n 200 --seed 7 --label final --executor-dir runs/<run>/evo   # 3. Alpaca paper
```

Expected: (1) at least one LLM-written version promoted and at least one candidate rolled back; (2) ai-hedge-fund goes from 4/9 to 9/9 safe; (3) 0 duplicates on Alpaca paper, compared with v0 on the same seed.

### Alpaca paper setup (whoever runs check 3)

1. Free account at alpaca.markets, then switch the dashboard to the **Paper** account.
2. Generate API keys **on the Paper account**.
3. Put them in `.env` at the repo root (gitignored):
   ```
   APCA_API_KEY_ID=...
   APCA_API_SECRET_KEY=...
   ```
4. Load them in the same command: `set -a && source .env && set +a && uv run python -m arena.live_soak ...`
5. Weekends: crypto (`LTC/USD`, the default) fills right away; stocks just queue. The proxy refuses live Alpaca.

### Key hygiene

- Never paste keys into chats, screenshots or the video. Keep them in `.env` or `~/.hedge-fund/.env`, both outside git.
- Any key that has appeared on screen or in a chat: rotate it after the event.

---

## What TradeOnce wraps

We do **not** build the trading agent. We use **virattt/ai-hedge-fund** (PyPI `aihf==2.7.0`, MIT), **installed unmodified**, and plug TradeOnce in through **its own `Broker` interface**. This is built: see [Agent bridge reference](#agent-bridge-reference).

**Decision (after reading its code):** its decision code (blending, risk limits, target validation, sizing from current holdings) is already careful, so we do **not** edit it. Its weak spot is execution. Its broker contract says *"place_order either fills the order COMPLETELY and returns a Fill, or raises"*, and it has no live broker ("a live broker implements the same three methods later"). Real brokers break that contract. TradeOnce supplies the missing live broker and keeps improving it.

```
 ai-hedge-fund (unmodified)                         TradeOnce
   cached analysts ─▶ blend ─▶ risk limits ─▶ sizing ─▶ ArenaBroker ─▶ Executor ─orders─▶ Arena / Alpaca paper
   (frozen signals)     its own pipeline code              (its Broker        (purple: the code       (true record)
                                                            interface)         the Builder improves)        │
                                        Judge ◀──────────────── ledger ────────────────────────────────────┘
                                          │
                              verdict ──▶ Loop ──▶ promote / roll back (git tag)
```

**What improves** (versioned; promoted or rolled back):
1. **The Executor**: the code that turns the agent's "buy 128 AAPL" into broker calls, behind the agent's `Broker` interface.
   - Fixes it should learn: `client_order_id` plus lookup before retry (no duplicates), don't trust stale status, finish partial fills, cancel or wait before replacing a late order.
   - **Measured on the real agent** (`agent_bridge/run_baseline.py`, same 9 faults as Arena training): naive v0 = **4/9 safe**. A timeout after accept makes it buy AAPL twice: **50% of the fund against the agent's own 25% limit, cash -$10,396, and the agent doesn't notice** (its books just mirror the broker). A correct Executor reaches 9/9 (checked with a throwaway reference, not committed).
2. **The Builder's own lessons and tools** (`LESSONS.md`, `executor/tools/`): it fixes the next failure faster. This is the "improves itself" part of the track.

**What never changes during a run:** the agent's code and its analysts (signals are frozen in `agent_bridge/recorded/`), the Arena, the Judge's checks, the hidden set. If the system could change its own tests, it could grade itself a pass.

**Who wrote what** (say this in the README and the pitch; the rules disqualify unclear contributions):

| Piece | Written by | Changed by |
|---|---|---|
| ai-hedge-fund (`aihf` 2.7.0): pipeline, risk limits, sizing | Its authors (third-party, MIT) | Nobody: installed unmodified |
| Cached analyst signals (`agent_bridge/recorded/signals.json`) | Us (made-up stand-ins for its LLM analysts) | Nobody during a run |
| Recorded closes (`agent_bridge/recorded/closes.json`) | Real Yahoo Finance data, fetched by our script | Nobody during a run |
| `ArenaBroker`, bridge, baseline harness (`agent_bridge/`) | **Us, during the hackathon** | Fixed |
| Arena, Executor, Builder, Judge, loop | **Us, during the hackathon** | The Executor by our Builder; the rest is fixed |

**Why the analysts are frozen:** its LLM analysts need an LLM key plus a paid Financial Datasets key, take minutes per run and give different answers each time. Cached signals make each run take seconds and give the same result every time.

**Priority:** the **Executor loop is the core demo**. The agent bridge is how we show it on a real third-party agent: run `run_baseline.py` before (v0) and after (promoted Executor) and show the two tables side by side. Editing the agent's own code is **dropped** unless the Builder finds a real decision bug.

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
*Executor + evolution engine. The Executor is the one target; ai-hedge-fund uses it through `ArenaBroker` (see [Agent bridge reference](#agent-bridge-reference)).*
- **Executor** (the code the agent rewrites): `execute(intent) -> result`, where an intent is `{intent_id, symbol, side, qty}`.
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
  - **On the real agent:** the bridge's outcome classes (`SILENT_DESYNC`, `DUPLICATE`, `LIMIT_BREACH`, `NEGATIVE_CASH`, `HALTED_DESYNC`, `HALTED_CLEAN`, `SAFE`) are the same checks, seen from the agent's side. Optional: run hidden scenarios through `run_baseline.py`-style cycles too, so promotion also requires the agent to stay safe.
  - Decision invariants on the agent's own sizing were **dropped**: its pipeline already enforces limits, cash and sizing from current holdings.
- **Adversary**: a red-team LLM that writes new fault scripts, checked against the format before use. Its score is how many invariant violations it causes against the current promoted version. Its scenarios go into training, then into regression once the Builder beats them.
- **Output:** new attacks, and a promote/reject verdict with reasons for every candidate.

### 4. The Loop & Stage (runs it and shows it)
*Orchestrator + versioning + demo*
- **One round:** attack → fail → patch → judge → promote (`git tag vN`) or roll back (`git revert`).
- Writes everything to `events.jsonl`, with a replay mode for the demo (live LLM output varies).
- A Rich live terminal view of rounds, attacks, failures, patches, verdicts and the version timeline. No Streamlit; the dashboard is not the main feature.
- The final Alpaca paper trade through the promoted Executor.
- **The external agent:** done in `agent_bridge/` (see [Agent bridge reference](#agent-bridge-reference)). For the demo, show `run_baseline.py` before (v0) and after (promoted Executor).
- README ("built during the hackathon" section, plus the who-wrote-what table), 1-minute video, pitch.

---

## How they connect

```
Judge (Adversary) ──fault scripts──▶ Arena ◀──orders── Executor ◀──orders── ai-hedge-fund (via ArenaBroker)
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
8. **Agent → Executor mapping** (`agent_bridge/arena_broker.py`): each `hedge_fund` `Order` becomes one intent, `intent_id = "<run_id>-<session>-<ticker>-<side>"` (stable, so one decision can't become two intents). `Fill` is returned only when the result is `filled` with the full quantity; anything else raises, keeping the agent's contract.

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

Code: `arena/` (broker, fault engine, true record, runner). Tests: `tests/test_arena.py`. Reference v0 Executor: `examples/naive_executor.py`.

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

## Loop & Stage reference

Code: `loop/` (round loop, git versioning, event log, Judge/Builder/Adversary bridges, local hidden-set generator), `stage/view.py` (Rich live view and replay), `demo/` (v0 seed and scripted candidates). Tests: `tests/test_loop.py`.

### Running it

```bash
uv run python -m loop.make_hidden                 # local stand-in hidden set -> hidden_set/ (gitignored)
uv run python -m loop.run                         # scripted run: 4 rounds, ~35s, ends at v3
uv run python -m stage.view                       # in a 2nd terminal: follow runs/latest live
uv run python -m stage.view runs/latest/events.jsonl --replay   # play a finished run back (record the video from this)
git -C runs/latest/evo log --oneline --decorate --graph --branches --tags   # the version history
```

Each run writes `runs/<run_id>/`: `events.jsonl`, `evo/` (its own git repo: `main` = promoted line, tags `v0..vN`, one branch per candidate, rejected branches never merged and annotated with a git note), and `rounds/NN/{attack.json, training/, traces.json, verdict.json}`.

### One round (`loop/run.py`)
1. **Adversary** returns a `Scenario`. It is run against the promoted version; if it survives, the attack joins `regression` and the round ends.
2. If it breaks: it joins `training`. The promoted version's failing training traces go to `rounds/NN/traces.json`.
3. **Builder** edits one git worktree per candidate (`r03-a`, `r03-b`).
4. **Judge** runs every candidate on `regression` + `training` + `hidden` (each in its own Arena, in parallel). Only hidden scenarios whose faults were already attacked, plus counter-case faults (`transient_500`, `rate_limit_429`, `real_reject`), are active.
5. **Decide:** eligible = passes everything; winner = most hidden passes, then fewer broker calls. Promote = merge + `git tag vN`; everyone else is rejected (rolled back).

### Plug in your part (no Loop changes needed)

| Flag | Default | Your implementation |
|---|---|---|
| `--builder` | `scripted:demo/scripted` | `aider` (models from `TRADEONCE_AIDER_MODELS=m1,m2`, extra flags from `TRADEONCE_AIDER_ARGS`) or `module:function` taking a `loop.builder.BuildContext` (`round, candidate, index, workdir, training_dir, traces_path, project_root`), editing `workdir/executor/` in place and returning a one-line summary |
| `--adversary` | `queue:arena/scenarios/training@t02…,t05…,t07…,t06…` | `module:function(round, history) -> Scenario or None`; `history` = `[{round, scenario_id, faults, broke_promoted}]` |
| `--judge` | `auditor.audit_run` per intent + the Arena's completion checks | `module:function(trace) -> [failure dicts with "code", "message"]` |
| `--hidden` | `auditor/hidden/` if it exists, else `hidden_set/` | the Judge team's hidden set |
| `--seed` | `demo/seed` (naive v0) | a directory containing `executor/` |

**Judge bridge** (`loop/judge.py`): `ledger_to_snapshot(trace, intent)` converts the Arena ledger to `audit_run`'s snapshot shape. Orders map to intents by `client_order_id` (`== intent_id` or `"<intent_id>-<n>"`), falling back to a unique (symbol, side) match. `submit_unknown` / `retry` / `reconciled` events are derived from the broker call log; re-sending the same `client_order_id` counts as reconciled (the broker refuses duplicates).

**Scripted candidates are a stand-in**, used to test the loop and as a deterministic fallback. They are hand-written, so if the demo uses them, say so. The real demo should run `--builder aider` (or the Builder team's function) and be recorded from replay.

### Scripted run (what the default produces)

| Round | Attack | Result |
|---|---|---|
| 1 | `t02_accept_then_timeout` | v0 breaks (`OVERFILL`, `UNSAFE_RETRY`). r01-a (client_order_id + lookup) → **v1**. r01-b (3s timeout) rejected: fails hidden `h01_timeout_long` |
| 2 | `t05_timeout_then_stale` | v1 breaks (`COMPLETION`). r02-a (re-check 2 times) passes all training but fails hidden `h08_timeout_stale_long` → **rolled back**. r02-b (poll to a final state) → **v2** |
| 3 | `t07_delayed_fill` | v2 survives; added to regression |
| 4 | `t06_partial_fill` | v2 breaks. r04-a (follow-up order for the remainder) → **v3**. r04-b (re-send full order) rejected for `OVERFILL` |

### Not built yet (Loop & Stage owner)
- Alpaca paper orders through the promoted Executor: **done** via `arena/live_soak.py` (see [Live soak](#live-soak-on-alpaca-paper-arenaalpaca_proxypy-arenalive_soakpy)). Still open: ai-hedge-fund's own orders to Alpaca paper (point `ArenaBroker` at the proxy URL; the proxy adds the auth headers).
- Optional: show `agent_bridge/run_baseline.py` before/after tables in the Stage view.

---

## Agent bridge reference

Code: `agent_bridge/`. Runs **virattt/ai-hedge-fund** (`aihf==2.7.0`, MIT, installed unmodified) through the Arena, using the agent's own interfaces.

| File | What it is |
|---|---|
| `pyproject.toml` | **Separate environment**: `aihf` pins `rich<15`, the main project uses `rich>=15`. Imports `arena` from the repo root. |
| `recorded/closes.json` | **Real** daily closes from Yahoo Finance (free, no key), 2024-06-03 to 06-07, for AAPL, AMZN, MSFT, NVDA and SPY. Fetched once by `fetch_prices.py` and frozen. |
| `fetch_prices.py` | Re-fetches `closes.json` via yfinance (`--start --end --tickers`). Not needed for normal runs. |
| `agent_setup.py` | The one fund configuration both runs share (analysts, 25% limit per name, $100k). |
| `run_alone.py` | **The agent by itself**: its pipeline plus its own `SimBroker` (never fails). No Arena, no Executor. Writes `reports/alone.json`. |
| `recorded/signals.json` | Cached analyst convictions per day (`cached_value`, `cached_momentum`), standing in for its LLM analysts. |
| `recorded_data.py` | `RecordedDataClient` (its `DataClient` interface) and `CachedAnalyst` (its analyst interface). |
| `arena_broker.py` | `ArenaBroker`: its 3-method `Broker` protocol (`positions`, `cash`, `place_order`) backed by **our Executor**, called the same way the Arena runner calls it (stdin/stdout). |
| `run_baseline.py` | One assess (06-03) + execute (06-04) cycle of its real pipeline per fault set from `arena/scenarios/training/`. Writes `reports/<label>.json`. |

```bash
cd agent_bridge && uv sync                                          # once
uv run python run_alone.py                                          # the agent alone, perfect broker
uv run python run_baseline.py                                       # before: naive v0 Executor
uv run python run_baseline.py --executor-cmd "python -m executor.run" --label after   # after: promoted Executor
```

**Everything is free:** real prices come from Yahoo (no key), and the analysts are cached. Running the agent's own LLM analysts is not free: they read company fundamentals from Financial Datasets (paid key). It does support a free Gemini model (`GOOGLE_API_KEY`, `gemini-3.8-flash`), but the fundamentals are still paid.

Alone (`run_alone.py`), the agent decides 4 orders on the cached signals: buy 128 AAPL, 60 AMZN, 60 MSFT, 214 NVDA. It ends at about 25% / 11% / 25% / 25% of NAV, all within its 25% limit. Baseline with v0 through the Arena (`reports/baseline.json`):

| Scenario | Outcome (v0) |
|---|---|
| clean, transient 500, duplicate-fill activity, real reject | SAFE (4/9; for real reject, halting with nothing traded is correct) |
| accept-then-timeout, accept-then-500, timeout-then-stale | DUPLICATE + LIMIT_BREACH + NEGATIVE_CASH: 5 orders at the broker for 4 decided, AAPL 50%, cash -$10,396, agent's books "match" because they mirror the broker |
| partial fill, delayed fill | HALTED_DESYNC: the agent raised after the first order, but that order (half or fully) filled at the broker without the agent knowing |

**Outcome classes:** `SILENT_DESYNC` (agent finished but its books differ from the broker), `DUPLICATE`, `LIMIT_BREACH` (>25% in one name), `NEGATIVE_CASH`, `HALTED_DESYNC` (raised, but fills it doesn't know about exist), `HALTED_CLEAN` (raised, nothing unknown happened), `SAFE`.

**Point it at Alpaca paper:** `ArenaBroker(base_url="https://paper-api.alpaca.markets", ...)` with real `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` in the environment. Only with the promoted Executor, and only for the final demo trade.

---

## Live soak on Alpaca paper (`arena/alpaca_proxy.py`, `arena/live_soak.py`)

Real brokers don't fail on demand, so a **fault proxy** sits in front of Alpaca **paper**: every order really reaches Alpaca, then the proxy injects the fault on the way back (accept-then-timeout, accept-then-500, timeout-then-stale-status, transient 500, 429). The proxy adds Alpaca's auth headers itself, so Executors need no changes.

- **Paper only.** The proxy refuses any upstream except `paper-api.alpaca.markets` or localhost; `api.alpaca.markets` (live) is always refused.
- **Weekends:** use crypto (`--symbols LTC/USD`, the default). Alpaca paper trades crypto 24/7 and accepts only `gtc`/`ioc`, so the proxy rewrites `day` to `gtc` for symbols with a `/`. Stock orders placed while the market is closed just queue.
- Paced to `--rpm 150` (Alpaca allows about 200 requests/minute). Orders left open are canceled at the end.
- Counts from the broker side: orders that reached the broker per intent. A correct Executor places exactly 1.

```bash
export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...            # Alpaca PAPER keys, your own shell only
uv run python -m arena.live_soak --n 200 --seed 7 --label v0 --executor-cmd "python examples/naive_executor.py"
uv run python -m arena.live_soak --n 200 --seed 7 --label v2 --executor-dir runs/<run>/evo   # same seed = same faults
# dry run without Alpaca: --upstream http://127.0.0.1:<arena port>
```

Local check through the proxy (Arena upstream, 40 trades, seed 7): naive v0 put **53 orders at the broker for 40 trades (13 duplicates)**; the round-4 scripted Executor put **40 (0 duplicates)**. Reports go to `runs/soak/` (gitignored).

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
6. **On a real third-party agent:** ai-hedge-fund, unmodified, runs through the same 9 faults. Before (v0): 4/9 safe, and a timeout makes it put **50% of the fund in AAPL against its own 25% limit, with cash at -$10,396, without noticing**. After (promoted Executor): same agent, same faults, all safe.
7. The same agent places a small trade on the **Alpaca paper account** through the promoted Executor.

Closing line: "ai-hedge-fund is someone else's agent. We didn't touch its code. TradeOnce gave it the live broker it was missing, and made that broker safe on its own."

## Timeline

| Time | Milestone |
|---|---|
| 12:35 | Shared interfaces agreed |
| 1:30 | Arena working; Executor v0; invariants; event log |
| 1:45 | First integration: v0 duplicate order caught by the Auditor (fallback demo) |
| 3:15 | Full loop: promote → attack → rollback → promote |
| 3:30 | Agent bridge "after" run with the promoted Executor (bridge itself is done) |
| 4:00 | Feature freeze; Alpaca paper trade |
| 4:40 | Video recorded from replay, README done, repo public |
| 5:00 | Submitted |

## Rules

- Chaos tests run only against the simulated broker, never against real funds.
- Agents cannot write to `auditor/` or the hidden set. The Builder may only write under `executor/`. Never `agent_bridge/` (including the recorded data) or the installed agent.
- The external agent is third-party (ai-hedge-fund, MIT): credit it in the README and never present it as our work.
- The repo is public and a sandboxed Builder can search the web, so **the hidden set is never committed** before judging. `auditor/hidden/` and `hidden_set/` are gitignored; generate or store it locally.
- Agents never get the Arena admin token.
- Meta credit claims close at 3:00 PM.
