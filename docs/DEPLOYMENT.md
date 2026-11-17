# Production deployment path and external gates

## Runtime and migration separation

Compose now provisions a distinct `sentinel_runtime` login. It receives SELECT/INSERT on application tables, UPDATE on operational state, limited column updates on policies/decisions/payments, and DELETE only on ephemeral sessions/jobs. It cannot change original requests, original decision verdicts, payment amounts or recorded evidence, delete financial/audit history, TRUNCATE, create schema objects or administer roles. Startup rejects excessive runtime privileges. Database administrators remain trusted; privileged tampering still requires independent audit/backup controls.

`app.ops.initialize` consumes `SENTINEL_MIGRATION_DATABASE_URL` before constructing the database engine, runs migrations, performs one-time admin bootstrap and applies grants. Only the initialization job receives schema-owner access and `SENTINEL_RUNTIME_DB_PASSWORD`. Existing authority is checked before bootstrap material is parsed, so later migrations do not require retaining the initial plaintext admin key. Runtime services receive their own connection URL and reject migration/provisioning credentials in the production environment. Existing local environments need a newly generated 32+ character runtime DB password in root `.env`; never reuse the owner password. `scripts/setup_dev.py` generates separate random values for new environments.

The provisioning identity must be able to create/manage the runtime role. On managed PostgreSQL, a DBA may need to apply the grants separately. This is an operator task, not an HTTP admin function. Review schema extensions when adding migrations: new tables receive no runtime rights until explicitly added to the grants policy. Re-run initialization after migrations. Do not give the runtime user ownership or membership in a more privileged role.

## Kubernetes renderer

`deploy/kubernetes/render.py` accepts an explicit DNS hostname and backend/frontend/gateway images pinned to SHA256 digests, then renders a secret-free bundle. It does not deploy anything. Gateway API replaces the former community ingress-nginx dependency; no Ingress object or controller-specific ingress annotation is rendered. It creates two replicas per serving tier, required placement on different nodes, disruption budgets retaining one replica, bounded resources, separate startup/readiness/liveness probes (gateway dependency probes use an internal, unexposed port), graceful termination, non-root/read-only containers, disabled service-account token mounts, an API ingress NetworkPolicy, TLS Gateway API routing and a separate one-shot migration Job. Rolling updates use zero surge and at most one unavailable replica so required node separation does not deadlock a two-node cluster; at least one replica per tier must remain ready. Public `/health` on the rendered gateway is a cheap gateway check; database-dependent health paths are blocked at public ingress and gateway readiness uses private port 8081. The base local Compose health contract is unchanged. It requires at least two schedulable nodes; do not infer multi-zone availability from the replica count. CI validates the bundle with a real disposable Kubernetes API server; it does not prove application rollout in a production cluster.

Build/publish the two project images, choose a pinned nginx-unprivileged image for the gateway, and run:

```sh
python deploy/kubernetes/render.py --host YOUR_DOMAIN --backend-image YOUR_BACKEND_IMAGE_AT_DIGEST --frontend-image YOUR_FRONTEND_IMAGE_AT_DIGEST --gateway-image YOUR_GATEWAY_IMAGE_AT_DIGEST --gateway-class sentinel --output deployment.yaml
```

Each image argument must have the form `registry/path@sha256:<64 lowercase hex digits>`. Configure the model explicitly in the generated ConfigMap if using another supported model/provider. Runtime image pinning does not by itself prove reproducible builds or a verified supply chain.

Create these secrets through the deployment secret manager before rollout; no real values belong in Git:

| Secret | Purpose |
|---|---|
| `sentinel-runtime` | Restricted `SENTINEL_DATABASE_URL`, authenticated `REDIS_URL`, chosen model API credential, Razorpay **test** keys and webhook secret, optional enrichment/alert credentials |
| `sentinel-migration` | Separate migration URL, initial scoped admin bootstrap JSON on first deployment, runtime role name and password for grants; do not mount in serving pods |
| `sentinel-db-ca` | `ca.crt` mounted at `/run/sentinel-ca/ca.crt` for PostgreSQL certificate verification |
| `sentinel-tls` | Gateway TLS certificate/key for the explicit browser host |

Production startup also requires a configured HTTPS operational notification destination. It cannot prove a human recipient has acknowledged a notification; verify that separately.

Production requires PostgreSQL `sslmode=verify-full&sslrootcert=/run/sentinel-ca/ca.crt` and authenticated `rediss://` with certificate validation. Provision managed HA PostgreSQL and Redis with backups, private endpoints and tested client failover. Redis private CA options can be supplied in the URL without weakening verification. The app uses PostgreSQL for accepted jobs; Redis outage stops new HTTP admission but does not discard accepted work.

Apply the namespace and configuration first, run the migration Job to completion, then roll out serving tiers, services, policies and Gateway/HTTPRoute resources. An existing completed migration Job must be replaced deliberately for a new migration run. Confirm readiness, version, TLS, certificate renewal, restore ability and failover before routing real customer traffic. Migration jobs must not run concurrently with other schema-management tools. A maintained Gateway API controller with compatible CRDs, a platform-managed GatewayClass, NetworkPolicy-capable CNI and external DNS are required. The runtime intentionally trusts no forwarded client-IP header by default; configure exact trusted proxy ranges only after checking the cluster's traffic path. Credential/subject quotas remain authoritative.

## Gates not proven by this repository

No real cloud account, cluster, managed HA service, domain, TLS issuer, secret manager or operational notification recipient was provided. None has been provisioned, charged or deployed. Multi-zone failure, certificate renewal, managed-database failover and independent backup retention require exercises on the chosen infrastructure. Commercial enrichment/model/test-payment contracts need valid credentials. Acquirer identity and real-money settlement remain unsupported; the payment adapter rejects live keys. These are release gates, not claims a template or mock test can establish.


Automatic commercial enrichment is disabled by default. To enable the separate refresh worker, add `SENTINEL_PROVIDER_REFRESH_ENABLED=1` to the deployed configuration and opt in each reviewed mapping in Providers. Verify licensing/quota, failure alerts and actual transport contracts before enabling real calls. Apply revision `0008_provider_refresh` first. Existing mappings require an explicit administrator resave after checking their merchant identities; the migration never reattests them or enables automatic contacts.


## Gateway API controller and TLS routing

The community ingress-nginx controller retired in March 2026. The application bundle now emits Gateway API v1 Gateway and HTTPRoute resources, references an existing platform-owned GatewayClass, terminates TLS using sentinel-tls, restricts route attachment to its namespace, redirects HTTP to the explicit HTTPS hostname and adds HSTS to HTTPS responses. The internal nginx reverse proxy remains a separate maintained container image; it is not the retired Kubernetes controller.

The platform administrator installs compatible Gateway API CRDs and a maintained controller, then creates the class. The checked-in gateway-class.example.yaml targets Envoy Gateway. CI pins Envoy Gateway v1.10.0; its published compatibility matrix lists Kubernetes v1.34–v1.37 and Gateway API v1.6.1. Recheck current support and pin/verify platform artifacts before an actual release. Installing or upgrading cluster-scoped CRDs/controllers remains a platform operation, separate from the application renderer.

The deployment-gateway CI job installs the real controller in an explicitly named disposable local kind cluster. It exercises the rendered internal gateway using fixture upstreams and a temporary CA. Checks cover observed-generation Accepted/ResolvedRefs conditions, HTTP redirect, verified TLS/hostname routing, HSTS, API prefix routing, rejection of public dependency probes and wrong hosts, untrusted certificate rejection, and TLS Secret rotation. Private test keys are excluded from uploaded artifacts. The test refuses non-disposable/non-loopback cluster targets.

This is a routing fixture with one internal gateway replica and synthetic upstream responses. It does not run the full Sentinel deployment in Kubernetes, prove cross-zone HA, exercise a public certificate issuer/DNS renewal, or validate real provider contracts. Compose separately tests the actual application replicas and data services. Existing production deployments must be migrated deliberately: install/validate the new class and certificate, check current Gateway/HTTPRoute status and actual HTTPS behavior, then switch DNS/load-balancer routing and remove the old Ingress only after successful cutover.

Primary references:
- https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/
- https://gateway.envoyproxy.io/docs/tasks/quickstart/
- https://gateway.envoyproxy.io/news/releases/matrix/
