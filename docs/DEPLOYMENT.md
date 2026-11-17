# Production deployment path and external gates

## Runtime and migration separation

Compose now provisions a distinct `sentinel_runtime` login. It receives SELECT/INSERT on application tables, UPDATE on operational state, limited column updates on policies/decisions/payments, and DELETE only on ephemeral sessions/jobs. It cannot change original requests, original decision verdicts, payment amounts or recorded evidence, delete financial/audit history, TRUNCATE, create schema objects or administer roles. Startup rejects excessive runtime privileges. Database administrators remain trusted; privileged tampering still requires independent audit/backup controls.

`app.ops.initialize` consumes `SENTINEL_MIGRATION_DATABASE_URL` before constructing the database engine, runs migrations, performs one-time admin bootstrap and applies grants. Only the initialization job receives schema-owner access and `SENTINEL_RUNTIME_DB_PASSWORD`. Existing authority is checked before bootstrap material is parsed, so later migrations do not require retaining the initial plaintext admin key. Runtime services receive their own connection URL and reject migration/provisioning credentials in the production environment. Existing local environments need a newly generated 32+ character runtime DB password in root `.env`; never reuse the owner password. `scripts/setup_dev.py` generates separate random values for new environments.

The provisioning identity must be able to create/manage the runtime role. On managed PostgreSQL, a DBA may need to apply the grants separately. This is an operator task, not an HTTP admin function. Review schema extensions when adding migrations: new tables receive no runtime rights until explicitly added to the grants policy. Re-run initialization after migrations. Do not give the runtime user ownership or membership in a more privileged role.

## Kubernetes renderer

`deploy/kubernetes/render.py` accepts an explicit DNS hostname and backend/frontend/gateway images pinned to SHA256 digests, then renders a secret-free bundle. It does not deploy anything. It creates two replicas per serving tier, required placement on different nodes, disruption budgets retaining one replica, bounded resources, separate startup/readiness/liveness probes, graceful termination, non-root/read-only containers, disabled service-account token mounts, an API ingress NetworkPolicy, TLS Ingress and a separate one-shot migration Job. Rolling updates use zero surge and at most one unavailable replica so required node separation does not deadlock a two-node cluster; at least one replica per tier must remain ready. It requires at least two schedulable nodes; do not infer multi-zone availability from the replica count. CI validates the bundle with a real disposable Kubernetes API server; it does not prove application rollout in a production cluster.

Build/publish the two project images, choose a pinned nginx-unprivileged image for the gateway, and run:

```sh
python deploy/kubernetes/render.py --host YOUR_DOMAIN --backend-image YOUR_BACKEND_IMAGE_AT_DIGEST --frontend-image YOUR_FRONTEND_IMAGE_AT_DIGEST --gateway-image YOUR_GATEWAY_IMAGE_AT_DIGEST --output deployment.yaml
```

Each image argument must have the form `registry/path@sha256:<64 lowercase hex digits>`. Configure the model explicitly in the generated ConfigMap if using another supported model/provider. Runtime image pinning does not by itself prove reproducible builds or a verified supply chain.

Create these secrets through the deployment secret manager before rollout; no real values belong in Git:

| Secret | Purpose |
|---|---|
| `sentinel-runtime` | Restricted `SENTINEL_DATABASE_URL`, authenticated `REDIS_URL`, chosen model API credential, Razorpay **test** keys and webhook secret, optional enrichment/alert credentials |
| `sentinel-migration` | Separate migration URL, initial scoped admin bootstrap JSON on first deployment, runtime role name and password for grants; do not mount in serving pods |
| `sentinel-db-ca` | `ca.crt` mounted at `/run/sentinel-ca/ca.crt` for PostgreSQL certificate verification |
| `sentinel-tls` | Ingress TLS certificate/key for the explicit browser host |

Production startup also requires a configured HTTPS operational notification destination. It cannot prove a human recipient has acknowledged a notification; verify that separately.

Production requires PostgreSQL `sslmode=verify-full&sslrootcert=/run/sentinel-ca/ca.crt` and authenticated `rediss://` with certificate validation. Provision managed HA PostgreSQL and Redis with backups, private endpoints and tested client failover. Redis private CA options can be supplied in the URL without weakening verification. The app uses PostgreSQL for accepted jobs; Redis outage stops new HTTP admission but does not discard accepted work.

Apply the namespace and configuration first, run the migration Job to completion, then roll out serving tiers, services, policies and Ingress. An existing completed migration Job must be replaced deliberately for a new migration run. Confirm readiness, version, TLS, certificate renewal, restore ability and failover before routing real customer traffic. Migration jobs must not run concurrently with other schema-management tools. An ingress controller supporting the rendered nginx annotations, NetworkPolicy-capable CNI and external DNS are required. The runtime intentionally trusts no forwarded client-IP header by default; configure exact trusted proxy ranges only after checking the cluster's traffic path. Credential/subject quotas remain authoritative.

## Gates not proven by this repository

No real cloud account, cluster, managed HA service, domain, TLS issuer, secret manager or operational notification recipient was provided. None has been provisioned, charged or deployed. Multi-zone failure, certificate renewal, managed-database failover and independent backup retention require exercises on the chosen infrastructure. Commercial enrichment/model/test-payment contracts need valid credentials. Acquirer identity and real-money settlement remain unsupported; the payment adapter rejects live keys. These are release gates, not claims a template or mock test can establish.
