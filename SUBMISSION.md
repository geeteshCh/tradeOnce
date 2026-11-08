# TradeOnce hackathon submission

- **Team name:** TradeOnce
- **Project name:** TradeOnce
- **Track:** The System That Gets Better
- **Project Description length:** 2,398 / 3,000 characters
- **Public GitHub repository:** https://github.com/geeteshCh/tradeOnce
- **One-minute YouTube demo:** TODO — add the YouTube video URL before submitting.
- **Submission form:** https://cerebralvalley.ai/e/aie-code-2026-hackathon/hackathon/submit

## Project Description

Your trading agent decides to buy once. A lost broker reply makes it buy twice. TradeOnce finds and repairs that execution code, independently verifies each change, and carries approved lessons into the next repair.

Our demo starts with a concrete failure: buy 1 share, receive 2. Gemini 4 Argon writes a real Python repair. Google Research Harness² refines the Builder's instructions through a sequential improvement step. Each candidate runs in a sandbox against an unreliable broker simulator, with normal trades, lost replies, temporary errors and genuine rejections. A fixed, independent auditor checks the broker's actual records, including private cases the Builder cannot see or edit. A repair must pass training, regression and held-out checks before it is kept.

Accepted code is versioned in Git. Approved Builder instructions, lessons and verification provenance are persisted in MongoDB. Rejected candidates leave the approved memory intact. On a later, related broker failure, we reset to the original broken code and reload the saved Builder guidance. In our recorded live run, the first repair required 3 code proposals and 147 seconds; the related repair required 1 proposal and 36 seconds. These are measured results on different tasks, not a guaranteed speedup.

We also connected the repaired Executor to the existing open-source ai-hedge-fund agent, leaving its pipeline unchanged. In six matched scenarios against Alpaca's paper broker, the original Executor produced 3 duplicate orders; the Gemini-written version produced none and completed all six scenarios safely by our checks. This uses fake money, real broker API calls, injected faults and cached stand-in analyst signals. Partial fills remain a known gap.

During the event, we built the broker simulator and fault injection, sandboxed coding workflow, independent verification gate, versioned memory integration, external-agent adapter and an expandable terminal presentation. We build on existing Gemini, Harness², MongoDB and ai-hedge-fund components. The terminal shows the mistake, repair, outcome, saved lesson and next problem; finished steps fold away so the audience can follow the story.

TradeOnce demonstrates a self-improving coding system whose progress is earned through execution and independent evidence. The useful output is both a working repair and a better-informed Builder for the next failure.

## Evidence for the submission

The Project Description above is the text to paste into the form. The notes below are supporting material outside that field.

- [Recorded live coding run](demo/recorded/argon-coding.json): actual proposal counts, timings, code diffs, decisions and memory revisions.
- [Gemini-written Executor provenance](demo/promoted/argon-v2/PROVENANCE.md): origin of the repaired code and its known partial-fill limitation.
- [Alpaca paper comparison](agent_bridge/reports/side_by_side.json): original versus Gemini-written Executor under the matched scenarios.
- [Demo evidence guide](docs/demo-evidence.md): boundaries between verified behavior, external reports and proposed scenes.

For the one-minute video, show only work built during the event, credit existing components, and label recorded runs, paper trading, injected faults and stand-in signals when they appear. Replace the YouTube placeholder before submitting.
