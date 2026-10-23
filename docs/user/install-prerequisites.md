# Install prerequisites & companion packages

Install these **before** Mission Control or Watchfloor. Order matters: toolchain
→ Community Argus (target) → Watchfloor (optional). After this page, the rest
of the setup is configuration, not hunting for missing tools.

## 0. Recommended order

```text
1. Toolchain          Docker (or Podman) · kubectl · Helm · (Python/Node if building from source)
2. Community Argus    ghcr.io/zyvorai/zyvor-argus  OR  git clone + make install
3. OSS service token  so Watchfloor (or CI) can call /api/v2
4. Watchfloor         only if you need Argus Enterprise — user package / Helm
5. Sign in & use      claim → SSO/demo login → register target → smoke
```

Product map: [Which product](which-product.md).

---

## 1. Toolchain packages

### macOS (Homebrew)

```bash
brew install --cask docker          # or OrbStack / Colima + docker CLI
brew install kubectl helm
# Source / CLI install of Community only:
brew install python@3.12 node@20 git
```

Start **Docker Desktop** (or your engine) once so `docker info` works.

### Linux (Debian/Ubuntu sketch)

```bash
# Docker Engine — follow https://docs.docker.com/engine/install/ubuntu/
sudo apt-get update
sudo apt-get install -y ca-certificates curl
# …then the Docker apt repo steps from that page…

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Source install of Community:
sudo apt-get install -y python3.12 python3.12-venv nodejs npm git
```

### Verify

```bash
docker info
kubectl version --client
helm version
python3 --version    # 3.11+ if using make install
node --version       # 20+ if using make install
```

**k3s single-node lab:** `curl -sfL https://get.k3s.io | sh -` then
`export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`. Helm and `kubectl` talk to that
cluster. Image loads use `sudo k3s ctr images import …`.

---

## 2. Install Community Argus (the engine)

Watchfloor does **not** replace this. Every Enterprise target is an `argus serve`.

### Option A — Container (fastest)

```bash
docker pull ghcr.io/zyvorai/zyvor-argus:latest
# pin a release if you prefer, e.g. :v0.9.2

mkdir -p ~/argus-target && cd ~/argus-target
cat > .env <<'EOF'
ZYVOR_BASE_URL=https://zyvor.dev
DASHBOARD_PASSWORD=Admin@321
ZYVOR_ENV=development
EOF

docker run -d --name argus-serve \
  -p 8080:8080 \
  --env-file .env \
  -v "$(pwd)/reports:/app/reports" \
  ghcr.io/zyvorai/zyvor-argus:latest \
  serve --port 8080 --host 0.0.0.0

curl -s http://127.0.0.1:8080/health
# Mission Control: http://127.0.0.1:8080/dashboard
# Login when DASHBOARD_PASSWORD is set: admin / Admin@321 (or DASHBOARD_USER)
```

More: [docs/releases.md](../releases.md) · remote/k3s: [docs/remote-deploy.md](../remote-deploy.md).

### Option B — From source

```bash
git clone https://github.com/zyvorai/argus.git
cd zyvor-argus
cp .env.example .env          # set ZYVOR_BASE_URL at minimum
make install                  # CLI + Playwright browsers
argus serve --port 8080
```

User walkthrough: [Getting Started](getting-started.md).

---

## 3. Mint an OSS service token (for Watchfloor / automation)

Enterprise (and CI) call Community over **Bearer** tokens, not the Mission
Control cookie.

```bash
# From a zyvor-argus checkout (or any host with the tools/ script):
python tools/hash_api_token.py
# Prints TOKEN=… and SHA256=…
```

Create a JSON file (only the **hash** is stored on the server):

```json
{
  "<paste-SHA256-here>": {
    "subject": "watchfloor",
    "role": "admin"
  }
}
```

Point the Community process at it and restart:

```bash
export ZYVOR_API_TOKENS_FILE=/path/to/api-tokens.json
# docker: mount the file and set the env on the container
```

Keep the raw `TOKEN=` value for Watchfloor’s “Add target” form. Details:
[Enterprise v2 — service tokens](../enterprise-v2.md#service-tokens) ·
[MCP server mint steps](../mcp-server.md).

Roles: `viewer` | `operator` | `admin` (admin needed to create engagements).

---

## 4. Install Argus Enterprise (Watchfloor) — after Community is up

### What the package already includes

| Piece | Bundled? |
|-------|----------|
| Watchfloor app image | Yes (`docker load`) |
| Helm chart | Yes |
| Postgres for Watchfloor | Yes (Helm `postgresql.enabled`, default on) |
| Keycloak + demo users | Yes (Helm `keycloak.enabled`, default on) |
| Community `argus serve` | **No** — install §2 first |
| Your app under test | **No** — set `ZYVOR_BASE_URL` / target `app_url` |

### Download trial / user package

Public eval builds are published on the OSS repo’s releases (tag shape
`v*-trial`). The archive **must include a signed `trial.token`** (Ed25519 JWT).

- [v1.1.1-ent-trial](https://github.com/zyvorai/argus/releases/tag/v1.1.1-ent-trial)

```bash
# Example — use the asset names from the latest Enterprise trial release
tar xzf argus-enterprise-*.tar.gz && cd argus-enterprise-*
ls -l trial.token
# Read GETTING-STARTED.md then INSTALL.md
# Keep trial.token, or: export ARGUS_TRIAL_TOKEN="$(cat trial.token)"
```

After expiry email **sales@zyvor.dev** for a renewed JWT (see `LICENSING.md` /
`AFTER-TRIAL.md` in the package).

### Helm (typical)

```bash
docker load -i argus-enterprise-image-*.tar   # or k3s ctr images import

helm install argus charts/argus-enterprise-*.tgz \
  --namespace argus-enterprise --create-namespace \
  --set appUrl=https://argus.example.com \
  --set keycloak.externalUrl=https://sso.example.com \
  --set keycloak.ingress.enabled=true \
  --set keycloak.ingress.className=nginx \
  --set keycloak.ingress.host=sso.example.com \
  --set-file license.token=./trial.token
```

No IdP? `--set keycloak.enabled=false --set localAuth.enabled=true`.

Full flags, demo logins (`demo`/`demo`, `ssouser`/`Sso@321`), claim token:
[Enterprise SSO](enterprise-sso.md) · package `INSTALL.md` / `GETTING-STARTED.md`.

### Wire the Community target in Watchfloor

After claim + sign-in → **Targets → Add**:

| Field | Example |
|-------|---------|
| `base_url` | `http://<host>:8080` (OSS API origin) |
| `app_url` | `https://myapp.example.com` (site under test) |
| Token | Raw `TOKEN=` from §3 |

Then run **Smoke** from Watchfloor. Success = job in Activity / Runs.

---

## 5. Optional packages

| Package | When |
|---------|------|
| **Ingress controller** (e.g. nginx) | Real HTTPS hostnames for Watchfloor + Keycloak |
| **cert-manager / Let’s Encrypt** | Trusted TLS instead of self-signed |
| **External Postgres / IdP** | Production HA or corporate SSO (`postgresql.enabled=false`, `keycloak.enabled=false`) |
| **Enterprise v2 overlay** on each OSS target | Durable jobs / SSRF policy — [enterprise-v2.md](../enterprise-v2.md) |
| **Ollama or cloud LLM** | NL create / richer generation on Community |

---

## Stuck?

| Symptom | Likely missing package / step |
|---------|--------------------------------|
| `docker: command not found` | §1 Docker |
| `helm: command not found` | §1 Helm |
| Watchfloor installs but Targets jobs 502 | §2 Community not running, or wrong `base_url` |
| 401 from OSS when running jobs | §3 token hash not loaded / wrong raw token |
| No SSO button | Keycloak/OIDC not enabled — [enterprise-sso.md](enterprise-sso.md) |
| `make install` fails on browsers | Node 20+; re-run `npx playwright install` |

Support: Community [GitHub Issues](https://github.com/zyvorai/argus/issues) ·
Watchfloor **sales@zyvor.dev**

## Operate from the console (UX)

1. Open this route from the nav or command palette and wait for live API data.
2. Use filters/search when present; drill into a row for detail.
3. For mutating actions: confirm role gates and impact before applying.
4. **Empty / fail:** Check service health, auth, and that required CRDs/backends for this domain are installed.
5. **Success:** Live data loads; created/updated objects appear without error toasts.

