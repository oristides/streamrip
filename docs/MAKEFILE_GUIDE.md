# Streamrip Makefile Guide

This Makefile provides convenient commands for developing, testing, and maintaining the streamrip project.

## Quick Start

```bash
# Show all available commands
make help

# Set up development environment
make dev-setup

# Run all tests
make test

# Run database tests only
make test-db
```

## Command Categories

### 🧪 Testing Commands

| Command | Description |
|---------|-------------|
| `make test` | Run all tests |
| `make test-db` | Run database command tests only |
| `make test-unit` | Run unit tests only |
| `make test-integration` | Run integration tests only |
| `make test-coverage` | Run tests with coverage report |
| `make test-quick` | Run quick tests (exclude slow tests) |
| `make test-db-runner` | Run database tests using custom runner |

### 🗄️ Database Commands

| Command | Description |
|---------|-------------|
| `make db-status` | Check database migration status |
| `make db-migrate` | Migrate database to enhanced structure |
| `make db-backfill` | Backfill database (dry run) |
| `make db-backfill-real` | Backfill database (real) |
| `make db-stats` | Show database statistics |
| `make db-inspect` | Inspect database tables |
| `make db-cleanup` | Clean up old database tables |
| `make db-commands` | Test all database commands |

### 🛠️ Development Commands

| Command | Description |
|---------|-------------|
| `make install` | Install streamrip in production mode |
| `make install-dev` | Install in development mode with all deps |
| `make dev-setup` | Set up development environment |
| `make check-deps` | Check if all dependencies are installed |
| `make format` | Format code with black and isort |
| `make lint` | Run linting checks |
| `make clean` | Clean up temporary files |

### 📚 Documentation Commands

| Command | Description |
|---------|-------------|
| `make docs` | Generate documentation |
| `make docs-serve` | Serve documentation locally |

### 🚀 Build & Release Commands

| Command | Description |
|---------|-------------|
| `make build` | Build the package |
| `make dist` | Create distribution packages |
| `make release-check` | Check if ready for release |

## Workflow Examples

### Complete Development Setup
```bash
make dev-setup    # Install dependencies
make test         # Run all tests
make lint         # Check code quality
```

### Database Development Workflow
```bash
make db-status    # Check current state
make db-migrate   # Migrate if needed
make db-backfill  # Preview backfill
make db-stats     # Check results
```

### Test Development Workflow
```bash
make test-db-runner  # Run database tests
make test-coverage   # Check coverage
make lint            # Check code quality
```

### Pre-Release Checklist
```bash
make release-check  # Run all checks
make clean          # Clean up
make build          # Build package
```

## Advanced Usage

### Custom Test Runs
```bash
# Run specific test file
make test TEST_FILE=tests/test_database_commands.py

# Run with specific pytest options
make test PYTEST_OPTS="-v -k test_migration"
```

### Database Testing
```bash
# Test database commands
make db-commands

# Complete database workflow
make db-dev
```

### Code Quality
```bash
# Format and check
make format
make format-check

# Lint and security
make lint
make security
```

## Environment Variables

You can customize behavior with environment variables:

```bash
# Custom test database path
DB_PATH=/custom/path make db-status

# Custom pytest options
PYTEST_OPTS="-v -x" make test

# Custom coverage threshold
COV_THRESHOLD=90 make test-coverage
```

## Integration with CI/CD

The Makefile is designed to work well with CI/CD systems:

```yaml
# GitHub Actions example
- name: Run Tests
  run: make ci-test

- name: Check Code Quality
  run: make lint

- name: Security Check
  run: make security
```

## Troubleshooting

### Common Issues

1. **"make: command not found"**
   ```bash
   # Install make (Ubuntu/Debian)
   sudo apt-get install make

   # Install make (macOS)
   brew install make
   ```

2. **Permission errors**
   ```bash
   # Make sure you're in the right directory
   cd streamrip

   # Check file permissions
   ls -la Makefile
   ```

3. **Dependencies missing**
   ```bash
   make check-deps
   make install-dev
   ```

### Getting Help

```bash
# Show all commands
make help

# Show specific categories
make help-test
make help-db
make help-dev

# Show environment info
make env-info
```

## Best Practices

### 1. Development Workflow
```bash
# Start development
make dev-setup
make test

# Make changes, then test
make test-quick
make lint

# Before committing
make all-tests
```

### 2. Database Workflow
```bash
# Check status
make db-status

# Migrate if needed
make db-migrate

# Backfill metadata
make db-backfill-real

# Verify
make db-stats
```

### 3. Testing Workflow
```bash
# Quick tests during development
make test-quick

# Full test suite
make test

# Coverage check
make test-coverage
```

## Customization

You can extend the Makefile by adding your own targets:

```makefile
# Add to Makefile
my-custom-test: ## Run my custom test
	@echo "Running custom test..."
	python my_custom_test.py
```

The Makefile provides a solid foundation for streamrip development while being easily extensible for your specific needs.
