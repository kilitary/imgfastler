IMAGE ?= imgfastler/imgfastler
TAG ?= latest

PYTHON ?= .venv/bin/python
TEST_PORT ?= 8099

BUILDER = buildx-multi-arch

INFO_COLOR = \033[0;36m
NO_COLOR   = \033[m

help: ## Show this help
	@echo Please specify a build target. The choices are:
	@grep -E '^[0-9a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "$(INFO_COLOR)%-30s$(NO_COLOR) %s\n", $$1, $$2}'

build-extension: ## Build the extension image
	docker build --tag=$(IMAGE):$(TAG) .

install-extension: build-extension ## Install the extension in Docker Desktop
	docker extension install $(IMAGE):$(TAG)

update-extension: build-extension ## Update an already installed extension
	docker extension update $(IMAGE):$(TAG)

uninstall-extension: ## Remove the extension from Docker Desktop
	docker extension rm $(IMAGE):$(TAG)

inspect-extension: ## Show what Docker Desktop knows about the extension
	docker extension inspect $(IMAGE):$(TAG)

run-backend: ## Run the backend locally on http://127.0.0.1:$(TEST_PORT) (needs a local Docker socket)
	TEST_PORT=$(TEST_PORT) $(PYTHON) backend/server.py

run-backend-docker: ## Run the backend in Docker on http://127.0.0.1:$(TEST_PORT) (use this if local venv has issues)
	docker compose -f docker-compose.dev.yaml up --build -d
	@echo "Backend running on http://127.0.0.1:$(TEST_PORT)"
	@echo "Stop with: docker compose -f docker-compose.dev.yaml down"

stop-backend-docker: ## Stop the Docker backend
	docker compose -f docker-compose.dev.yaml down

dev-ui: ## Serve ui/ on http://127.0.0.1:8000 for browser development (no-cache, avoids stale app.js)
	cd ui && $(PYTHON) $(CURDIR)/scripts/dev_ui_server.py 8000 .

test-backend: ## Smoke test the API served by `make run-backend` (uses python3 only)
	python3 scripts/smoke.py http://127.0.0.1:$(TEST_PORT)

check: ## Syntax check backend + frontend
	$(PYTHON) -m py_compile backend/server.py
	@command -v node >/dev/null && node --check ui/app.js || true
	@$(PYTHON) -m json.tool metadata.json > /dev/null && echo "metadata.json: OK"

test: ## Run unit tests (backend/tests, no Docker daemon needed)
	@if $(PYTHON) -c "import pydantic_core, fastapi, docker, httpx" 2>/dev/null; then \
		$(PYTHON) -m pytest backend/tests; \
	elif [ -x "$(HOME)/.venvs/imgfastler/bin/python" ]; then \
		echo "note: project .venv cannot import compiled extensions (see SETUP.md);" \
			"using $(HOME)/.venvs/imgfastler instead"; \
		$(HOME)/.venvs/imgfastler/bin/python -m pytest backend/tests; \
	else \
		echo "error: $(PYTHON) cannot import the backend dependencies." >&2; \
		echo "  On restricted mounts (noexec) create a dev venv outside the project:" >&2; \
		echo "  python3 -m venv --system-site-packages ~/.venvs/imgfastler" >&2; \
		echo "  ~/.venvs/imgfastler/bin/pip install pytest docker httpx" >&2; \
		exit 2; \
	fi

lint: ## Run linters on backend code
	@command -v ruff >/dev/null && ruff check backend/ || echo "ruff not installed; install with: pip install ruff"

format: ## Format backend code with ruff
	@command -v ruff >/dev/null && ruff format backend/ || echo "ruff not installed; install with: pip install ruff"

lint-fix: lint format ## Fix linting issues and format code

push-extension: prepare-buildx ## Build & upload the image (tag must not exist yet)
	docker pull $(IMAGE):$(TAG) && echo "Failure: Tag already exists" || \
	docker buildx build --push --builder=$(BUILDER) --platform=linux/amd64,linux/arm64 --tag=$(IMAGE):$(TAG) .

prepare-buildx: ## Create a buildx builder for multi-arch builds, if it does not exist
	docker buildx inspect $(BUILDER) || docker buildx create --name=$(BUILDER) --driver=docker-container --driver-opt=network=host

.PHONY: help build-extension install-extension update-extension uninstall-extension \
	inspect-extension run-backend run-backend-docker stop-backend-docker dev-ui test-backend \
	test check lint format lint-fix push-extension prepare-buildx
