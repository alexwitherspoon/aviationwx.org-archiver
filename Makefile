# AviationWX.org Archiver — Makefile
# Common development and operations commands.

.DEFAULT_GOAL := help
SHELL         := /bin/bash

IMAGE_NAME    ?= aviationwx-archiver
CONFIG_FILE   ?= config/config.yaml

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
.PHONY: help
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
.PHONY: setup
setup: ## Copy example config and install dev dependencies
	@[ -f $(CONFIG_FILE) ] || cp config/config.yaml.example $(CONFIG_FILE)
	@echo "Config: $(CONFIG_FILE)"
	pip install -r requirements.txt -r requirements-dev.txt

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
.PHONY: build
build: ## Build the Docker image
	docker build -t $(IMAGE_NAME) .

.PHONY: up
up: ## Start the archiver container (builds if needed)
	@[ -f $(CONFIG_FILE) ] || (cp config/config.yaml.example $(CONFIG_FILE) && echo "Created $(CONFIG_FILE) from example")
	docker compose up --build -d
	@echo "Web GUI: http://localhost:8080"

.PHONY: down
down: ## Stop and remove the archiver container
	docker compose down

.PHONY: logs
logs: ## Tail container logs
	docker compose logs -f

.PHONY: restart
restart: down up ## Restart the archiver container

# ---------------------------------------------------------------------------
# Development (run locally without Docker)
# ---------------------------------------------------------------------------
.PHONY: dev
dev: ## Run the archiver locally (requires pip install)
	@[ -f $(CONFIG_FILE) ] || cp config/config.yaml.example $(CONFIG_FILE)
	ARCHIVER_CONFIG=$(CONFIG_FILE) python main.py

# ---------------------------------------------------------------------------
# Linting and Formatting
# ---------------------------------------------------------------------------
.PHONY: lint
lint: ## Run ruff linter
	python3 -m ruff check app/ tests/ main.py

.PHONY: format
format: ## Format code with ruff
	python3 -m ruff format app/ tests/ main.py

.PHONY: format-check
format-check: ## Check formatting without modifying
	python3 -m ruff format --check app/ tests/ main.py

.PHONY: test-ci
test-ci: lint format-check test ## Run lint + format check + tests (matches CI)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
.PHONY: test
test: ## Run the test suite
	python3 -m pytest tests/ -v

.PHONY: test-coverage
test-coverage: ## Run tests with coverage report
	python3 -m pytest tests/ --cov=app --cov-report=term-missing -v

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
.PHONY: clean
clean: ## Remove Python cache files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .coverage htmlcov/ .ruff_cache/
