"""Downloadable classes for different streaming services."""

import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

import aiohttp

logger = logging.getLogger("streamrip")


class Downloadable(ABC):
    """Abstract base class for downloadable media."""

    @abstractmethod
    async def size(self) -> int:
        """Get the size of the file to download in bytes."""
        raise NotImplementedError

    @abstractmethod
    async def download(self, path: str | Path, callback: Callable[[int], None]) -> Path:
        """Download the file to the given path.

        Args:
            path: Destination file path
            callback: Progress callback function that takes bytes downloaded

        Returns:
            Path: The actual path where the file was saved (may differ from
                input if format was corrected)
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def source(self) -> str:
        """The source service name (e.g., 'tidal', 'qobuz')."""
        raise NotImplementedError

    @property
    @abstractmethod
    def extension(self) -> str:
        """File extension (e.g., 'flac', 'mp3')."""
        raise NotImplementedError


class BasicDownloadable(Downloadable):
    """Simple HTTP downloadable for direct URL downloads."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        extension: str,
        source: str = "qobuz",
    ):
        """Initialize BasicDownloadable.

        Args:
            session: aiohttp ClientSession
            url: Direct download URL
            extension: File extension (e.g., 'flac', 'mp3', 'jpg')
            source: Source service name
        """
        self.session = session
        self.url = url
        self._extension = extension
        self._source = source
        self._size_cache: int | None = None

    @property
    def source(self) -> str:
        return self._source

    @property
    def extension(self) -> str:
        return self._extension

    async def size(self) -> int:
        """Get file size from HTTP HEAD request."""
        if self._size_cache is not None:
            return self._size_cache

        try:
            async with self.session.head(self.url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self._size_cache = int(content_length)
                    return self._size_cache

            # Fallback: use GET request with range header
            async with self.session.get(
                self.url, headers={"Range": "bytes=0-0"}
            ) as resp:
                content_range = resp.headers.get("Content-Range")
                if content_range:
                    # Content-Range: bytes 0-0/1234567
                    total = content_range.split("/")[-1]
                    self._size_cache = int(total)
                    return self._size_cache

            # Last resort: assume 0 if we can't determine size
            logger.warning(f"Could not determine size for {self.url}")
            self._size_cache = 0
            return 0
        except Exception as e:
            logger.warning(f"Error getting size for {self.url}: {e}")
            self._size_cache = 0
            return 0

    async def download(self, path: str | Path, callback: Callable[[int], None]) -> Path:
        """Download file from URL."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self.session.get(self.url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
                    callback(len(chunk))
        return path


class TidalDownloadable(Downloadable):
    """Tidal-specific downloadable that handles Tidal streaming."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str | list[str] | None,
        codec: str,
        restrictions: list | None = None,
    ):
        """Initialize TidalDownloadable.

        Args:
            session: aiohttp ClientSession
            url: Stream URL(s) - single URL string or list of segment URLs for DASH
            codec: Audio codec (e.g., 'flac', 'mqa', 'aac')
            restrictions: List of restrictions (if any)
        """
        self.session = session
        self.url = url
        self.codec = codec.lower()
        self.restrictions = restrictions or []
        self._size_cache: int | None = None

        # Detect if we have multiple URLs (DASH segments)
        self.is_dash_segments = isinstance(url, list) and len(url) > 1

    @property
    def source(self) -> str:
        return "tidal"

    @property
    def extension(self) -> str:
        """Map Tidal codec to file extension."""
        codec_lower = self.codec.lower()

        # Handle various codec formats from Tidal
        if "flac" in codec_lower:
            return "flac"
        elif "mqa" in codec_lower:
            return "flac"  # MQA is packaged as FLAC
        elif "aac" in codec_lower or "mp4a" in codec_lower:
            return "m4a"
        elif "mp3" in codec_lower:
            return "mp3"
        else:
            # Default to m4a for unknown codecs
            # (most Tidal streams are AAC/M4A)
            return "m4a"

    async def size(self) -> int:
        """Get file size from stream URL(s)."""
        if self._size_cache is not None:
            return self._size_cache

        if not self.url:
            self._size_cache = 0
            return 0

        # For DASH segments, sum the size of all segments
        if self.is_dash_segments:
            total_size = 0
            for segment_url in self.url:
                try:
                    async with self.session.head(segment_url) as resp:
                        resp.raise_for_status()
                        content_length = resp.headers.get("Content-Length")
                        if content_length:
                            total_size += int(content_length)
                        else:
                            # If we can't determine size, return 0 (will estimate during download)
                            self._size_cache = 0
                            return 0
                except Exception:
                    self._size_cache = 0
                    return 0
            self._size_cache = total_size
            return total_size

        # Single URL - original logic
        try:
            async with self.session.head(self.url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self._size_cache = int(content_length)
                    return self._size_cache

            # Fallback: try GET with range header
            async with self.session.get(
                self.url, headers={"Range": "bytes=0-0"}
            ) as resp:
                content_range = resp.headers.get("Content-Range")
                if content_range:
                    total = content_range.split("/")[-1]
                    self._size_cache = int(total)
                    return self._size_cache

            # If still unknown, try streaming a bit to estimate
            # (This is a fallback, actual implementation might differ)
            logger.warning(f"Could not determine size for Tidal stream {self.url}")
            self._size_cache = 0
            return 0
        except Exception as e:
            logger.warning(f"Error getting size for Tidal stream: {e}")
            self._size_cache = 0
            return 0

    async def download(self, path: str | Path, callback: Callable[[int], None]) -> Path:
        """Download Tidal stream.

        For DASH manifests with multiple segments, downloads all segments
        and concatenates them into a single file.
        """
        if not self.url:
            raise ValueError("No URL available for Tidal download")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Handle DASH segments - download all segments and concatenate
        if self.is_dash_segments:
            logger.info(
                f"Downloading {len(self.url)} DASH segments and concatenating..."
            )

            # Download first segment to detect format (HEAD request, don't consume body)
            async with self.session.head(self.url[0]) as resp:
                resp.raise_for_status()

                # Detect actual format from Content-Type header
                content_type = resp.headers.get("Content-Type", "").lower()
                actual_extension = self.extension

                if "audio/mpeg" in content_type or "audio/mp3" in content_type:
                    actual_extension = "mp3"
                elif (
                    "audio/mp4" in content_type
                    or "audio/aac" in content_type
                    or "audio/x-m4a" in content_type
                ):
                    actual_extension = "m4a"
                elif "audio/flac" in content_type or "audio/x-flac" in content_type:
                    actual_extension = "flac"

                # Update path extension if needed
                current_extension = path.suffix[1:].lower() if path.suffix else ""
                if actual_extension != current_extension:
                    path = path.with_suffix(f".{actual_extension}")
                    logger.info(
                        f"Detected format mismatch: expected {current_extension}, "
                        f"detected {actual_extension} from Content-Type "
                        f"{content_type}. Using {path.name}"
                    )

            # Download all segments to temporary files, then use ffmpeg to reconstruct valid MP4
            # DASH MP4 segments are ISOBMFF fragments that need proper reconstruction
            temp_dir = tempfile.mkdtemp(prefix="streamrip_dash_")
            segment_files = []

            try:
                # Check if ffmpeg is available
                if not shutil.which("ffmpeg"):
                    raise ValueError(
                        "ffmpeg is required to reconstruct DASH segments into a valid MP4 file. "
                        "Please install ffmpeg."
                    )

                # Download all segments to temporary files in parallel for better performance
                async def download_segment(segment_idx: int, segment_url: str) -> str:
                    """Download a single segment and return its file path."""
                    segment_file = os.path.join(temp_dir, f"seg_{segment_idx:05d}.m4s")

                    async with self.session.get(segment_url) as resp:
                        resp.raise_for_status()
                        # Use larger chunk size (64KB) for better I/O performance
                        chunk_size = 65536  # 64KB chunks
                        with open(segment_file, "wb") as f:
                            async for chunk in resp.content.iter_chunked(chunk_size):
                                f.write(chunk)
                                callback(len(chunk))

                    return segment_file

                # Download all segments in parallel (limit concurrency)
                # This dramatically improves speed for DASH downloads
                import asyncio

                # Create download tasks for all segments
                download_tasks = [
                    download_segment(idx, url) for idx, url in enumerate(self.url)
                ]

                # Use semaphore to limit concurrent downloads (max 20 parallel)
                # Increased from 10 to 20 for better performance on fast connections
                # Prevents overwhelming server while maintaining good speed
                semaphore = asyncio.Semaphore(20)

                async def bounded_download(task):
                    """Wrap task with semaphore for controlled concurrency."""
                    async with semaphore:
                        return await task

                # Execute all downloads in parallel with concurrency limit
                segment_files = await asyncio.gather(
                    *[bounded_download(task) for task in download_tasks]
                )

                # Create concat file list for ffmpeg
                concat_file = os.path.join(temp_dir, "concat.txt")
                with open(concat_file, "w") as f:
                    for seg_file in segment_files:
                        # Use absolute path and escape single quotes
                        abs_path = os.path.abspath(seg_file).replace("'", "'\"'\"'")
                        f.write(f"file '{abs_path}'\n")

                # Use ffmpeg to reconstruct valid MP4 from segments
                # For ISOBMFF fragments, we need to use 'concat' protocol with proper format
                temp_output = os.path.join(temp_dir, "output.m4a")

                # Try using concat demuxer first (works for most ISOBMFF fragments)
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_file,
                    "-c",
                    "copy",  # Copy codec without re-encoding
                    "-y",  # Overwrite output
                    "-loglevel",
                    "error",  # Suppress ffmpeg output
                    temp_output,
                ]

                logger.debug(
                    f"Reconstructing MP4 with ffmpeg from {len(self.url)} segments"
                )

                # For DASH ISOBMFF fragments, we need to use ffmpeg's concat demuxer
                # which properly handles ISOBMFF fragment boxes (moof/mfhd)
                logger.debug(
                    "Using ffmpeg concat demuxer to reconstruct MP4 from ISOBMFF fragments..."
                )

                # First try concat demuxer (proper way for ISOBMFF)
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_file,
                    "-c",
                    "copy",  # Copy codec without re-encoding
                    "-movflags",
                    "+faststart",  # Optimize for streaming
                    "-y",  # Overwrite output
                    "-loglevel",
                    "error",
                    temp_output,
                ]

                logger.debug(f"Running ffmpeg concat: {' '.join(ffmpeg_cmd)}")
                result = subprocess.run(
                    ffmpeg_cmd, capture_output=True, text=True, check=False
                )

                # If concat fails, try processing concatenated fragments as ISOBMFF
                # (slower but more compatible)
                if result.returncode != 0:
                    logger.warning(
                        "concat demuxer failed, trying ISOBMFF fragment processing..."
                    )

                    # Concatenate segments raw - optimized with larger buffer
                    temp_raw = os.path.join(temp_dir, "raw_concatenated.m4a")
                    # Use larger buffer (1MB) for faster concatenation
                    buffer_size = 1024 * 1024  # 1MB buffer
                    with open(temp_raw, "wb") as outfile:
                        for seg_file in segment_files:
                            with open(seg_file, "rb") as infile:
                                shutil.copyfileobj(infile, outfile, length=buffer_size)

                    # Process ISOBMFF fragments by extracting audio and re-encapsulating
                    # Fragmentos ISOBMFF não têm 'moov' box, então precisamos extrair o áudio
                    # e re-encapsular em MP4 válido
                    logger.info(
                        "Processing ISOBMFF fragments - extracting audio and creating valid MP4..."
                    )

                    # Extract raw audio from fragments and re-encapsulate
                    # Use 'ffmpeg -f mp4' to read fragments, extract audio, then save as valid MP4
                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-f",
                        "mp4",
                        "-fflags",
                        "+genpts+ignidx",  # Generate timestamps, ignore index
                        "-analyzeduration",
                        "100000000",  # Large analysis window
                        "-probesize",
                        "100000000",
                        "-i",
                        temp_raw,
                        "-vn",  # No video
                        "-c:a",
                        "aac",  # Re-encode AAC (necessary to create valid container)
                        "-b:a",
                        "320k",  # High quality
                        "-movflags",
                        "+faststart+empty_moov",  # Fast start + empty moov workaround
                        "-y",
                        "-loglevel",
                        "error",
                        temp_output,
                    ]

                    result = subprocess.run(
                        ffmpeg_cmd, capture_output=True, text=True, check=False
                    )

                    if result.returncode == 0:
                        logger.info(
                            "Successfully processed ISOBMFF fragments into valid MP4 "
                            "(re-encoded AAC 320kbps for compatibility)"
                        )
                    else:
                        # Skip individual segment extraction (it always fails for ISOBMFF)
                        # Go directly to the method that works: treat concatenated file as raw PCM
                        logger.info(
                            "Skipping individual segment extraction (ISOBMFF fragments "
                            "require concatenation first). Processing concatenated file..."
                        )
                        result.returncode = 1  # Force fallback to raw PCM method

                # If ffmpeg fails, try one final method: treat concatenated file as raw PCM
                # This works because ISOBMFF fragments contain raw audio data
                # that can be extracted even without proper container structure
                if result.returncode != 0:
                    logger.warning(
                        "All reconstruction methods failed. Trying raw audio extraction "
                        "from concatenated fragments..."
                    )
                    error_msg = result.stderr or "Unknown ffmpeg error"
                    logger.debug(f"ffmpeg error: {error_msg}")

                    # Ensure we have the concatenated file
                    temp_raw = os.path.join(temp_dir, "raw_concatenated.m4a")
                    if not os.path.exists(temp_raw):
                        # Use larger buffer (1MB) for faster concatenation
                        buffer_size = 1024 * 1024  # 1MB buffer
                        with open(temp_raw, "wb") as outfile:
                            for seg_file in segment_files:
                                with open(seg_file, "rb") as infile:
                                    shutil.copyfileobj(
                                        infile, outfile, length=buffer_size
                                    )

                    # Try to extract audio by treating concatenated file as raw PCM
                    # This bypasses MP4 structure requirements
                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-f",
                        "s16le",  # Raw PCM signed 16-bit little-endian
                        "-ar",
                        "44100",  # Sample rate
                        "-ac",
                        "2",  # Stereo
                        "-i",
                        temp_raw,
                        "-c:a",
                        "aac",
                        "-b:a",
                        "320k",
                        "-movflags",
                        "+faststart",
                        "-y",
                        "-loglevel",
                        "error",
                        temp_output,
                    ]

                    result = subprocess.run(
                        ffmpeg_cmd, capture_output=True, text=True, check=False
                    )

                    if result.returncode == 0:
                        logger.info(
                            "Successfully extracted audio from concatenated fragments "
                            "by treating as raw PCM - created valid MP4"
                        )
                    else:
                        # Ultimate fallback: save raw concatenated file
                        logger.warning(
                            "All audio extraction methods failed. Saving concatenated "
                            "fragments as-is (may not play in all players)."
                        )
                        temp_output = temp_raw  # Use raw concatenated file

                # Move file to final location (either reconstructed or raw concatenated)
                if os.path.exists(temp_output):
                    shutil.move(temp_output, path)
                    if result.returncode == 0:
                        logger.info(
                            f"Successfully reconstructed {len(self.url)} DASH segments "
                            f"into valid MP4: {path.name}"
                        )
                    else:
                        logger.warning(
                            f"Saved {len(self.url)} DASH segments as concatenated file "
                            f"(may be ISOBMFF fragments, not standard MP4): {path.name}"
                        )
                else:
                    raise ValueError("Failed to create output file from DASH segments")

            finally:
                # Clean up temporary directory
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")

            return path

        # Single URL - original logic
        async with self.session.get(self.url) as resp:
            resp.raise_for_status()

            # Detect actual format from Content-Type header
            content_type = resp.headers.get("Content-Type", "").lower()
            actual_extension = self.extension  # Default to expected extension

            # Check if Content-Type indicates a different format than expected
            if "audio/mpeg" in content_type or "audio/mp3" in content_type:
                actual_extension = "mp3"
            elif (
                "audio/mp4" in content_type
                or "audio/aac" in content_type
                or "audio/x-m4a" in content_type
            ):
                actual_extension = "m4a"
            elif "audio/flac" in content_type or "audio/x-flac" in content_type:
                actual_extension = "flac"

            # If extension doesn't match, update the path
            current_extension = path.suffix[1:].lower() if path.suffix else ""
            if actual_extension != current_extension:
                # Update path with correct extension
                path = path.with_suffix(f".{actual_extension}")
                logger.info(
                    f"Detected format mismatch: expected {current_extension}, "
                    f"detected {actual_extension} from Content-Type "
                    f"{content_type}. Using {path.name}"
                )

            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
                    callback(len(chunk))
        return path


class DeezerDownloadable(Downloadable):
    """Deezer-specific downloadable."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        quality: int,
        extension: str | None = None,
    ):
        """Initialize DeezerDownloadable.

        Args:
            session: aiohttp ClientSession
            url: Download URL
            quality: Quality level (0=MP3_128, 1=MP3_320, 2=FLAC)
            extension: File extension (auto-detected from quality if not
                provided)
        """
        self.session = session
        self.url = url
        self.quality = quality
        self._extension = extension
        self._size_cache: int | None = None

    @property
    def source(self) -> str:
        return "deezer"

    @property
    def extension(self) -> str:
        if self._extension:
            return self._extension
        # Auto-detect from quality
        quality_map = {0: "mp3", 1: "mp3", 2: "flac"}
        return quality_map.get(self.quality, "mp3")

    @property
    def _size(self) -> int:
        """Internal size property for compatibility."""
        if self._size_cache is None:
            return 0
        return self._size_cache

    async def size(self) -> int:
        """Get file size."""
        if self._size_cache is not None:
            return self._size_cache

        try:
            async with self.session.head(self.url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self._size_cache = int(content_length)
                    return self._size_cache

            # Fallback
            async with self.session.get(
                self.url, headers={"Range": "bytes=0-0"}
            ) as resp:
                content_range = resp.headers.get("Content-Range")
                if content_range:
                    total = content_range.split("/")[-1]
                    self._size_cache = int(total)
                    return self._size_cache

            logger.warning(f"Could not determine size for Deezer URL {self.url}")
            self._size_cache = 0
            return 0
        except Exception as e:
            logger.warning(f"Error getting size for Deezer stream: {e}")
            self._size_cache = 0
            return 0

    async def download(self, path: str | Path, callback: Callable[[int], None]) -> Path:
        """Download Deezer file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self.session.get(self.url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
                    callback(len(chunk))
        return path


class SoundcloudDownloadable(Downloadable):
    """Soundcloud-specific downloadable."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        extension: str = "mp3",
    ):
        """Initialize SoundcloudDownloadable.

        Args:
            session: aiohttp ClientSession
            url: Download URL
            extension: File extension (default: 'mp3')
        """
        self.session = session
        self.url = url
        self._extension = extension
        self._size_cache: int | None = None

    @property
    def source(self) -> str:
        return "soundcloud"

    @property
    def extension(self) -> str:
        return self._extension

    async def size(self) -> int:
        """Get file size."""
        if self._size_cache is not None:
            return self._size_cache

        try:
            async with self.session.head(self.url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self._size_cache = int(content_length)
                    return self._size_cache

            # Fallback
            async with self.session.get(
                self.url, headers={"Range": "bytes=0-0"}
            ) as resp:
                content_range = resp.headers.get("Content-Range")
                if content_range:
                    total = content_range.split("/")[-1]
                    self._size_cache = int(total)
                    return self._size_cache

            logger.warning(f"Could not determine size for Soundcloud URL {self.url}")
            self._size_cache = 0
            return 0
        except Exception as e:
            logger.warning(f"Error getting size for Soundcloud stream: {e}")
            self._size_cache = 0
            return 0

    async def download(self, path: str | Path, callback: Callable[[int], None]) -> Path:
        """Download Soundcloud file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with self.session.get(self.url) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
                    callback(len(chunk))
        return path
