.DEFAULT_GOAL := help
.PHONY: help setup lint test licenses test-docker test-live dev dev-images-env images tf-check eval

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

licenses: ## License gate: no GPL/AGPL in shipped Python (runtime) or npm (non-dev) dependencies
	$(UV) export --frozen --no-dev --no-hashes --no-emit-project --no-header --no-annotate \
	  | $(UV) run python scripts/license_gate.py --python-requirements - --npm-lock web/package-lock.json

test-docker: ## Tests on the built images (make images first; one test downloads from PyPI)
	$(UV) run pytest -m docker tests/images

test-live: ## Real Token Factory / Tavily / Nebius calls: costs money, ask first
	@echo "not implemented yet (A-20)"; exit 1

dev: ## Migrations + uvicorn --reload on :8000 with P2P_ENV=dev and .env (local artifacts, no jobs run)
	set -a; [ ! -f .env ] || . ./.env; set +a; export P2P_ENV=dev; \
	$(UV) run alembic upgrade head && \
	$(UV) run uvicorn app.main:app --reload --reload-dir app --port 8000

dev-images-env: ## Print .env lines that point dev jobs (P2P_JOB_BACKEND=docker) at the local images by id
	@echo "P2P_JOB_BACKEND=docker"
	@echo "P2P_PROVISIONER_IMAGE=local/p2p-provisioner@$$($(DOCKER) image inspect --format '{{.Id}}' p2p-provisioner:$(IMAGE_TAG))"
	@echo "P2P_RUNNER_CPU_IMAGE=local/p2p-runner-cpu@$$($(DOCKER) image inspect --format '{{.Id}}' p2p-runner-cpu:$(IMAGE_TAG))"
	@echo "P2P_RUNNER_IMAGE=local/p2p-runner-cpu@$$($(DOCKER) image inspect --format '{{.Id}}' p2p-runner-cpu:$(IMAGE_TAG))"

images: ## Build api, runner, runner-cpu, provisioner images locally (tag $(IMAGE_TAG)) + size check
	$(DOCKER) build --platform linux/amd64 -f images/runner-cpu/Dockerfile -t p2p-runner-cpu:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/provisioner/Dockerfile -t p2p-provisioner:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/api/Dockerfile \
	  --build-arg BUILD_COMMIT=$$(git rev-parse HEAD) -t p2p-api:$(IMAGE_TAG) .
	$(DOCKER) build --platform linux/amd64 -f images/runner/Dockerfile -t p2p-runner:$(IMAGE_TAG) .
	bash scripts/check_image_size.sh $(IMAGE_TAG)

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
