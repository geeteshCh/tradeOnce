# Lessons

- r1: A timeout does not mean the order failed. Send client_order_id = intent_id and look it up before any retry.
- r2: A "new" status can be stale. Poll until the order is final, bounded by a deadline, not a guessed count.
- r4: A canceled remainder is unfilled quantity. Place a follow-up order for the remainder only ("<intent_id>-<n>").
