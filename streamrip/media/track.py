import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from .. import converter
from ..client import Client, Downloadable
from ..config import Config
from ..db import Database
from ..exceptions import NonStreamableError
from ..filepath_utils import clean_filename
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..progress import add_title, get_progress_callback, remove_title
from .artwork import download_artwork
from .media import Media, Pending
from .semaphore import global_download_semaphore

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # change?
    download_path: str = ""
    is_single: bool = False

    async def preprocess(self):
        self._set_download_path()
        os.makedirs(self.folder, exist_ok=True)
        if self.is_single:
            add_title(self.meta.title)

    def _validate_downloaded_file(self, path: str) -> tuple[bool, str | None]:
        """Validate that a downloaded file is a valid music file.

        Returns:
            tuple: (is_valid, error_message)
        """
        from pathlib import Path

        file_path = Path(path)

        # Check file exists and has content
        if not file_path.exists():
            return False, "File does not exist"

        try:
            file_size = file_path.stat().st_size
            if file_size == 0:
                return False, "File is empty (0 bytes)"
            if file_size < 1000:  # Less than 1KB is suspicious
                return False, f"File too small ({file_size} bytes) - likely corrupted"
        except Exception as e:
            return False, f"Error checking file size: {e}"

        # Try to validate the file format with mutagen
        ext = file_path.suffix[1:].lower()

        try:
            from mutagen import File as MutagenFile
            from mutagen.flac import FLAC
            from mutagen.mp3 import MP3
            from mutagen.mp4 import MP4

            # Try to open with mutagen based on extension
            audio_file = None
            try:
                if ext == "flac":
                    audio_file = FLAC(str(file_path))
                elif ext in ["m4a", "mp4", "aac"]:
                    audio_file = MP4(str(file_path))
                elif ext == "mp3":
                    audio_file = MP3(str(file_path))
                else:
                    # Try generic MutagenFile
                    audio_file = MutagenFile(str(file_path))

                # Verify it's actually readable by accessing tags
                if audio_file is None:
                    return False, "MutagenFile returned None"

                # Try to access tags to trigger any lazy-loading errors
                _ = audio_file.tags

                # Check if it's an MP4 fragment (starts with moof instead of ftyp)
                # NOTE: We allow moof fragments now - they will be reconstructed by ffmpeg for DASH segments
                # Only check if file is completely unreadable, not if it's a fragment
                if ext in ["m4a", "mp4", "aac"]:
                    # Allow files that start with moof - they're DASH segments that will be reconstructed
                    # Only fail if file is completely unreadable by mutagen
                    pass  # Removed strict moof check - allow DASH fragments to pass validation

                return True, None

            except Exception as e:
                error_msg = str(e).lower()

                # Check if it's an MP4 fragment issue - but allow fragments for DASH segments
                if "not a mp4" in error_msg or "not a valid" in error_msg:
                    # Check if it's a fragment, but allow it if it's a concatenated DASH file
                    try:
                        with open(file_path, "rb") as f:
                            header = f.read(12)
                            if header[4:8] == b"moof":
                                # Allow ISOBMFF fragments - they're valid DASH segments
                                # Many media players can handle these
                                logger.debug(
                                    "File starts with 'moof' (ISOBMFF fragment) - allowing as valid DASH segment"
                                )
                                return True, None  # Allow DASH fragments
                    except Exception:
                        pass

                # For other errors, still fail validation
                return False, f"File validation failed: {e}"

        except ImportError:
            # Mutagen not available - skip validation
            logger.warning("mutagen not available, skipping file validation")
            return True, None
        except Exception as e:
            return False, f"Validation error: {e}"

    async def download(self):
        # TODO: progress bar description
        async with global_download_semaphore(self.config.session.downloads):
            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                f"Track {self.meta.tracknumber}",
            ) as callback:
                try:
                    actual_path = await self.downloadable.download(
                        self.download_path, callback
                    )
                    # Update download_path if format was corrected during download
                    if str(actual_path) != str(self.download_path):
                        self.download_path = str(actual_path)

                    # Validate the downloaded file before proceeding
                    is_valid, error_msg = self._validate_downloaded_file(
                        str(actual_path)
                    )
                    if not is_valid:
                        # Delete invalid file
                        try:
                            os.remove(str(actual_path))
                            logger.warning(
                                f"Deleted invalid download: {actual_path} - {error_msg}"
                            )
                        except Exception as e:
                            logger.error(f"Failed to delete invalid file: {e}")

                        raise Exception(f"Downloaded file is invalid: {error_msg}")

                    retry = False
                except Exception as e:
                    error_msg = str(e)
                    if "invalid" in error_msg.lower():
                        # File was invalid, mark as failed and don't retry
                        logger.error(
                            f"Invalid file downloaded for '{self.meta.title}': {error_msg}"
                        )
                        self.db.set_failed(
                            self.downloadable.source, "track", self.meta.info.id
                        )
                        return

                    logger.error(
                        f"Error downloading track '{self.meta.title}', retrying: {e}"
                    )
                    retry = True

            if not retry:
                return

            with get_progress_callback(
                self.config.session.cli.progress_bars,
                await self.downloadable.size(),
                f"Track {self.meta.tracknumber} (retry)",
            ) as callback:
                try:
                    actual_path = await self.downloadable.download(
                        self.download_path, callback
                    )
                    # Update download_path if format was corrected during download
                    if str(actual_path) != str(self.download_path):
                        self.download_path = str(actual_path)

                    # Validate the downloaded file after retry
                    is_valid, error_msg = self._validate_downloaded_file(
                        str(actual_path)
                    )
                    if not is_valid:
                        # Delete invalid file
                        try:
                            os.remove(str(actual_path))
                            logger.warning(
                                f"Deleted invalid download after retry: {actual_path} - {error_msg}"
                            )
                        except Exception as e:
                            logger.error(f"Failed to delete invalid file: {e}")

                        raise Exception(f"Downloaded file is invalid: {error_msg}")

                except Exception as e:
                    logger.error(
                        f"Persistent error downloading track '{self.meta.title}', skipping: {e}"
                    )
                    self.db.set_failed(
                        self.downloadable.source, "track", self.meta.info.id
                    )

    async def postprocess(self):
        if self.is_single:
            remove_title(self.meta.title)

        # Check if file still exists (it might have been deleted during validation)
        if not os.path.exists(self.download_path):
            logger.warning(
                f"File {self.download_path} does not exist, skipping tagging. "
                "File may have been deleted due to validation failure."
            )
            return

        await tag_file(self.download_path, self.meta, self.cover_path)
        if self.config.session.conversion.enabled:
            await self._convert()

        # Get file size for database
        try:
            file_size = (
                self.download_path.stat().st_size
                if self.download_path.exists()
                else None
            )
        except Exception:
            file_size = None

        # Get playlist information from track metadata
        playlist_id = getattr(self.meta.info, "playlist_id", None)
        playlist_position = getattr(self.meta.info, "playlist_position", None)

        # Update database with full metadata
        # Note: source_playlist_id stores the PRIMARY playlist (the one being downloaded)
        # Multiple playlist relationships are stored in track_collections table
        self.db.set_downloaded(
            self.meta.info.id,
            source=self.downloadable.source,
            title=self.meta.title,
            artist=self.meta.artist,
            album=self.meta.album.album,
            album_artist=self.meta.album.albumartist,
            track_number=self.meta.tracknumber,
            disc_number=self.meta.discnumber,
            year=self.meta.album.year,
            genre=self.meta.album.get_genres(),
            duration=getattr(self.meta.info, "duration", None),
            quality=self.meta.info.quality,
            file_path=str(self.download_path),
            file_size=file_size,
            download_date=datetime.now().isoformat(),
            source_playlist_id=playlist_id,  # Primary playlist for this download
            source_album_id=getattr(self.meta.info, "album_id", None),
            source_url=getattr(self.meta.info, "source_url", None),
        )

        # Populate track_collections table for this specific playlist relationship
        # This allows the same track to be linked to multiple playlists
        if playlist_id:
            self.db.link_track_to_collection(
                self.meta.info.id, playlist_id, position=playlist_position
            )

    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        engine = engine_class(
            filename=self.download_path,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed

    def _set_download_path(self):
        c = self.config.session.filepaths
        formatter = c.track_format
        try:
            formatted = self.meta.format_track_path(formatter)
        except (ValueError, KeyError, TypeError) as e:
            # If formatting fails, use a simple fallback
            # Use f-string to avoid % formatting issues with metadata
            logger.warning(
                f"Failed to format track path for '{self.meta.title}': {e!s}. Using fallback."
            )
            formatted = (
                f"{self.meta.tracknumber:02}. {self.meta.artist} - {self.meta.title}"
            )
        track_path = clean_filename(
            formatted,
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        # Route singles into dedicated 'tracks' subfolder when the parent
        # is the session downloads folder (i.e., not inside album/playlist)
        parent = self.folder
        session_root = self.config.session.downloads.folder
        try:
            if os.path.abspath(parent) == os.path.abspath(session_root):
                # Route singles by source
                source_dir = (
                    "soundcloud"
                    if getattr(self.config, "session", None)
                    and self.client.source == "soundcloud"
                    else None
                )
                if source_dir:
                    parent = os.path.join(parent, source_dir)
                parent = os.path.join(parent, "tracks")
        except Exception:
            pass

        self.download_path = os.path.join(
            parent, f"{track_path}.{self.downloadable.extension}"
        )


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        source = self.client.source
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            return None

        if meta is None:
            logger.error(f"Track {self.id} not available for stream on {source}")
            self.db.set_failed(source, "track", self.id)
            return None

        quality = self.config.session.get_source(source).quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)
        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} [{self.id}]: {e}"
            )
            return None

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        # Wrap entire resolution in try/except to catch format string errors
        try:
            resp = await self.client.get_metadata(self.id, "track")
            # Patch for soundcloud
            try:
                album = AlbumMetadata.from_track_resp(resp, self.client.source)
            except Exception as e:
                # Safely log error - exception message might contain format chars
                error_type = type(e).__name__
                logger.error(
                    f"Error building album metadata for track {self.id}: {error_type}"
                )
                import traceback

                tb = traceback.format_exception(type(e), e, e.__traceback__)
                for line in tb[-5:]:
                    if ".py" in line:
                        logger.debug(f"  at {line.strip()}")
                return None

            if album is None:
                self.db.set_failed(self.client.source, "track", self.id)
                logger.error(
                    f"Cannot stream track (am) ({self.id}) on {self.client.source}",
                )
                return None

            try:
                meta = TrackMetadata.from_resp(album, self.client.source, resp)
            except Exception as e:
                # Safely log error - exception message might contain format chars
                error_type = type(e).__name__
                logger.error(
                    f"Error building track metadata for track {self.id}: {error_type}"
                )
                import traceback

                tb = traceback.format_exception(type(e), e, e.__traceback__)
                for line in tb[-5:]:
                    if ".py" in line:
                        logger.debug(f"  at {line.strip()}")
                return None

            if meta is None:
                self.db.set_failed(self.client.source, "track", self.id)
                logger.error(
                    f"Cannot stream track (tm) ({self.id}) on {self.client.source}",
                )
                return None

            config = self.config.session
            quality = getattr(config, self.client.source).quality
            assert isinstance(quality, int)
            parent = config.downloads.folder
            if config.filepaths.add_singles_to_folder:
                folder = os.path.join(parent, self._format_folder(album))
            else:
                folder = parent

            os.makedirs(folder, exist_ok=True)

            embedded_cover_path, downloadable = await asyncio.gather(
                self._download_cover(album.covers, folder),
                self.client.get_downloadable(self.id, quality),
            )
            return Track(
                meta,
                downloadable,
                self.config,
                folder,
                embedded_cover_path,
                self.db,
                is_single=True,
            )
        except NonStreamableError as e:
            error_type = type(e).__name__
            logger.error(f"Error fetching track {self.id}: {error_type}")
            return None
        except TypeError as e:
            # Catch TypeError specifically - often caused by format string issues
            error_type = type(e).__name__
            import traceback

            tb = traceback.format_exception(type(e), e, e.__traceback__)
            logger.error(f"TypeError resolving track {self.id}: {error_type}")
            # Find the relevant line in traceback
            for line in tb:
                if ".py" in line and (
                    "track.py" in line
                    or "tidal.py" in line
                    or "metadata" in line.lower()
                    or "format" in line.lower()
                ):
                    logger.error(f"  Location: {line.strip()}")
            return None
        except Exception as e:
            # Catch all other exceptions
            error_type = type(e).__name__
            logger.error(f"Error resolving track {self.id}: {error_type}")
            return None

    def _format_folder(self, meta: AlbumMetadata) -> str:
        c = self.config.session
        parent = c.downloads.folder
        formatter = c.filepaths.folder_format
        if c.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())

        return os.path.join(parent, meta.format_folder_path(formatter))

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
