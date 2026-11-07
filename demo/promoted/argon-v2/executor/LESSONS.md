# Lessons

- **Idempotent Client Order ID**: Always send `client_order_id=str(intent["intent_id"])` on `POST /v2/orders` so duplicate submissions are rejected with HTTP 422 instead of creating duplicate fills.
- **Pre-Retry Reconciliation**: After any ambiguous submission (`httpx.HTTPError`, HTTP 5xx, HTTP 429, or HTTP 422), never retry `POST /v2/orders` before querying `GET /v2/orders:by_client_order_id?client_order_id=<intent_id>`.
- **Reconciliation Decision Rule**:
  - If the lookup returns HTTP 200, adopt that broker order (and poll `GET /v2/orders/{id}` if its status is still non-terminal).
  - Only retry `POST /v2/orders` when the lookup returns HTTP 404 confirming the prior submission was not recorded.
  - Report deterministic client errors (such as HTTP 403) immediately as `status="rejected"`, and report unresolved ambiguous state after retries as `status="unknown"`.
