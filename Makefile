.DEFAULT_GOAL := help
.PHONY: help setup lint test test-live dev images tf-check eval

UV ?= uv
NPM ?= npm
SHELL_SCRIPTS := $(wildcard spikes/*.sh deploy/*.sh scripts/*.sh)

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

setup: ## Install Python and web dependencies, install the pre-commit hook
	$(UV) sync --locked
	$(UV) run pre-commit install
	cd web && $(NPM) ci

lint: ## ruff, ruff format --check, mypy --strict app, shellcheck, actionlint, oxlint, tsc
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy app
ifneq ($(strip $(SHELL_SCRIPTS)),)
	$(UV) run shellcheck $(SHELL_SCRIPTS)
endif
	$(UV) run actionlint
	$(UV) run actionlint spikes/s7_oidc.yml
	cd web && $(NPM) run lint && $(NPM) run typecheck

test: ## pytest (no live tests) + vitest
	$(UV) run pytest -m "not live" --cov=app/verdict --cov-fail-under=100
	cd web && $(NPM) test

test-live: ## Real Token Factory / Tavily / Nebius calls: costs money, ask first
	@echo "not implemented yet (A-20)"; exit 1

dev: ## MinIO + uvicorn --reload with P2P_ENV=dev
	@echo "not implemented yet (A-13)"; exit 1

images: ## Build api, runner, runner-cpu, provisioner images locally
	@echo "not implemented yet (A-06)"; exit 1

TERRAFORM ?= terraform
TFLINT ?= tflint
CHECKOV ?= $(UV) tool run --from checkov==3.3.22 checkov
TF_DIR := infra/terraform

TF_ENVS := prod prod-iam

tf-check: ## terraform fmt -check, validate, tflint, checkov
	$(TERRAFORM) -chdir=$(TF_DIR) fmt -check -recursive -diff
	for env in $(TF_ENVS); do \
	  $(TERRAFORM) -chdir=$(TF_DIR)/envs/$$env init -backend=false -input=false -lockfile=readonly && \
	  $(TERRAFORM) -chdir=$(TF_DIR)/envs/$$env validate || exit 1; \
	done
	$(TFLINT) --chdir=$(TF_DIR) --recursive --config=$(CURDIR)/.tflint.hcl
	$(CHECKOV) --config-file $(TF_DIR)/.checkov.yaml

eval: ## ReproBench-mini: costs money, ask first
	@echo "not implemented yet (A-30)"; exit 1
