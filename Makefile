# Every target names the failure it prevents. `make` with no target lists them.
.DEFAULT_GOAL := help
SHELL := /bin/bash

BATFISH_IMAGE ?= batfish/allinone:2026.08.27.3685@sha256:54cb0ed94fd9a3c1ca0985f73e5be479e955cee9b9f6a6799b66be3364fd8e5c
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
VERIFIER_IMAGE ?= ghcr.io/vkrishnabhatk/fabloom-verifier

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Sync the locked env incl. Batfish client (prevents "works on my machine" drift from uv.lock)
	uv sync --frozen --extra batfish --extra eval --extra web --group docs
	uv run pre-commit install

.PHONY: format
format: ## Apply ruff formatting and safe fixes
	uv run ruff format src tests scripts spikes
	uv run ruff check --fix src tests scripts spikes

.PHONY: lint
lint: ## ruff + mypy --strict + plane boundaries (prevents the model grading its own homework)
	uv run ruff format --check src tests scripts spikes
	uv run ruff check src tests scripts spikes
	uv run mypy
	uv run lint-imports

.PHONY: test
test: ## Offline tests with the 80% coverage gate (prevents untested code paths reaching the demo)
	uv run pytest -m "unit or integration" --cov --cov-report=term-missing

.PHONY: test-live
test-live: ## Tests that spend credits or need Sandboxes; needs secrets (nightly/manual only)
	uv run pytest -m "llm or sandbox"

.PHONY: check
check: lint test ## Everything CI's lint and test jobs run

.PHONY: docs
docs: ## Build the docs site strictly (prevents broken links reaching the published docs)
	uv run --group docs mkdocs build --strict

.PHONY: scenarios
scenarios: ## Validate every benchmark scenario compiles (prevents a broken benchmark file)
	uv run fabloom scenarios validate

.PHONY: doctor
doctor: ## Probe every configured model on Token Factory (prevents a retired model ID killing the demo)
	uv run fabloom doctor

.PHONY: bench-plan
bench-plan: ## Show the benchmark matrix and its worst-case spend (prevents an unbudgeted run)
	uv run fabloom bench run --dry-run

.PHONY: serve
serve: ## Run the web demo on :8000 (read-only gallery without a Token Factory key)
	uv run fabloom serve --port 8000

.PHONY: batfish-up
batfish-up: ## Start a local Batfish for the batfish-local verifier backend
	docker run -d --rm --name fabloom-batfish -p 9996:9996 $(BATFISH_IMAGE)
	@echo "Batfish starting on :9996 (takes ~30s)"

.PHONY: batfish-down
batfish-down: ## Stop the local Batfish
	docker stop fabloom-batfish

.PHONY: image-verifier
image-verifier: ## Build the verifier image with GIT_SHA baked in (prevents untraceable sandbox results)
	docker build -f docker/Dockerfile.verifier --build-arg GIT_SHA=$(GIT_SHA) \
		--build-arg BATFISH_IMAGE=$(BATFISH_IMAGE) -t $(VERIFIER_IMAGE):git-$(GIT_SHA) .

.PHONY: image-app
image-app: ## Build the web demo image (prevents a demo that only runs on this laptop)
	docker build -f docker/Dockerfile.app --build-arg GIT_SHA=$(GIT_SHA) \
		-t ghcr.io/vkrishnabhatk/fabloom-app:git-$(GIT_SHA) .

.PHONY: demo-up
demo-up: ## Caddy + app + Batfish via docker compose (FABLOOM_DOMAIN, keys from .env)
	docker compose up -d --build

.PHONY: training-jobs
training-jobs: ## Regenerate the AOT training jobs after changing checks (prevents a cold-JVM image)
	PYTHONPATH=. uv run python scripts/make_training_jobs.py

.PHONY: clean
clean: ## Remove caches and local run directories
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist runs
