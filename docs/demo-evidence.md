# Why this trading demo matters

**The problem:** a broker can execute a trade even when the trading bot receives an error. Sending a fresh order can then buy or sell twice.

This guide is the source for the demo's external evidence and claim boundaries. Sources were checked on **7 November 2026**. Keep the demo in the stock/trading domain.

## What the sources establish

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| [Alpaca staff response, 5 March 2026](https://forum.alpaca.markets/t/open-orders-failing-to-close-on-paper-account/18533) | An order submission can succeed despite an error response; a fresh submission can duplicate it. A stable client order ID lets the broker reject a duplicate. | A measured failure rate or a specific customer losing $500. The staff member calls the situation infrequent. |
| [Customer report and Alpaca response, 26 May 2021](https://forum.alpaca.markets/t/replace-order-creates-a-new-order-successfully-but-the-old-order-is-filled-later-thus-creating-duplicate-order/5794) | A customer reported that an apparently unfilled order later filled, leading to repeated selling and an unintended short position. Alpaca staff said it appeared related to delayed fill updates. | Verified live-money loss, independent confirmation of every customer claim, or evidence that the historical issue remains present. The report is explicitly about paper trading. |
| [Alpaca order lifecycle](https://docs.alpaca.markets/us/docs/orders-at-alpaca#order-lifecycle) | Orders can be partially filled, canceled, or waiting for cancellation. Receiving an order acknowledgment is different from receiving the requested shares. | Our particular 10-share example or proof that our current Executor handles every state. |

The most direct statement is from Alpaca's Dan Whitnable in the March 2026 response:

> if submitting an order, there is a chance that the initial request succeeded, and you will end up submitting a duplicate order.

The broker also provides [lookup by client order ID](https://docs.alpaca.markets/us/reference/getorderbyclientorderid). Use the broker's actual identity, lookup, and state rules when implementing recovery; a prompt alone cannot prevent duplicate side effects.

## A story anyone can follow

**Proposed mock: “You approved $500. The bot spent $1,000.”**

Use a fictional stock at a fixed $50 per share and an account with $2,000 cash. The customer authorizes only 10 shares ($500). The simulated broker fills the order, but its confirmation disappears. The naive bot sends a fresh order and buys another 10. The extra order breaches the customer's instruction even though the account has enough buying power.

These dollar amounts are invented demonstration inputs, not figures from either external report. The doubled purchase shows unauthorized spending; it is not a claim that $500 of market value vanished.

| Beat | What the audience sees | What must really happen |
|---|---|---|
| Instruction | “Buy 10 shares. Approved spend: $500.” | One stable trading intent enters the Executor. |
| Failure | “Bought 20. Spent $1,000.” | The Arena ledger records two fills after a lost confirmation and unsafe retry. |
| Repair | “Bought the requested 10, once.” | Harness² refines Builder guidance; Gemini writes a real patch; the independent Judge checks actual broker records. |
| Transfer | “The next failure uses the saved lesson.” | A later task reloads approved MongoDB memory and passes its own independent checks. Report actual attempts. |
| Next challenge | “Only 4 of 10 shares arrived. Can it finish the remaining 6?” | A newly generated partial-fill repair must pass the Judge and retain earlier protections before being presented as solved. |

Show requested shares, actual shares, approved spend, and actual spend. Keep raw traces and test counts in expandable details. Label the broker as simulated, the price as fixed, and failures as injected. Label recorded execution as replay.

## What already works, and what is proposed

The [recorded Argon run](../demo/recorded/argon-coding.json) contains a timeout repair and a related accepted-then-500 repair: **3 proposals, then 1**, with **5/5 hidden cases** on each. These are measured results on different tasks, not a guaranteed speedup. Replay instructions are in the [README](../readme.md#real-coding-demo-one-timeout-two-orders-one-lesson).

The saved [Argon v2 provenance](../demo/promoted/argon-v2/PROVENANCE.md) records **8/9 Arena training cases**, with partial fills still failing. The $500 headline fixture and the 10/4/6 partial-fill extension above are proposed scenes, not newly implemented or verified features. Do not describe the recorded run as using these new numbers.

## Where teammates should build

| Task | Existing seam |
|---|---|
| Reproduce lost confirmation | [Accepted-then-timeout fixture](../arena/scenarios/training/t02_accept_then_timeout.json) and [accepted-then-500 fixture](../arena/scenarios/training/t03_accept_then_500.json) |
| Reject a fake “never retry” fix | [Transient-500 counter-case](../arena/scenarios/training/t04_transient_500.json): the first request records nothing, so a safe retry is necessary |
| Add the partial-fill learning round | [Partial-fill-then-cancel fixture](../arena/scenarios/training/t06_partial_fill.json); use measured filled quantity and preserve prior regressions |
| Generate and evaluate real repairs | [Builder workflow](../builder/workflow.py), [evidence](../builder/evidence.py), [Harness² integration](../harnessing/runner.py), and existing MongoDB storage |
| Present the results | [Terminal view](../builder/terminal_view.py); derive outcomes from verified events or reports |

The Judge reads broker ledger truth and owns hidden fixtures. Model-written claims, external anecdotes, and mock headlines do not authorize promotion. A partial-fill fix is complete only when the new case, existing regressions, and held-out checks pass with retained code and verification provenance.

## Safe wording for the pitch

“A broker may execute your trade even when its confirmation is lost. A bot that blindly retries can trade twice. TradeOnce repairs that execution code, checks the result independently, and reuses approved lessons on later failures.”

Describe the 2021 link as a **historical paper-trading customer report**, the 2026 link as **broker staff guidance**, and our numbers as **mock inputs**. Those distinctions should remain visible in slides, narration, README claims, and agent-generated documentation.
