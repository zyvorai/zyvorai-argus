# Kubernetes Deployment (Phase 3)

Run Zyvor Argus on Kubernetes.

## Manifests

| File | Purpose |
|------|---------|
| `configmap.yaml` | Non-secret configuration (feature flags, URLs) |
| `secret.yaml` | API keys, tokens (use ExternalSecrets in production) |
| `rbac.yaml` | ServiceAccount + read-only Role for the Mission Control dashboard (pods, logs, events, workloads) |
| `cronjob.yaml` | Nightly smoke tests (`argus test exec`) |
| `deployment.yaml` | Webhook server + dashboard (`argus serve`) |
| `service.yaml` | ClusterIP service for webhook |
| `ingress.yaml` | External access to GitHub webhook endpoint |

## Deploy

**Prerequisites:** A running Kubernetes cluster (`kubectl cluster-info` must succeed).

```bash
# Validate manifests locally (no cluster required)
make k8s-validate

# Apply to a running cluster
make k8s-apply
```

Do not put shell comments on the same line as `make` — `make k8s-apply # comment` is parsed as multiple targets and will fail.

```bash
# Edit secret.yaml with your API keys, and ingress.yaml's host with your
# own domain, first
kubectl apply -f kubernetes/configmap.yaml
kubectl apply -f kubernetes/secret.yaml
kubectl apply -f kubernetes/rbac.yaml
kubectl apply -f kubernetes/pvc.yaml
kubectl apply -f kubernetes/deployment.yaml
kubectl apply -f kubernetes/service.yaml
kubectl apply -f kubernetes/cronjob.yaml
kubectl apply -f kubernetes/ingress.yaml
```

### Local cluster options

```bash
# kind
kind create cluster --name argus

# minikube
minikube start

# Docker Desktop: enable Kubernetes in settings
```

Then verify:

```bash
kubectl cluster-info
make k8s-apply
```

## GitHub Webhook

Point your GitHub webhook to the host you set in `ingress.yaml`:
```
https://qa-webhook.example.com/webhook/github
```

Events: `push`, `pull_request`, `repository_dispatch`

## Mission Control dashboard

The webhook Deployment also serves a live dashboard (pods, workloads, log tails, QA run history, and an Actions panel that can trigger test runs, generation, discovery, NL test creation, and visual regression) at `/dashboard`. NL test creation requires an LLM API key in `secret.yaml`; run history and generated tests live in the pod filesystem and reset on pod restart. RBAC for it is in `rbac.yaml` (read-only: pods, pods/log, events, deployments, cronjobs) bound to the `argus` ServiceAccount used by the Deployment.

**Default access is via port-forward** — the dashboard exposes pod logs, so it is deliberately *not* routed through the ingress:

```bash
kubectl port-forward svc/argus-webhook 8080:80
open http://localhost:8080/dashboard
```

### Dashboard: optional ingress exposure

Only expose it behind authentication. Example for ingress-nginx with basic auth:

```yaml
# Create the auth secret first:
#   htpasswd -c auth qa-admin && kubectl create secret generic argus-dashboard-auth --from-file=auth
# Then add a second ingress with:
metadata:
  annotations:
    nginx.ingress.kubernetes.io/auth-type: basic
    nginx.ingress.kubernetes.io/auth-secret: argus-dashboard-auth
spec:
  rules:
    - host: qa-webhook.example.com
      http:
        paths:
          - path: /dashboard
            pathType: Prefix
            backend: { service: { name: argus-webhook, port: { number: 80 } } }
          - path: /api/dashboard
            pathType: Prefix
            backend: { service: { name: argus-webhook, port: { number: 80 } } }
```

See [Tutorial 10](../docs/tutorials/10-mission-control-dashboard.md) for the full walkthrough.
