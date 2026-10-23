# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
.PHONY: install test generate run serve docker lint regression create rust \
        k8s-check k8s-validate k8s-validate-cluster k8s-apply k8s-delete \
        desktop-dev desktop-build desktop-build-signed desktop-check

# Bare `pip` on macOS is often the Xcode CLT interpreter (3.9.6), which
# fails requires-python = ">=3.11". Prefer PYTHON=..., else the first
# 3.11+ on PATH whose stdlib works (Homebrew CPythons can fail ensurepip
# with a libexpat symbol error). If none, fall back to `uv`.
ifndef PYTHON
PYTHON := $(shell \
	for c in python3.12 python3.13 python3.11 python3; do \
		command -v $$c >/dev/null 2>&1 || continue; \
		$$c -c 'import sys; from xml.parsers import expat; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
			2>/dev/null && echo $$c && break; \
	done)
endif
VENV ?= .venv

install:
	@set -e; \
	if [ -n "$(PYTHON)" ]; then \
		echo "Creating $(VENV) with $(PYTHON) ($$($(PYTHON) --version 2>&1))"; \
		$(PYTHON) -m venv $(VENV); \
		$(VENV)/bin/pip install -e ".[dev]"; \
	elif command -v uv >/dev/null 2>&1; then \
		echo "No working system Python >= 3.10; creating $(VENV) with uv (3.12)"; \
		uv venv $(VENV) --python 3.12; \
		uv pip install -e ".[dev]" --python $(VENV)/bin/python; \
	else \
		echo "ERROR: No working Python >= 3.10 found."; \
		echo "  macOS system python is 3.9.6; Homebrew pythons may be broken (libexpat)."; \
		echo "  Install one:  uv python install 3.12   or   brew install python@3.12"; \
		echo "  then:         make install PYTHON=python3.12"; \
		exit 1; \
	fi
	npm install
	npx playwright install --with-deps chromium

test:
	argus test exec

generate:
	argus test generate --spec prompts/examples/vm-create.md

create:
	argus test create "Verify homepage loads and shows product suite"

run:
	argus test run --source local

regression:
	ENABLE_REGRESSION=true argus vision regression

regression-update:
	ENABLE_REGRESSION=true argus vision regression --update-baselines

serve:
	argus serve --port 8080

desktop-dev:
	cd desktop && npm install && npm run tauri dev

desktop-build:
	cd desktop && npm install && npm run tauri build

# Signed + notarized .app/.dmg. Tauri's bundler signs/notarizes automatically
# during `tauri build` when these Apple credentials are present in the
# environment — nothing else to configure. See desktop/README.md.
desktop-build-signed:
	@test -n "$(APPLE_SIGNING_IDENTITY)" || { echo "❌  APPLE_SIGNING_IDENTITY not set — see desktop/README.md#code-signing--notarization"; exit 1; }
	@test -n "$(APPLE_ID)" || { echo "❌  APPLE_ID not set — see desktop/README.md#code-signing--notarization"; exit 1; }
	@test -n "$(APPLE_PASSWORD)" || { echo "❌  APPLE_PASSWORD not set — see desktop/README.md#code-signing--notarization"; exit 1; }
	@test -n "$(APPLE_TEAM_ID)" || { echo "❌  APPLE_TEAM_ID not set — see desktop/README.md#code-signing--notarization"; exit 1; }
	cd desktop && npm install && npm run tauri build

desktop-check:
	cd desktop && npm install && cd src-tauri && cargo check && cargo test

rust:
	cd rust && cargo build --release

docker:
	docker build -f docker/Dockerfile -t zyvor-argus .
	docker run --env-file .env zyvor-argus

lint:
	ruff check orchestrator agents github_integration
	npx tsc --noEmit

K8S_DIR := kubernetes

k8s-check:
	@kubectl cluster-info >/dev/null 2>&1 || { \
		echo "ERROR: No Kubernetes cluster is reachable."; \
		echo "  Start a local cluster (minikube, kind, Docker Desktop K8s) or set KUBECONFIG."; \
		echo "  To validate manifests without a cluster, run: make k8s-validate"; \
		exit 1; \
	}
	@echo "Kubernetes cluster is reachable."

k8s-validate:
	python3 scripts/validate_k8s_manifests.py

k8s-validate-cluster: k8s-check
	kubectl apply --dry-run=server -f $(K8S_DIR)/configmap.yaml -f $(K8S_DIR)/secret.yaml -f $(K8S_DIR)/rbac.yaml -f $(K8S_DIR)/pvc.yaml -f $(K8S_DIR)/deployment.yaml -f $(K8S_DIR)/service.yaml -f $(K8S_DIR)/cronjob.yaml

k8s-apply: k8s-check
	kubectl apply -f $(K8S_DIR)/configmap.yaml -f $(K8S_DIR)/secret.yaml -f $(K8S_DIR)/rbac.yaml -f $(K8S_DIR)/pvc.yaml -f $(K8S_DIR)/deployment.yaml -f $(K8S_DIR)/service.yaml -f $(K8S_DIR)/cronjob.yaml

k8s-delete: k8s-check
	kubectl delete -f $(K8S_DIR)/configmap.yaml -f $(K8S_DIR)/secret.yaml -f $(K8S_DIR)/rbac.yaml -f $(K8S_DIR)/pvc.yaml -f $(K8S_DIR)/deployment.yaml -f $(K8S_DIR)/service.yaml -f $(K8S_DIR)/cronjob.yaml --ignore-not-found
