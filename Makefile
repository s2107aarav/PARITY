PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
PARITY := $(VENV)/bin/parity

.PHONY: setup test check demo demo-live contracts
setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -r requirements.lock
	$(PY) -m pip install --no-deps -e .

test:
	$(PY) -m pytest -q

check:
	$(VENV)/bin/ruff check parity apps scripts tests
	$(VENV)/bin/ruff format --check parity apps scripts tests
	$(VENV)/bin/mypy parity apps

demo:
	$(PARITY) demo --artifact commerce-live-002

demo-live:
	$(PARITY) generate --old contracts/commerce/v1.json --new contracts/commerce/v2.json --provider codex

contracts:
	$(PY) scripts/export_contracts.py
