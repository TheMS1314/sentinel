# Incident operations

The shared monitor deduplicates unknown payment outcomes, failed jobs, old accepted work, review backlogs and Redis admission failures into durable incident records. Administrators can acknowledge, assign and resolve incidents through the Operations tab or `/ops/incidents`. Each action needs a reason and current version. Active payment/dependency conditions cannot be dismissed as resolved. Historical failed jobs and manual cases may be closed explicitly. Detection clears the underlying condition but does not silently close the case.

`/ops/metrics` is authenticated and contains bounded state labels, not request IDs, domains or human notes. Scrapers need an administrator credential; keep it in a secret manager and use HTTPS. A database outage requires independent infrastructure monitoring: an application cannot persist an alert while its database is unavailable.

Set `SENTINEL_ALERT_WEBHOOK_URL`, `SENTINEL_ALERT_ALLOWED_HOSTS` (exact hosts), and a random 32+ character `SENTINEL_ALERT_SIGNING_SECRET`. HTTPS certificate validation is required; optional `SENTINEL_ALERT_CA_BUNDLE` supports an internal CA. Redirects and environment proxy inheritance are disabled. Do not point this at an unrestricted forwarding service. A missing destination is reported `not_configured`; pending notifications are not reported delivered.

Delivery is **at least once**. The receiver must verify HMAC-SHA256 of the raw body using `X-Sentinel-Signature` and deduplicate `X-Sentinel-Event-Id`. Replay protection at the receiver must retain event IDs across restarts. No human case text is included in outbound notifications. HTTPS 2xx marks transport delivery, not human acknowledgement. Five leased attempts use bounded exponential retry; exhausted deliveries require an audited administrator retry, retaining the same event ID. A crash after receipt may redeliver, so receiver idempotency is required.

CI runs a disposable TLS receiver with a generated test certificate and verifies a real container-to-container signed delivery. This proves the integration path, not delivery to an actual on-call destination. No real notification recipient is configured by this repository.
