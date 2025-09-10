import asyncio
import base64
import json
import logging
import re
import time
from json import JSONDecodeError

import aiohttp

from ..config import Config
from ..exceptions import NonStreamableError
from .client import Client
from .downloadable import TidalDownloadable

logger = logging.getLogger("streamrip")

BASE = "https://api.tidalhifi.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"

CLIENT_ID = base64.b64decode("elU0WEhWVmtjMnREUG80dA==").decode("iso-8859-1")
CLIENT_SECRET = base64.b64decode(
    "VkpLaERGcUpQcXZzUFZOQlY2dWtYVEptd2x2YnR0UDd3bE1scmM3MnNlND0=",
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
    3: "HI_RES",  # MQA
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
                self._api_request(
                    f"{url}/albums", params={"filter": "EPSANDSINGLES"}
                ),
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
                    item["lyrics"] = (
                        resp.get("subtitles") or resp.get("lyrics") or ""
                    )
            except TypeError as e:
                logger.warning(f"Failed to get lyrics for {item_id}: {e}")

        logger.debug(item)
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
        params = {
            "audioquality": QUALITY_MAP[quality],
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
        }
        resp = await self._api_request(
            f"tracks/{track_id}/playbackinfopostpaywall", params
        )
        logger.debug(resp)
        try:
            manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
        except KeyError:
            raise Exception(resp["userMessage"])
        except JSONDecodeError:
            logger.warning(
                (
                    f"Failed to get manifest for {track_id}. Retrying with lower"
                    " quality."
                )
            )
            return await self.get_downloadable(track_id, quality - 1)

        logger.debug(manifest)
        enc_key = manifest.get("keyId")
        if manifest.get("encryptionType") == "NONE":
            enc_key = None
        return TidalDownloadable(
            self.session,
            url=manifest["urls"][0],
            codec=manifest["codecs"],
            encryption_key=enc_key,
            restrictions=manifest.get("restrictions"),
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
        resp = await self._api_request(
            f"videos/{video_id}/playbackinfopostpaywall", params=params
        )
        manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
        async with self.session.get(manifest["urls"][0]) as resp:
            available_urls = await resp.json()
        available_urls.encoding = "utf-8"

        # Highest resolution is last
        *_, last_match = STREAM_URL_REGEX.finditer(available_urls.text)

        return last_match.group(1)

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
                    logger.warning("TIDAL: track not found", resp)
                    raise NonStreamableError("TIDAL: Track not found")
                resp.raise_for_status()
                return await resp.json()

    # ---------- Comparison Methods ---------------

    async def compare_collections_with_downloads(self, playlists=False, albums=False, recommended=False, missing_only=False, db=None):
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
        from rich.table import Table
        from rich.console import Console
        
        console = Console()
        results = {
            'playlists': {'total_tracks': 0, 'missing_tracks': 0, 'missing': []},
            'albums': {'total_tracks': 0, 'missing_tracks': 0, 'missing': []},
            'recommended_playlists': {'total_tracks': 0, 'missing_tracks': 0, 'missing': []},
            'recommended_albums': {'total_tracks': 0, 'missing_tracks': 0, 'missing': []}
        }
        
        if playlists:
            console.print("[blue]📋 Checking playlists...")
            playlists_data = await self.get_user_playlists()
            for playlist in playlists_data:
                tracks = await self._get_playlist_tracks(playlist['id'])
                missing = self._find_missing_tracks(tracks, db)
                results['playlists']['total_tracks'] += len(tracks)
                results['playlists']['missing_tracks'] += len(missing)
                results['playlists']['missing'].extend(missing)
                
            if recommended:
                console.print("[blue]🎯 Checking recommended playlists...")
                rec_playlists = await self.get_recommended_playlists()
                for playlist in rec_playlists:
                    tracks = await self._get_playlist_tracks(playlist['id'])
                    missing = self._find_missing_tracks(tracks, db)
                    results['recommended_playlists']['total_tracks'] += len(tracks)
                    results['recommended_playlists']['missing_tracks'] += len(missing)
                    results['recommended_playlists']['missing'].extend(missing)
        
        if albums:
            console.print("[blue]💿 Checking albums...")
            albums_data = await self.get_user_albums()
            for album in albums_data:
                tracks = await self._get_album_tracks(album['id'])
                missing = self._find_missing_tracks(tracks, db)
                results['albums']['total_tracks'] += len(tracks)
                results['albums']['missing_tracks'] += len(missing)
                results['albums']['missing'].extend(missing)
                
            if recommended:
                console.print("[blue]🎯 Checking recommended albums...")
                rec_albums = await self.get_recommended_albums()
                for album in rec_albums:
                    tracks = await self._get_album_tracks(album['id'])
                    missing = self._find_missing_tracks(tracks, db)
                    results['recommended_albums']['total_tracks'] += len(tracks)
                    results['recommended_albums']['missing_tracks'] += len(missing)
                    results['recommended_albums']['missing'].extend(missing)
        
        # Display results
        self._display_comparison_results(results, missing_only)
        
        return results

    async def get_missing_tracks_for_download(self, playlists=False, albums=False, recommended=False, db=None):
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
                tracks = await self._get_playlist_tracks(playlist['id'])
                missing = self._find_missing_tracks(tracks, db)
                for track in missing:
                    track_id = track.get('id')
                    if track_id:
                        missing_urls.append(f"https://tidal.com/track/{track_id}")
                
            if recommended:
                console.print("[blue]🎯 Getting missing tracks from recommended playlists...")
                rec_playlists = await self.get_recommended_playlists()
                for playlist in rec_playlists:
                    tracks = await self._get_playlist_tracks(playlist['id'])
                    missing = self._find_missing_tracks(tracks, db)
                    for track in missing:
                        track_id = track.get('id')
                        if track_id:
                            missing_urls.append(f"https://tidal.com/track/{track_id}")
        
        if albums:
            console.print("[blue]💿 Getting missing tracks from albums...")
            albums_data = await self.get_user_albums()
            for album in albums_data:
                tracks = await self._get_album_tracks(album['id'])
                missing = self._find_missing_tracks(tracks, db)
                for track in missing:
                    track_id = track.get('id')
                    if track_id:
                        missing_urls.append(f"https://tidal.com/track/{track_id}")
                
            if recommended:
                console.print("[blue]🎯 Getting missing tracks from recommended albums...")
                rec_albums = await self.get_recommended_albums()
                for album in rec_albums:
                    tracks = await self._get_album_tracks(album['id'])
                    missing = self._find_missing_tracks(tracks, db)
                    for track in missing:
                        track_id = track.get('id')
                        if track_id:
                            missing_urls.append(f"https://tidal.com/track/{track_id}")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in missing_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        
        console.print(f"[green]Found {len(unique_urls)} unique missing tracks")
        return unique_urls

    async def _get_playlist_tracks(self, playlist_id):
        """Get tracks from a specific playlist."""
        try:
            tracks_data = await self._api_request(f"playlists/{playlist_id}/tracks")
            return tracks_data.get('items', [])
        except Exception as e:
            logger.warning(f"Failed to get tracks for playlist {playlist_id}: {e}")
            return []

    async def _get_album_tracks(self, album_id):
        """Get tracks from a specific album."""
        try:
            tracks_data = await self._api_request(f"albums/{album_id}/tracks")
            return tracks_data.get('items', [])
        except Exception as e:
            logger.warning(f"Failed to get tracks for album {album_id}: {e}")
            return []

    def _find_missing_tracks(self, collection_tracks, db):
        """Find tracks that are in collection but not downloaded."""
        missing = []
        
        for track in collection_tracks:
            track_id = track.get('id')
            if track_id and db and not db.downloaded(track_id):
                missing.append(track)
            elif track_id and not db:
                # If no database provided, assume all tracks are missing
                missing.append(track)
        
        return missing

    def _display_comparison_results(self, results, missing_only=False):
        """Display comparison results in a nice table."""
        from rich.table import Table
        from rich.console import Console
        
        console = Console()
        
        table = Table(title="TIDAL Collection vs Downloads Comparison")
        table.add_column("Collection Type", style="cyan")
        table.add_column("Total Tracks", style="green")
        table.add_column("Missing Tracks", style="red")
        table.add_column("Downloaded", style="green")
        
        for collection_type, data in results.items():
            if data['total_tracks'] > 0:
                downloaded = data['total_tracks'] - data['missing_tracks']
                table.add_row(
                    collection_type.replace('_', ' ').title(),
                    str(data['total_tracks']),
                    str(data['missing_tracks']),
                    str(downloaded)
                )
        
        console.print(table)
        
        if missing_only:
            console.print("\n[yellow]Missing tracks:")
            for collection_type, data in results.items():
                if data['missing']:
                    console.print(f"\n[blue]{collection_type.replace('_', ' ').title()}:")
                    for track in data['missing'][:10]:  # Show first 10
                        console.print(f"  - {track.get('title', 'Unknown')} by {track.get('artist', {}).get('name', 'Unknown')}")
                    if len(data['missing']) > 10:
                        console.print(f"  ... and {len(data['missing']) - 10} more")
