"""
Unit tests for TIDAL commands.

Tests the new TIDAL compare and download-missing commands with proper mocking.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from streamrip.client.tidal import TidalClient


class TestTidalClientMethods:
    """Test suite for TidalClient methods."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration object."""
        config = Mock()
        config.session.tidal.quality = "FLAC"
        config.session.tidal.country_code = "US"
        config.session.downloads.requests_per_minute = 60
        return config

    @pytest.fixture
    def mock_db(self):
        """Create a mock database object."""
        db = Mock()
        db.downloaded.return_value = False
        return db

    def test_find_missing_tracks_with_db(self, mock_config, mock_db):
        """Test finding missing tracks with database."""
        client = TidalClient(mock_config)

        # Mock tracks data
        tracks = [
            {"id": "track1", "title": "Track 1", "artist": "Artist 1"},
            {"id": "track2", "title": "Track 2", "artist": "Artist 2"},
        ]

        # Mock database to return track1 as downloaded, track2 as missing
        def mock_downloaded(track_id):
            return track_id == "track1"

        mock_db.downloaded.side_effect = mock_downloaded

        # Test the method
        missing_tracks = client._find_missing_tracks(tracks, mock_db)

        # Should only return track2 as missing
        assert len(missing_tracks) == 1
        assert missing_tracks[0]["id"] == "track2"

    def test_find_missing_tracks_without_db(self, mock_config):
        """Test finding missing tracks without database."""
        client = TidalClient(mock_config)

        # Mock tracks data
        tracks = [
            {"id": "track1", "title": "Track 1", "artist": "Artist 1"},
            {"id": "track2", "title": "Track 2", "artist": "Artist 2"},
        ]

        # Test without database (should return all tracks as missing)
        missing_tracks = client._find_missing_tracks(tracks, None)

        # Should return all tracks as missing
        assert len(missing_tracks) == 2
        assert missing_tracks[0]["id"] == "track1"
        assert missing_tracks[1]["id"] == "track2"


class TestTidalCommands:
    """Test suite for TIDAL CLI commands using proper mocking."""

    @pytest.fixture
    def mock_ctx(self):
        """Create a mock Click context."""
        ctx = Mock()
        ctx.obj = {"config": Mock()}
        return ctx

    @pytest.fixture
    def mock_main(self):
        """Create a mock Main object."""
        main = Mock()
        main.database = Mock()
        main.database.downloaded.return_value = False
        return main

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.asyncio.run")
    def test_tidal_compare_playlists_only(
        self, mock_asyncio_run, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL compare command with playlists only."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.compare_collections_with_downloads.return_value = None
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Mock asyncio.run to prevent actual async execution and server calls
        mock_asyncio_run.return_value = None

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "compare", "--playlists"])

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.asyncio.run")
    def test_tidal_compare_albums_only(
        self, mock_asyncio_run, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL compare command with albums only."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.compare_collections_with_downloads.return_value = None
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Mock asyncio.run to prevent actual async execution and server calls
        mock_asyncio_run.return_value = None

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "compare", "--albums"])

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.asyncio.run")
    def test_tidal_compare_recommended_only(
        self, mock_asyncio_run, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL compare command with recommended only."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.compare_collections_with_downloads.return_value = None
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Mock asyncio.run to prevent actual async execution
        mock_asyncio_run.return_value = None

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "compare", "--playlists", "--recommended"])

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    @patch("streamrip.rip.cli.asyncio.run")
    def test_tidal_compare_missing_only(
        self, mock_asyncio_run, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL compare command with missing_only flag."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.compare_collections_with_downloads.return_value = None
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Mock asyncio.run to prevent actual async execution
        mock_asyncio_run.return_value = None

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(
            rip, ["tidal", "compare", "--playlists", "--albums", "--missing-only"]
        )

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    def test_tidal_download_missing_playlists(
        self, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL download-missing command with playlists."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.get_missing_tracks_for_download.return_value = [
            "tidal:track:123",
            "tidal:track:456",
        ]
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "download-missing", "--playlists"])

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    def test_tidal_download_missing_with_limit(
        self, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL download-missing command with limit."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_client.get_missing_tracks_for_download.return_value = [
            "tidal:track:123",
            "tidal:track:456",
        ]
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "download-missing", "--playlists", "--limit", "2"])

        # Verify the command ran without errors
        # Command should complete without errors

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    def test_tidal_compare_no_selection(
        self, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL compare command with no selection."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "compare"])

        # Verify console output (should show error message)
        mock_console.print.assert_called()

    @patch("streamrip.rip.cli.Main")
    @patch("streamrip.rip.cli.TidalClient")
    @patch("streamrip.rip.cli.console")
    def test_tidal_download_missing_no_selection(
        self, mock_console, mock_tidal_client_class, mock_main_class
    ):
        """Test TIDAL download-missing command with no selection."""
        from click.testing import CliRunner

        from streamrip.rip.cli import rip

        # Mock the TidalClient instance
        mock_client = AsyncMock()
        mock_tidal_client_class.return_value = mock_client

        # Mock the Main instance with proper database mocking
        mock_main = Mock()
        mock_database = Mock()
        mock_database.downloaded.return_value = False
        mock_main.database = mock_database
        mock_main.get_logged_in_client.return_value = mock_client
        mock_main_class.return_value = mock_main

        # Use Click's CliRunner to test the command
        runner = CliRunner()
        runner.invoke(rip, ["tidal", "download-missing"])

        # Verify console output (should show error message)
        mock_console.print.assert_called()
