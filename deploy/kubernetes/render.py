"""Render a secret-free TLS/replica deployment; applying it is a separate operator step."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re


def dns_name(value, max_length=253):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_length
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in value.split(".")
        )
    )


def render(host, images, namespace="sentinel", gateway_class="sentinel"):
    import ipaddress

    if not dns_name(host) or "." not in host:
        raise ValueError("An explicit valid DNS hostname is required")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a DNS hostname rather than an IP address")
    if set(images) != {"backend", "frontend", "gateway"}:
        raise ValueError("Backend, frontend and gateway images are required")
    if not dns_name(gateway_class):
        raise ValueError("Invalid GatewayClass name")
    for image in images.values():
        if not re.fullmatch(r"[A-Za-z0-9_./:-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("All deployment images must be pinned by SHA256 digest")
    if not dns_name(namespace, 63) or "." in namespace:
        raise ValueError("Invalid namespace")

    def resource(kind, name, version="v1", **body):
        return dict(
            apiVersion=version,
            kind=kind,
            metadata=dict(name=name, namespace=namespace),
            **body,
        )

    items = [
        dict(
            apiVersion="v1",
            kind="Namespace",
            metadata=dict(
                name=namespace,
                labels={"pod-security.kubernetes.io/enforce": "restricted"},
            ),
        )
    ]
    config = {
        "SENTINEL_ENV": "production",
        "SENTINEL_AUTH_BACKEND": "database",
        "QUEUE_BACKEND": "postgres",
        "RATE_LIMIT_BACKEND": "redis",
        "SENTINEL_ALLOWED_HOSTS": host + ",localhost",
        "SENTINEL_ALLOWED_ORIGINS": "https://" + host,
        "SENTINEL_REQUIRE_RESTRICTED_DB": "1",
        "SENTINEL_REQUIRE_MIGRATIONS": "1",
        "SENTINEL_TRUSTED_PROXY_IPS": "",
        "LLM_PROVIDER": "openrouter",
        "LLM_MODEL": "anthropic/claude-sonnet-4.5",
    }
    items.append(resource("ConfigMap", "sentinel-config", data=config))
    nginx = """pid /tmp/nginx.pid;
events { worker_connections 1024; }
http {
 client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
 client_max_body_size 512k;
 upstream api { server sentinel-api:8000; keepalive 16; }
 upstream web { server sentinel-frontend:8080; }
 server { listen 8080;
  client_body_timeout 10s; client_header_timeout 10s; send_timeout 15s;
  proxy_connect_timeout 2s; proxy_send_timeout 10s; proxy_read_timeout 40s;
  proxy_http_version 1.1; proxy_set_header Connection ""; proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_next_upstream error timeout; proxy_next_upstream_tries 2;
  add_header X-Content-Type-Options nosniff always;
  add_header Referrer-Policy same-origin always;
  add_header X-Frame-Options DENY always;
  location ^~ /api/health { return 404; }
  location /api/ { proxy_pass http://api/; }
  location = /health { return 200 "ok"; }
  location / { proxy_pass http://web; }
 }
 server { listen 8081;
  proxy_connect_timeout 2s; proxy_read_timeout 3s;
  proxy_set_header Host $host;
  location = /live { return 200 "ok"; }
  location = /ready { proxy_pass http://api/health/ready; }
  location / { return 404; }
 }
}"""
    items.append(
        resource("ConfigMap", "sentinel-gateway-config", data={"nginx.conf": nginx})
    )
    pod_security = dict(runAsNonRoot=True, seccompProfile=dict(type="RuntimeDefault"))
    container_security = dict(
        allowPrivilegeEscalation=False,
        readOnlyRootFilesystem=True,
        capabilities=dict(drop=["ALL"]),
    )

    def pod(name, image, api=False):
        c = dict(
            name=name,
            image=image,
            ports=[dict(containerPort=8000 if api else 8080)],
            securityContext=dict(container_security),
            resources=dict(
                requests=dict(
                    cpu="250m" if api else "100m", memory="512Mi" if api else "64Mi"
                ),
                limits=dict(
                    cpu="2" if api else "500m", memory="1Gi" if api else "256Mi"
                ),
            ),
            volumeMounts=[dict(name="tmp", mountPath="/tmp")],
        )
        volumes = [dict(name="tmp", emptyDir=dict(sizeLimit="128Mi"))]
        if api:
            c["envFrom"] = [
                dict(secretRef=dict(name="sentinel-runtime")),
                dict(configMapRef=dict(name="sentinel-config")),
            ]
            c["env"] = [
                dict(
                    name="SENTINEL_ENV",
                    valueFrom=dict(
                        configMapKeyRef=dict(name="sentinel-config", key="SENTINEL_ENV")
                    ),
                ),
                dict(
                    name="SENTINEL_INSTANCE_ID",
                    valueFrom=dict(fieldRef=dict(fieldPath="metadata.name")),
                ),
            ]
            c["volumeMounts"].append(
                dict(name="db-ca", mountPath="/run/sentinel-ca", readOnly=True)
            )
            volumes.append(dict(name="db-ca", secret=dict(secretName="sentinel-db-ca")))
            c["securityContext"]["runAsUser"] = 10001
        elif name == "sentinel-gateway":
            c["ports"].append(dict(containerPort=8081, name="probes"))
            c["volumeMounts"].append(
                dict(
                    name="nginx",
                    mountPath="/etc/nginx/nginx.conf",
                    subPath="nginx.conf",
                    readOnly=True,
                )
            )
            volumes.append(
                dict(name="nginx", configMap=dict(name="sentinel-gateway-config"))
            )
            c["securityContext"]["runAsUser"] = 101
        else:
            c["securityContext"]["runAsUser"] = 101
        live = "/health/live" if api else "/live" if name == "sentinel-gateway" else "/"
        ready = (
            "/health/ready" if api else "/ready" if name == "sentinel-gateway" else "/"
        )
        for key, path in [
            ("livenessProbe", live),
            ("readinessProbe", ready),
            ("startupProbe", live),
        ]:
            c[key] = dict(
                httpGet=dict(
                    path=path,
                    port=8000 if api else 8081 if name == "sentinel-gateway" else 8080,
                    httpHeaders=[dict(name="Host", value=host)],
                ),
                timeoutSeconds=3,
                periodSeconds=10,
                failureThreshold=30 if key == "startupProbe" else 3,
            )
        return dict(
            automountServiceAccountToken=False,
            securityContext=pod_security,
            terminationGracePeriodSeconds=50,
            containers=[c],
            volumes=volumes,
            affinity=dict(
                podAntiAffinity=dict(
                    requiredDuringSchedulingIgnoredDuringExecution=[
                        dict(
                            labelSelector=dict(matchLabels=dict(app=name)),
                            topologyKey="kubernetes.io/hostname",
                        )
                    ]
                )
            ),
        ), c

    for name, key, is_api in [
        ("sentinel-api", "backend", True),
        ("sentinel-frontend", "frontend", False),
        ("sentinel-gateway", "gateway", False),
    ]:
        spec, _ = pod(name, images[key], is_api)
        metadata = dict(labels=dict(app=name))
        if is_api:
            data = json.dumps(config, sort_keys=True)
        elif name == "sentinel-gateway":
            data = nginx
        else:
            data = None
        if data is not None:
            metadata["annotations"] = {
                "sentinel/config-sha256": sha256(data.encode()).hexdigest()
            }
        items.append(
            resource(
                "Deployment",
                name,
                "apps/v1",
                spec=dict(
                    replicas=2,
                    revisionHistoryLimit=3,
                    selector=dict(matchLabels=dict(app=name)),
                    strategy=dict(
                        type="RollingUpdate",
                        rollingUpdate=dict(maxSurge=0, maxUnavailable=1),
                    ),
                    template=dict(metadata=metadata, spec=spec),
                ),
            )
        )
        items.append(
            resource(
                "Service",
                name,
                spec=dict(
                    selector=dict(app=name),
                    ports=[
                        dict(
                            port=8000 if is_api else 8080,
                            targetPort=8000 if is_api else 8080,
                        )
                    ],
                ),
            )
        )
        items.append(
            resource(
                "PodDisruptionBudget",
                name,
                "policy/v1",
                spec=dict(minAvailable=1, selector=dict(matchLabels=dict(app=name))),
            )
        )
    migration, c = pod("sentinel-migrate", images["backend"], True)
    for key in ("livenessProbe", "readinessProbe", "startupProbe"):
        c.pop(key)
    c["command"] = ["python", "-m", "app.ops.initialize"]
    c["envFrom"] = [
        dict(secretRef=dict(name="sentinel-migration")),
        dict(configMapRef=dict(name="sentinel-config")),
    ]
    migration.pop("affinity")
    migration["restartPolicy"] = "Never"
    items.append(
        resource(
            "Job",
            "sentinel-migrate",
            "batch/v1",
            spec=dict(
                backoffLimit=0,
                activeDeadlineSeconds=300,
                template=dict(
                    metadata=dict(labels=dict(app="sentinel-migrate")), spec=migration
                ),
            ),
        )
    )
    # The platform owns the GatewayClass/controller. Application bundles never
    # install a retired controller or take over that cluster-scoped responsibility.
    items.append(
        resource(
            "Gateway",
            "sentinel-edge",
            "gateway.networking.k8s.io/v1",
            spec={
                "gatewayClassName": gateway_class,
                "listeners": [
                    {
                        "name": "http",
                        "hostname": host,
                        "port": 80,
                        "protocol": "HTTP",
                        "allowedRoutes": {"namespaces": {"from": "Same"}},
                    },
                    {
                        "name": "https",
                        "hostname": host,
                        "port": 443,
                        "protocol": "HTTPS",
                        "tls": {
                            "mode": "Terminate",
                            "certificateRefs": [
                                {"group": "", "kind": "Secret", "name": "sentinel-tls"}
                            ],
                        },
                        "allowedRoutes": {"namespaces": {"from": "Same"}},
                    },
                ],
            },
        )
    )
    items.append(
        resource(
            "HTTPRoute",
            "sentinel-http-redirect",
            "gateway.networking.k8s.io/v1",
            spec={
                "parentRefs": [{"name": "sentinel-edge", "sectionName": "http"}],
                "hostnames": [host],
                "rules": [
                    {
                        "filters": [
                            {
                                "type": "RequestRedirect",
                                "requestRedirect": {
                                    "scheme": "https",
                                    "port": 443,
                                    "statusCode": 301,
                                },
                            }
                        ]
                    }
                ],
            },
        )
    )
    items.append(
        resource(
            "HTTPRoute",
            "sentinel-https",
            "gateway.networking.k8s.io/v1",
            spec={
                "parentRefs": [{"name": "sentinel-edge", "sectionName": "https"}],
                "hostnames": [host],
                "rules": [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                        "filters": [
                            {
                                "type": "ResponseHeaderModifier",
                                "responseHeaderModifier": {
                                    "set": [
                                        {
                                            "name": "Strict-Transport-Security",
                                            "value": "max-age=31536000",
                                        }
                                    ]
                                },
                            }
                        ],
                        "timeouts": {"request": "60s", "backendRequest": "55s"},
                        "backendRefs": [{"name": "sentinel-gateway", "port": 8080}],
                    }
                ],
            },
        )
    )
    items.append(
        resource(
            "NetworkPolicy",
            "sentinel-api-ingress",
            "networking.k8s.io/v1",
            spec={
                "podSelector": {"matchLabels": {"app": "sentinel-api"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "podSelector": {
                                    "matchLabels": {"app": "sentinel-gateway"}
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8000}],
                    }
                ],
            },
        )
    )
    return items


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--namespace", default="sentinel")
    p.add_argument(
        "--gateway-class",
        default="sentinel",
        help="Existing platform-managed GatewayClass",
    )
    for name in ("backend", "frontend", "gateway"):
        p.add_argument("--" + name + "-image", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = render(
        a.host,
        {k: getattr(a, k + "_image") for k in ("backend", "frontend", "gateway")},
        a.namespace,
        a.gateway_class,
    )
    Path(a.output).write_text(
        "\n---\n".join(json.dumps(item, indent=2) for item in result) + "\n"
    )
    print("Rendered deployment configuration; nothing was applied")
