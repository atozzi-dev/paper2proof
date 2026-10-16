.DEFAULT_GOAL := help
.PHONY: help setup lint test test-docker test-live dev images tf-check eval

UV ?= uv
NPM ?= npm
DOCKER ?= docker
IMAGE_TAG ?= dev
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
	$(UV) run pytest -m "not live and not docker" --cov=app/verdict --cov-fail-under=100
	cd web && $(NPM) test

test-docker: ## Tests on the built images (make images first; one test downloads from PyPI)
	$(UV) run pytest -m docker tests/images

test-live: ## Real Token Factory / Tavily / Nebius calls: costs money, ask first
	@echo "not implemented yet (A-20)"; exit 1

dev: ## MinIO + uvicorn --reload with P2P_ENV=dev
	@echo "not implemented yet (A-13)"; exit 1

images: ## Build api, runner, runner-cpu, provisioner images locally (tag $(IMAGE_TAG)) + size check
	$(DOCKER) build --platform linux/amd64 -f images/runner-cpu/Dockerfile -t p2p-runner-cpu:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/provisioner/Dockerfile -t p2p-provisioner:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/api/Dockerfile -t p2p-api:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/runner/Dockerfile -t p2p-runner:$(IMAGE_TAG) .
	bash scripts/check_image_size.sh $(IMAGE_TAG)

tf-check: ## terraform fmt -check, validate, tflint, checkov
	@echo "not implemented yet (A-03)"; exit 1

eval: ## ReproBench-mini: costs money, ask first
	@echo "not implemented yet (A-30)"; exit 1
