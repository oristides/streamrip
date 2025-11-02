import asyncio
import logging
import os
import re
from dataclasses import dataclass

from .. import progress
from ..client import Client
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filepath
from ..metadata import AlbumMetadata
from ..metadata.util import get_album_track_ids
from .artwork import download_artwork
from .media import Media, Pending
from .track import PendingTrack

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Album(Media):
    meta: AlbumMetadata
    tracks: list[PendingTrack]
    config: Config
    # folder where the tracks will be downloaded
    folder: str
    db: Database

    async def preprocess(self):
        progress.add_title(self.meta.album)

    async def download(self):
        async def _resolve_and_download(pending: Pending):
            try:
                track = await pending.resolve()
                if track is None:
                    return
                await track.rip()
            except Exception as e:
                logger.error(f"Error downloading track: {e}")

        results = await asyncio.gather(
            *[_resolve_and_download(p) for p in self.tracks], return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Album track processing error: {result}")

    async def postprocess(self):
        progress.remove_title(self.meta.album)


@dataclass(slots=True)
class PendingAlbum(Pending):
    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Album | None:
        try:
            resp = await self.client.get_metadata(self.id, "album")
        except NonStreamableError as e:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source} ({e})",
            )
            return None

        try:
            meta = AlbumMetadata.from_album_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for {id=}: {e}")
            return None

        if meta is None:
            logger.error(
                f"Album {self.id} not available to stream on {self.client.source}",
            )
            return None

        tracklist = get_album_track_ids(self.client.source, resp)
        folder = self.config.session.downloads.folder
        album_folder = self._album_folder(folder, meta)
        os.makedirs(album_folder, exist_ok=True)
        embed_cover, _ = await download_artwork(
            self.client.session,
            album_folder,
            meta.covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        pending_tracks = [
            PendingTrack(
                id,
                album=meta,
                client=self.client,
                config=self.config,
                folder=album_folder,
                db=self.db,
                cover_path=embed_cover,
            )
            for id in tracklist
        ]
        logger.debug("Pending tracks: %s", pending_tracks)
        return Album(meta, pending_tracks, self.config, album_folder, self.db)

    def _album_folder(self, parent: str, meta: AlbumMetadata) -> str:
        config = self.config.session
        # Always use source-specific folder structure (same as playlists)
        # Get source name from client and capitalize it (e.g., "tidal" -> "Tidal")
        source_name = self.client.source.capitalize()
        parent = os.path.join(parent, source_name)
        # Route albums into a dedicated subfolder
        parent = os.path.join(parent, "albums")

        # Use simplified format: {albumartist} - {title} ({year})
        # This matches the clean naming convention from organize-albums command
        from ..filepath_utils import clean_filename

        # Extract main artist (first before comma)
        main_artist = (
            meta.albumartist.split(",")[0].strip() if meta.albumartist else "Unknown"
        )
        album_title = clean_filename(meta.album)

        # Build simplified folder name
        folder_name = f"{clean_filename(main_artist)} - {album_title}"

        # Add year if available (extract just the year from datetime strings)
        if meta.year and meta.year != "Unknown":
            year_str = str(meta.year)
            # Extract year from datetime strings like "2013-11-01T000000.000+0000" -> "2013"
            if "T" in year_str:
                year_only = year_str.split("T")[0].split("-")[0]
                if year_only.isdigit() and len(year_only) == 4:
                    folder_name += f" ({year_only})"
            # Check if it's already a 4-digit year
            elif year_str.isdigit() and len(year_str) == 4:
                folder_name += f" ({year_str})"
            # Try to find a 4-digit year in the string
            elif len(year_str) >= 4:
                year_match = re.search(r"\b(19|20)\d{2}\b", year_str)
                if year_match:
                    folder_name += f" ({year_match.group()})"

        folder = clean_filepath(folder_name, config.filepaths.restrict_characters)

        return os.path.join(parent, folder)
