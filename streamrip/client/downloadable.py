"""Downloadable classes for different streaming services."""

import logging
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
        url: str | None,
        codec: str,
        restrictions: list | None = None,
    ):
        """Initialize TidalDownloadable.

        Args:
            session: aiohttp ClientSession
            url: Stream URL (may be None if unavailable)
            codec: Audio codec (e.g., 'flac', 'mqa', 'aac')
            restrictions: List of restrictions (if any)
        """
        self.session = session
        self.url = url
        self.codec = codec.lower()
        self.restrictions = restrictions or []
        self._size_cache: int | None = None

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
        """Get file size from stream URL."""
        if self._size_cache is not None:
            return self._size_cache

        if not self.url:
            self._size_cache = 0
            return 0

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
        """Download Tidal stream."""
        if not self.url:
            raise ValueError("No URL available for Tidal download")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

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
