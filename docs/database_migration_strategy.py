"""
Database Migration Strategy for Streamrip

This document outlines how to safely migrate from the current simple database
structure to an enhanced one without breaking existing functionality.
"""

import os
import shutil
import sqlite3
from datetime import datetime


class DatabaseMigration:
    """
    Handles safe migration from old database structure to new enhanced structure.
    """

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
            print(f"✅ Database backed up to: {self.backup_path}")
            return True
        except Exception as e:
            print(f"❌ Failed to backup database: {e}")
            return False

    def migrate_database(self) -> bool:
        """Perform the migration from old to new structure."""
        if not self.needs_migration():
            print("i  Database already up to date")
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
                    print("✅ Database migration completed successfully")
                    return True
                else:
                    print("❌ Migration verification failed")
                    return False

        except Exception as e:
            print(f"❌ Migration failed: {e}")
            # Restore backup
            self._restore_backup()
            return False

    def _create_enhanced_tables(self, conn: sqlite3.Connection):
        """Create new enhanced table structures."""

        # Enhanced Downloads Table
        conn.execute(
            """
            CREATE TABLE downloads_enhanced (
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
            CREATE TABLE collections (
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
            CREATE TABLE track_collections (
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
            CREATE TABLE failed_downloads_enhanced (
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
                INSERT INTO downloads_enhanced
                (id, source, title, artist, file_path, download_date)
                VALUES (?, 'unknown', 'Unknown Track', 'Unknown Artist', 'unknown', ?)
            """,
                (track_id, datetime.now().isoformat()),
            )

        # Migrate failed downloads
        cursor = conn.execute("SELECT source, media_type, id FROM failed_downloads")
        old_failed = cursor.fetchall()

        for source, media_type, track_id in old_failed:
            conn.execute(
                """
                INSERT INTO failed_downloads_enhanced
                (id, source, media_type, last_attempt)
                VALUES (?, ?, ?, ?)
            """,
                (track_id, source, media_type, datetime.now().isoformat()),
            )

    def _verify_migration(self, conn: sqlite3.Connection) -> bool:
        """Verify that migration was successful."""
        try:
            # Check that all old data was migrated
            old_count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            new_count = conn.execute(
                "SELECT COUNT(*) FROM downloads_enhanced"
            ).fetchone()[0]

            if old_count != new_count:
                print(f"❌ Download count mismatch: {old_count} -> {new_count}")
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
                    print(f"❌ Missing table: {table}")
                    return False

            return True

        except Exception as e:
            print(f"❌ Verification error: {e}")
            return False

    def _restore_backup(self):
        """Restore database from backup."""
        try:
            shutil.copy2(self.backup_path, self.db_path)
            print("✅ Database restored from backup")
        except Exception as e:
            print(f"❌ Failed to restore backup: {e}")


# Migration Strategy Options:

"""
OPTION 1: GRADUAL MIGRATION (RECOMMENDED)
==========================================

1. Keep old tables alongside new ones
2. Gradually migrate data as tracks are re-downloaded
3. Eventually deprecate old tables

Pros:
- Zero downtime
- No data loss risk
- Backward compatible
- Can be done incrementally

Cons:
- Temporary storage overhead
- More complex code during transition

OPTION 2: IMMEDIATE MIGRATION
==============================

1. Backup existing database
2. Create new structure
3. Migrate all existing data at once
4. Replace old tables

Pros:
- Clean break
- Immediate benefits
- Simpler long-term code

Cons:
- Risk of data loss if migration fails
- Requires downtime
- Complex rollback if issues occur

OPTION 3: HYBRID APPROACH
=========================

1. Create new enhanced tables
2. Keep old tables for compatibility
3. New downloads go to enhanced tables
4. Old downloads stay in old tables
5. Provide migration command for users

Pros:
- Safe and gradual
- User-controlled migration
- No forced changes

Cons:
- Dual maintenance during transition
- User needs to run migration command
"""


def get_migration_recommendation() -> str:
    """
    Returns recommended migration strategy based on current situation.
    """
    return """
    RECOMMENDED APPROACH: GRADUAL MIGRATION

    1. Add new enhanced database classes alongside existing ones
    2. Modify download process to write to both old and new tables
    3. Update comparison functions to use enhanced data when available
    4. Provide migration command: 'rip database migrate'
    5. Eventually deprecate old tables in future version

    This approach ensures:
    - No breaking changes for existing users
    - Gradual improvement of data quality
    - Safe rollback if issues occur
    - Users can migrate at their own pace
    """


# Implementation Steps:

"""
STEP 1: Create Enhanced Database Classes
- Add new database classes with enhanced structure
- Keep existing classes for backward compatibility

STEP 2: Modify Download Process
- Write to both old and new tables during downloads
- Use enhanced data for new downloads

STEP 3: Update Comparison Logic
- Check enhanced tables first, fall back to old tables
- Gradually improve data quality

STEP 4: Add Migration Command
- 'rip database migrate' command
- 'rip database status' to check migration status
- 'rip database rollback' if needed

STEP 5: Documentation
- Migration guide for users
- Benefits of enhanced database
- Troubleshooting guide
"""
