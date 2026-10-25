# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
# syntax=docker/dockerfile:1

FROM node:25-bookworm AS node-base
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
RUN npx playwright install --with-deps chromium

FROM python:3.15-slim-bookworm AS python-base
WORKDIR /app
COPY pyproject.toml ./
COPY orchestrator/ ./orchestrator/
COPY agents/ ./agents/
COPY github_integration/ ./github_integration/
COPY prompts/ ./prompts/
COPY templates/ ./templates/
COPY playwright/ ./playwright/
COPY tests/ ./tests/
RUN pip install --no-cache-dir -e .

FROM python:3.15-slim-bookworm
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Node runtime — node_modules alone are useless without node/npx binaries
COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=node-base /app/node_modules ./node_modules
COPY --from=node-base /root/.cache/ms-playwright /root/.cache/ms-playwright
# The argus console script and installed packages, not just the source tree
COPY --from=python-base /usr/local/bin /usr/local/bin
COPY --from=python-base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=python-base /app /app
COPY package.json tsconfig.json ./
COPY orchestrator/ ./orchestrator/
COPY agents/ ./agents/
COPY github_integration/ ./github_integration/
COPY prompts/ ./prompts/
COPY templates/ ./templates/
COPY playwright/ ./playwright/
COPY tests/ ./tests/

ENV ZYVOR_BASE_URL=https://zyvor.dev
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
ENV ZYVOR_NO_SANDBOX=true
# Cap Playwright parallelism in containers — default (= CPU count) OOMs a 2-4Gi pod
ENV ZYVOR_PW_WORKERS=2

# Used only by action.yml (entrypoint override) — standalone/K8s/dashboard use is
# unaffected since the default ENTRYPOINT/CMD below don't reference this file.
COPY docker/action-entrypoint.sh /action-entrypoint.sh
RUN chmod +x /action-entrypoint.sh

ENTRYPOINT ["argus"]
CMD ["test", "run", "--source", "local"]
