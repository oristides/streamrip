# Database Management Commands

This document describes the enhanced database management commands available in `streamrip`. These commands help you manage your music library database, migrate to enhanced schemas, and inspect your downloaded tracks.

## Overview

The enhanced database system provides:
- **Rich metadata storage** for tracks (title, artist, album, quality, etc.)
- **Collection tracking** for playlists and albums
- **Migration tools** to upgrade from old database formats
- **Inspection tools** to explore your music library
- **Backfill capabilities** to extract metadata from existing files

## Database Commands

All database commands are accessed via:
```bash
rip database <command>
```

### Status and Migration

#### `rip database status`
Check the current state of your database and migration status.

```bash
rip database status
```

**Output:**
- Shows if migration is needed
- Displays track counts for old and enhanced formats
- Provides guidance on next steps

**Example:**
```
🔍 Checking database status...
✅ Database is up to date with enhanced structure
📊 Old format tracks: 1006
📊 Enhanced format tracks: 1006
```

#### `rip database migrate`
Migrate your database from the old format to the enhanced structure.

```bash
rip database migrate
```

**Features:**
- Creates automatic backup before migration
- Migrates existing track data
- Preserves all download history
- Handles failed downloads migration
- Verifies migration success

**Safety:**
- Automatic backup creation
- Rollback capability if migration fails
- Verification of data integrity

#### `rip database rollback`
Restore your database from the most recent backup.

```bash
rip database rollback
```

**Use when:**
- Migration failed
- You want to revert to old format
- Data corruption occurred

#### `rip database verify`
Verify the integrity of your migrated database.

```bash
rip database verify
```

**Checks:**
- Download counts match between old and new tables
- All required tables exist
- Data consistency

#### `rip database cleanup`
Remove old database tables after successful migration.

```bash
rip database cleanup
rip database cleanup --confirm  # Skip confirmation prompt
```

**⚠️ Warning:** This permanently removes old tables. Only run after verifying migration success.

### Inspection and Analysis

#### `rip database inspect`
Inspect database tables and their contents.

```bash
# Show all tables
rip database inspect

# Show specific table
rip database inspect --table downloads_enhanced

# Show specific columns with limit
rip database inspect --table downloads_enhanced --columns "title,artist,album" --limit 5
```

**Options:**
- `--table`: Specific table to inspect
- `--limit`: Number of rows to show (default: 10)
- `--columns`: Comma-separated list of columns to display

**Output:**
- Table schema (column names, types, nullable)
- Row counts
- Sample data in formatted tables

#### `rip database query`
Execute custom SQL queries on your database.

```bash
# Count tracks by source
rip database query "SELECT source, COUNT(*) FROM downloads_enhanced GROUP BY source"

# Find high-quality tracks
rip database query "SELECT title, artist FROM downloads_enhanced WHERE quality = 'HIGH' LIMIT 10"

# Recent downloads
rip database query "SELECT title, artist, download_date FROM downloads_enhanced ORDER BY download_date DESC LIMIT 5"
```

**Features:**
- Full SQLite query support
- Formatted table output
- Error handling
- Column name detection

#### `rip database stats`
Show comprehensive database statistics.

```bash
rip database stats
```

**Statistics include:**
- Table overview with row counts
- Source distribution (TIDAL, SoundCloud, etc.)
- Quality distribution (HIGH, MEDIUM, LOW)
- Recent download activity
- File size statistics (total, average, range)
- Collection analysis

### Metadata Backfill

#### `rip database backfill`
Extract metadata from downloaded music files and populate the database.

```bash
# Preview changes (dry run)
rip database backfill --dry-run

# Process all files
rip database backfill

# Process specific directory
rip database backfill --scan-path /path/to/music

# Limit processing for testing
rip database backfill --limit 10
```

**Options:**
- `--scan-path`: Custom directory to scan (default: downloads folder)
- `--dry-run`: Preview changes without applying them
- `--limit`: Process only first N files (for testing)

**Extracted Metadata:**
- Track title and artist
- Album information
- Track and disc numbers
- Year and genre
- Duration and quality
- File size and path
- Source detection (TIDAL, SoundCloud, etc.)

**Supported Formats:**
- MP3, FLAC, M4A, AAC, OGG, WAV

## Database Schema

### Enhanced Downloads Table (`downloads_enhanced`)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT | Unique track identifier |
| `source` | TEXT | Source platform (tidal, soundcloud, etc.) |
| `title` | TEXT | Track title |
| `artist` | TEXT | Artist name |
| `album` | TEXT | Album name (nullable) |
| `album_artist` | TEXT | Album artist (nullable) |
| `track_number` | INTEGER | Track number (nullable) |
| `disc_number` | INTEGER | Disc number (nullable) |
| `year` | INTEGER | Release year (nullable) |
| `genre` | TEXT | Genre (nullable) |
| `duration` | INTEGER | Duration in seconds (nullable) |
| `quality` | TEXT | Audio quality (HIGH, MEDIUM, LOW) |
| `file_path` | TEXT | Full path to downloaded file |
| `file_size` | INTEGER | File size in bytes (nullable) |
| `download_date` | TEXT | ISO timestamp of download |
| `source_playlist_id` | TEXT | Source playlist ID (nullable) |
| `source_album_id` | TEXT | Source album ID (nullable) |
| `source_url` | TEXT | Original source URL (nullable) |
| `checksum` | TEXT | File checksum (nullable) |

### Collections Table (`collections`)

| Column | Type | Description |
|--------|------|-------------|
| `collection_id` | TEXT | Unique collection identifier |
| `collection_type` | TEXT | Type (playlist, album, etc.) |
| `name` | TEXT | Collection name |
| `description` | TEXT | Collection description (nullable) |
| `owner_id` | TEXT | Owner identifier (nullable) |
| `last_synced` | TEXT | Last sync timestamp |
| `metadata` | TEXT | JSON metadata (nullable) |

### Track Collections Table (`track_collections`)

| Column | Type | Description |
|--------|------|-------------|
| `track_id` | TEXT | Track identifier |
| `collection_id` | TEXT | Collection identifier |
| `position` | INTEGER | Position in collection (nullable) |
| `added_date` | TEXT | When track was added to collection |

## Migration Process

### Step 1: Check Status
```bash
rip database status
```

### Step 2: Migrate (if needed)
```bash
rip database migrate
```

### Step 3: Verify Migration
```bash
rip database verify
```

### Step 4: Backfill Metadata
```bash
rip database backfill
```

### Step 5: Clean Up (optional)
```bash
rip database cleanup
```

## Common Use Cases

### Explore Your Music Library
```bash
# Get overview
rip database stats

# Find tracks by artist
rip database query "SELECT title, album FROM downloads_enhanced WHERE artist LIKE '%Daft Punk%'"

# Find high-quality tracks
rip database query "SELECT title, artist, quality FROM downloads_enhanced WHERE quality = 'HIGH' LIMIT 10"
```

### Analyze Download Patterns
```bash
# Downloads by source
rip database query "SELECT source, COUNT(*) as count FROM downloads_enhanced GROUP BY source ORDER BY count DESC"

# Recent downloads
rip database query "SELECT title, artist, download_date FROM downloads_enhanced ORDER BY download_date DESC LIMIT 10"

# File size analysis
rip database query "SELECT AVG(file_size) as avg_size, MIN(file_size) as min_size, MAX(file_size) as max_size FROM downloads_enhanced WHERE file_size IS NOT NULL"
```

### Find Missing Metadata
```bash
# Tracks with missing album info
rip database query "SELECT title, artist FROM downloads_enhanced WHERE album IS NULL LIMIT 10"

# Tracks with unknown source
rip database query "SELECT title, artist FROM downloads_enhanced WHERE source = 'unknown' LIMIT 10"
```

## Troubleshooting

### Migration Issues
- **Backup failed**: Check disk space and permissions
- **Migration failed**: Use `rip database rollback` to restore
- **Verification failed**: Check database file integrity

### Backfill Issues
- **No files found**: Verify scan path exists
- **Metadata extraction failed**: Install `mutagen` library
- **No matching tracks**: Ensure database has tracks to update

### General Issues
- **Database locked**: Close other applications using the database
- **Permission denied**: Check file permissions on database file
- **Command not found**: Ensure you're in the correct directory

## Dependencies

- **mutagen**: Required for metadata extraction (`pip install mutagen`)
- **sqlite3**: Built into Python
- **rich**: For formatted output (included with streamrip)

## Examples

### Complete Migration Workflow
```bash
# 1. Check current status
rip database status

# 2. Migrate if needed
rip database migrate

# 3. Verify migration
rip database verify

# 4. Backfill metadata
rip database backfill --dry-run  # Preview
rip database backfill            # Apply

# 5. Check results
rip database stats
```

### Library Analysis
```bash
# Get comprehensive overview
rip database stats

# Find your most downloaded artists
rip database query "SELECT artist, COUNT(*) as tracks FROM downloads_enhanced GROUP BY artist ORDER BY tracks DESC LIMIT 10"

# Analyze file sizes
rip database query "SELECT quality, COUNT(*) as count, AVG(file_size) as avg_size FROM downloads_enhanced GROUP BY quality"
```

### Quality Control
```bash
# Find tracks with missing metadata
rip database query "SELECT title, artist FROM downloads_enhanced WHERE title = 'Unknown Track' OR artist = 'Unknown Artist'"

# Check for duplicate tracks
rip database query "SELECT title, artist, COUNT(*) as count FROM downloads_enhanced GROUP BY title, artist HAVING count > 1"
```

This enhanced database system provides powerful tools for managing and analyzing your music library, making it easy to track downloads, organize collections, and maintain metadata integrity.
