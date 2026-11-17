"""Run actual Sentinel images in the disposable CI cluster with mock providers.

Uses the production renderer and preserves replica placement, probes, security
contexts, grants and rollout strategy. Explicit CI overrides use locally built
images, development provider mode and disposable TLS SQL/Redis. This is
neither a production-profile startup nor a cloud/HA or live-provider proof.
Run after gateway_acceptance.py in the same isolated cluster.
"""

from http.client import HTTPSConnection
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sentinel_gateway_ci", ROOT / "scripts/gateway_acceptance.py"
)
edge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge)
NAMESPACE = "sentinel-ci-application"
HOST = edge.HOST


def ns(*args, **kwargs):
    return edge.kube("-n", NAMESPACE, *args, **kwargs)


def application_resources(images):
    for key in ("backend", "frontend"):
        if not re.fullmatch(r"sentinel-ci-" + key + r":[a-f0-9]{40}", images[key]):
            raise ValueError(
                "Only explicit commit-tagged CI application images allowed"
            )
    spec = importlib.util.spec_from_file_location(
        "sentinel_ci_render", ROOT / "deploy/kubernetes/render.py"
    )
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    resources = renderer.render(
        HOST,
        dict(
            backend="registry.invalid/backend@sha256:" + "a" * 64,
            frontend="registry.invalid/frontend@sha256:" + "b" * 64,
            gateway=images["gateway"],
        ),
        NAMESPACE,
        gateway_class="sentinel-ci",
    )
    for row in resources:
        if row["kind"] == "ConfigMap" and row["metadata"]["name"] == "sentinel-config":
            row["data"].update(
                SENTINEL_ENV="development", SENTINEL_RATE_LIMIT_REQUESTS="5000"
            )
        if row["kind"] in ("Deployment", "Job"):
            container = row["spec"]["template"]["spec"]["containers"][0]
            key = (
                "backend"
                if row["kind"] == "Job" or row["metadata"]["name"] == "sentinel-api"
                else "frontend"
                if row["metadata"]["name"] == "sentinel-frontend"
                else None
            )
            if key:
                container.update(image=images[key], imagePullPolicy="Never")
    return resources


def dependency(name, image, uid, port, args, env=None, storage=None):
    """Single disposable dependency, deliberately not a production DB/cache HA plan."""
    container = dict(
        name=name,
        image=image,
        args=args,
        securityContext=dict(
            runAsUser=uid,
            allowPrivilegeEscalation=False,
            readOnlyRootFilesystem=True,
            capabilities=dict(drop=["ALL"]),
        ),
        resources=dict(
            requests=dict(cpu="100m", memory="64Mi"),
            limits=dict(cpu="500m", memory="256Mi"),
        ),
        readinessProbe=dict(
            tcpSocket=dict(port=port), periodSeconds=2, timeoutSeconds=1
        ),
        volumeMounts=[
            dict(name="tmp", mountPath="/tmp"),
            dict(name="data", mountPath=storage or "/data"),
            dict(name="server-tls", mountPath="/run/sentinel-tls", readOnly=True),
        ],
    )
    if env:
        container["envFrom"] = [dict(secretRef=dict(name=env))]
    if name == "ci-postgres":
        # The upstream bootstrap clears PGHOST. Preserve its default socket
        # location and make it writable rather than relying on an env override.
        container["volumeMounts"].append(
            dict(name="tmp", mountPath="/var/run/postgresql")
        )
        container["volumeMounts"].append(
            dict(name="hba", mountPath="/run/sentinel-hba", readOnly=True)
        )
    volumes = [
        dict(name="tmp", emptyDir={}),
        dict(name="data", emptyDir={}),
        dict(
            name="server-tls", secret=dict(secretName=name + "-tls", defaultMode=0o440)
        ),
    ]
    if name == "ci-postgres":
        volumes[1] = dict(
            name="data", persistentVolumeClaim=dict(claimName="ci-postgres-data")
        )
        volumes.append(dict(name="hba", configMap=dict(name="ci-postgres-hba")))
    return [
        dict(
            apiVersion="apps/v1",
            kind="Deployment",
            metadata=dict(name=name, namespace=NAMESPACE),
            spec=dict(
                replicas=1,
                selector=dict(matchLabels=dict(app=name)),
                template=dict(
                    metadata=dict(labels=dict(app=name)),
                    spec=dict(
                        automountServiceAccountToken=False,
                        securityContext=dict(
                            runAsNonRoot=True,
                            fsGroup=uid,
                            seccompProfile=dict(type="RuntimeDefault"),
                        ),
                        containers=[container],
                        volumes=volumes,
                    ),
                ),
            ),
        ),
        dict(
            apiVersion="v1",
            kind="Service",
            metadata=dict(name=name, namespace=NAMESPACE),
            spec=dict(
                selector=dict(app=name), ports=[dict(port=port, targetPort=port)]
            ),
        ),
    ]


def put_secret(name, data, kind="Opaque"):
    ns(
        "apply",
        "-f",
        "-",
        sensitive=True,
        input=json.dumps(
            dict(
                apiVersion="v1",
                kind="Secret",
                type=kind,
                metadata=dict(name=name, namespace=NAMESPACE),
                stringData=data,
            )
        ).encode(),
    )


def dependency_certificate(name, certs):
    """Private keys stay under the CI artifact exclusion, never in report JSON."""
    key, csr, crt, ext = [
        certs / (name + suffix) for suffix in (".key", ".csr", ".crt", ".ext")
    ]
    ext.write_text(
        "subjectAltName=DNS:"
        + name
        + "\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\n"
    )
    edge.command(
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            "/CN=" + name,
        ]
    )
    edge.command(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(certs / "ca.crt"),
            "-CAkey",
            str(certs / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(crt),
            "-days",
            "1",
            "-extfile",
            str(ext),
        ]
    )
    edge.command(
        [
            "openssl",
            "verify",
            "-x509_strict",
            "-CAfile",
            str(certs / "ca.crt"),
            str(crt),
        ]
    )
    put_secret(
        name + "-tls",
        {
            "server.crt": crt.read_text(),
            "server.key": key.read_text(),
            "ca.crt": (certs / "ca.crt").read_text(),
        },
    )


def ready_pods(name):
    rows = json.loads(ns("get", "pods", "-l", "app=" + name, "-o", "json"))["items"]
    return [
        r
        for r in rows
        if not r["metadata"].get("deletionTimestamp")
        and any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in r.get("status", {}).get("conditions", [])
        )
    ]


def main():
    if os.environ.get("SENTINEL_KUBERNETES_ACCEPTANCE") != "1":
        raise SystemExit("Explicit disposable Kubernetes application opt-in required")
    server = edge.kube(
        "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
    ).decode()
    edge.validate_target(edge.CONTEXT, server)
    # Refuse existing namespaces rather than disturbing somebody's previous work.
    existing = json.loads(edge.kube("get", "namespaces", "-o", "json"))["items"]
    if any(row["metadata"]["name"] == NAMESPACE for row in existing):
        raise RuntimeError("Application acceptance requires a fresh namespace")
    sha = os.environ.get("GITHUB_SHA", "")
    images = dict(
        backend="sentinel-ci-backend:" + sha,
        frontend="sentinel-ci-frontend:" + sha,
        gateway=os.environ.get("SENTINEL_GATEWAY_FIXTURE_IMAGE", ""),
    )
    resources = application_resources(images)
    report = dict(
        scope="real Sentinel Kubernetes images, mock model/payment providers, disposable SQL/Redis",
        checks={},
        overrides=[
            "development provider profile",
            "commit-tagged locally loaded app images",
            "disposable SQL PVC and Redis data",
        ],
        not_proven=[
            "production-profile credentials and platform certificate lifecycle",
            "network-policy enforcement by kind CNI",
            "cloud/multi-zone HA",
            "real provider contracts",
            "cross-node/zone storage failure and backup disaster recovery",
        ],
        images=images,
    )

    def check(name, ok):
        if not ok:
            raise AssertionError(name)
        report["checks"][name] = "pass"
        print(name + ": pass", flush=True)

    edge.apply([r for r in resources if r["kind"] == "Namespace"])
    certs = ROOT / "artifacts/gateway-certs"
    put_secret(
        "sentinel-tls",
        {
            "tls.crt": (certs / "server.crt").read_text(),
            "tls.key": (certs / "server.key").read_text(),
        },
        "kubernetes.io/tls",
    )
    put_secret("sentinel-db-ca", {"ca.crt": (certs / "ca.crt").read_text()})
    for name in ("ci-postgres", "ci-redis"):
        dependency_certificate(name, certs)
    admin_key, owner_password, runtime_password, redis_password = [
        secrets.token_urlsafe(40) for _ in range(4)
    ]
    owner_url = "postgresql://sentinel:" + owner_password + "@ci-postgres:5432/sentinel"
    runtime_url = (
        "postgresql://sentinel_runtime:"
        + runtime_password
        + "@ci-postgres:5432/sentinel"
    )
    db_tls = "?sslmode=verify-full&sslrootcert=/run/sentinel-ca/ca.crt"
    owner_url += db_tls
    runtime_url += db_tls
    redis_url = (
        "rediss://:"
        + redis_password
        + "@ci-redis:6379/0?ssl_cert_reqs=required&ssl_check_hostname=true&ssl_ca_certs=/run/sentinel-ca/ca.crt"
    )
    put_secret("ci-redis", {"REDIS_PASSWORD": redis_password})
    put_secret(
        "ci-postgres",
        dict(
            POSTGRES_DB="sentinel",
            POSTGRES_USER="sentinel",
            POSTGRES_PASSWORD=owner_password,
            PGDATA="/var/lib/postgresql/data/pgdata",
        ),
    )
    provider_keys = dict(
        OPENROUTER_API_KEY="",
        ANTHROPIC_API_KEY="",
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
    )
    put_secret(
        "sentinel-runtime",
        dict(
            provider_keys,
            SENTINEL_DATABASE_URL=runtime_url,
            REDIS_URL=redis_url,
        ),
    )
    put_secret(
        "sentinel-migration",
        dict(
            provider_keys,
            SENTINEL_DATABASE_URL=owner_url,
            SENTINEL_RUNTIME_DB_PASSWORD=runtime_password,
            SENTINEL_CREDENTIALS=json.dumps(
                [dict(key=admin_key, subject="kubernetes-ci-admin", role="admin")]
            ),
        ),
    )
    edge.apply(
        [
            dict(
                apiVersion="v1",
                kind="PersistentVolumeClaim",
                metadata=dict(name="ci-postgres-data", namespace=NAMESPACE),
                spec=dict(
                    accessModes=["ReadWriteOnce"],
                    resources=dict(requests=dict(storage="1Gi")),
                ),
            ),
            dict(
                apiVersion="v1",
                kind="ConfigMap",
                metadata=dict(name="ci-postgres-hba", namespace=NAMESPACE),
                data={
                    "pg_hba.conf": "local all all trust\nhostnossl all all all reject\nhostssl all all all scram-sha-256\n"
                },
            ),
        ]
    )
    edge.apply(
        dependency(
            "ci-postgres",
            "postgres:16-alpine",
            70,
            5432,
            [
                "postgres",
                "-c",
                "ssl=on",
                "-c",
                "ssl_cert_file=/run/sentinel-tls/server.crt",
                "-c",
                "ssl_key_file=/run/sentinel-tls/server.key",
                "-c",
                "hba_file=/run/sentinel-hba/pg_hba.conf",
            ],
            "ci-postgres",
            "/var/lib/postgresql/data",
        )
        + dependency(
            "ci-redis",
            "redis:7-alpine",
            999,
            6379,
            [
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
                "--port",
                "0",
                "--tls-port",
                "6379",
                "--tls-cert-file",
                "/run/sentinel-tls/server.crt",
                "--tls-key-file",
                "/run/sentinel-tls/server.key",
                "--tls-ca-cert-file",
                "/run/sentinel-tls/ca.crt",
                "--tls-auth-clients",
                "no",
                "--requirepass",
                "$(REDIS_PASSWORD)",
            ],
            "ci-redis",
        )
    )
    for name in ("ci-postgres", "ci-redis"):
        ns("rollout", "status", "deployment/" + name, "--timeout=120s", timeout=130)
    # SQL must accept connections, not merely have a running container.
    edge.wait_for(
        lambda: (
            ns(
                "exec",
                "deployment/ci-postgres",
                "--",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "sentinel",
            )
            .decode()
            .find("accepting connections")
            >= 0
        )
    )
    edge.apply(
        [
            r
            for r in resources
            if r["kind"]
            in ("ConfigMap", "Service", "PodDisruptionBudget", "NetworkPolicy", "Job")
        ]
    )
    ns(
        "wait",
        "--for=condition=complete",
        "job/sentinel-migrate",
        "--timeout=180s",
        timeout=190,
    )
    check("rendered_migration_job_completed", True)
    edge.apply(
        [r for r in resources if r["kind"] in ("Deployment", "Gateway", "HTTPRoute")]
    )
    for name in ("sentinel-api", "sentinel-frontend", "sentinel-gateway"):
        ns("rollout", "status", "deployment/" + name, "--timeout=180s", timeout=190)
        pods = ready_pods(name)
        check(
            name + "_two_ready_replicas_on_distinct_nodes",
            len(pods) == 2 and len({p["spec"]["nodeName"] for p in pods}) == 2,
        )
    grants = ns(
        "exec",
        "deployment/sentinel-api",
        "--",
        "python",
        "-c",
        "import os;from app.models.db import SessionLocal;from app.ops.db_privileges import validate;db=SessionLocal();validate(db);assert not os.environ.get('SENTINEL_RUNTIME_DB_PASSWORD');assert not os.environ.get('SENTINEL_MIGRATION_DATABASE_URL');db.close();print('restricted')",
    )
    check(
        "actual_runtime_grants_and_no_provisioning_secret",
        grants.strip() == b"restricted",
    )
    tls_probe = """import os,ssl,json,certifi,redis
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from app.models.db import engine
from app.core.settings import validate_production_transport
from urllib.parse import urlsplit,parse_qsl,urlencode,urlunsplit
db_url,cache_url=os.environ['SENTINEL_DATABASE_URL'],os.environ['REDIS_URL']
validate_production_transport(db_url,cache_url)
with engine.connect() as conn:
 assert conn.execute(text('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).scalar_one() is True
client=redis.Redis.from_url(cache_url,socket_timeout=2,socket_connect_timeout=2)
assert client.ping()
conn=client.connection_pool.get_connection()
assert isinstance(conn._sock,ssl.SSLSocket) and conn._sock.version()
client.connection_pool.release(conn);client.close()
def rejected_pg(options):
 e=create_engine(make_url(db_url).update_query_dict(options),connect_args={'connect_timeout':2})
 try:
  with e.connect(): pass
 except Exception: return True
 finally: e.dispose()
 return False
assert rejected_pg({'sslmode':'disable'})
assert rejected_pg({'sslrootcert':certifi.where()})
parts=urlsplit(cache_url)
options=dict(parse_qsl(parts.query));options['ssl_ca_certs']=certifi.where()
wrong_ca=urlunsplit(parts._replace(query=urlencode(options)))
def rejected_cache(client):
 try: client.ping()
 except redis.exceptions.ConnectionError: return True
 finally: client.close()
 return False
assert rejected_cache(redis.Redis.from_url(wrong_ca,socket_timeout=2,socket_connect_timeout=2))
assert rejected_cache(redis.Redis(host='ci-redis',port=6379,password=parts.password,socket_timeout=2,socket_connect_timeout=2))
print('verified_tls_and_rejections')
"""
    check(
        "postgres_redis_verified_tls_reject_plaintext_and_untrusted_ca",
        ns("exec", "deployment/sentinel-api", "--", "python", "-c", tls_probe).strip()
        == b"verified_tls_and_rejections",
    )

    def envoy_service():
        rows = json.loads(
            edge.kube(
                "get",
                "services",
                "-A",
                "-l",
                "gateway.envoyproxy.io/owning-gateway-namespace="
                + NAMESPACE
                + ",gateway.envoyproxy.io/owning-gateway-name=sentinel-edge",
                "-o",
                "json",
            )
        )["items"]
        return rows[0] if rows else None

    svc = edge.wait_for(envoy_service)
    tls_port = edge.available_port()
    log = (ROOT / "artifacts/kubernetes-forward.log").open("wb")
    forwarded = subprocess.Popen(
        [
            "kubectl",
            "--context",
            edge.CONTEXT,
            "-n",
            svc["metadata"]["namespace"],
            "port-forward",
            "service/" + svc["metadata"]["name"],
            str(tls_port) + ":443",
            "--address=127.0.0.1",
            "--pod-running-timeout=120s",
        ],
        cwd=ROOT,
        stdout=log,
        stderr=log,
    )

    def http(path, body=None, key=admin_key):
        context = ssl.create_default_context(cafile=str(certs / "ca.crt"))
        conn = HTTPSConnection(HOST, timeout=15, context=context)
        raw = socket.create_connection(("127.0.0.1", tls_port), timeout=5)
        try:
            conn.sock = context.wrap_socket(raw, server_hostname=HOST)
            conn.request(
                "POST" if body is not None else "GET",
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Host": HOST,
                    "X-API-Key": key,
                    "Content-Type": "application/json",
                    "Origin": "https://" + HOST,
                },
            )
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data.startswith(
                (b"{", b"[")
            ) else data
        finally:
            conn.close()
            raw.close()

    try:
        edge.wait_for(lambda: http("/")[0] == 200)
        check(
            "actual_built_frontend_through_verified_https",
            b"<html" in http("/")[1].lower(),
        )
        check("unauthenticated_api_rejected", http("/api/auth/me", key="")[0] == 401)
        agent = "kube_" + uuid.uuid4().hex
        policy = dict(
            agent_id=agent,
            owner_human_id="ci-owner",
            authorized_goal="buy weekly groceries",
            max_amount_per_txn=1000,
            max_daily_total=100000,
            velocity_max_count=1000,
            time_window_start=0,
            time_window_end=23,
        )
        check("https_policy_create", http("/api/policies", policy)[0] == 200)
        purchase = dict(
            request_id="req_" + uuid.uuid4().hex,
            agent_id=agent,
            merchant_name="KubernetesShop",
            merchant_category="grocery",
            amount=1,
            stated_intent="buy weekly groceries",
        )
        approved = http("/api/transactions/evaluate", purchase)
        check(
            "https_mock_payment_approved",
            approved[0] == 200 and approved[1]["verdict"] == "APPROVE",
        )
        check(
            "https_idempotent_replay",
            http("/api/transactions/evaluate", purchase)[1] == approved[1],
        )
        check(
            "https_conflicting_replay_rejected",
            http("/api/transactions/evaluate", dict(purchase, amount=2))[0] == 409,
        )
        check(
            "https_policy_hard_cap_blocks",
            http(
                "/api/transactions/evaluate",
                dict(purchase, request_id="req_" + uuid.uuid4().hex, amount=1001),
            )[1]["verdict"]
            == "BLOCK",
        )
        # A voluntary single-pod replacement exercises the unchanged two-node placement.
        old_pods = ready_pods("sentinel-api")
        ns("delete", "pod", old_pods[0]["metadata"]["name"], "--wait=false")
        check(
            "https_serves_during_single_api_replacement",
            edge.wait_for(lambda: http("/api/auth/me")[0] == 200),
        )
        edge.wait_for(
            lambda: (
                len(ready_pods("sentinel-api")) == 2
                and all(
                    p["metadata"]["uid"] != old_pods[0]["metadata"]["uid"]
                    for p in ready_pods("sentinel-api")
                )
            ),
            timeout=180,
        )
        check("replacement_replica_recovers", len(ready_pods("sentinel-api")) == 2)
        check(
            "ledger_replay_survives_replica_replacement",
            http("/api/transactions/evaluate", purchase)[1] == approved[1],
        )
        ns("rollout", "restart", "deployment/sentinel-api")
        ns(
            "rollout",
            "status",
            "deployment/sentinel-api",
            "--timeout=180s",
            timeout=190,
        )
        check(
            "two_node_rolling_restart_completes", len(ready_pods("sentinel-api")) == 2
        )
        old_db = ready_pods("ci-postgres")[0]["metadata"]["uid"]
        ns(
            "delete",
            "pod",
            ready_pods("ci-postgres")[0]["metadata"]["name"],
            "--wait=false",
        )
        edge.wait_for(
            lambda: (
                len(ready_pods("ci-postgres")) == 1
                and ready_pods("ci-postgres")[0]["metadata"]["uid"] != old_db
            ),
            timeout=180,
        )
        check(
            "postgres_pvc_preserves_ledger_after_pod_replacement",
            edge.wait_for(
                lambda: http("/api/transactions/evaluate", purchase)[1] == approved[1],
                timeout=180,
            ),
        )
        # No new financial request is admitted without the shared quota service.
        ns("scale", "deployment/ci-redis", "--replicas=0")
        try:
            edge.wait_for(
                lambda: (
                    not json.loads(
                        ns("get", "pods", "-l", "app=ci-redis", "-o", "json")
                    )["items"]
                )
            )
            probe = "import urllib.request,urllib.error,json;result={};\nfor p in ('live','ready'):\n try: r=urllib.request.urlopen('http://localhost:8000/health/'+p,timeout=10);result[p]=r.status\n except urllib.error.HTTPError as e: result[p]=e.code\nprint(json.dumps(result))"
            health = json.loads(
                ns("exec", "deployment/sentinel-api", "--", "python", "-c", probe)
            )
            check(
                "quota_outage_liveness_200_readiness_503",
                health == dict(live=200, ready=503),
            )
            new_purchase = dict(purchase, request_id="req_" + uuid.uuid4().hex)
            check(
                "quota_outage_financial_admission_unavailable",
                http("/api/transactions/evaluate", new_purchase)[0] in (502, 503, 504),
            )
        finally:
            ns("scale", "deployment/ci-redis", "--replicas=1")
        edge.wait_for(lambda: len(ready_pods("sentinel-api")) == 2)
        check(
            "quota_recovery_restores_https_api",
            edge.wait_for(lambda: http("/api/auth/me")[0] == 200),
        )
        check(
            "private_dependency_probes_remain_unpublished",
            http("/api/health/ready")[0] == 404,
        )
        # Metadata and hashes only; no manifests containing credentials are saved.
        report["pods"] = [
            {
                "name": p["metadata"]["name"],
                "node": p["spec"]["nodeName"],
                "image_ids": [
                    c.get("imageID") for c in p["status"].get("containerStatuses", [])
                ],
            }
            for p in ready_pods("sentinel-api")
            + ready_pods("sentinel-frontend")
            + ready_pods("sentinel-gateway")
        ]
        (ROOT / "artifacts/kubernetes-acceptance.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print("KUBERNETES_APPLICATION_ACCEPTANCE_PASS", flush=True)
    finally:
        forwarded.terminate()
        try:
            forwarded.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forwarded.kill()
            forwarded.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
