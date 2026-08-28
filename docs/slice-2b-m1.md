# Slice 2B-M1: independent evidence-comment delivery

`CommentDeliveryWorker.deliver_one()` claims and delivers one persisted
`evidence_comment_outbox` row. Claiming and each finalization are committed
SQLite transactions; the injected adapter call occurs between those
transactions and never while a transaction is open.

Retries use deterministic exponential backoff:
`min(max_retry_delay, base_retry_delay * 2 ** (attempt_count - 1))`.
Attempts are counted on claim, and the configured maximum is terminal.
Adapter errors are redacted and bounded before persistence.

This slice intentionally does not solve exactly-once remote delivery after an
ambiguous external result. That is the explicit Slice 2B-M2 boundary.
