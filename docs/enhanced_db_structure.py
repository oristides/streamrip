"""
Enhanced Database Structure Proposal for Streamrip

This would replace the current simple database with a more comprehensive
tracking system that includes metadata, timestamps, and better organization.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Enhanced Downloads Table Structure
ENHANCED_DOWNLOADS_STRUCTURE = {
    "id": ["text", "primary", "key"],  # Track ID
    "source": ["text", "not", "null"],  # tidal, qobuz, deezer, etc.
    "title": ["text", "not", "null"],
    "artist": ["text", "not", "null"],
    "album": ["text"],
    "album_artist": ["text"],
    "track_number": ["integer"],
    "disc_number": ["integer"],
    "year": ["integer"],
    "genre": ["text"],
    "duration": ["integer"],  # Duration in seconds
    "quality": ["text"],  # LOW, HIGH, LOSSLESS, HI_RES
    "file_path": ["text", "not", "null"],  # Full path to downloaded file
    "file_size": ["integer"],  # File size in bytes
    "download_date": ["text", "not", "null"],  # ISO timestamp
    "source_playlist_id": ["text"],  # Which playlist it came from
    "source_album_id": ["text"],  # Which album it came from
    "source_url": ["text"],  # Original URL
    "checksum": ["text"],  # File integrity check
}

# Enhanced Failed Downloads Table
ENHANCED_FAILED_STRUCTURE = {
    "id": ["text", "primary", "key"],
    "source": ["text", "not", "null"],
    "media_type": ["text", "not", "null"],
    "title": ["text"],
    "artist": ["text"],
    "error_message": ["text"],
    "retry_count": ["integer", "default", "0"],
    "last_attempt": ["text"],
    "original_url": ["text"],
}

# New Collections Tracking Table
COLLECTIONS_STRUCTURE = {
    "collection_id": ["text", "primary", "key"],
    "collection_type": ["text", "not", "null"],  # playlist, album, artist
    "source": ["text", "not", "null"],  # tidal, qobuz, etc.
    "name": ["text", "not", "null"],
    "description": ["text"],
    "track_count": ["integer"],
    "last_synced": ["text"],
    "last_checked": ["text"],
    "is_recommended": ["boolean", "default", "false"],
}

# Track-Collection Relationship Table
TRACK_COLLECTIONS_STRUCTURE = {
    "track_id": ["text", "not", "null"],
    "collection_id": ["text", "not", "null"],
    "position": ["integer"],  # Position in playlist
    "added_date": ["text"],
    "primary_key": ["track_id", "collection_id"],
}


@dataclass
class EnhancedTrackRecord:
    """Enhanced track record with full metadata"""

    id: str
    source: str
    title: str
    artist: str
    album: Optional[str] = None
    album_artist: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    duration: Optional[int] = None
    quality: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    download_date: Optional[datetime] = None
    source_playlist_id: Optional[str] = None
    source_album_id: Optional[str] = None
    source_url: Optional[str] = None
    checksum: Optional[str] = None


@dataclass
class CollectionRecord:
    """Collection (playlist/album) record"""

    collection_id: str
    collection_type: str  # playlist, album, artist
    source: str
    name: str
    description: Optional[str] = None
    track_count: Optional[int] = None
    last_synced: Optional[datetime] = None
    last_checked: Optional[datetime] = None
    is_recommended: bool = False


# Benefits of Enhanced Structure:
"""
1. 📊 Better Analytics:
   - Track download history
   - Most downloaded artists/albums
   - Quality preferences over time
   - Storage usage statistics

2. 🔍 Enhanced Search:
   - Search by artist, album, genre
   - Find duplicates across sources
   - Track collection relationships

3. 🔄 Better Sync:
   - Track which collections are up-to-date
   - Detect removed tracks from playlists
   - Handle collection changes

4. 🛠️ Maintenance:
   - Verify file integrity
   - Clean up orphaned files
   - Re-download missing files

5. 📈 Reporting:
   - Download statistics
   - Collection health
   - Storage optimization suggestions
"""
