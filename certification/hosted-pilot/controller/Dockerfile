FROM python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ARG RELEASE_SOURCE_SHA
ARG RELEASE_TREE_ID
ARG RELEASE_ARCHIVE_SHA256
LABEL org.opencontainers.image.source="https://github.com/honua-io/honua-release" \
      org.opencontainers.image.revision="${RELEASE_SOURCE_SHA}" \
      io.honua.release.tree="${RELEASE_TREE_ID}" \
      io.honua.release.archive-sha256="${RELEASE_ARCHIVE_SHA256}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 honua-controller \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /work honua-controller \
    && mkdir -p /work && chown 10001:10001 /work
# Context is an exact git archive, never an ambient workspace or worker context.
COPY . /controller/
WORKDIR /controller
RUN python -m pip install --no-cache-dir --require-hashes -r certification/hosted-pilot/controller/requirements.txt \
    && git init --quiet && git add --force . \
    && test "$(git write-tree)" = "$RELEASE_TREE_ID" \
    && git config --system --add safe.directory /controller \
    && python tools/hosted_pilot/controller_source.py record \
       --source-sha "$RELEASE_SOURCE_SHA" --tree-id "$RELEASE_TREE_ID" \
       --archive-sha256 "$RELEASE_ARCHIVE_SHA256" --receipt /opt/honua-controller-source.json \
    && chmod -R a-w /controller
USER 10001:10001
CMD ["python", "-c", "raise SystemExit('An explicit reviewed controller command and frozen bundle are required.')"]
