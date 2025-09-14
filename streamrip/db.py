"""Wrapper over a database that stores item IDs."""

import logging
import os
import shutil
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Final, List, Optional

logger = logging.getLogger("streamrip")


class DatabaseInterface(ABC):
    @abstractmethod
    def create(self):
        pass

    @abstractmethod
    def contains(self, **items) -> bool:
        pass

    @abstractmethod
    def add(self, kvs):
        pass

    @abstractmethod
    def remove(self, kvs):
        pass

    @abstractmethod
    def all(self) -> list:
        pass


class Dummy(DatabaseInterface):
    """This exists as a mock to use in case databases are disabled."""

    def create(self):
        pass

    def contains(self, **_):
        return False

    def add(self, *_):
        pass

    def remove(self, *_):
        pass

    def all(self):
        return []


class DatabaseBase(DatabaseInterface):
    """A wrapper for an sqlite database."""

    structure: dict
    name: str

    def __init__(self, path: str):
        """Create a Database instance.

        :param path: Path to the database file.
        """
        assert self.structure != {}
        assert self.name
        assert path

        self.path = path

        # Always ensure the table exists
        self.create()

    def create(self):
        """Create a database."""
        with sqlite3.connect(self.path) as conn:
            # Check if table already exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (self.name,),
            )
            if cursor.fetchone():
                logger.debug(f"Table {self.name} already exists")
                return

            # Build column definitions
            columns = []
            primary_keys = []

            for key, props in self.structure.items():
                if key == "PRIMARY KEY":
                    # Handle composite primary key
                    primary_keys.extend(props)
                else:
                    # Handle regular column
                    column_def = f"{key} {' '.join(map(str.upper, props))}"
                    if "NOT NULL" not in column_def and "nullable" not in props:
                        column_def += " NOT NULL"
                    columns.append(column_def)

            # Build the CREATE TABLE command
            params = ", ".join(columns)
            if primary_keys:
                params += f", PRIMARY KEY ({', '.join(primary_keys)})"

            command = f"CREATE TABLE {self.name} ({params})"

            logger.debug("executing %s", command)

            conn.execute(command)

    def keys(self):
        """Get the column names of the table."""
        return self.structure.keys()

    def contains(self, **items) -> bool:
        """Check whether items matches an entry in the table.

        :param items: a dict of column-name + expected value
        :rtype: bool
        """
        allowed_keys = set(self.structure.keys())
        assert all(
            key in allowed_keys for key in items.keys()
        ), f"Invalid key. Valid keys: {allowed_keys}"

        items = {k: str(v) for k, v in items.items()}

        with sqlite3.connect(self.path) as conn:
            conditions = " AND ".join(f"{key}=?" for key in items.keys())
            command = f"SELECT EXISTS(SELECT 1 FROM {self.name} WHERE {conditions})"

            logger.debug("Executing %s", command)

            return bool(conn.execute(command, tuple(items.values())).fetchone()[0])

    def add(self, items: tuple[str]):
        """Add a row to the table.

        :param items: Column-name + value. Values must be provided for all cols.
        :type items: Tuple[str]
        """
        assert len(items) == len(self.structure)

        params = ", ".join(self.structure.keys())
        question_marks = ", ".join("?" for _ in items)
        command = f"INSERT INTO {self.name} ({params}) VALUES ({question_marks})"

        logger.debug("Executing %s", command)
        logger.debug("Items to add: %s", items)

        with sqlite3.connect(self.path) as conn:
            try:
                conn.execute(command, tuple(items))
            except sqlite3.IntegrityError as e:
                # tried to insert an item that was already there
                logger.debug(e)

    def remove(self, **items):
        """Remove items from a table.

        Warning: NOT TESTED!

        :param items:
        """
        conditions = " AND ".join(f"{key}=?" for key in items.keys())
        command = f"DELETE FROM {self.name} WHERE {conditions}"

        with sqlite3.connect(self.path) as conn:
            logger.debug(command)
            conn.execute(command, tuple(items.values()))

    def all(self):
        """Iterate through the rows of the table."""
        with sqlite3.connect(self.path) as conn:
            return list(conn.execute(f"SELECT * FROM {self.name}"))

    def reset(self):
        """Delete the database file."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


class Downloads(DatabaseBase):
    """A table that stores the downloaded IDs."""

    name = "downloads"
    structure: Final[dict] = {
        "id": ["text", "unique"],
    }


class Failed(DatabaseBase):
    """A table that stores information about failed downloads."""

    name = "failed_downloads"
    structure: Final[dict] = {
        "source": ["text"],
        "media_type": ["text"],
        "id": ["text", "unique"],
    }


# Enhanced Database Classes for Migration
class EnhancedDownloads(DatabaseBase):
    """Enhanced downloads table with full metadata."""

    name = "downloads_enhanced"
    structure: Final[dict] = {
        "id": ["text", "primary", "key"],
        "source": ["text"],
        "title": ["text"],
        "artist": ["text"],
        "album": ["text", "nullable"],
        "album_artist": ["text", "nullable"],
        "track_number": ["integer", "nullable"],
        "disc_number": ["integer", "nullable"],
        "year": ["integer", "nullable"],
        "genre": ["text", "nullable"],
        "duration": ["integer", "nullable"],
        "quality": ["text", "nullable"],
        "file_path": ["text"],
        "file_size": ["integer", "nullable"],
        "download_date": ["text"],
        "source_playlist_id": ["text", "nullable"],
        "source_album_id": ["text", "nullable"],
        "source_url": ["text", "nullable"],
        "checksum": ["text", "nullable"],
    }


class Collections(DatabaseBase):
    """Table for tracking playlists and albums."""

    name = "collections"
    structure: Final[dict] = {
        "collection_id": ["text", "primary", "key"],
        "collection_type": ["text"],  # playlist, album, recommended_playlist, etc.
        "source": ["text"],
        "name": ["text"],
        "description": ["text", "nullable"],
        "track_count": ["integer", "nullable"],
        "last_synced": ["text", "nullable"],
        "last_checked": ["text", "nullable"],
        "is_recommended": ["boolean", "default", "0"],
    }


class TrackCollections(DatabaseBase):
    """Many-to-many relationship between tracks and collections."""

    name = "track_collections"
    structure: Final[dict] = {
        "track_id": ["text"],
        "collection_id": ["text"],
        "position": ["integer", "nullable"],
        "added_date": ["text", "nullable"],
        "PRIMARY KEY": ["track_id", "collection_id"],
    }


class EnhancedFailed(DatabaseBase):
    """Enhanced failed downloads table."""

    name = "failed_downloads_enhanced"
    structure: Final[dict] = {
        "id": ["text", "primary", "key"],
        "source": ["text"],
        "media_type": ["text"],
        "title": ["text", "nullable"],
        "artist": ["text", "nullable"],
        "error_message": ["text", "nullable"],
        "retry_count": ["integer", "default", "0"],
        "last_attempt": ["text"],
        "original_url": ["text", "nullable"],
    }


class DatabaseMigration:
    """Handles safe migration from old database structure to new enhanced structure."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.backup_path = (
            f"{db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )

    def needs_migration(self) -> bool:
        """Check if database needs migration to enhanced structure."""
        if not os.path.exists(self.db_path):
            return False

        with sqlite3.connect(self.db_path) as conn:
            # Check if old structure exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'"
            )
            if not cursor.fetchone():
                return False

            # Check if new enhanced structure exists
            cursor = conn.execute("PRAGMA table_info(downloads)")
            columns = [row[1] for row in cursor.fetchall()]

            # If only has 'id' column, needs migration
            return len(columns) == 1 and "id" in columns

    def backup_database(self) -> bool:
        """Create backup of current database."""
        try:
            shutil.copy2(self.db_path, self.backup_path)
            logger.info(f"Database backed up to: {self.backup_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to backup database: {e}")
            return False

    def migrate_database(self) -> bool:
        """Perform the migration from old to new structure."""
        if not self.needs_migration():
            logger.info("Database already up to date")
            return True

        if not self.backup_database():
            return False

        try:
            with sqlite3.connect(self.db_path) as conn:
                # Step 1: Create new enhanced tables
                self._create_enhanced_tables(conn)

                # Step 2: Migrate existing data
                self._migrate_existing_data(conn)

                # Step 3: Verify migration
                if self._verify_migration(conn):
                    logger.info("Database migration completed successfully")
                    return True
                else:
                    logger.error("Migration verification failed")
                    return False

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            # Restore backup
            self._restore_backup()
            return False

    def _create_enhanced_tables(self, conn: sqlite3.Connection):
        """Create new enhanced table structures."""

        # Enhanced Downloads Table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads_enhanced (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT,
                album_artist TEXT,
                track_number INTEGER,
                disc_number INTEGER,
                year INTEGER,
                genre TEXT,
                duration INTEGER,
                quality TEXT,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                download_date TEXT NOT NULL,
                source_playlist_id TEXT,
                source_album_id TEXT,
                source_url TEXT,
                checksum TEXT
            )
        """
        )

        # Collections Table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                collection_id TEXT PRIMARY KEY,
                collection_type TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                track_count INTEGER,
                last_synced TEXT,
                last_checked TEXT,
                is_recommended BOOLEAN DEFAULT 0
            )
        """
        )

        # Track-Collection Relationships
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS track_collections (
                track_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                position INTEGER,
                added_date TEXT,
                PRIMARY KEY (track_id, collection_id)
            )
        """
        )

        # Enhanced Failed Downloads
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS failed_downloads_enhanced (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                last_attempt TEXT,
                original_url TEXT
            )
        """
        )

    def _migrate_existing_data(self, conn: sqlite3.Connection):
        """Migrate existing data from old tables to new structure."""

        # Migrate downloads
        cursor = conn.execute("SELECT id FROM downloads")
        old_downloads = cursor.fetchall()

        for (track_id,) in old_downloads:
            # Insert with minimal required data, rest will be filled later
            conn.execute(
                """
                INSERT OR IGNORE INTO downloads_enhanced
                (id, source, title, artist, file_path, download_date)
                VALUES (?, 'unknown', 'Unknown Track', 'Unknown Artist', 'unknown', ?)
            """,
                (track_id, datetime.now().isoformat()),
            )

        # Migrate failed downloads (if table exists)
        try:
            cursor = conn.execute("SELECT source, media_type, id FROM failed_downloads")
            old_failed = cursor.fetchall()

            for source, media_type, track_id in old_failed:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO failed_downloads_enhanced
                    (id, source, media_type, last_attempt)
                    VALUES (?, ?, ?, ?)
                """,
                    (track_id, source, media_type, datetime.now().isoformat()),
                )
        except sqlite3.OperationalError:
            # failed_downloads table doesn't exist, which is fine
            logger.debug("No failed_downloads table to migrate")

    def _verify_migration(self, conn: sqlite3.Connection) -> bool:
        """Verify that migration was successful."""
        try:
            # Check that all old data was migrated
            old_count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            new_count = conn.execute(
                "SELECT COUNT(*) FROM downloads_enhanced"
            ).fetchone()[0]

            if old_count != new_count:
                logger.error(f"Download count mismatch: {old_count} -> {new_count}")
                return False

            # Check that new tables exist
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            required_tables = [
                "downloads_enhanced",
                "collections",
                "track_collections",
                "failed_downloads_enhanced",
            ]
            for table in required_tables:
                if table not in tables:
                    logger.error(f"Missing table: {table}")
                    return False

            return True

        except Exception as e:
            logger.error(f"Verification error: {e}")
            return False

    def _restore_backup(self):
        """Restore database from backup."""
        try:
            shutil.copy2(self.backup_path, self.db_path)
            logger.info("Database restored from backup")
        except Exception as e:
            logger.error(f"Failed to restore backup: {e}")


@dataclass(slots=True)
class Database:
    downloads: DatabaseInterface
    failed: DatabaseInterface
    enhanced_downloads: Optional[DatabaseInterface] = None
    collections: Optional[DatabaseInterface] = None
    track_collections: Optional[DatabaseInterface] = None
    enhanced_failed: Optional[DatabaseInterface] = None

    def downloaded(self, item_id: str) -> bool:
        """Check if item is downloaded, using enhanced table if available."""
        if self.enhanced_downloads:
            return self.enhanced_downloads.contains(id=item_id)
        return self.downloads.contains(id=item_id)

    def set_downloaded(self, item_id: str, **metadata):
        """Set item as downloaded, writing to both old and new tables."""
        # Always write to old table for backward compatibility
        self.downloads.add((item_id,))

        # Write to enhanced table if available
        if self.enhanced_downloads:
            # Extract metadata with defaults
            source = metadata.get("source", "unknown")
            title = metadata.get("title", "Unknown Track")
            artist = metadata.get("artist", "Unknown Artist")
            album = metadata.get("album")
            album_artist = metadata.get("album_artist")
            track_number = metadata.get("track_number")
            disc_number = metadata.get("disc_number")
            year = metadata.get("year")
            genre = metadata.get("genre")
            duration = metadata.get("duration")
            quality = metadata.get("quality")
            file_path = metadata.get("file_path", "unknown")
            file_size = metadata.get("file_size")
            download_date = metadata.get("download_date", datetime.now().isoformat())
            source_playlist_id = metadata.get("source_playlist_id")
            source_album_id = metadata.get("source_album_id")
            source_url = metadata.get("source_url")
            checksum = metadata.get("checksum")

            self.enhanced_downloads.add(
                (
                    item_id,
                    source,
                    title,
                    artist,
                    album,
                    album_artist,
                    track_number,
                    disc_number,
                    year,
                    genre,
                    duration,
                    quality,
                    file_path,
                    file_size,
                    download_date,
                    source_playlist_id,
                    source_album_id,
                    source_url,
                    checksum,
                )
            )

    def get_failed_downloads(self) -> list[tuple[str, str, str]]:
        """Get failed downloads, using enhanced table if available."""
        if self.enhanced_failed:
            return self.enhanced_failed.all()
        return self.failed.all()

    def set_failed(self, source: str, media_type: str, id: str, **metadata):
        """Set item as failed, writing to both old and new tables."""
        # Always write to old table for backward compatibility
        self.failed.add((source, media_type, id))

        # Write to enhanced table if available
        if self.enhanced_failed and metadata:
            title = metadata.get("title")
            artist = metadata.get("artist")
            error_message = metadata.get("error_message")
            retry_count = metadata.get("retry_count", 0)
            last_attempt = metadata.get("last_attempt", datetime.now().isoformat())
            original_url = metadata.get("original_url")

            self.enhanced_failed.add(
                (
                    id,
                    source,
                    media_type,
                    title,
                    artist,
                    error_message,
                    retry_count,
                    last_attempt,
                    original_url,
                )
            )

    def add_collection(
        self,
        collection_id: str,
        collection_type: str,
        source: str,
        name: str,
        **metadata,
    ):
        """Add a collection (playlist/album) to the database."""
        if self.collections:
            description = metadata.get("description")
            track_count = metadata.get("track_count")
            last_synced = metadata.get("last_synced", datetime.now().isoformat())
            last_checked = metadata.get("last_checked", datetime.now().isoformat())
            is_recommended = metadata.get("is_recommended", False)

            self.collections.add(
                (
                    collection_id,
                    collection_type,
                    source,
                    name,
                    description,
                    track_count,
                    last_synced,
                    last_checked,
                    is_recommended,
                )
            )

    def link_track_to_collection(
        self, track_id: str, collection_id: str, position: Optional[int] = None
    ):
        """Link a track to a collection."""
        if self.track_collections:
            added_date = datetime.now().isoformat()
            self.track_collections.add((track_id, collection_id, position, added_date))

    def get_tracks_in_collection(self, collection_id: str) -> List[str]:
        """Get all track IDs in a collection."""
        if self.track_collections:
            with sqlite3.connect(self.track_collections.path) as conn:
                cursor = conn.execute(
                    "SELECT track_id FROM track_collections WHERE collection_id = ? ORDER BY position",
                    (collection_id,),
                )
                return [row[0] for row in cursor.fetchall()]
        return []

    def get_collections_for_track(self, track_id: str) -> List[str]:
        """Get all collection IDs that contain a track."""
        if self.track_collections:
            with sqlite3.connect(self.track_collections.path) as conn:
                cursor = conn.execute(
                    "SELECT collection_id FROM track_collections WHERE track_id = ?",
                    (track_id,),
                )
                return [row[0] for row in cursor.fetchall()]
        return []
