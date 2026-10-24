FROM mcr.microsoft.com/playwright:v1.61.1-noble@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48 AS canonical-browsers
FROM node:22.23.3-bookworm-slim@sha256:3ed98443660ff48413b5af19ecefce3b9d50845a901a43420d47cc63a9fddbe9 AS node-runtime
WORKDIR /opt/honua-render
COPY certification/hosted-pilot/controller/browser/package.json certification/hosted-pilot/controller/browser/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d AS browser-runtime
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /opt/honua-render /opt/honua-render
COPY certification/hosted-pilot/controller/browser/copy-canonical.mjs /opt/honua-render/copy-canonical.mjs
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/honua-render/browsers \
    HONUA_RENDER_PLAYWRIGHT=/opt/honua-render/node_modules/playwright-core/index.mjs
RUN --mount=from=canonical-browsers,source=/ms-playwright,target=/canonical-browsers,ro \
    test "$(node --version)" = v22.23.3 \
    && node /opt/honua-render/node_modules/playwright-core/cli.js install-deps chromium \
    && node /opt/honua-render/copy-canonical.mjs \
    && chmod -R a-w /opt/honua-render \
    && rm -rf /var/lib/apt/lists/*

FROM browser-runtime AS controller-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 honua-controller \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /work honua-controller \
    && mkdir -p /work && chown 10001:10001 /work
COPY certification/hosted-pilot/controller/requirements.txt /opt/honua-controller-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/honua-controller-requirements.txt

FROM controller-runtime AS controller
ARG RELEASE_SOURCE_SHA
ARG RELEASE_TREE_ID
ARG RELEASE_ARCHIVE_SHA256
LABEL org.opencontainers.image.source="https://github.com/honua-io/honua-release" \
      org.opencontainers.image.revision="${RELEASE_SOURCE_SHA}" \
      io.honua.release.tree="${RELEASE_TREE_ID}" \
      io.honua.release.archive-sha256="${RELEASE_ARCHIVE_SHA256}"
# Context is an exact git archive, never an ambient workspace or worker context.
COPY . /controller/
WORKDIR /controller
RUN git init --quiet && git add --force . \
    && test "$(git write-tree)" = "$RELEASE_TREE_ID" \
    && git config --system --add safe.directory /controller \
    && python tools/hosted_pilot/controller_source.py record \
       --source-sha "$RELEASE_SOURCE_SHA" --tree-id "$RELEASE_TREE_ID" \
       --archive-sha256 "$RELEASE_ARCHIVE_SHA256" --receipt /opt/honua-controller-source.json \
    && chmod -R a-w /controller
VOLUME ["/work"]
USER 10001:10001
CMD ["python", "-c", "raise SystemExit('An explicit reviewed controller command and frozen bundle are required.')"]
