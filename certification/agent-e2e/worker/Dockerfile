FROM python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt \
    && groupadd --gid 10001 honua-agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /workspace honua-agent \
    && mkdir -p /workspace /journal \
    && chown 10001:10001 /workspace /journal
COPY agent_e2e_errors.py agent_e2e_worker.py agent_e2e_container.py agent_e2e_container_entry.py /app/
COPY agent_e2e_mcp.py agent_e2e_mcp_runner.py agent_e2e_mcp_entry.py /app/
USER 10001:10001
WORKDIR /workspace
VOLUME ["/workspace", "/journal"]
CMD ["python", "-I", "/app/agent_e2e_container_entry.py"]
