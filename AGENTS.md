# TradeOnce: System Architecture

**Pitch:** The agent that learns not to lose your money. Every bug the red team finds becomes a versioned fix, and it ships only if an independent auditor, running a different model, can't break it.

**Hackathon tracks:** *The System That Gets Better* (Builder) + *Trust but Verify* (Judge).

TradeOnce does **not** predict prices. It is a coding system that builds and hardens trade-execution code against an unreliable broker.

---

## The 4 parts (one owner each)

### 1. The Arena (the world)
*Simulated broker + fault engine*
- A fake broker with Alpaca's API shape (`POST /v2/orders`, `GET /v2/orders/{id}`, `GET /v2/orders:by_client_order_id`, `GET /v2/positions`, `GET /v2/account`).
- Holds the **true record** of orders, fills and cash. The Judge reads this; it never trusts the Builder's claims.
- Injects faults from JSON fault scripts, seeded so runs are reproducible.
  - Example: `{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout"}`
  - Fault types: `accept_then_timeout`, `duplicate_fill`, `partial_fill_then_cancel`, `stale_status`, `late_webhook`, `real_reject`
- **Output:** a place where trades can go wrong in ways you can reproduce.
- **Critical path:** working by 1:30.

### 2. The Builder (gets better)
*Executor + evolution engine*
- **Executor** (the code the agent rewrites): `execute(intent) -> result`, where an intent is `{intent_id, symbol, side, qty}`.
  - `tools/` holds agent-created tools (e.g. `reconcile_order.py`), loaded through a registry.
  - v0 is deliberately naive: it retries on timeout, which causes duplicate orders.
  - The same code runs against the simulator and against the Alpaca paper account; only the base URL changes.
- **Evolution engine**: takes failure traces from the training set and produces a candidate patch (an edited file or a new tool) on its own git branch.
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
- **Adversary**: a red-team LLM that writes new fault scripts, checked against the format before use. Its score is how many invariant violations it causes against the current promoted version. Its scenarios go into training, then into regression once the Builder beats them.
- **Output:** new attacks, and a promote/reject verdict with reasons for every candidate.

### 4. The Loop & Stage (runs it and shows it)
*Orchestrator + versioning + demo*
- **One round:** attack → fail → patch → judge → promote (`git tag vN`) or roll back (`git revert`).
- Writes everything to `events.jsonl`, with a replay mode for the demo (live LLM output varies).
- A Rich live terminal view of rounds, attacks, failures, patches, verdicts and the version timeline. No Streamlit; the dashboard is not the main feature.
- The final Alpaca paper trade through the promoted Executor.
- README ("built during the hackathon" section), 1-minute video, pitch.

---

## How they connect

```
Judge (Adversary) ──fault scripts──▶ Arena ◀──orders── Builder
                                       │                  ▲
                                 true records        failure traces
                                       ▼                  │
                    Judge (Auditor) ──verdict──▶ Loop & Stage ──▶ promote / rollback
```

Until the Arena is ready, the Builder and the Judge code against an in-memory fake broker class with the same methods.

---

## Shared interfaces (agree on these first)

1. **Broker API subset:** endpoints and JSON fields, copied from Alpaca.
2. **Intent / result** for `execute()`: `{intent_id, symbol, side, qty}` → `{intent_id, status, broker_order_id, filled_qty}`
3. **Fault script:** `{match, nth, fault, params?}`
4. **Event:** `{ts, round, actor, type, data}`
5. **Verdict:** `{candidate, decision: "promote" | "reject", invariant_failures[], hidden_pass_rate, reasons}`

---

## Demo story

1. v0 retries on timeout and places a **duplicate order**. The Auditor catches it.
2. The Builder writes reconciliation using `client_order_id`, passes the hidden set, and is **promoted to v1**.
3. The Adversary attacks with a **stale order status**.
4. One candidate "fixes" it by never retrying. That passes training but fails the hidden real-rejection case, so it is **rolled back automatically**.
5. The better candidate wins the tournament and is **promoted to v2**.
6. The promoted Executor places a small trade on the **Alpaca paper account**.

## Timeline

| Time | Milestone |
|---|---|
| 12:35 | Shared interfaces agreed |
| 1:30 | Arena working; Executor v0; invariants; event log |
| 1:45 | First integration: v0 duplicate order caught by the Auditor (fallback demo) |
| 3:15 | Full loop: promote → attack → rollback → promote |
| 4:00 | Feature freeze; Alpaca paper trade |
| 4:40 | Video recorded from replay, README done, repo public |
| 5:00 | Submitted |

## Rules

- Chaos tests run only against the simulated broker, never against real funds.
- Agents cannot write to `auditor/` or the hidden set.
- Meta credit claims close at 3:00 PM.
