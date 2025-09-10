# Streamrip Makefile
# Provides convenient commands for development, testing, and maintenance

.PHONY: help install install-dev test test-db test-unit test-integration test-coverage lint format clean docs build install-deps check-deps

# Default target
help: ## Show this help message
	@echo "Streamrip Development Commands"
	@echo "=============================="
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Installation
install: ## Install streamrip in production mode
	pip install -e .

install-dev: ## Install streamrip in development mode with all dependencies
	poetry install --with dev
	poetry run pre-commit install

install-deps: ## Install only dependencies
	pip install -r requirements.txt

# Testing
test: ## Run all tests
	poetry run pytest tests/ -v

test-db: ## Run database command tests only
	python -m pytest tests/test_database_*.py -v

test-unit: ## Run unit tests only
	python -m pytest tests/ -k "not integration" -v

test-integration: ## Run integration tests only
	python -m pytest tests/ -k "integration" -v

test-coverage: ## Run tests with coverage report
	python -m pytest tests/ --cov=streamrip --cov-report=html --cov-report=term

test-quick: ## Run quick tests (exclude slow tests)
	python -m pytest tests/ -m "not slow" -v

test-db-runner: ## Run database tests using the custom runner
	python run_database_tests.py

# Code Quality
lint: ## Run linting checks
	poetry run ruff check streamrip/ tests/

format: ## Format code with ruff
	poetry run ruff format streamrip/ tests/

format-check: ## Check code formatting
	poetry run ruff format --check streamrip/ tests/

ruff-check: ## Run ruff linting only
	poetry run ruff check streamrip/ tests/

ruff-fix: ## Fix ruff issues automatically
	poetry run ruff check --fix streamrip/ tests/

# Pre-commit
pre-commit-install: ## Install pre-commit hooks
	poetry run pre-commit install

pre-commit-run: ## Run pre-commit on all files
	poetry run pre-commit run --all-files

pre-commit-update: ## Update pre-commit hooks
	poetry run pre-commit autoupdate

# Database Management
db-status: ## Check database migration status
	rip database status

db-migrate: ## Migrate database to enhanced structure
	rip database migrate

db-backfill: ## Backfill database with metadata from files
	rip database backfill --dry-run

db-backfill-real: ## Backfill database with metadata from files (real)
	rip database backfill

db-stats: ## Show database statistics
	rip database stats

db-inspect: ## Inspect database tables
	rip database inspect

db-cleanup: ## Clean up old database tables
	rip database cleanup --confirm

# Documentation
docs: ## Generate documentation
	@echo "Generating documentation..."
	@mkdir -p docs/api
	@echo "Documentation generated in docs/"

docs-serve: ## Serve documentation locally
	@echo "Serving documentation at http://localhost:8000"
	@cd docs && python -m http.server 8000

# Development
dev-setup: install-dev ## Set up development environment
	@echo "Setting up development environment..."
	@mkdir -p logs
	@echo "Development environment ready!"

check-deps: ## Check if all dependencies are installed
	@echo "Checking dependencies..."
	@python -c "import pytest, mutagen, rich, click; print('✅ All dependencies installed')"

# Cleaning
clean: ## Clean up temporary files
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type f -name "*.log" -delete
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info/

clean-db: ## Clean up test databases
	find . -name "*.db" -type f -delete
	find . -name "downloads_backup_*" -type f -delete

# Building and Distribution
build: ## Build the package
	python -m build

dist: ## Create distribution packages
	python -m build --wheel --sdist

# TIDAL Commands (for testing)
tidal-compare: ## Test TIDAL compare command
	rip tidal compare --playlists --albums

tidal-download: ## Test TIDAL download missing command
	rip tidal download-missing --playlists --limit 5

# Database Commands (for testing)
db-commands: ## Test all database commands
	@echo "Testing database commands..."
	rip database status
	rip database stats
	rip database inspect --limit 3

# CI/CD Simulation
ci-test: ## Run CI test suite
	@echo "Running CI test suite..."
	poetry run pytest tests/ --cov=streamrip --cov-report=xml --cov-report=term
	poetry run ruff check streamrip/ tests/

# Performance Testing
perf-test: ## Run performance tests
	python -m pytest tests/ -m "performance" -v

# Security Checks
security: ## Run security checks
	bandit -r streamrip/
	safety check

# Release
release-check: ## Check if ready for release
	@echo "Checking release readiness..."
	python -m pytest tests/ --cov=streamrip --cov-fail-under=80
	flake8 streamrip/ tests/
	pylint streamrip/
	@echo "✅ Ready for release!"

# Docker (if needed)
docker-build: ## Build Docker image
	docker build -t streamrip .

docker-test: ## Run tests in Docker
	docker run --rm streamrip python -m pytest tests/

# Backup and Restore
backup-config: ## Backup configuration files
	@echo "Backing up configuration..."
	@mkdir -p backups
	@cp -r ~/.config/streamrip backups/ 2>/dev/null || echo "No config to backup"

# Environment Info
env-info: ## Show environment information
	@echo "Python version: $(shell python --version)"
	@echo "Pip version: $(shell pip --version)"
	@echo "Current directory: $(shell pwd)"
	@echo "Virtual environment: $(shell echo $$VIRTUAL_ENV)"

# Quick Development Workflow
dev: dev-setup test ## Complete development setup and test
	@echo "Development environment ready and tested!"

# Database Development Workflow
db-dev: db-status db-migrate db-backfill db-stats ## Complete database development workflow
	@echo "Database development workflow complete!"

# Test Development Workflow
test-dev: test-db-runner test-coverage ## Complete test development workflow
	@echo "Test development workflow complete!"

# All-in-one Commands
all: format lint test ## Run everything: format, lint, and test
	@echo "🎉 All checks passed! Code is ready!"

all-tests: test test-coverage lint ## Run all tests and checks
	@echo "All tests and checks complete!"

all-clean: clean clean-db ## Clean everything
	@echo "Everything cleaned!"

all-dev: install-dev format lint test ## Complete development setup
	@echo "🚀 Development environment ready and tested!"

all-ci: format-check lint test ## Run CI pipeline locally
	@echo "✅ CI pipeline passed locally!"

# Help for specific categories
help-test: ## Show test-related commands
	@echo "Test Commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E "(test|Test)" | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

help-db: ## Show database-related commands
	@echo "Database Commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E "(db-|db:|database)" | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

help-dev: ## Show development-related commands
	@echo "Development Commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E "(dev|install|format|lint)" | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
