PYTHON ?= python3
VENV ?= .venv
CONFIG ?= config/config.toml
SECRETS ?= config/credentials.env

.PHONY: bootstrap check history dashboard first-run test

bootstrap:
	./scripts/bootstrap-local.sh "$(PYTHON)" "$(VENV)"

check:
	"$(VENV)/bin/perp-trade-history" --config "$(CONFIG)" --secrets "$(SECRETS)" check-config

history: check
	"$(VENV)/bin/perp-trade-history" --config "$(CONFIG)" --secrets "$(SECRETS)" collect --full

dashboard:
	"$(VENV)/bin/perp-trade-history-dashboard" --config "$(CONFIG)"

first-run: history dashboard

test:
	"$(VENV)/bin/python" -m pytest
