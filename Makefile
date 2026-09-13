# Wake. `make install && make test`

PY      ?= .venv/bin/python
VENV_PY ?= python3.12          # NOT python3: that is 3.6 on some machines

.PHONY: help install test serve demo bench clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

install: ## .venv with the test tools, including the real OpenTelemetry SDK
	$(VENV_PY) -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt

test: ## decoding, stitching, storage, charts, and the real exporter over HTTP
	$(PY) -m pytest -q

serve: ## collector and pages on http://127.0.0.1:4318
	$(PY) -m wake.cli serve

demo: ## send synthetic multi-service traffic to a running collector
	$(PY) -m wake.cli demo --count 400

bench: ## ingest rate, sustained storage rate, and memory per span
	$(PY) -m bench.ingest --spans 200000
	$(PY) -m bench.memory --spans 100000

clean:
	rm -rf .venv .pytest_cache .run bench/.run **/__pycache__ *.db *.db-wal *.db-shm
