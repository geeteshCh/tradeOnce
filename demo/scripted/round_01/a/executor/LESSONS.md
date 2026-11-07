# Lessons

- r1: A timeout does not mean the order failed. The broker may have filled it. Send client_order_id = intent_id and
  look it up (GET /v2/orders:by_client_order_id) before any retry. Re-sending the same client_order_id is safe: the
  broker refuses duplicates.
