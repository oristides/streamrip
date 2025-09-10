# Streamrip Test Structure

This document describes the test structure and organization for the `streamrip` project.

## Test Organization

### Directory Structure
```
streamrip/
├── tests/                          # Main test directory
│   ├── fixtures/                   # Test fixtures and utilities
│   │   ├── clients.py             # Mock client fixtures
│   │   ├── config.py              # Mock configuration fixtures
│   │   └── util.py                # General test utilities
│   ├── test_database_commands.py  # Database command tests
│   ├── test_database_utils.py     # Database utility tests
│   ├── test_tidal_commands.py     # TIDAL command tests
│   └── ...                        # Other existing tests
├── pytest.ini                     # Pytest configuration
└── run_database_tests.py          # Test runner script
```

### Test Categories

#### 1. Database Command Tests (`test_database_commands.py`)
Tests for the new database management commands:
- `database_status` - Check migration status
- `database_migrate` - Migrate to enhanced schema
- `database_rollback` - Restore from backup
- `database_verify` - Verify migration integrity
- `database_cleanup` - Remove old tables
- `database_inspect` - Inspect table contents
- `database_query` - Execute custom SQL
- `database_stats` - Show statistics
- `database_backfill` - Extract metadata from files

#### 2. Database Utility Tests (`test_database_utils.py`)
Helper classes and utilities for database testing:
- `DatabaseTestHelper` - Database creation and cleanup
- `MockConfig` - Mock configuration objects
- `MockContext` - Mock click context objects

#### 3. TIDAL Command Tests (`test_tidal_commands.py`)
Tests for TIDAL-specific commands:
- `tidal_compare` - Compare collections with downloads
- `tidal_download_missing` - Download missing tracks
- TIDAL client method testing

## Running Tests

### Individual Test Files
```bash
# Run specific test file
python -m pytest tests/test_database_commands.py -v

# Run with coverage
python -m pytest tests/test_database_commands.py --cov=streamrip.db

# Run specific test class
python -m pytest tests/test_database_commands.py::TestDatabaseCommands -v

# Run specific test method
python -m pytest tests/test_database_commands.py::TestDatabaseCommands::test_database_status_no_db -v
```

### All Database Tests
```bash
# Run the test runner script
python run_database_tests.py

# Or run all tests
python -m pytest tests/ -v
```

### Test Configuration
The `pytest.ini` file configures:
- Test discovery patterns
- Output verbosity
- Warning filters
- Markers for test categorization

## Test Fixtures

### Database Fixtures
```python
@pytest.fixture
def temp_db_path():
    """Create a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)
```

### Mock Fixtures
```python
@pytest.fixture
def mock_config(temp_db_path):
    """Create a mock configuration object."""
    config = Mock()
    config.session.database.downloads_enabled = True
    config.session.database.downloads_path = temp_db_path
    return config
```

## Test Patterns

### 1. Database Command Testing
```python
def test_database_command(mock_ctx, temp_db_path):
    """Test a database command."""
    # Setup
    mock_ctx.obj["config"].session.database.downloads_path = temp_db_path

    # Execute
    with patch('streamrip.rip.cli.console') as mock_console:
        database_command(mock_ctx)

        # Verify
        mock_console.print.assert_called()
```

### 2. Async Command Testing
```python
@patch('streamrip.rip.cli.Main')
def test_async_command(mock_main_class, mock_ctx):
    """Test an async command."""
    # Setup mocks
    mock_main = Mock()
    mock_client = AsyncMock()
    mock_main.get_logged_in_client.return_value = mock_client
    mock_main_class.return_value = mock_main

    # Execute
    async_command(mock_ctx)

    # Verify async method was called
    mock_client.async_method.assert_called_once()
```

### 3. Database Migration Testing
```python
def test_migration_success(temp_db_path):
    """Test successful database migration."""
    # Create old format database
    DatabaseTestHelper.create_old_format_db(temp_db_path)

    # Migrate
    migration = DatabaseMigration(temp_db_path)
    result = migration.migrate_database()

    # Verify
    assert result is True
    assert DatabaseTestHelper.table_exists(temp_db_path, 'downloads_enhanced')
```

## Test Coverage

### Current Coverage Areas
- ✅ Database command execution
- ✅ Database migration process
- ✅ Error handling
- ✅ Mock interactions
- ✅ File I/O operations
- ✅ SQLite operations

### Areas Needing Coverage
- ⚠️ Integration tests with real databases
- ⚠️ Performance testing for large datasets
- ⚠️ Edge cases in metadata extraction
- ⚠️ Network error handling in TIDAL commands

## Best Practices

### 1. Test Isolation
- Each test uses its own temporary database
- Tests don't depend on external state
- Clean up resources after each test

### 2. Mock Usage
- Mock external dependencies (file system, network)
- Use realistic mock data
- Verify mock interactions

### 3. Error Testing
- Test both success and failure paths
- Test edge cases and error conditions
- Verify error messages are appropriate

### 4. Async Testing
- Use `pytest-asyncio` for async tests
- Mock async dependencies properly
- Test both sync and async code paths

## Continuous Integration

### GitHub Actions Integration
```yaml
name: Database Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.12
      - name: Install dependencies
        run: |
          pip install -e .
          pip install pytest pytest-asyncio pytest-mock
      - name: Run database tests
        run: python run_database_tests.py
```

## Debugging Tests

### Verbose Output
```bash
python -m pytest tests/test_database_commands.py -v -s
```

### Debug Mode
```bash
python -m pytest tests/test_database_commands.py --pdb
```

### Coverage Report
```bash
python -m pytest tests/ --cov=streamrip --cov-report=html
```

## Future Improvements

### 1. Test Organization
- [ ] Group tests by functionality
- [ ] Add integration test suite
- [ ] Create performance test suite

### 2. Test Coverage
- [ ] Add property-based testing
- [ ] Add mutation testing
- [ ] Increase edge case coverage

### 3. Test Infrastructure
- [ ] Add test data factories
- [ ] Create test database templates
- [ ] Add automated test generation

This test structure provides comprehensive coverage for the new database management commands while maintaining good organization and maintainability.
