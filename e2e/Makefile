# Phase A local-docker E2E seam harness — single entrypoint.
# `make e2e` is the one way to run the seam tier (humans and the release train both use it).

PY ?= python3
COMPOSE := docker compose -f local-docker/docker-compose.yml

.PHONY: e2e e2e-strict deps check test up down logs clean help

help:
	@echo "make deps       - install runner Python deps (PyYAML)"
	@echo "make check      - static gates only: validate compose + compile scenarios (no images needed)"
	@echo "make test       - unit-test the runner verdict logic + parsers (no images needed)"
	@echo "make e2e        - run the local-docker seam scenarios (BLOCKED while pins are placeholders)"
	@echo "make e2e-strict - run with E2E_REQUIRE_REAL=1 (BLOCKED/SKIPPED => FAIL; the real release gate)"
	@echo "make up/down    - bring the composed stack up/down by hand"
	@echo "make clean      - tear down stack + remove gate-report.json"

deps:
	$(PY) -m pip install -r requirements.txt

# Static gates that must be able to FAIL with no images present: compose validity + scenario compile.
check:
	$(COMPOSE) config >/dev/null && echo "compose config: OK"
	$(PY) -m compileall -q run.py runner scenarios

# Unit tests for the runner's verdict logic + parsers. No images needed; runs via pytest if present,
# else the test module's standalone runner. Server-independent, so it runs on every PR.
test:
	$(PY) -m pytest runner/test_runner.py -q || $(PY) runner/test_runner.py

e2e: deps
	$(PY) run.py

e2e-strict: deps
	E2E_REQUIRE_REAL=1 $(PY) run.py

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f

clean: down
	rm -f gate-report.json
