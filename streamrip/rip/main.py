import asyncio
import json
import logging
import platform

import aiofiles

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..media import (
    Media,
    Pending,
    PendingAlbum,
    PendingArtist,
    PendingLabel,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingSingle,
    remove_artwork_tempdirs,
)
from ..media.track import Track
from ..metadata import SearchResults
from ..metadata.album import AlbumMetadata
from ..metadata.track import TrackMetadata
from ..progress import clear_progress
from .parse_url import parse_url
from .prompter import get_prompter

logger = logging.getLogger("streamrip")


def safe_log_error(message: str, *args):
    """Safely log error messages with fallback to console if logging fails."""
    try:
        if args:
            logger.error(message, *args)
        else:
            logger.error(message)
    except Exception as e:
        # If logging fails, use console.print as fallback
        try:
            formatted_msg = message % args if args else message
        except (TypeError, ValueError):
            formatted_msg = str(message)
        console.print(f"[red]Error: {formatted_msg}[/red]")
        console.print(f"[yellow]Logging system error: {e}[/yellow]")


def safe_log_info(message: str, *args):
    """Safely log info messages with fallback to console if logging fails."""
    try:
        if args:
            logger.info(message, *args)
        else:
            logger.info(message)
    except Exception as e:
        # If logging fails, use console.print as fallback
        try:
            formatted_msg = message % args if args else message
        except (TypeError, ValueError):
            formatted_msg = str(message)
        console.print(f"[blue]Info: {formatted_msg}[/blue]")
        console.print(f"[yellow]Logging system error: {e}[/yellow]")


if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
            # Initialize enhanced database tables if they don't exist
            enhanced_downloads_db = db.EnhancedDownloads(c.downloads_path)
            collections_db = db.Collections(c.downloads_path)
            track_collections_db = db.TrackCollections(c.downloads_path)
        else:
            downloads_db = db.Dummy()
            enhanced_downloads_db = db.Dummy()
            collections_db = db.Dummy()
            track_collections_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
            enhanced_failed_db = db.EnhancedFailed(c.failed_downloads_path)
        else:
            failed_downloads_db = db.Dummy()
            enhanced_failed_db = db.Dummy()

        self.database = db.Database(
            downloads_db,
            failed_downloads_db,
            enhanced_downloads_db,
            collections_db,
            track_collections_db,
            enhanced_failed_db,
        )

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        client = await self.get_logged_in_client(source)
        self._add_by_id_client(client, media_type, id)

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self._add_by_id_client(clients[source], media_type, id)

    def _add_by_id_client(self, client: Client, media_type: str, id: str):
        if media_type == "track":
            item = PendingSingle(id, client, self.config, self.database)
        elif media_type == "album":
            item = PendingAlbum(id, client, self.config, self.database)
        elif media_type == "playlist":
            item = PendingPlaylist(id, client, self.config, self.database)
        elif media_type == "label":
            item = PendingLabel(id, client, self.config, self.database)
        elif media_type == "artist":
            item = PendingArtist(id, client, self.config, self.database)
        else:
            raise Exception(media_type)

        self.pending.append(item)

    async def add_all(self, urls: list[str], skip_downloaded: bool = False):
        """Add multiple urls concurrently as pending items with progress tracking.

        Args:
            urls: List of URLs to add
            skip_downloaded: If True, skip URLs that are already downloaded
        """
        if not urls:
            return

        total_urls = len(urls)
        console.print(f"[blue]📋 Processing {total_urls} URL(s)...")

        # Parse URLs first
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        invalid_count = 0
        skipped_count = 0

        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]❌ Invalid URL [cyan]{urls[i]}[/cyan], skipping.",
                )
                invalid_count += 1
                continue

            # Check if already downloaded when skip_downloaded is True
            if skip_downloaded and hasattr(p, "id") and p.id:
                if self.database.downloaded(p.id):
                    skipped_count += 1
                    continue

            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        if invalid_count > 0:
            console.print(f"[yellow]⚠️  Skipped {invalid_count} invalid URL(s)")

        if skipped_count > 0:
            console.print(f"[blue]⏭️  Skipped {skipped_count} already downloaded URL(s)")

        if not url_client_pairs:
            console.print("[red]❌ No valid URLs to process")
            return

        # Show progress for large numbers of URLs
        if len(url_client_pairs) > 5:
            from rich.progress import (
                BarColumn,
                Progress,
                SpinnerColumn,
                TaskProgressColumn,
                TextColumn,
            )

            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
                transient=True,
            )

            with progress:
                task = progress.add_task(
                    "Creating pending items...", total=len(url_client_pairs)
                )

                # Process in batches
                batch_size = max(1, len(url_client_pairs) // 10)
                pendings = []

                for i in range(0, len(url_client_pairs), batch_size):
                    batch = url_client_pairs[i : i + batch_size]
                    batch_coros = [
                        url.into_pending(client, self.config, self.database)
                        for url, client in batch
                    ]
                    batch_results = await asyncio.gather(
                        *batch_coros, return_exceptions=True
                    )

                    for result in batch_results:
                        if result is not None and not isinstance(result, Exception):
                            pendings.append(result)
                        elif isinstance(result, Exception):
                            safe_log_error(f"Error creating pending item: {result}")

                    progress.update(task, advance=len(batch))
        else:
            # For small numbers, use simple processing
            pendings = await asyncio.gather(
                *[
                    url.into_pending(client, self.config, self.database)
                    for url, client in url_client_pairs
                ],
                return_exceptions=True,
            )

            # Filter out exceptions
            pendings = [
                p for p in pendings if p is not None and not isinstance(p, Exception)
            ]

        self.pending.extend(pendings)
        console.print(f"[green]✅ Added {len(pendings)} pending item(s) to queue")

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )
        if not client.logged_in:
            prompter = get_prompter(client, self.config)
            if not prompter.has_creds():
                # Get credentials from user and log into client
                await prompter.prompt_and_login()
                prompter.save()
            else:
                with console.status(f"[cyan]Logging into {source}", spinner="dots"):
                    # Log into client using credentials from config
                    await client.login()

        assert client.logged_in
        return client

    async def resolve_batch(self, batch_size: int = 10) -> list:
        """Resolve a batch of pending items and return them with optimized processing."""
        if not self.pending:
            return []

        # Take up to batch_size items from pending
        batch = self.pending[:batch_size]
        remaining = self.pending[batch_size:]

        console.print(f"[blue]🔍 Resolving batch of {len(batch)} item(s)...")

        # Optimize batch processing based on item types
        playlist_items = [p for p in batch if hasattr(p, "playlist_name")]
        other_items = [p for p in batch if not hasattr(p, "playlist_name")]

        new_media = []

        # Process playlist tracks with optimized batching
        if playlist_items:
            console.print(
                f"[blue]🎵 Optimizing {len(playlist_items)} playlist tracks..."
            )
            playlist_media = await self._resolve_playlist_batch_optimized(
                playlist_items
            )
            new_media.extend(playlist_media)

        # Process other items normally
        if other_items:
            console.print(f"[blue]🔍 Resolving {len(other_items)} other items...")
            coros = [p.resolve() for p in other_items]
            results = await asyncio.gather(*coros, return_exceptions=True)

            for result in results:
                if result is not None and not isinstance(result, Exception):
                    new_media.append(result)
                elif isinstance(result, Exception):
                    safe_log_error(f"Error resolving pending item: {result}")

        # Update pending list to remaining items
        self.pending = remaining

        resolved_count = len(new_media)
        if resolved_count > 0:
            console.print(
                f"[green]✅ Resolved {resolved_count}/{len(batch)} items in batch"
            )
        else:
            console.print(f"[yellow]⚠️  No items resolved in batch of {len(batch)}")

        return new_media

    async def _resolve_playlist_batch_optimized(self, playlist_items: list) -> list:
        """Optimized resolution for playlist tracks with batching and caching."""
        if not playlist_items:
            return []

        # Group by client source for batch API calls
        by_source = {}
        for item in playlist_items:
            source = item.client.source
            if source not in by_source:
                by_source[source] = []
            by_source[source].append(item)

        all_resolved = []

        for source, items in by_source.items():
            console.print(f"[blue]🚀 Optimizing {len(items)} {source} tracks...")

            # Use source-specific optimization
            if source == "tidal":
                resolved = await self._optimize_tidal_playlist_tracks(items)
            elif source == "soundcloud":
                resolved = await self._optimize_soundcloud_playlist_tracks(items)
            else:
                # Fallback to normal processing
                coros = [item.resolve() for item in items]
                results = await asyncio.gather(*coros, return_exceptions=True)
                resolved = [
                    r for r in results if r is not None and not isinstance(r, Exception)
                ]

            all_resolved.extend(resolved)

        return all_resolved

    async def _optimize_tidal_playlist_tracks(self, items: list) -> list:
        """Optimized TIDAL playlist track resolution with batch API calls."""
        if not items:
            return []

        client = items[0].client
        track_ids = [item.id for item in items]

        try:
            # Batch API call to get all track metadata at once
            console.print(
                f"[blue]📡 Batch fetching {len(track_ids)} TIDAL track metadata..."
            )

            # Use TIDAL's batch track endpoint if available
            batch_data = await client._api_request(
                "tracks", params={"ids": ",".join(str(i) for i in track_ids)}
            )

            if batch_data and "data" in batch_data:
                # Normalize keys to strings to avoid int/str mismatches
                track_metadata = {
                    str(track["id"]): track for track in batch_data["data"]
                }

                # Process each item with pre-fetched metadata
                resolved_items = []
                for item in items:
                    item_key = str(item.id)
                    if item_key in track_metadata:
                        try:
                            # Create track with pre-fetched metadata
                            track = await self._create_track_from_metadata(
                                item, track_metadata[item_key]
                            )
                            if track:
                                resolved_items.append(track)
                        except Exception as e:
                            safe_log_error(f"Error creating track {item.id}: {e}")
                    else:
                        # Fallback to individual resolution
                        try:
                            track = await item.resolve()
                            if track:
                                resolved_items.append(track)
                        except Exception as e:
                            safe_log_error(f"Error resolving track {item.id}: {e}")

                console.print(
                    f"[green]✅ Batch resolved {len(resolved_items)}/{len(items)} TIDAL tracks"
                )
                return resolved_items

        except Exception as e:
            msg = str(e)
            if "400" in msg or "Bad Request" in msg:
                # TIDAL batch endpoint likely unsupported; degrade quietly
                logger.debug(
                    f"Batch TIDAL API not supported: {msg}. Falling back to per-track."
                )
            else:
                safe_log_error(f"Batch TIDAL API call failed: {e}")

        # Fallback to individual processing
        console.print("[yellow]⚠️  Falling back to individual track resolution...")
        coros = [item.resolve() for item in items]
        results = await asyncio.gather(*coros, return_exceptions=True)
        return [r for r in results if r is not None and not isinstance(r, Exception)]

    async def _optimize_soundcloud_playlist_tracks(self, items: list) -> list:
        """Optimized SoundCloud playlist track resolution."""
        # SoundCloud already has some batching in _get_playlist
        # Just use normal processing for now
        coros = [item.resolve() for item in items]
        results = await asyncio.gather(*coros, return_exceptions=True)
        return [r for r in results if r is not None and not isinstance(r, Exception)]

    async def _create_track_from_metadata(self, pending_item, metadata: dict):
        """Create a Track object from pre-fetched metadata."""
        try:
            # Create album metadata from track response
            album = AlbumMetadata.from_track_resp(metadata, pending_item.client.source)
            if album is None:
                return None

            # Create track metadata
            meta = TrackMetadata.from_resp(album, pending_item.client.source, metadata)
            if meta is None:
                return None

            # Apply playlist-specific settings
            c = pending_item.config.session.metadata
            if c.renumber_playlist_tracks:
                meta.tracknumber = pending_item.position
            if c.set_playlist_to_album:
                album.album = pending_item.playlist_name

            # Get downloadable info
            quality = pending_item.config.session.get_source(
                pending_item.client.source
            ).quality
            downloadable = await pending_item.client.get_downloadable(
                pending_item.id, quality
            )

            # Download cover
            embedded_cover_path = await pending_item._download_cover(
                album.covers, pending_item.folder
            )

            return Track(
                meta,
                downloadable,
                pending_item.config,
                pending_item.folder,
                embedded_cover_path,
                pending_item.db,
            )
        except Exception as e:
            safe_log_error(f"Error creating track from metadata: {e}")
            return None

    async def resolve(self):
        """Resolve all currently pending items (legacy method for compatibility)."""
        if not self.pending:
            return

        total_pending = len(self.pending)
        console.print(f"[blue]🔍 Resolving {total_pending} URL(s)...")

        # Use batch processing for large numbers
        if total_pending > 10:
            batch_size = max(1, total_pending // 20)  # Process in ~20 batches
            all_resolved = []

            while self.pending:
                batch_resolved = await self.resolve_batch(batch_size)
                all_resolved.extend(batch_resolved)

            self.media.extend(all_resolved)
        else:
            # For small numbers, resolve all at once
            new_media = await self.resolve_batch(len(self.pending))
            self.media.extend(new_media)

    async def process_streaming(self, batch_size: int = 10, resume: bool = False):
        """Process items in streaming fashion: resolve batch -> download batch -> repeat."""
        if not self.pending:
            console.print("[yellow]⚠️  No pending items to process")
            return

        total_pending = len(self.pending)
        console.print(
            f"[blue]🚀 Starting streaming processing of {total_pending} item(s)..."
        )
        console.print(f"[blue]📦 Processing in batches of {batch_size}")

        if resume:
            console.print("[blue]🔄 Resume mode: skipping already downloaded tracks")

        total_resolved = 0
        total_downloaded = 0
        total_failed = 0
        total_skipped = 0

        while self.pending:
            # Step 1: Resolve a batch
            resolved_batch = await self.resolve_batch(batch_size)
            total_resolved += len(resolved_batch)

            if not resolved_batch:
                console.print("[yellow]⚠️  No items resolved in this batch, skipping...")
                continue

            # Step 2: Filter out already downloaded items if resuming
            if resume:
                original_count = len(resolved_batch)
                resolved_batch = [
                    item
                    for item in resolved_batch
                    if not self.database.downloaded(item.meta.info.id)
                ]
                skipped_count = original_count - len(resolved_batch)
                total_skipped += skipped_count

                if skipped_count > 0:
                    console.print(
                        f"[blue]⏭️  Skipped {skipped_count} already downloaded tracks"
                    )

                if not resolved_batch:
                    console.print(
                        "[blue]⏭️  All tracks in this batch already downloaded, skipping..."
                    )
                    continue

            # Step 3: Download the resolved batch immediately
            console.print(
                f"[blue]📥 Downloading batch of {len(resolved_batch)} item(s)..."
            )

            # Download the batch
            download_results = await asyncio.gather(
                *[item.rip() for item in resolved_batch], return_exceptions=True
            )

            # Count results
            batch_downloaded = 0
            batch_failed = 0
            for result in download_results:
                if isinstance(result, Exception):
                    safe_log_error(f"Error downloading item: {result}")
                    batch_failed += 1
                else:
                    batch_downloaded += 1

            total_downloaded += batch_downloaded
            total_failed += batch_failed

            # Show batch progress
            console.print(
                f"[green]✅ Batch complete: {batch_downloaded} downloaded, "
                f"{batch_failed} failed"
            )

            # Show overall progress
            remaining = len(self.pending)
            console.print(
                f"[blue]📊 Progress: {total_resolved} resolved, "
                f"{total_downloaded} downloaded, {total_skipped} skipped, "
                f"{remaining} remaining"
            )

        # Final summary
        console.print("[green]🎉 Streaming processing complete!")
        console.print(
            f"[green]📈 Final stats: {total_resolved} resolved, {total_downloaded} downloaded, {total_skipped} skipped, {total_failed} failed"
        )

    async def rip(self):
        """Download all resolved items with progress tracking."""
        if not self.media:
            console.print("[yellow]⚠️  No items to download")
            return

        total_items = len(self.media)
        console.print(f"[blue]📥 Starting download of {total_items} item(s)...")

        # Show progress for large numbers of items
        if total_items > 5:
            from rich.progress import (
                BarColumn,
                Progress,
                SpinnerColumn,
                TaskProgressColumn,
                TextColumn,
            )

            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
                transient=True,
            )

            with progress:
                task = progress.add_task("Downloading items...", total=total_items)

                # Process in batches to show progress
                batch_size = max(1, total_items // 20)  # Show progress every 5%
                completed_count = 0
                failed_count = 0

                for i in range(0, total_items, batch_size):
                    batch = self.media[i : i + batch_size]
                    batch_coros = [item.rip() for item in batch]
                    batch_results = await asyncio.gather(
                        *batch_coros, return_exceptions=True
                    )

                    for result in batch_results:
                        if isinstance(result, Exception):
                            safe_log_error(f"Error processing media item: {result}")
                            failed_count += 1
                        else:
                            completed_count += 1

                    # Update progress
                    progress.update(task, advance=len(batch))

                    # Update description with current progress
                    progress.update(
                        task,
                        description=f"Downloaded {completed_count}/{total_items} items...",
                    )
        else:
            # For small numbers, use simple processing
            results = await asyncio.gather(
                *[item.rip() for item in self.media], return_exceptions=True
            )

            failed_count = 0
            for result in results:
                if isinstance(result, Exception):
                    safe_log_error(f"Error processing media item: {result}")
                    failed_count += 1

        # Show final results
        completed_count = total_items - failed_count
        if failed_count == 0:
            console.print(
                f"[green]✅ Successfully downloaded all {completed_count} item(s)"
            )
        else:
            console.print(
                f"[yellow]⚠️  Download completed: {completed_count} successful, "
                f"{failed_count} failed out of {total_items} total"
            )

    async def search_interactive(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)

        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=100)
            if len(pages) == 0:
                console.print(f"[red]No search results found for query {query}")
                return
            search_results = SearchResults.from_pages(source, media_type, pages)

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=1)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        assert len(search_results.results) > 0
        first = search_results.results[0]
        await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=limit)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
