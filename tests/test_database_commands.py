"""
Unit tests for database management commands.

Tests the new database commands including migration, inspection, backfill, and query functionality.
"""

import os
import sqlite3
import tempfile
from unittest.mock import Mock, patch

import pytest

from streamrip.db import (
    Collections,
    DatabaseMigration,
    EnhancedDownloads,
    EnhancedFailed,
    TrackCollections,
)


class TestDatabaseMigration:
    """Test suite for DatabaseMigration class."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database file for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_needs_migration_true(self, temp_db_path):
        """Test that migration is needed when old tables exist."""
        # Create old-style database
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads VALUES ('test_id')")

        migration = DatabaseMigration(temp_db_path)
        assert migration.needs_migration() is True

    def test_needs_migration_false(self, temp_db_path):
        """Test that migration is not needed when enhanced tables exist."""
        # Create enhanced-style database
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE downloads_enhanced (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT
                )
            """
            )

        migration = DatabaseMigration(temp_db_path)
        assert migration.needs_migration() is False

    @patch("shutil.copy2")
    def test_backup_database(self, mock_copy, temp_db_path):
        """Test database backup functionality."""
        migration = DatabaseMigration(temp_db_path)
        result = migration.backup_database()

        assert result is True
        mock_copy.assert_called_once()

    def test_migrate_database_success(self, temp_db_path):
        """Test successful database migration."""
        # Create old-style database
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads VALUES ('test_id')")

        migration = DatabaseMigration(temp_db_path)
        result = migration.migrate_database()

        assert result is True

        # Verify new tables exist
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            assert "downloads_enhanced" in tables
            assert "collections" in tables
            assert "track_collections" in tables

    def test_migrate_database_no_failed_table(self, temp_db_path):
        """Test migration when failed_downloads table doesn't exist."""
        # Create old-style database without failed_downloads
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads VALUES ('test_id')")

        migration = DatabaseMigration(temp_db_path)
        result = migration.migrate_database()

        assert result is True

    def test_verify_migration_success(self, temp_db_path):
        """Test successful migration verification."""
        # Create both old and new tables
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads VALUES ('test_id')")
            conn.execute(
                """
                CREATE TABLE downloads_enhanced (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT
                )
            """
            )
            conn.execute(
                "INSERT INTO downloads_enhanced VALUES ('test_id', 'test_source', 'Test Track')"
            )
            # Create other required tables for verification
            conn.execute(
                """
                CREATE TABLE collections (
                    collection_id TEXT PRIMARY KEY,
                    collection_type TEXT,
                    name TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE track_collections (
                    track_id TEXT,
                    collection_id TEXT,
                    position INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE failed_downloads_enhanced (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    media_type TEXT
                )
                """
            )

        migration = DatabaseMigration(temp_db_path)
        result = migration._verify_migration(sqlite3.connect(temp_db_path))

        assert result is True

    def test_verify_migration_failure(self, temp_db_path):
        """Test migration verification failure."""
        # Create old table but no new table
        with sqlite3.connect(temp_db_path) as conn:
            conn.execute("CREATE TABLE downloads (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO downloads VALUES ('test_id')")

        migration = DatabaseMigration(temp_db_path)
        result = migration._verify_migration(sqlite3.connect(temp_db_path))

        assert result is False


class TestDatabaseClasses:
    """Test suite for database classes."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database file for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_enhanced_downloads_creation(self, temp_db_path):
        """Test EnhancedDownloads table creation."""
        _downloads = EnhancedDownloads(temp_db_path)

        # Verify table exists
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='downloads_enhanced'"
            )
            assert cursor.fetchone() is not None

    def test_collections_creation(self, temp_db_path):
        """Test Collections table creation."""
        _collections = Collections(temp_db_path)

        # Verify table exists
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='collections'"
            )
            assert cursor.fetchone() is not None

    def test_track_collections_creation(self, temp_db_path):
        """Test TrackCollections table creation."""
        _track_collections = TrackCollections(temp_db_path)

        # Verify table exists
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='track_collections'"
            )
            assert cursor.fetchone() is not None

    def test_enhanced_failed_creation(self, temp_db_path):
        """Test EnhancedFailed table creation."""
        _failed = EnhancedFailed(temp_db_path)

        # Verify table exists
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='failed_downloads_enhanced'"
            )
            assert cursor.fetchone() is not None


class TestCLICommands:
    """Test suite for CLI commands using proper mocking."""

    @pytest.fixture
    def mock_ctx(self):
        """Create a mock Click context."""
        ctx = Mock()
        mock_config = Mock()
        mock_config.session.database.downloads_enabled = True
        mock_config.session.database.downloads_path = "/test.db"
        ctx.obj = {"config": mock_config}
        return ctx

    @patch("streamrip.rip.cli.db.DatabaseMigration")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.os.path.exists")
    def test_database_status_no_db(
        self, mock_exists, mock_console, mock_migration_class
    ):
        """Test database status when no database exists."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the migration instance
        mock_migration = Mock()
        mock_migration.needs_migration.return_value = False
        mock_migration_class.return_value = mock_migration

        # Mock os.path.exists to return False for the database file
        mock_exists.return_value = False

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "status"])

        # Verify console output was called
        mock_console.print.assert_called()
        # Command should complete successfully

    @patch("streamrip.rip.cli.db.DatabaseMigration")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.os.path.exists")
    def test_database_status_needs_migration(
        self, mock_exists, mock_console, mock_migration_class
    ):
        """Test database status when migration is needed."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the migration instance
        mock_migration = Mock()
        mock_migration.needs_migration.return_value = True
        mock_migration_class.return_value = mock_migration

        # Mock os.path.exists to return True for the database file
        mock_exists.return_value = True

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "status"])

        # Verify console output was called
        mock_console.print.assert_called()

    @patch("streamrip.rip.cli.db.DatabaseMigration")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.os.path.exists")
    def test_database_migrate_success(
        self, mock_exists, mock_console, mock_migration_class
    ):
        """Test successful database migration."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the migration instance
        mock_migration = Mock()
        mock_migration.needs_migration.return_value = True
        mock_migration.migrate_database.return_value = True
        mock_migration_class.return_value = mock_migration

        # Mock os.path.exists to return True for the database file
        mock_exists.return_value = True

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "migrate"])

        # Verify migration was called
        mock_migration.migrate_database.assert_called_once()
        mock_console.print.assert_called()

    @patch("streamrip.rip.cli.sqlite3.connect")
    @patch("streamrip.rip.cli.console")
    def test_database_stats(self, mock_console, mock_connect):
        """Test database statistics command."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock database connection and cursor
        mock_cursor = Mock()
        mock_cursor.fetchall.side_effect = [
            [("downloads_enhanced", 10)],  # Table counts
            [("tidal", 5), ("qobuz", 3)],  # Source distribution
            [("FLAC", 8), ("MP3", 2)],  # Quality distribution
            [("2024-01-01", 3)],  # Recent downloads
            [(1024, 2048, 1536)],  # File size stats
        ]

        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_connect.return_value.__enter__.return_value = mock_conn

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "stats"])

        # Verify console output
        mock_console.print.assert_called()

    @patch("streamrip.rip.cli.sqlite3.connect")
    @patch("streamrip.rip.cli.console")
    def test_database_query_success(self, mock_console, mock_connect):
        """Test database query command."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock database connection and cursor
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [("test_id", "test_title")]
        mock_cursor.description = [("id",), ("title",)]

        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_connect.return_value.__enter__.return_value = mock_conn

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "query", "SELECT * FROM downloads_enhanced"])

        # Verify query was executed
        mock_conn.execute.assert_called_once_with("SELECT * FROM downloads_enhanced")
        mock_console.print.assert_called()

    @patch("streamrip.rip.cli.sqlite3.connect")
    @patch("streamrip.rip.cli.console")
    def test_database_query_error(self, mock_console, mock_connect):
        """Test database query command with error."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock database connection to raise error
        mock_conn = Mock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        mock_connect.return_value.__enter__.return_value = mock_conn

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["database", "query", "SELECT * FROM nonexistent_table"])

        # Verify error handling
        mock_console.print.assert_called()
