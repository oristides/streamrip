import asyncio
import base64
import json
import logging
import re
import time
from json import JSONDecodeError
from xml.etree import ElementTree as ET  # noqa: N817

import aiohttp

from ..config import Config
from ..exceptions import NonStreamableError
from .client import Client
from .downloadable import TidalDownloadable

logger = logging.getLogger("streamrip")

BASE = "https://api.tidalhifi.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"

# NEW WORKING CREDENTIALS from omnunum's fixed version
CLIENT_ID = base64.b64decode("ZlgySnhkbW50WldLMGl4VA==").decode("iso-8859-1")
CLIENT_SECRET = base64.b64decode(
    "MU5tNUFmREFqeHJnSkZKYktOV0xlQXlLR1ZHbUlOdVhQUExIVlhBdnhBZz0=",
).decode("iso-8859-1")
AUTH = aiohttp.BasicAuth(login=CLIENT_ID, password=CLIENT_SECRET)
STREAM_URL_REGEX = re.compile(
    (
        r"#EXT-X-STREAM-INF:BANDWIDTH=\d+,AVERAGE-BANDWIDTH=\d+,CODECS=\""
        r"(?!jpeg)[^\"]+\",RESOLUTION=\d+x\d+\n(.+)"
    )
)

QUALITY_MAP = {
    0: "LOW",  # AAC
    1: "HIGH",  # AAC
    2: "LOSSLESS",  # CD Quality
    3: "HI_RES_LOSSLESS",  # Hi-Res (was MQA, now lossless)
}


class TidalClient(Client):
    """TidalClient."""

    source = "tidal"
    max_quality = 3

    def __init__(self, config: Config):
        self.logged_in = False
        self.global_config = config
        self.config = config.session.tidal
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )

    async def login(self):
        self.session = await self.get_session(
            verify_ssl=self.global_config.session.downloads.verify_ssl
        )
        c = self.config
        if not c.access_token:
            raise Exception("Access token not found in config.")

        self.token_expiry = float(c.token_expiry)
        self.refresh_token = c.refresh_token

        if self.token_expiry - time.time() < 86400:  # 1 day
            await self._refresh_access_token()
        else:
            await self._login_by_access_token(c.access_token, c.user_id)

        self.logged_in = True

    async def get_metadata(self, item_id: str, media_type: str) -> dict:
        """Send a request to the api for information.

        :param item_id:
        :type item_id: str
        :param media_type: track, album, playlist, or video.
        :type media_type: str
        :rtype: dict
        """
        assert media_type in (
            "track",
            "album",
            "playlist",
            "video",
            "artist",
        ), media_type

        url = f"{media_type}s/{item_id}"
        item = await self._api_request(url)
        if media_type in ("playlist", "album"):
            # TODO: move into new method and make concurrent
            resp = await self._api_request(f"{url}/items")
            tracks_left = item["numberOfTracks"]
            if tracks_left > 100:
                offset = 0
                while tracks_left > 0:
                    offset += 100
                    tracks_left -= 100
                    items_resp = await self._api_request(
                        f"{url}/items", {"offset": offset}
                    )
                    resp["items"].extend(items_resp["items"])

            item["tracks"] = [item["item"] for item in resp["items"]]
        elif media_type == "artist":
            logger.debug("filtering eps")
            album_resp, ep_resp = await asyncio.gather(
                self._api_request(f"{url}/albums"),
                self._api_request(f"{url}/albums", params={"filter": "EPSANDSINGLES"}),
            )

            item["albums"] = album_resp["items"]
            item["albums"].extend(ep_resp["items"])
        elif media_type == "track":
            try:
                resp = await self._api_request(
                    f"tracks/{item_id!s}/lyrics",
                    base="https://listen.tidal.com/v1",
                )

                # Use unsynced lyrics for MP3, synced for others (FLAC, OPUS, etc)
                if (
                    self.global_config.session.conversion.enabled
                    and self.global_config.session.conversion.codec.upper() == "MP3"
                ):
                    item["lyrics"] = resp.get("lyrics") or ""
                else:
                    item["lyrics"] = resp.get("subtitles") or resp.get("lyrics") or ""
            except NonStreamableError:
                # Lyrics endpoint returns 404 for tracks without lyrics (very common)
                # This is normal and not an error - silently skip
                item["lyrics"] = ""
            except Exception as e:
                # Other exceptions (network errors, etc) - log at debug level
                error_type = type(e).__name__
                logger.debug(f"Failed to get lyrics for {item_id}: {error_type}")
                item["lyrics"] = ""  # Set empty lyrics on error

        # Don't log the full item as it might contain format characters
        # that break Python's logging system (which uses % formatting)
        logger.debug(f"Metadata retrieved for track {item_id}")
        return item

    async def get_user_playlists(self) -> list[dict]:
        """Return user's saved/owned playlists with id, name, and url.

        Prefers the OpenAPI v2 "userCollections" endpoint to retrieve playlists.
        Falls back to the v1 users playlists endpoint if available.
        """
        user_id = self.config.user_id
        playlists: list[dict] = []

        # First try v2 userCollections with include=playlists
        try:
            resp = await self._api_request(
                f"userCollections/{user_id}",
                params={"include": "playlists"},
                base="https://openapi.tidal.com/v2",
            )

            included = resp.get("included") or []
            for item in included:
                if item.get("type") == "playlists":
                    pid = str(item.get("id"))
                    attrs = item.get("attributes") or {}
                    title = (
                        attrs.get("title")
                        or attrs.get("name")
                        or item.get("title")
                        or item.get("name")
                        or pid
                    )
                    playlists.append(
                        {
                            "id": pid,
                            "name": title,
                            "url": f"https://tidal.com/playlist/{pid}",
                        }
                    )

            if playlists:
                return playlists
        except Exception as e:
            logger.debug(f"Falling back to v1 playlists due to: {e}")

        # Fallback: try v1 users playlists
        try:
            resp_v1 = await self._api_request(f"users/{user_id}/playlists")
            for p in resp_v1.get("items", []):
                pid = str(p.get("uuid") or p.get("id"))
                if not pid:
                    continue
                title = p.get("title") or p.get("name") or pid
                playlists.append(
                    {
                        "id": pid,
                        "name": title,
                        "url": f"https://tidal.com/playlist/{pid}",
                    }
                )
        except Exception as e:
            logger.debug(f"Unable to retrieve v1 playlists: {e}")

        return playlists

    async def get_recommended_playlists(self) -> list[dict]:
        """Return recommended discovery mix playlists for the current user.

        Uses the OpenAPI v2 userRecommendations discoveryMixes relationship.
        """
        user_id = self.config.user_id
        playlists: list[dict] = []

        try:
            resp = await self._api_request(
                f"userRecommendations/{user_id}/relationships/discoveryMixes",
                params={"include": "discoveryMixes"},
                base="https://openapi.tidal.com/v2",
            )

            # Data can be in data and/or included arrays
            candidates = []
            data_arr = resp.get("data") or []
            incl_arr = resp.get("included") or []
            if isinstance(data_arr, list):
                candidates.extend(data_arr)
            if isinstance(incl_arr, list):
                candidates.extend(incl_arr)

            seen: set[str] = set()
            for item in candidates:
                if item.get("type") != "playlists":
                    continue
                pid = str(item.get("id"))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                attrs = item.get("attributes") or {}
                title = (
                    attrs.get("title")
                    or attrs.get("name")
                    or item.get("title")
                    or item.get("name")
                    or pid
                )
                playlists.append(
                    {
                        "id": pid,
                        "name": title,
                        "url": f"https://tidal.com/playlist/{pid}",
                    }
                )
        except Exception as e:
            logger.debug(f"Unable to retrieve discovery mixes: {e}")

        return playlists

    async def get_user_albums(self) -> list[dict]:
        """Return user's saved/owned albums with id, name, artists, and url.

        Uses the v1 favorites endpoint with pagination to ensure full coverage.
        """
        user_id = self.config.user_id
        albums: list[dict] = []

        try:
            offset = 0
            while True:
                resp = await self._api_request(
                    f"users/{user_id}/favorites/albums", params={"offset": offset}
                )
                items = resp.get("items", [])
                if not items:
                    break

                for p in items:
                    album = p.get("item") or p
                    aid = str(album.get("id"))
                    if not aid:
                        continue
                    name = album.get("title") or album.get("name") or aid
                    # artists can be a list under 'artists' or a single under 'artist'
                    artist_names: list[str] = []
                    if isinstance(album.get("artists"), list):
                        artist_names = [a.get("name", "") for a in album["artists"]]
                    elif isinstance(album.get("artist"), dict):
                        artist_names = [album["artist"].get("name", "")]
                    artists_joined = ", ".join([a for a in artist_names if a])

                    albums.append(
                        {
                            "id": aid,
                            "name": name,
                            "artists": artists_joined,
                            "url": f"https://tidal.com/album/{aid}",
                        }
                    )

                offset += len(items)
        except Exception as e:
            logger.debug(f"Unable to retrieve v1 albums: {e}")

        return albums

    async def get_recommended_albums(self) -> list[dict]:
        """Return recommended albums for the current user.

        Uses the OpenAPI v2 userRecommendations albums relationship.
        """
        user_id = self.config.user_id
        albums: list[dict] = []

        try:
            resp = await self._api_request(
                f"userRecommendations/{user_id}/relationships/albums",
                params={"include": "albums"},
                base="https://openapi.tidal.com/v2",
            )
            candidates: list[dict] = []
            data_arr = resp.get("data") or []
            incl_arr = resp.get("included") or []
            if isinstance(data_arr, list):
                candidates.extend(data_arr)
            if isinstance(incl_arr, list):
                candidates.extend(incl_arr)

            seen: set[str] = set()
            for item in candidates:
                if item.get("type") != "albums":
                    continue
                aid = str(item.get("id"))
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                attrs = item.get("attributes") or {}
                name = (
                    attrs.get("title")
                    or attrs.get("name")
                    or item.get("title")
                    or item.get("name")
                    or aid
                )
                # Artists may be provided as a list or a string
                artist_names: list[str] = []
                if isinstance(attrs.get("artists"), list):
                    artist_names = [a.get("name", "") for a in attrs["artists"]]
                artist_str = attrs.get("artistName") or ", ".join(
                    [a for a in artist_names if a]
                )

                albums.append(
                    {
                        "id": aid,
                        "name": name,
                        "artists": artist_str or "",
                        "url": f"https://tidal.com/album/{aid}",
                    }
                )
        except Exception as e:
            logger.debug(f"Unable to retrieve recommended albums: {e}")

        return albums

    async def search(self, media_type: str, query: str, limit: int = 100) -> list[dict]:
        """Search for a query.

        :param query:
        :type query: str
        :param media_type: track, album, playlist, or video.
        :type media_type: str
        :param limit: max is 100
        :type limit: int
        :rtype: dict
        """
        params = {
            "query": query,
            "limit": limit,
        }
        assert media_type in ("album", "track", "playlist", "video", "artist")
        resp = await self._api_request(f"search/{media_type}s", params=params)
        if len(resp["items"]) > 1:
            return [resp]
        return []

    async def get_downloadable(self, track_id: str, quality: int):
        # Map generic quality int to Tidal-specific format
        quality_map = ["LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS"]

        # Fallback order: 3 → 2 → 1 → 0 (will go all the way down if needed)
        fallback_qualities = [3, 2, 1, 0]
        if quality not in fallback_qualities:
            fallback_qualities = [quality]
        else:
            # Start from requested quality and go down
            start_idx = fallback_qualities.index(quality)
            fallback_qualities = fallback_qualities[start_idx:]

        last_error = None
        last_quality = quality

        for attempt_quality in fallback_qualities:
            tidal_quality = quality_map[attempt_quality]

            # Try DOWNLOAD mode first for complete files, fallback to STREAM if needed
            # DOWNLOAD mode returns complete files, STREAM mode can return fragments
            playback_modes = ["DOWNLOAD", "STREAM"]
            playback_error = None

            for playback_mode in playback_modes:
                params = {
                    "audioquality": tidal_quality,
                    "playbackmode": playback_mode,
                    "assetpresentation": "FULL",
                }

                try:
                    resp = await self._api_request(
                        f"tracks/{track_id}/playbackinfo", params
                    )
                    manifest_b64 = resp["manifest"]
                    manifest_mime = resp.get(
                        "manifestMimeType", "application/vnd.tidal.bts"
                    )

                    # Parse manifest based on MIME type
                    if manifest_mime == "application/dash+xml":
                        manifest_data = await self._parse_dash_manifest(manifest_b64)
                        logger.debug(
                            f"Parsed DASH manifest for track {track_id}: "
                            f"{len(manifest_data.get('urls', []))} segments"
                        )
                    else:  # application/vnd.tidal.bts or fallback
                        manifest_decoded = base64.b64decode(manifest_b64).decode(
                            "utf-8"
                        )
                        manifest_data = json.loads(manifest_decoded)
                        logger.debug(
                            f"Parsed BTS manifest for track {track_id}: "
                            f"URL count: {len(manifest_data.get('urls', []))}"
                        )

                except (KeyError, JSONDecodeError, NonStreamableError) as e:
                    error_msg = (
                        resp.get("userMessage", str(e))
                        if "resp" in locals()
                        else str(e)
                    )
                    playback_error = e

                    # Try next playback mode if available
                    if playback_mode != playback_modes[-1]:
                        logger.debug(
                            f"Playback mode {playback_mode} failed for track "
                            f"{track_id}, trying {playback_modes[1]}"
                        )
                        continue
                    else:
                        # Both playback modes failed, try next quality
                        last_error = playback_error
                        last_quality = attempt_quality

                        # If fallback is enabled and not the last quality, try next
                        if (
                            self.config.lower_quality_if_not_available
                            and attempt_quality != fallback_qualities[-1]
                        ):
                            logger.warning(
                                f"Quality {attempt_quality} not available for track "
                                f"{track_id}, trying quality {attempt_quality - 1}"
                            )
                            break  # Break out of playback_mode loop
                        else:
                            # No more fallbacks or fallback disabled
                            if isinstance(e, KeyError):
                                raise Exception(f"Missing manifest data: {error_msg}")
                            elif isinstance(e, NonStreamableError):
                                raise
                            else:  # JSONDecodeError
                                raise Exception(
                                    f"Failed to decode manifest for track "
                                    f"{track_id}: {error_msg}"
                                )

                # Handle both single URL and URL list formats
                urls = manifest_data.get("urls", [])
                if isinstance(urls, list) and len(urls) > 0:
                    # DASH manifests return a list of segment URLs that need to be concatenated
                    # BTS manifests return a single URL or list with one URL

                    # Check if we have multiple segments (DASH format)
                    # DASH can have: urls = [url1, url2, url3, ...] (list of strings)
                    # Or: urls = [[url1, url2, ...]] (list containing a list)
                    has_multiple_segments = len(urls) > 1
                    if not has_multiple_segments and isinstance(urls[0], list):
                        has_multiple_segments = len(urls[0]) > 1

                    if has_multiple_segments:
                        # Multiple segments - DASH manifest requires downloading all segments
                        segment_list = urls[0] if isinstance(urls[0], list) else urls
                        logger.info(
                            f"DASH manifest detected for track {track_id}: "
                            f"{len(segment_list)} segments will be downloaded and concatenated"
                        )
                        url = segment_list  # Pass list of URLs to TidalDownloadable
                    else:
                        # Single URL (BTS manifest or single segment)
                        url = urls[0] if not isinstance(urls[0], list) else urls[0][0]
                        logger.debug(
                            f"Single URL manifest for track {track_id}: {url[:80]}..."
                        )
                else:
                    url = None

                # If URL is None, try next playback mode
                if url is None:
                    if playback_mode != playback_modes[-1]:
                        logger.debug(
                            f"Playback mode {playback_mode} returned no URL for "
                            f"track {track_id}, trying {playback_modes[1]}"
                        )
                        continue
                    else:
                        # Both playback modes returned no URL, try next quality
                        if (
                            self.config.lower_quality_if_not_available
                            and attempt_quality != fallback_qualities[-1]
                        ):
                            logger.warning(
                                f"Quality {attempt_quality} returned no URL for "
                                f"track {track_id}, trying quality "
                                f"{attempt_quality - 1}"
                            )
                            break  # Break out of playback_mode loop
                        else:
                            # No URL and no more fallbacks
                            raise NonStreamableError(
                                f"No stream URL available for track {track_id} "
                                f"at quality {attempt_quality}"
                            )

                # Success - return downloadable with this quality and playback mode
                if attempt_quality != quality:
                    logger.info(
                        f"Downloading track {track_id} at quality "
                        f"{attempt_quality} (requested {quality} was not "
                        f"available) using {playback_mode} mode"
                    )
                elif playback_mode == "DOWNLOAD":
                    logger.debug(
                        f"Using DOWNLOAD mode for track {track_id} "
                        "(complete file, not fragmented)"
                    )

                return TidalDownloadable(
                    self.session,
                    url=url,
                    codec=manifest_data["codecs"],
                    restrictions=manifest_data.get("restrictions"),
                )

        # Should never reach here, but handle just in case
        if last_error:
            raise last_error
        raise NonStreamableError(
            f"Could not get downloadable for track {track_id} at any quality"
        )

    async def get_video_file_url(self, video_id: str) -> str:
        """Get the HLS video stream url.

        The stream is downloaded using ffmpeg for now.

        :param video_id:
        :type video_id: str
        :rtype: str
        """
        params = {
            "videoquality": "HIGH",
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
        }
        resp = await self._api_request(f"videos/{video_id}/playbackinfo", params=params)
        manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
        async with self.session.get(manifest["urls"][0]) as resp:
            available_urls = await resp.json()
        available_urls.encoding = "utf-8"

        # Highest resolution is last
        *_, last_match = STREAM_URL_REGEX.finditer(available_urls.text)

        return last_match.group(1)

    async def _parse_dash_manifest(self, manifest_b64: str) -> dict:
        """Parse DASH XML manifest into a format compatible with TidalDownloadable.

        DASH manifests use MPEG-DASH format with segment templates.
        Returns a dict with: urls (list of segment URLs), codecs, encryptionType
        """
        try:
            manifest_xml = base64.b64decode(manifest_b64).decode("utf-8")
            root = ET.fromstring(manifest_xml)

            # Define XML namespace for DASH
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}

            # Find the first Representation element (highest quality is typically listed first or last)
            representation = root.find(".//mpd:Representation", ns)
            if representation is None:
                raise Exception("No Representation found in DASH manifest")

            # Extract codec info
            codecs = representation.get("codecs", "flac")

            # Find SegmentTemplate
            segment_template = representation.find(".//mpd:SegmentTemplate", ns)
            if segment_template is None:
                raise Exception("No SegmentTemplate found in DASH manifest")

            # Get media URL template
            media_template = segment_template.get("media")
            start_number = int(segment_template.get("startNumber", "0"))

            # Get SegmentTimeline to determine number of segments
            timeline = segment_template.find("mpd:SegmentTimeline", ns)
            if timeline is None:
                raise Exception("No SegmentTimeline found in DASH manifest")

            segments = timeline.findall("mpd:S", ns)

            # Calculate total number of segments from timeline
            segment_urls = []
            segment_number = start_number

            for seg in segments:
                repeat = int(seg.get("r", "0"))
                # r=-1 means repeat until end, r=0 means 1 segment, r=N means N+1 segments
                num_segments = repeat + 1 if repeat >= 0 else 1

                for _ in range(num_segments):
                    # Replace $Number$ placeholder with actual segment number
                    url = media_template.replace("$Number$", str(segment_number))
                    segment_urls.append(url)
                    segment_number += 1

            # Return in BTS manifest-compatible format
            return {
                "urls": segment_urls,
                "codecs": codecs,
                "encryptionType": "NONE",  # DASH manifests from tiddl analysis show no encryption
                "mimeType": f"audio/{codecs}",
            }

        except ET.ParseError as e:
            raise Exception(f"Failed to parse DASH XML manifest: {e}")

    # ---------- Login Utilities ---------------

    async def _login_by_access_token(self, token: str, user_id: str):
        """Login using the access token.

        Used after the initial authorization.

        :param token: access token
        :param user_id: To verify that the user is correct
        """
        headers = {"authorization": f"Bearer {token}"}  # temporary
        async with self.session.get(
            "https://api.tidal.com/v1/sessions",
            headers=headers,
        ) as _resp:
            resp = await _resp.json()

        if resp.get("status", 200) != 200:
            raise Exception(f"Login failed {resp}")

        if str(resp.get("userId")) != str(user_id):
            raise Exception(f"User id mismatch {resp['userId']} v {user_id}")

        c = self.config
        c.user_id = resp["userId"]
        c.country_code = resp["countryCode"]
        c.access_token = token
        self._update_authorization_from_config()

    async def _get_login_link(self) -> str:
        data = {
            "client_id": CLIENT_ID,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/device_authorization", data)

        if resp.get("status", 200) != 200:
            raise Exception(f"Device authorization failed {resp}")

        device_code = resp["deviceCode"]
        return f"https://{device_code}"

    def _update_authorization_from_config(self):
        self.session.headers.update(
            {"authorization": f"Bearer {self.config.access_token}"},
        )

    async def _get_auth_status(self, device_code) -> tuple[int, dict[str, int | str]]:
        """Check if the user has logged in inside the browser.

        returns (status, authentication info)
        """
        data = {
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": "r_usr+w_usr+w_sub",
        }
        logger.debug("Checking with %s", data)
        resp = await self._api_post(f"{AUTH_URL}/token", data, AUTH)

        if "status" in resp and resp["status"] != 200:
            if resp["status"] == 400 and resp["sub_status"] == 1002:
                return 2, {}
            else:
                return 1, {}

        ret = {}
        ret["user_id"] = resp["user"]["userId"]
        ret["country_code"] = resp["user"]["countryCode"]
        ret["access_token"] = resp["access_token"]
        ret["refresh_token"] = resp["refresh_token"]
        ret["token_expiry"] = resp["expires_in"] + time.time()
        return 0, ret

    async def _refresh_access_token(self):
        """Refresh the access token given a refresh token.

        The access token expires in a week, so it must be refreshed.
        Requires a refresh token.
        """
        data = {
            "client_id": CLIENT_ID,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/token", data, AUTH)

        if resp.get("status", 200) != 200:
            raise Exception("Refresh failed")

        c = self.config
        c.access_token = resp["access_token"]
        c.token_expiry = resp["expires_in"] + time.time()
        self._update_authorization_from_config()

    async def _get_device_code(self) -> tuple[str, str]:
        """Get the device code that will be used to log in on the browser."""
        if not hasattr(self, "session"):
            self.session = await self.get_session()

        data = {
            "client_id": CLIENT_ID,
            "scope": "r_usr+w_usr+w_sub",
        }
        resp = await self._api_post(f"{AUTH_URL}/device_authorization", data)

        if resp.get("status", 200) != 200:
            raise Exception(f"Device authorization failed {resp}")

        return resp["deviceCode"], resp["verificationUriComplete"]

    # ---------- API Request Utilities ---------------

    async def _api_post(self, url, data, auth: aiohttp.BasicAuth | None = None) -> dict:
        """Post to the Tidal API. Status not checked!

        :param url:
        :param data:
        :param auth:
        """
        async with self.rate_limiter:
            async with self.session.post(url, data=data, auth=auth) as resp:
                return await resp.json()

    async def _api_request(self, path: str, params=None, base: str = BASE) -> dict:
        """Handle Tidal API requests.

        :param path:
        :type path: str
        :param params:
        :rtype: dict
        """
        if params is None:
            params = {}

        params["countryCode"] = self.config.country_code
        params["limit"] = 100

        async with self.rate_limiter:
            async with self.session.get(f"{base}/{path}", params=params) as resp:
                if resp.status == 404:
                    # Don't log warning for lyrics endpoint - 404 is normal when no lyrics exist
                    # Only log for actual track/album/etc endpoints
                    if "/lyrics" not in path:
                        logger.warning(f"TIDAL: track not found (status {resp.status})")
                    raise NonStreamableError("TIDAL: Track not found")
                resp.raise_for_status()
                return await resp.json()

    # ---------- Comparison Methods ---------------

    async def compare_collections_with_downloads(
        self,
        playlists=False,
        albums=False,
        recommended=False,
        missing_only=False,
        db=None,
    ):
        """Compare TIDAL collections with downloaded files to find missing tracks.

        Args:
            playlists: Include playlists in comparison
            albums: Include albums in comparison
            recommended: Include recommended content
            missing_only: Show only missing tracks
            db: Database instance to check downloaded tracks

        Returns:
            dict: Comparison results with missing tracks
        """
        from rich.console import Console

        console = Console()
        results = {
            "playlists": {"total_tracks": 0, "missing_tracks": 0, "missing": []},
            "albums": {"total_tracks": 0, "missing_tracks": 0, "missing": []},
            "recommended_playlists": {
                "total_tracks": 0,
                "missing_tracks": 0,
                "missing": [],
            },
            "recommended_albums": {
                "total_tracks": 0,
                "missing_tracks": 0,
                "missing": [],
            },
        }

        if playlists:
            console.print("[blue]📋 Checking playlists...")
            playlists_data = await self.get_user_playlists()
            for playlist in playlists_data:
                tracks = await self._get_playlist_tracks(playlist["id"])
                missing = self._find_missing_tracks(tracks, db)
                results["playlists"]["total_tracks"] += len(tracks)
                results["playlists"]["missing_tracks"] += len(missing)
                results["playlists"]["missing"].extend(missing)

            if recommended:
                console.print("[blue]🎯 Checking recommended playlists...")
                rec_playlists = await self.get_recommended_playlists()
                for playlist in rec_playlists:
                    tracks = await self._get_playlist_tracks(playlist["id"])
                    missing = self._find_missing_tracks(tracks, db)
                    results["recommended_playlists"]["total_tracks"] += len(tracks)
                    results["recommended_playlists"]["missing_tracks"] += len(missing)
                    results["recommended_playlists"]["missing"].extend(missing)

        if albums:
            console.print("[blue]💿 Checking albums...")
            albums_data = await self.get_user_albums()
            for album in albums_data:
                tracks = await self._get_album_tracks(album["id"])
                missing = self._find_missing_tracks(tracks, db)
                results["albums"]["total_tracks"] += len(tracks)
                results["albums"]["missing_tracks"] += len(missing)
                results["albums"]["missing"].extend(missing)

            if recommended:
                console.print("[blue]🎯 Checking recommended albums...")
                rec_albums = await self.get_recommended_albums()
                for album in rec_albums:
                    tracks = await self._get_album_tracks(album["id"])
                    missing = self._find_missing_tracks(tracks, db)
                    results["recommended_albums"]["total_tracks"] += len(tracks)
                    results["recommended_albums"]["missing_tracks"] += len(missing)
                    results["recommended_albums"]["missing"].extend(missing)

        # Display results
        self._display_comparison_results(results, missing_only)

        return results

    async def get_missing_tracks_for_download(
        self, playlists=False, albums=False, recommended=False, db=None
    ):
        """Get URLs of missing tracks for download.

        Args:
            playlists: Include playlists
            albums: Include albums
            recommended: Include recommended content
            db: Database instance to check downloaded tracks

        Returns:
            list: URLs of missing tracks
        """
        from rich.console import Console

        console = Console()
        missing_urls = []

        if playlists:
            console.print("[blue]📋 Getting missing tracks from playlists...")
            playlists_data = await self.get_user_playlists()
            for playlist in playlists_data:
                tracks = await self._get_playlist_tracks(playlist["id"])
                missing = self._find_missing_tracks(tracks, db)
                for track in missing:
                    track_id = track.get("id")
                    if track_id:
                        missing_urls.append(f"https://tidal.com/track/{track_id}")

            if recommended:
                console.print(
                    "[blue]🎯 Getting missing tracks from recommended playlists..."
                )
                rec_playlists = await self.get_recommended_playlists()
                for playlist in rec_playlists:
                    tracks = await self._get_playlist_tracks(playlist["id"])
                    missing = self._find_missing_tracks(tracks, db)
                    for track in missing:
                        track_id = track.get("id")
                        if track_id:
                            missing_urls.append(f"https://tidal.com/track/{track_id}")

        if albums:
            console.print("[blue]💿 Getting missing tracks from albums...")
            albums_data = await self.get_user_albums()
            for album in albums_data:
                tracks = await self._get_album_tracks(album["id"])
                missing = self._find_missing_tracks(tracks, db)
                for track in missing:
                    track_id = track.get("id")
                    if track_id:
                        missing_urls.append(f"https://tidal.com/track/{track_id}")

            if recommended:
                console.print(
                    "[blue]🎯 Getting missing tracks from recommended albums..."
                )
                rec_albums = await self.get_recommended_albums()
                for album in rec_albums:
                    tracks = await self._get_album_tracks(album["id"])
                    missing = self._find_missing_tracks(tracks, db)
                    for track in missing:
                        track_id = track.get("id")
                        if track_id:
                            missing_urls.append(f"https://tidal.com/track/{track_id}")

        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in missing_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        return unique_urls

    async def get_missing_tracks_with_context(
        self, playlists=False, albums=False, recommended=False, db=None
    ):
        """Get missing tracks with playlist context for proper folder organization.

        Args:
            playlists: Include playlists
            albums: Include albums
            recommended: Include recommended content
            db: Database instance to check downloaded tracks

        Returns:
            dict: Dictionary with playlist names as keys and lists of track data as values
        """
        from rich.console import Console

        console = Console()
        missing_tracks_by_playlist = {}
        missing_urls = []  # For backward compatibility with albums

        if playlists:
            console.print("[blue]📋 Getting missing tracks from playlists...")
            playlists_data = await self.get_user_playlists()
            for playlist in playlists_data:
                playlist_name = playlist.get("name", "Unknown Playlist")
                tracks = await self._get_playlist_tracks(playlist["id"])
                missing = self._find_missing_tracks(tracks, db)

                if missing:
                    missing_tracks_by_playlist[playlist_name] = []
                    for track in missing:
                        track_id = track.get("id")
                        if track_id:
                            missing_tracks_by_playlist[playlist_name].append(
                                {
                                    "id": track_id,
                                    "url": f"https://tidal.com/track/{track_id}",
                                    "playlist_id": playlist["id"],
                                    "playlist_name": playlist_name,
                                }
                            )

            if recommended:
                console.print(
                    "[blue]🎯 Getting missing tracks from recommended playlists..."
                )
                rec_playlists = await self.get_recommended_playlists()
                for playlist in rec_playlists:
                    playlist_name = playlist.get("name", "Unknown Playlist")
                    tracks = await self._get_playlist_tracks(playlist["id"])
                    missing = self._find_missing_tracks(tracks, db)

                    if missing:
                        if playlist_name not in missing_tracks_by_playlist:
                            missing_tracks_by_playlist[playlist_name] = []
                        for track in missing:
                            track_id = track.get("id")
                            if track_id:
                                missing_tracks_by_playlist[playlist_name].append(
                                    {
                                        "id": track_id,
                                        "url": f"https://tidal.com/track/{track_id}",
                                        "playlist_id": playlist["id"],
                                        "playlist_name": playlist_name,
                                    }
                                )

        if albums:
            console.print("[blue]💿 Getting missing tracks from albums...")
            albums_data = await self.get_user_albums()
            for album in albums_data:
                tracks = await self._get_album_tracks(album["id"])
                missing = self._find_missing_tracks(tracks, db)
                for track in missing:
                    track_id = track.get("id")
                    if track_id:
                        missing_urls.append(f"https://tidal.com/track/{track_id}")

            if recommended:
                console.print(
                    "[blue]🎯 Getting missing tracks from recommended albums..."
                )
                rec_albums = await self.get_recommended_albums()
                for album in rec_albums:
                    tracks = await self._get_album_tracks(album["id"])
                    missing = self._find_missing_tracks(tracks, db)
                    for track in missing:
                        track_id = track.get("id")
                        if track_id:
                            missing_urls.append(f"https://tidal.com/track/{track_id}")

        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in missing_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        # Return the appropriate data structure based on what was requested
        if playlists:
            total_playlist_tracks = sum(
                len(tracks) for tracks in missing_tracks_by_playlist.values()
            )
            console.print(
                f"[green]Found {total_playlist_tracks} missing tracks across {len(missing_tracks_by_playlist)} playlists"
            )
            return missing_tracks_by_playlist
        else:
            console.print(f"[green]Found {len(unique_urls)} unique missing tracks")
            return unique_urls

    async def _get_playlist_tracks(self, playlist_id):
        """Get tracks from a specific playlist with pagination support."""
        try:
            # Get initial page
            tracks_data = await self._api_request(f"playlists/{playlist_id}/tracks")
            all_tracks = tracks_data.get("items", [])

            # Check if there are more tracks to fetch (pagination)
            total_tracks = tracks_data.get("totalNumberOfItems", len(all_tracks))
            tracks_left = total_tracks - len(all_tracks)

            # Fetch remaining pages if needed
            offset = 100  # Start from second page
            while tracks_left > 0:
                try:
                    page_data = await self._api_request(
                        f"playlists/{playlist_id}/tracks",
                        {"offset": offset, "limit": 100},
                    )
                    page_tracks = page_data.get("items", [])
                    all_tracks.extend(page_tracks)

                    tracks_left -= len(page_tracks)
                    offset += 100

                    # Safety check to prevent infinite loops
                    if len(page_tracks) == 0:
                        break

                except Exception as e:
                    logger.warning(
                        f"Failed to fetch page at offset {offset} for playlist {playlist_id}: {e}"
                    )
                    break

            logger.debug(
                f"Fetched {len(all_tracks)} tracks from playlist {playlist_id} (total: {total_tracks})"
            )
            return all_tracks

        except Exception as e:
            logger.warning(f"Failed to get tracks for playlist {playlist_id}: {e}")
            return []

    async def _get_album_tracks(self, album_id):
        """Get tracks from a specific album."""
        try:
            tracks_data = await self._api_request(f"albums/{album_id}/tracks")
            return tracks_data.get("items", [])
        except Exception as e:
            logger.warning(f"Failed to get tracks for album {album_id}: {e}")
            return []

    def _find_missing_tracks(self, collection_tracks, db):
        """Find tracks that are in collection but not downloaded."""
        from rich.console import Console

        console = Console()
        missing = []
        total_tracks = len(collection_tracks)
        already_downloaded = 0

        for track in collection_tracks:
            track_id = track.get("id")
            if track_id and db:
                is_downloaded = db.downloaded(track_id)
                if not is_downloaded:
                    missing.append(track)
                else:
                    already_downloaded += 1
            elif track_id and not db:
                # If no database provided, assume all tracks are missing
                missing.append(track)

        if total_tracks > 0:
            console.print(
                f"[blue]🔍 Filtered {total_tracks} tracks: {len(missing)} missing, {already_downloaded} already downloaded"
            )

            # Debug: Show first few missing track IDs
            if len(missing) > 0 and len(missing) <= 5:
                missing_ids = [track.get("id") for track in missing if track.get("id")]
                console.print(f"[yellow]🔍 Missing track IDs: {missing_ids}")
            elif len(missing) > 5:
                missing_ids = [
                    track.get("id") for track in missing[:3] if track.get("id")
                ]
                console.print(f"[yellow]🔍 First 3 missing track IDs: {missing_ids}")

        return missing

    def _display_comparison_results(self, results, missing_only=False):
        """Display comparison results in a nice table."""
        from rich.console import Console
        from rich.table import Table

        console = Console()

        table = Table(title="TIDAL Collection vs Downloads Comparison")
        table.add_column("Collection Type", style="cyan")
        table.add_column("Total Tracks", style="green")
        table.add_column("Missing Tracks", style="red")
        table.add_column("Downloaded", style="green")

        for collection_type, data in results.items():
            if data["total_tracks"] > 0:
                downloaded = data["total_tracks"] - data["missing_tracks"]
                table.add_row(
                    collection_type.replace("_", " ").title(),
                    str(data["total_tracks"]),
                    str(data["missing_tracks"]),
                    str(downloaded),
                )

        console.print(table)

        if missing_only:
            console.print("\n[yellow]Missing tracks:")
            for collection_type, data in results.items():
                if data["missing"]:
                    console.print(
                        f"\n[blue]{collection_type.replace('_', ' ').title()}:"
                    )
                    for track in data["missing"][:10]:  # Show first 10
                        console.print(
                            f"  - {track.get('title', 'Unknown')} by {track.get('artist', {}).get('name', 'Unknown')}"
                        )
                    if len(data["missing"]) > 10:
                        console.print(f"  ... and {len(data['missing']) - 10} more")
