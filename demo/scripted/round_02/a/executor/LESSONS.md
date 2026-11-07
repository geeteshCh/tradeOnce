# Lessons

- r1: A timeout does not mean the order failed. Send client_order_id = intent_id and look it up before any retry.
- r2: Check the order up to 2 more times after a timeout.
