# Builder brief (TradeOnce)

Hand this file to the Builder owner, or to their coding agent. It is self-contained. The full architecture is in `AGENTS.md`.

**Deadline: a promoted, LLM-written Executor by 3:15 PM.** Feature freeze 4:00, submission 5:00.

---

## 1. The project in four lines

- TradeOnce wraps an **external trading agent** (virattt/ai-hedge-fund, unmodified) with a **software layer** that places its trades at a broker.
- The layer's core is the **Executor**: code that turns "buy 128 AAPL" into broker calls. It must place each trade **exactly once**, even when the broker times out after accepting, returns errors, or reports a stale status.
- **Your job: the Builder.** An LLM that rewrites the Executor after each failure. That is what makes the system "improve itself" (*The System That Gets Better* track).
- An independent **Judge** (another team member, a different model family) decides whether your rewrite is promoted or rolled back. You never see its hidden tests.

## 2. What already exists (don't rebuild it)

| Piece | Where |
|---|---|
| Fake, unreliable broker with Alpaca's API (the Arena) | `arena/` |
| Round loop: attack → your candidates → Judge → promote (`git tag vN`) or roll back | `loop/run.py` |
| Your plug-in point | `loop/builder.py` |
| v0 Executor (deliberately naive; places duplicates) | `demo/seed/executor/run.py` |
| Scripted candidates (hand-written stand-ins, **not** LLM output) | `demo/scripted/` |
| Training check, usable as a test command | `python -m arena.run_training` |
| Gemini and SubQ clients | `providers/gemini.py`, `providers/subq.py` |

Setup: `uv sync`, then `uv run pytest -q` (expect 50 passed, 5 skipped).

## 3. The one thing missing

**Every promotion so far uses scripted candidates.** The demo needs at least one promotion where **an LLM wrote the Executor**. That is your deliverable.

## 4. How you plug in (no loop changes needed)

**Option A, Aider (already wired, fastest):**

```bash
uv tool install aider-chat                                   # Aider isn't installed yet
export GEMINI_API_KEY=...                                    # in your own shell; never commit or paste it
export TRADEONCE_AIDER_MODELS=gemini/gemini-4-argon,gemini/gemini-3.8-flash   # one model per candidate (a, b)
uv run python -m loop.make_hidden                            # stand-in hidden set, until the Judge's exists
uv run python -m loop.run --builder aider --rounds 4
```

For each candidate, the loop runs Aider in that candidate's git worktree with:
- `--auto-test --test-cmd "python -m arena.run_training --executor-dir . --scenarios <this round's training dir>"`, so Aider keeps fixing until training passes;
- `--read <failure traces>` and the prompt `BUILDER_PROMPT` in `loop/builder.py`;
- only the files under `executor/` (plus `executor/LESSONS.md`) open for editing.

Logs land at `runs/<run>/cands/<candidate>.aider.log`. Check the model names against Aider's docs: Aider uses LiteLLM model strings, and Argon may need a different prefix. If Argon doesn't work through Aider, use Flash for both candidates, or Option B.

**Option B, your own function:**

```bash
uv run python -m loop.run --builder yourpkg.builder:build
```

```python
from loop.builder import BuildContext   # round, candidate, index, workdir, training_dir, traces_path, project_root

def build(ctx: BuildContext) -> str:
    """Edit ctx.workdir / "executor" in place. Return a one-line summary for the event log."""
    traces = ctx.traces_path.read_text()          # failing training traces: claims vs the broker's true ledger
    code = (ctx.workdir / "executor" / "run.py").read_text()
    # ... ask the LLM for a new run.py (and optional tools under executor/tools/), write the files,
    # ... then run the training check and iterate a few times:
    #     python -m arena.run_training --executor-dir <workdir> --scenarios <training_dir>   (exit 0 = all pass)
    return "gemini: client_order_id + lookup before retry"
```

`providers.GeminiClient.from_env()` reads `GEMINI_API_KEY` and `GEMINI_MODEL` (default `gemini-4-argon`) and offers `generate_text(prompt, system_instruction=...)`. `ctx.index` is 0 for candidate `a` and 1 for `b`: give the two candidates different prompts or models. That's the tournament.

## 5. Rules your generated Executor must follow

1. **Protocol:** read a JSON list of intents `[{intent_id, symbol, side, qty}]` from **stdin**. Print a JSON list of results on the **last stdout line**: `[{intent_id, status, broker_order_id, filled_qty}]`. Log anything else to stderr.
2. `status` must be one of `filled | partially_filled | rejected | unknown | failed`. **Never pass the broker's own status through** (`new` and `canceled` are invalid here).
3. Talk to the broker at **`BROKER_URL`** from the environment. Never hard-code a host, never read or send API keys: the proxy adds Alpaca's auth, so the same code runs on the Arena and on Alpaca paper.
4. Use **`client_order_id = intent_id`**, or `"<intent_id>-<n>"` for follow-up orders such as the rest of a partial fill. The Judge maps orders to intents this way.
5. **Don't special-case** scenario IDs, symbols or timings. The hidden set uses different timings, symbols, sizes and multi-intent mixes.
6. Write **only under `executor/`** (`run.py`, `tools/*.py`, `LESSONS.md`). Never touch `auditor/`, `arena/`, `loop/` or the hidden set.
7. Keep scenarios fast: use a short client timeout (`EXECUTOR_TIMEOUT_S`, default 0.5s).

## 6. What a correct Executor has to handle (the Arena's faults)

| Fault | What the broker does | The correct move |
|---|---|---|
| `accept_then_timeout` | Fills the order, then the reply times out | Look up `GET /v2/orders:by_client_order_id` before retrying. Never blindly re-post. |
| `accept_then_500` | Fills, then answers 500 | Same: look it up first |
| `transient_500`, `rate_limit_429` | Records nothing | Retrying is correct (after a short pause for 429) |
| `real_reject` (403) | Records nothing | Report `rejected`. Don't retry forever. |
| `stale_status` | Lookups say `new` / `filled_qty 0` for a few calls | Poll until a final state. Don't treat "new" as "failed". |
| `delayed_fill` | Accepts as `new`, fills a few calls later | Wait or poll. Never place a replacement while the original can still fill. |
| `partial_fill_then_cancel` | Fills part, cancels the rest | Place a follow-up for the remainder (`"<intent_id>-1"`) |
| `duplicate_fill` | The fill appears twice in the activity feed | Trust the order's `filled_qty`, not the activity feed |

Reusing a `client_order_id` returns 422 "must be unique", as at real Alpaca. Treat it as "it already exists: look it up".

## 7. Done when

- [ ] `uv run python -m loop.run --builder aider` (or your function) **promotes at least one version written by the LLM** (check `runs/<run>/events.jsonl` and `git -C runs/<run>/evo log --oneline --decorate --graph --branches --tags`).
- [ ] Ideally at least one candidate is **rejected** by the Judge (a rollback for the demo).
- [ ] Note the run id and tell the Loop & Stage owner. They will run the agent and Alpaca checks against `runs/<run>/evo`.

## 8. Useful commands

```bash
uv run python -m arena.run_training --executor-cmd "python demo/seed/executor/run.py"   # v0: 4/9 training pass
uv run python -m loop.run                                                                # scripted reference run (~35s, ends at v3)
uv run python -m stage.view                                                              # live view of runs/latest
```

## 9. Don'ts

- Don't commit keys. `.env` is gitignored, so keep them there or in your shell.
- Don't commit `hidden_set/` or `auditor/hidden/`.
- Don't call a scripted candidate "LLM-written" in the demo.
