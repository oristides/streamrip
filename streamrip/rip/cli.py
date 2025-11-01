import asyncio
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
import click
from click_help_colors import HelpColorsGroup  # type: ignore
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.prompt import Confirm
from rich.traceback import install

from .. import __version__, db
from ..client.tidal import TidalClient
from ..config import DEFAULT_CONFIG_PATH, Config, OutdatedConfigError, set_user_defaults
from ..console import console
from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .main import Main


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group(
    cls=HelpColorsGroup,
    help_headers_color="yellow",
    help_options_color="green",
)
@click.version_option(version=__version__)
@click.option(
    "--config-path",
    default=DEFAULT_CONFIG_PATH,
    help="Path to the configuration file",
    type=click.Path(readable=True, writable=True),
)
@click.option(
    "-f",
    "--folder",
    help="The folder to download items into.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "-ndb",
    "--no-db",
    help="Download items even if they have been logged in the database",
    default=False,
    is_flag=True,
)
@click.option(
    "-q",
    "--quality",
    help="The maximum quality allowed to download",
    type=click.IntRange(min=0, max=4),
)
@click.option(
    "-c",
    "--codec",
    help="Convert the downloaded files to an audio codec (ALAC, FLAC, MP3, AAC, or OGG)",
)
@click.option(
    "--no-progress",
    help="Do not show progress bars",
    is_flag=True,
    default=False,
)
@click.option(
    "--no-ssl-verify",
    help="Disable SSL certificate verification (use if you encounter SSL errors)",
    is_flag=True,
    default=False,
)
@click.option(
    "-v",
    "--verbose",
    help="Enable verbose output (debug mode)",
    is_flag=True,
)
@click.pass_context
def rip(
    ctx, config_path, folder, no_db, quality, codec, no_progress, no_ssl_verify, verbose
):
    """Streamrip: the all in one music downloader.

    Enhanced TIDAL Support:
    • List and download your saved albums: 'rip tidal list-albums' /
      'rip tidal download-albums'
    • List and download recommended albums: 'rip tidal list-albums --recommended' /
      'rip tidal download-albums --recommended'
    • List and download your playlists: 'rip tidal list-playlists' /
      'rip tidal download-playlists'
    • List and download recommended playlists: 'rip tidal list-playlists --recommended' /
      'rip tidal download-playlists --recommended'
    • Preview tracks before downloading: 'rip tidal preview-track <track-id>'
    • Compare collections with downloads: 'rip tidal compare --playlists --albums'
    • Download missing tracks: 'rip tidal download-missing --playlists --albums'

    All downloads are organized into structured folders: albums/, playlists/, tracks/
    """
    global logger

    # Create log directory if it doesn't exist
    log_dir = Path.home() / ".config" / "streamrip" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create timestamped log file
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"streamrip_{timestamp}.log"

    # Set up handlers
    handlers = [RichHandler()]

    if verbose:
        # Add file handler for verbose mode
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        handlers.append(file_handler)

        console.print(f"[blue]📝 Logging to file: {log_file}")

    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=handlers,
    )
    logger = logging.getLogger("streamrip")

    if verbose:
        install(
            console=console,
            suppress=[
                click,
            ],
            show_locals=True,
            locals_hide_sunder=False,
        )
        logger.setLevel(logging.DEBUG)
        logger.debug("Showing all debug logs")
        logger.info(f"Log file: {log_file}")
    else:
        install(console=console, suppress=[click, asyncio], max_frames=1)
        logger.setLevel(logging.INFO)

    if not os.path.isfile(config_path):
        console.print(
            (
                f"No file found at [bold cyan]{config_path}[/bold cyan], creating"
                " default config."
            ),
        )
        set_user_defaults(config_path)

    # pass to subcommands
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    try:
        c = Config(config_path)
    except OutdatedConfigError as e:
        console.print(e)
        console.print("Auto-updating config file...")
        Config.update_file(config_path)
        c = Config(config_path)
    except Exception as e:
        console.print(
            (
                f"Error loading config from [bold cyan]{config_path}[/bold cyan]: {e}\n"
                "Try running [bold]rip config reset[/bold]"
            ),
        )
        ctx.obj["config"] = None
        return

    # set session config values to command line args
    if no_db:
        c.session.database.downloads_enabled = False
    if folder is not None:
        c.session.downloads.folder = folder

    if quality is not None:
        c.session.qobuz.quality = quality
        c.session.tidal.quality = quality
        c.session.deezer.quality = quality
        c.session.soundcloud.quality = quality

    if codec is not None:
        c.session.conversion.enabled = True
        assert codec.upper() in ("ALAC", "FLAC", "OGG", "MP3", "AAC")
        c.session.conversion.codec = codec.upper()

    if no_progress:
        c.session.cli.progress_bars = False

    if no_ssl_verify:
        c.session.downloads.verify_ssl = False

    ctx.obj["config"] = c


@rip.command()
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--batch-size",
    default=10,
    help="Number of items to process in each batch (default: 10)",
    type=click.IntRange(min=1, max=100),
)
@click.pass_context
@coro
async def url(ctx, urls, batch_size):
    """Download content from URLs using streaming batch processing.

    Items are processed in batches: resolve a batch, then download that batch,
    then move to the next batch. This provides better progress feedback and
    memory efficiency for large playlists.

    If a single URL is provided and it resolves to a playlist or album,
    use 'rip show <url>' to preview track listings before downloading.
    """
    if ctx.obj["config"] is None:
        return

    try:
        with ctx.obj["config"] as cfg:
            cfg: Config
            updates = cfg.session.misc.check_for_updates
            if updates:
                # Run in background
                version_coro = asyncio.create_task(
                    latest_streamrip_version(
                        verify_ssl=cfg.session.downloads.verify_ssl
                    )
                )
            else:
                version_coro = None

            async with Main(cfg) as main:
                await main.add_all(urls)
                # Use streaming processing for better memory usage and progress
                await main.process_streaming(batch_size=batch_size)

            if version_coro is not None:
                latest_version, notes = await version_coro
                if latest_version != __version__:
                    console.print(
                        (
                            f"\n[green]A new version of streamrip [cyan]v{latest_version}[/cyan]"
                            " is available! Run [white][bold]pip3 install streamrip --upgrade"
                            "[/bold][/white] to update.[/green]\n"
                        )
                    )

                    console.print(Markdown(notes))

    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.command()
@click.argument("url", required=True)
@click.pass_context
@coro
async def show(ctx, url):
    """Show tracks for a playlist or album URL in a table.

    Displays: index, track name, artist(s), and URL.
    """
    from rich.table import Table

    from ..rip.parse_url import parse_url

    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            parsed = parse_url(url)
            if parsed is None:
                console.print(f"[red]Unable to parse URL[/red] [cyan]{url}[/cyan]")
                return

            client = await main.get_logged_in_client(parsed.source)
            # resolve without enqueuing for download
            pending = await parsed.into_pending(client, cfg, main.database)

            # Playlist
            from ..media.album import PendingAlbum
            from ..media.playlist import PendingPlaylist

            if isinstance(pending, PendingPlaylist):
                resolved = await pending.resolve()
                if resolved is None:
                    console.print("[yellow]Could not resolve playlist.")
                    return
                t = Table(title=f"Playlist: {resolved.name}")
                t.add_column("#", style="white", justify="right")
                t.add_column("Track", style="green")
                t.add_column("Artist(s)", style="cyan")
                t.add_column("URL", style="blue")
                for i, p in enumerate(resolved.tracks, start=1):
                    track_obj = await p.resolve()
                    if track_obj is None:
                        continue
                    name = escape(track_obj.meta.title)
                    artists = escape(", ".join(a.name for a in track_obj.meta.artists))
                    track_url = (
                        f"https://{client.source}.com/track/{track_obj.meta.info.id}"
                    )
                    t.add_row(
                        f"{i:02}",
                        name,
                        artists,
                        f"[link={track_url}]{track_url}[/link]",
                    )
                console.print(t)
                return

            # Album
            if isinstance(pending, PendingAlbum):
                resolved = await pending.resolve()
                if resolved is None:
                    console.print("[yellow]Could not resolve album.")
                    return
                t = Table(title=f"Album: {resolved.meta.album}")
                t.add_column("#", style="white", justify="right")
                t.add_column("Track", style="green")
                t.add_column("Artist(s)", style="cyan")
                t.add_column("URL", style="blue")
                # pending tracks with tracknumbers
                for i, p in enumerate(resolved.tracks, start=1):
                    track_obj = await p.resolve()
                    if track_obj is None:
                        continue
                    name = escape(track_obj.meta.title)
                    artists = escape(", ".join(a.name for a in track_obj.meta.artists))
                    track_url = (
                        f"https://{client.source}.com/track/{track_obj.meta.info.id}"
                    )
                    t.add_row(
                        f"{i:02}",
                        name,
                        artists,
                        f"[link={track_url}]{track_url}[/link]",
                    )
                console.print(t)
                return

            console.print(
                "[yellow]URL is not a playlist or album, or could not be displayed."
            )


@rip.command()
@click.argument(
    "path",
    required=True,
    type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False),
)
@click.pass_context
@coro
async def file(ctx, path):
    """Download content from URLs in a file.

    Example usage:

        rip file urls.txt
    """
    try:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                async with aiofiles.open(path, "r") as f:
                    content = await f.read()
                    try:
                        items: Any = json.loads(content)
                        loaded = True
                    except json.JSONDecodeError:
                        items = content.split()
                        loaded = False
                if loaded:
                    console.print(
                        (
                            f"Detected json file. Loading [yellow]{len(items)}[/yellow]"
                            " items"
                        )
                    )
                    await main.add_all_by_id(
                        [(i["source"], i["media_type"], i["id"]) for i in items]
                    )
                else:
                    s = set(items)
                    if len(s) < len(items):
                        console.print(
                            (
                                f"Found [orange]{len(items) - len(s)}[/orange] repeated"
                                " URLs!"
                            )
                        )
                        items = list(s)
                    console.print(
                        (
                            f"Detected list of urls. Loading [yellow]{len(items)}[/yellow]"
                            " items"
                        )
                    )
                    await main.add_all(items)

                await main.resolve()
                await main.rip()
    except aiohttp.ClientConnectorCertificateError as e:
        from ..utils.ssl_utils import print_ssl_error_help

        console.print(f"[red]SSL Certificate verification error: {e}[/red]")
        print_ssl_error_help()


@rip.group()
def config():
    """Manage configuration files."""


@config.command("open")
@click.option("-v", "--vim", help="Open in (Neo)Vim", is_flag=True)
@click.pass_context
def config_open(ctx, vim):
    """Open the config file in a text editor."""
    config_path = ctx.obj["config_path"]

    console.print(f"Opening file at [bold cyan]{config_path}")
    if vim:
        if shutil.which("nvim") is not None:
            subprocess.run(["nvim", config_path])
        elif shutil.which("vim") is not None:
            subprocess.run(["vim", config_path])
        else:
            logger.error("Could not find nvim or vim. Using default launcher.")
            click.launch(config_path)
    else:
        click.launch(config_path)


@config.command("reset")
@click.option("-y", "--yes", help="Don't ask for confirmation.", is_flag=True)
@click.pass_context
def config_reset(ctx, yes):
    """Reset the config file."""
    config_path = ctx.obj["config_path"]
    if not yes:
        if not Confirm.ask(
            f"Are you sure you want to reset the config file at {config_path}?",
        ):
            console.print("[green]Reset aborted")
            return

    set_user_defaults(config_path)
    console.print(f"Reset the config file at [bold cyan]{config_path}!")


@config.command("path")
@click.pass_context
def config_path(ctx):
    """Display the path of the config file."""
    config_path = ctx.obj["config_path"]
    console.print(f"Config path: [bold cyan]'{config_path}'")


@rip.group()
def database():
    """View and modify the downloads and failed downloads databases."""


@database.command("browse")
@click.argument("table")
@click.pass_context
def database_browse(ctx, table):
    """Browse the contents of a table.

    Available tables:

        * Downloads

        * Failed
    """
    from rich.table import Table

    cfg: Config = ctx.obj["config"]

    if table.lower() == "downloads":
        downloads = db.Downloads(cfg.session.database.downloads_path)
        t = Table(title="Downloads database")
        t.add_column("Row")
        t.add_column("ID")
        for i, row in enumerate(downloads.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    elif table.lower() == "failed":
        failed = db.Failed(cfg.session.database.failed_downloads_path)
        t = Table(title="Failed downloads database")
        t.add_column("Source")
        t.add_column("Media Type")
        t.add_column("ID")
        for i, row in enumerate(failed.all()):
            t.add_row(f"{i:02}", *row)
        console.print(t)

    else:
        console.print(
            f"[red]Invalid database[/red] [bold]{table}[/bold]. [red]Choose[/red] [bold]downloads "
            "[red]or[/red] failed[/bold].",
        )


@rip.command()
@click.option(
    "-f",
    "--first",
    help="Automatically download the first search result without showing the menu.",
    is_flag=True,
)
@click.option(
    "-o",
    "--output-file",
    help="Write search results to a file instead of showing interactive menu.",
    type=click.Path(writable=True),
)
@click.option(
    "-n",
    "--num-results",
    help="Maximum number of search results to show",
    default=100,
    type=click.IntRange(min=1),
)
@click.argument("source", required=True)
@click.argument("media-type", required=True)
@click.argument("query", required=True)
@click.pass_context
@coro
async def search(ctx, first, output_file, num_results, source, media_type, query):
    """Search for content using a specific source.

    Example:

        rip search qobuz album 'rumours'
    """
    if first and output_file:
        console.print("Cannot choose --first and --output-file!")
        return
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            if first:
                await main.search_take_first(source, media_type, query)
            elif output_file:
                await main.search_output_file(
                    source, media_type, query, output_file, num_results
                )
            else:
                await main.search_interactive(source, media_type, query)
            await main.resolve()
            await main.rip()


@rip.command()
@click.option("-s", "--source", help="The source to search tracks on.")
@click.option(
    "-fs",
    "--fallback-source",
    help=(
        "The source to search tracks on if no results were found with the main source."
    ),
)
@click.argument("url", required=True)
@click.pass_context
@coro
async def lastfm(ctx, source, fallback_source, url):
    """Download tracks from a last.fm playlist."""
    config = ctx.obj["config"]
    if source is not None:
        config.session.lastfm.source = source
    if fallback_source is not None:
        config.session.lastfm.fallback_source = fallback_source
    with config as cfg:
        async with Main(cfg) as main:
            await main.resolve_lastfm(url)
            await main.rip()


@rip.command()
@click.argument("source")
@click.argument("media-type")
@click.argument("id")
@click.pass_context
@coro
async def id(ctx, source, media_type, id):
    """Download an item by ID."""
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_by_id(source, media_type, id)
            await main.resolve()
            await main.rip()


@rip.group()
def tidal():
    """TIDAL utilities: list and download playlists, albums, and preview tracks.

    Commands:
      login              Authenticate with TIDAL and update tokens
      list-playlists     List your playlists (or discovery mixes with
                         --recommended) and show an indexed table with
                         name and URL.
      download           Download playlists by indices from the list,
                         by exact names, or by URLs.
      list-albums        List your saved/owned albums (or recommended albums
                         with --recommended) and show an indexed table with
                         name, artists, and URL.
      download-albums    Download albums by indices from the list, by exact
                         names, or by URLs.
      preview-track      Preview a track by ID before downloading.

    Examples:
      # Authentication
      rip tidal login

      # Playlists
      rip tidal list-playlists
      rip tidal list-playlists --recommended
      rip tidal download -i "0,3"
      rip tidal download -n "My Mix,Deep House Essentials"
      rip tidal download -u "https://tidal.com/playlist/UUID"

      # Albums
      rip tidal list-albums
      rip tidal list-albums --recommended
      rip tidal download-albums -i "0,2"
      rip tidal download-albums --recommended -n "Album Name"
      rip tidal download-albums -u "https://tidal.com/album/UUID"

      # Track preview
      rip tidal preview-track 12345678
    """


@tidal.command("login")
@click.pass_context
def tidal_login(ctx):
    """Authenticate with TIDAL and update tokens in config.

    This command will:
    1. Automatically open a browser for you to log in to TIDAL
    2. Wait for you to copy the redirect URL from the 'Oops' page
    3. Get fresh authentication tokens
    4. Automatically update your streamrip config.toml

    You'll need to log in with your TIDAL email and password
    (not Google/social login).

    TIP: After logging in, just copy the URL from the 'Oops' page
    and paste it when prompted. The browser opens automatically!
    """
    import re
    import time
    import webbrowser

    try:
        import tidalapi
    except ImportError:
        console.print("[red]❌ tidalapi library not found![/red]")
        console.print("Please install it with: [cyan]pip install tidalapi[/cyan]")
        return

    config_path = ctx.obj["config_path"]

    console.print("\n" + "=" * 60)
    console.print("[bold cyan]TIDAL Authentication[/bold cyan]")
    console.print("=" * 60)
    console.print("[green]✓[/green] Browser will open automatically")
    console.print("[yellow]→[/yellow] Log in with your TIDAL email and password")
    console.print("[yellow]→[/yellow] After login, you'll see an 'Oops' page")
    console.print("[yellow]→[/yellow] Copy the URL from the 'Oops' page")
    console.print("[yellow]→[/yellow] Paste it below when prompted")
    console.print("=" * 60 + "\n")

    # Initialize TIDAL session
    console.print("Initializing TIDAL session...")
    session = tidalapi.Session()

    # Get device code and auth URL
    import base64
    import hashlib
    import secrets

    # Generate PKCE parameters
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    )
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )

    client_id = base64.b64decode(
        base64.b64decode(b"TmtKRVUxSmtjRXM=")
        + base64.b64decode(b"NWFIRkZRbFJuVlE9PQ==")
    ).decode("iso-8859-1")

    client_unique_key = secrets.token_hex(8)

    # Build the authorization URL (using TIDAL's redirect URI)
    auth_url = (
        f"https://login.tidal.com/authorize?"
        f"response_type=code&"
        f"redirect_uri=https://tidal.com/android/login/auth&"
        f"client_id={client_id}&"
        f"lang=EN&"
        f"appMode=android&"
        f"client_unique_key={client_unique_key}&"
        f"code_challenge={code_challenge}&"
        f"code_challenge_method=S256&"
        f"restrict_signup=true"
    )

    # Open browser automatically
    console.print("[cyan]Opening browser...[/cyan]")
    webbrowser.open(auth_url)

    console.print("\n[yellow]Waiting for you to log in and copy the URL...[/yellow]\n")

    # Prompt for the redirect URL
    try:
        redirect_url = input(
            "Paste the 'Oops' page URL here and press <ENTER>: "
        ).strip()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]❌ Authentication cancelled by user.[/yellow]")
        return

    if not redirect_url:
        console.print("[red]❌ No URL provided[/red]")
        return

    # Extract the authorization code from the URL
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)

        if "code" not in params:
            console.print("[red]❌ No authorization code found in URL[/red]")
            return

        auth_code = params["code"][0]
        console.print("[green]✓[/green] Authorization code extracted")

    except Exception as e:
        console.print(f"[red]❌ Failed to parse URL: {e}[/red]")
        return

    # Exchange code for tokens
    console.print("[dim]Exchanging code for tokens...[/dim]")

    try:
        import requests

        token_data = {
            "client_id": client_id,
            "code": auth_code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": "https://tidal.com/android/login/auth",
            "scope": "r_usr w_usr w_sub",
        }

        token_response = requests.post(
            "https://auth.tidal.com/v1/oauth2/token", data=token_data
        ).json()

        if "access_token" not in token_response:
            console.print(f"[red]❌ Token exchange failed: {token_response}[/red]")
            return

        # Set tokens in session
        session.access_token = token_response["access_token"]
        session.refresh_token = token_response["refresh_token"]
        session.token_type = token_response.get("token_type", "Bearer")
        session.expiry_time = token_response.get("expires_in", 86400) + time.time()

        # Load user info
        session.load_session()

    except Exception as e:
        console.print(f"[red]❌ Token exchange failed: {e}[/red]")
        return

    # Check if login was successful
    if not session.check_login():
        console.print("[red]❌ Authentication failed![/red]")
        return

    console.print("\n[green]✅ Authentication successful![/green]")

    # Extract tokens
    user_id = session.user.id
    access_token = session.access_token
    refresh_token = session.refresh_token

    # Get country code from current config or use default
    country_code = "BR"  # Default
    try:
        with open(config_path, "r") as f:
            content = f.read()
            match = re.search(r'country_code = "([^"]+)"', content)
            if match and match.group(1):
                country_code = match.group(1)
    except Exception:
        pass

    # Calculate expiry (tokens last 1 week)
    token_expiry = time.time() + (7 * 24 * 60 * 60)

    console.print(f"\n[dim]User ID:[/dim] {user_id}")
    console.print(f"[dim]Country Code:[/dim] {country_code}")
    console.print(f"[dim]Token Expiry:[/dim] {token_expiry}")

    # Update config file
    console.print(f"\n[blue]📝 Updating config file:[/blue] {config_path}")

    try:
        with open(config_path, "r") as f:
            content = f.read()

        # Update each field
        content = re.sub(r'user_id = "[^"]*"', f'user_id = "{user_id}"', content)
        content = re.sub(r"user_id = \d+", f'user_id = "{user_id}"', content)
        content = re.sub(
            r'country_code = "[^"]*"', f'country_code = "{country_code}"', content
        )
        content = re.sub(
            r'access_token = "[^"]*"', f'access_token = "{access_token}"', content
        )
        content = re.sub(
            r'refresh_token = "[^"]*"', f'refresh_token = "{refresh_token}"', content
        )
        content = re.sub(
            r"token_expiry = [0-9.]*", f"token_expiry = {token_expiry}", content
        )

        # Write back to file
        with open(config_path, "w") as f:
            f.write(content)

        console.print("[green]✅ Config file updated successfully![/green]")

        console.print("\n" + "=" * 60)
        console.print("[bold green]🎉 TIDAL authentication complete![/bold green]")
        console.print("=" * 60)
        console.print("You can now use streamrip with TIDAL:")
        console.print("  [cyan]rip tidal list-playlists[/cyan]")
        console.print("  [cyan]rip tidal list-albums[/cyan]")
        console.print("  [cyan]rip url <tidal-url>[/cyan]")
        console.print("=" * 60 + "\n")

    except Exception as e:
        console.print(f"[red]❌ Failed to update config: {e}[/red]")


@tidal.command("list-playlists")
@click.option(
    "--recommended",
    is_flag=True,
    help=("Show recommended discovery playlists instead of your saved/owned playlists"),
)
@click.pass_context
@coro
async def tidal_list_playlists(ctx, recommended):
    """Print an indexed list of TIDAL playlists.

    By default shows your saved/owned playlists. Pass --recommended to
    list discovery mixes recommended for your account.

    The output table includes an index, the playlist name, and URL.
    You can use the index(es) directly with 'rip tidal download -i'.
    """
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            client = await main.get_logged_in_client("tidal")
            assert isinstance(client, TidalClient)
            if recommended:
                playlists = await client.get_recommended_playlists()
            else:
                playlists = await client.get_user_playlists()

    if not playlists:
        console.print("[yellow]No playlists found.")
        return

    from rich.table import Table

    t = Table(
        title=("TIDAL Recommended Playlists" if recommended else "TIDAL Your Playlists")
    )
    t.add_column("#", style="white", justify="right")
    t.add_column("Name", style="green")
    t.add_column("URL", style="blue")
    for i, p in enumerate(playlists):
        name = escape(p.get("name", str(p.get("id"))))
        url = p.get("url", "")
        url_cell = f"[link={url}]{escape(url)}[/link]" if url else ""
        t.add_row(f"{i:02}", name, url_cell)
    console.print(t)


@tidal.command("list-albums")
@click.option(
    "--recommended",
    is_flag=True,
    help="Show recommended albums instead of your saved/owned albums",
)
@click.pass_context
@coro
async def tidal_list_albums(ctx, recommended):
    """Print an indexed list of your saved/owned or recommended TIDAL albums."""
    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            client = await main.get_logged_in_client("tidal")
            assert isinstance(client, TidalClient)
            albums = (
                await client.get_recommended_albums()
                if recommended
                else await client.get_user_albums()
            )

    if not albums:
        console.print("[yellow]No albums found.")
        return

    from rich.table import Table

    t = Table(
        title=("TIDAL Recommended Albums" if recommended else "TIDAL Your Albums")
    )
    t.add_column("#", style="white", justify="right")
    t.add_column("Album", style="green")
    t.add_column("Artist(s)", style="cyan")
    t.add_column("URL", style="blue")
    for i, a in enumerate(albums):
        name = escape(a.get("name", str(a.get("id"))))
        artists = escape(a.get("artists", ""))
        url = a.get("url", "")
        url_cell = f"[link={url}]{escape(url)}[/link]" if url else ""
        t.add_row(f"{i:02}", name, artists, url_cell)
    console.print(t)


@tidal.command("download-albums")
@click.option(
    "--recommended",
    is_flag=True,
    help="Use recommended albums as the source list",
)
@click.option(
    "-i", "--indices", help="Comma-separated indices to download from the printed list."
)
@click.option("-n", "--names", help="Comma-separated exact album names to download.")
@click.option("-u", "--urls", help="Comma-separated album URLs to download.")
@click.pass_context
@coro
async def tidal_download_albums(ctx, recommended, indices, names, urls):
    """Download TIDAL albums by index, name, or URL list.

    You can operate on your collection or on recommended albums with --recommended.
    """
    chosen_urls: list[str] = []

    if urls:
        chosen_urls.extend([u.strip() for u in urls.split(",") if u.strip()])

    if indices or names:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                client = await main.get_logged_in_client("tidal")
                assert isinstance(client, TidalClient)
                albums = (
                    await client.get_recommended_albums()
                    if recommended
                    else await client.get_user_albums()
                )

                if not albums:
                    console.print("[yellow]No albums found.")
                    return

                if indices:
                    idxs = [int(s.strip()) for s in indices.split(",") if s.strip()]
                    for idx in idxs:
                        if 0 <= idx < len(albums):
                            chosen_urls.append(albums[idx]["url"])
                        else:
                            console.print(f"[red]Index out of range:[/red] {idx}")

                if names:
                    wanted = {s.strip() for s in names.split(",") if s.strip()}
                    for a in albums:
                        if a.get("name") in wanted:
                            chosen_urls.append(a["url"])

    # De-duplicate
    seen = set()
    filtered = []
    for u in chosen_urls:
        if u not in seen:
            seen.add(u)
            filtered.append(u)
    chosen_urls = filtered

    if not chosen_urls:
        console.print("[yellow]No albums selected to download.")
        return

    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_all(chosen_urls)
            await main.resolve()
            await main.rip()


@tidal.command("download")
@click.option(
    "--recommended",
    is_flag=True,
    help="Use recommended discovery playlists as the source list",
)
@click.option(
    "-i",
    "--indices",
    help="Comma-separated indices to download from the printed list.",
)
@click.option(
    "-n",
    "--names",
    help="Comma-separated exact playlist names to download.",
)
@click.option(
    "-u",
    "--urls",
    help="Comma-separated playlist URLs to download.",
)
@click.pass_context
@coro
async def tidal_download(ctx, recommended, indices, names, urls):
    """Download TIDAL playlists by index, name, or JSON/URL list.

    Choose one or more of:
      -i/--indices  Comma-separated indices from 'list-playlists'
      -n/--names    Comma-separated exact playlist names
      -u/--urls     Comma-separated playlist URLs

    Examples:
      rip tidal download -i "0,2,5"
      rip tidal download -n "My Mix,Deep House Essentials"
      rip tidal download -u "https://tidal.com/playlist/UUID"
    """
    chosen_urls: list[str] = []

    # Direct URLs (fast path)
    if urls:
        chosen_urls.extend([u.strip() for u in urls.split(",") if u.strip()])

    # Selection by indices or names requires fetching the list first
    if indices or names:
        with ctx.obj["config"] as cfg:
            async with Main(cfg) as main:
                client = await main.get_logged_in_client("tidal")
                assert isinstance(client, TidalClient)
                playlists = (
                    await client.get_recommended_playlists()
                    if recommended
                    else await client.get_user_playlists()
                )

                if not playlists:
                    console.print("[yellow]No playlists found.")
                    return

                if indices:
                    idxs = [int(s.strip()) for s in indices.split(",") if s.strip()]
                    for idx in idxs:
                        if 0 <= idx < len(playlists):
                            chosen_urls.append(playlists[idx]["url"])
                        else:
                            console.print(f"[red]Index out of range:[/red] {idx}")

                if names:
                    wanted = {s.strip() for s in names.split(",") if s.strip()}
                    for p in playlists:
                        if p.get("name") in wanted:
                            chosen_urls.append(p["url"])

    # De-duplicate while preserving order
    seen = set()
    filtered = []
    for u in chosen_urls:
        if u not in seen:
            seen.add(u)
            filtered.append(u)
    chosen_urls = filtered

    if not chosen_urls:
        console.print("[yellow]No playlists selected to download.")
        return

    with ctx.obj["config"] as cfg:
        async with Main(cfg) as main:
            await main.add_all(chosen_urls)
            await main.resolve()
            await main.rip()


@tidal.command("download-missing")
@click.option(
    "--playlists",
    is_flag=True,
    help="Download missing tracks from your TIDAL playlists",
)
@click.option(
    "--albums",
    is_flag=True,
    help="Download missing tracks from your TIDAL saved albums",
)
@click.option(
    "--recommended", is_flag=True, help="Include recommended playlists/albums"
)
@click.option(
    "--limit", type=int, help="Limit number of tracks to download (useful for testing)"
)
@click.option(
    "--batch-size",
    default=10,
    help="Number of tracks to process in each batch (default: 10)",
    type=click.IntRange(min=1, max=100),
)
@click.pass_context
def tidal_download_missing(ctx, playlists, albums, recommended, limit, batch_size):
    """Download only missing tracks from your TIDAL collections using streaming batches.

    This command automatically skips tracks that are already downloaded, making it
    safe to run multiple times. Uses streaming batch processing for better progress
    feedback and resilience. Each batch is resolved and downloaded independently,
    so failures don't stop the entire process.

    Fast mode uses optimized batch API calls to significantly speed up playlist
    track resolution by fetching multiple track metadata in single API calls.

    Examples:
        rip tidal download-missing --playlists
        rip tidal download-missing --playlists --albums
        rip tidal download-missing --playlists --limit 10
        rip tidal download-missing --playlists --batch-size 5
        rip tidal download-missing --playlists --fast-mode
    """
    if not playlists and not albums:
        console.print("[red]Please specify --playlists and/or --albums to download")
        return

    console.print("[blue]🎵 Downloading missing tracks from TIDAL collections...")

    # Get TidalClient and run comparison + download
    with ctx.obj["config"] as cfg:
        import asyncio

        from .main import Main

        async def run_download():
            async with Main(cfg) as main:
                client = await main.get_logged_in_client("tidal")
                from ..client.tidal import TidalClient

                assert isinstance(client, TidalClient)

                # Get missing tracks with playlist context for proper organization
                if playlists:
                    missing_tracks_by_playlist = (
                        await client.get_missing_tracks_with_context(
                            playlists=playlists,
                            albums=albums,
                            recommended=recommended,
                            db=main.database,
                        )
                    )

                    if not missing_tracks_by_playlist:
                        console.print(
                            "[green]🎉 No missing tracks found! You're all caught up."
                        )
                        return

                    total_missing = sum(
                        len(tracks) for tracks in missing_tracks_by_playlist.values()
                    )

                    if limit:
                        # Apply limit across all playlists
                        remaining_limit = limit
                        limited_playlists = {}
                        for playlist_name, tracks in missing_tracks_by_playlist.items():
                            if remaining_limit <= 0:
                                break
                            limited_tracks = tracks[:remaining_limit]
                            if limited_tracks:
                                limited_playlists[playlist_name] = limited_tracks
                                remaining_limit -= len(limited_tracks)
                        missing_tracks_by_playlist = limited_playlists
                        total_missing = sum(
                            len(tracks)
                            for tracks in missing_tracks_by_playlist.values()
                        )
                        console.print(
                            f"[yellow]Limited to first {total_missing} missing tracks"
                        )

                    console.print(
                        f"[green]Found {total_missing} missing tracks across {len(missing_tracks_by_playlist)} playlists"
                    )

                    if click.confirm(
                        f"Download {total_missing} missing tracks with proper playlist organization?"
                    ):
                        console.print(
                            "[blue]🔄 Auto-skip mode: will skip any tracks that are already downloaded"
                        )
                        console.print(
                            "[blue]📁 Tracks will be organized into proper playlist folders"
                        )
                        # Use the new playlist-aware download method
                        await _download_playlist_tracks_with_context(
                            missing_tracks_by_playlist, main, batch_size, resume=True
                        )
                    else:
                        console.print("[yellow]Download cancelled")
                else:
                    # For albums-only downloads, use the old method
                    missing_tracks = await client.get_missing_tracks_for_download(
                        playlists=playlists,
                        albums=albums,
                        recommended=recommended,
                        db=main.database,
                    )

                    if not missing_tracks:
                        console.print(
                            "[green]🎉 No missing tracks found! You're all caught up."
                        )
                        return

                    if limit:
                        missing_tracks = missing_tracks[:limit]
                        console.print(
                            f"[yellow]Limited to first {limit} missing tracks"
                        )

                    console.print(
                        f"[green]Found {len(missing_tracks)} missing tracks to download"
                    )

                    if click.confirm(f"Download {len(missing_tracks)} missing tracks?"):
                        console.print(
                            "[blue]🔄 Auto-skip mode: will skip any tracks that are already downloaded"
                        )
                        await _download_track_list(
                            missing_tracks, main, batch_size, resume=True
                        )
                    else:
                        console.print("[yellow]Download cancelled")

        # Run the async function
        asyncio.run(run_download())


async def _download_track_list(track_urls, main, batch_size=10, resume=False):
    """Download a list of track URLs using streaming batch processing."""
    console.print(
        f"[blue]📥 Downloading {len(track_urls)} tracks using streaming batches..."
    )

    # For download-missing, skip already downloaded tracks at the URL level
    skip_downloaded = resume  # If resume=True, skip downloaded tracks
    await main.add_all(track_urls, skip_downloaded=skip_downloaded)

    # Use streaming processing for better progress and resilience
    await main.process_streaming(batch_size=batch_size, resume=resume)

    console.print("[green]✅ Download completed!")


async def _download_playlist_tracks_with_context(
    playlist_data, main, batch_size=10, resume=False
):
    """Download playlist tracks while preserving playlist folder structure."""
    console.print(
        "[blue]📥 Downloading playlist tracks with proper folder structure..."
    )

    import os

    from ..filepath_utils import clean_filepath
    from ..media.playlist import PendingPlaylistTrack

    total_tracks = 0
    all_pending_tracks = []

    for playlist_name, track_data_list in playlist_data.items():
        if not track_data_list:
            continue

        console.print(
            f"[blue]📁 Processing playlist: {playlist_name} ({len(track_data_list)} tracks)"
        )

        # Create proper playlist folder structure
        parent = main.config.session.downloads.folder
        parent = os.path.join(parent, "Tidal")  # TIDAL-specific folder
        parent = os.path.join(parent, "playlists")
        folder = os.path.join(parent, clean_filepath(playlist_name))

        # Create PendingPlaylistTrack objects with proper context
        for position, track_data in enumerate(track_data_list, 1):
            pending_track = PendingPlaylistTrack(
                id=track_data["id"],
                client=await main.get_logged_in_client("tidal"),
                config=main.config,
                folder=folder,
                playlist_name=playlist_name,
                position=position,
                db=main.database,
            )
            all_pending_tracks.append(pending_track)
            total_tracks += 1

    # Download all tracks with proper playlist context using efficient batch processing
    if all_pending_tracks:
        console.print(
            f"[blue]📥 Downloading {total_tracks} tracks with playlist context..."
        )

        # Add all pending tracks directly to main's pending queue
        # This preserves the playlist context while using the efficient streaming system
        for pending_track in all_pending_tracks:
            main.pending.append(pending_track)

        # Use the efficient streaming batch processing
        await main.process_streaming(batch_size=batch_size, resume=resume)

    console.print(
        f"[green]✅ Downloaded {total_tracks} tracks with proper playlist organization!"
    )


@tidal.command("compare")
@click.option(
    "--playlists",
    is_flag=True,
    help="Compare downloaded tracks with your TIDAL playlists",
)
@click.option(
    "--albums",
    is_flag=True,
    help="Compare downloaded tracks with your TIDAL saved albums",
)
@click.option(
    "--recommended",
    is_flag=True,
    help="Include recommended playlists/albums in comparison",
)
@click.option(
    "--missing-only",
    is_flag=True,
    help="Show only missing tracks (not already downloaded)",
)
@click.option(
    "--download-missing",
    is_flag=True,
    help="Automatically download missing tracks after comparison",
)
@click.pass_context
def tidal_compare(ctx, playlists, albums, recommended, missing_only, download_missing):
    """Compare your TIDAL collections with downloaded files to find missing tracks.

    Examples:
        rip tidal compare --playlists --albums
        rip tidal compare --playlists --recommended --missing-only
        rip tidal compare --albums --download-missing
    """
    if not playlists and not albums:
        console.print("[red]Please specify --playlists and/or --albums to compare")
        return

    console.print("[blue]🔍 Comparing TIDAL collections with downloaded files...")

    # Get TidalClient and run comparison
    with ctx.obj["config"] as cfg:
        import asyncio

        from .main import Main

        async def run_comparison():
            async with Main(cfg) as main:
                client = await main.get_logged_in_client("tidal")
                from ..client.tidal import TidalClient

                assert isinstance(client, TidalClient)

                # Run the comparison
                results = await client.compare_collections_with_downloads(
                    playlists=playlists,
                    albums=albums,
                    recommended=recommended,
                    missing_only=missing_only,
                    db=main.database,
                )

                # If download_missing is requested, offer to download missing tracks
                if download_missing:
                    total_missing = sum(
                        data["missing_tracks"] for data in results.values()
                    )
                    if total_missing > 0:
                        console.print(f"\n[green]Found {total_missing} missing tracks.")
                        if click.confirm(
                            "Would you like to download the missing tracks?"
                        ):
                            await _download_missing_tracks(client, results, cfg)
                    else:
                        console.print(
                            "[green]🎉 No missing tracks found! You're all caught up."
                        )

        # Run the async function
        asyncio.run(run_comparison())


async def _download_missing_tracks(client, results, config):
    """Download missing tracks found in comparison."""
    from .main import Main

    console.print("[blue]📥 Downloading missing tracks...")

    urls_to_download = []

    # Collect all missing track URLs
    for collection_type, data in results.items():
        for track in data["missing"]:
            track_id = track.get("id")
            if track_id:
                urls_to_download.append(f"https://tidal.com/track/{track_id}")

    if urls_to_download:
        console.print(f"[green]Downloading {len(urls_to_download)} missing tracks...")

        with config as cfg:
            async with Main(cfg) as main:
                await main.add_all(urls_to_download)
                # Use streaming processing for better progress and resilience
                await main.process_streaming(batch_size=10)

        console.print("[green]✅ Download completed!")
    else:
        console.print("[yellow]No valid track URLs found to download.")


async def latest_streamrip_version(verify_ssl: bool = True) -> tuple[str, str | None]:
    """Get the latest streamrip version from PyPI and release notes from GitHub.

    Args:
        verify_ssl: Whether to verify SSL certificates

    Returns:
        A tuple of (version, release_notes)
    """
    # Create connector with appropriate SSL settings
    connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
    connector = aiohttp.TCPConnector(**connector_kwargs)

    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get("https://pypi.org/pypi/streamrip/json") as resp:
            data = await resp.json()
        version = data["info"]["version"]

        if version == __version__:
            return version, None

        async with s.get(
            "https://api.github.com/repos/nathom/streamrip/releases/latest"
        ) as resp:
            json = await resp.json()
        notes = json["body"]
    return version, notes


# Database Migration Commands
@rip.group("database")
def database():
    """Database management commands."""
    pass


@database.command("migrate")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be migrated without making changes"
)
@click.option(
    "--force", is_flag=True, help="Force migration even if already up to date"
)
@click.pass_context
def database_migrate(ctx, dry_run, force):
    """Migrate database to enhanced structure with full metadata."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path
        migration = db.DatabaseMigration(db_path)

        console.print("[blue]🔄 Database Migration")

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist yet")
            return

        if not migration.needs_migration() and not force:
            console.print("[green]✅ Database is already up to date")
            return

        if dry_run:
            console.print("[blue]🔍 Dry run - no changes will be made")
            console.print("[blue]Would migrate database to enhanced structure")
            return

        console.print("[blue]📦 Creating backup...")
        if not migration.backup_database():
            console.print("[red]❌ Failed to create backup. Migration aborted.")
            return

        console.print("[blue]🔄 Migrating database...")
        if migration.migrate_database():
            console.print("[green]✅ Migration completed successfully!")
            console.print(f"[blue]💾 Backup saved to: {migration.backup_path}")
        else:
            console.print("[red]❌ Migration failed. Check logs for details.")


@database.command("rollback")
@click.pass_context
def database_rollback(ctx):
    """Rollback database to backup (if available)."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        # Find backup files
        backup_files = []
        for file in os.listdir(os.path.dirname(db_path)):
            if file.startswith(os.path.basename(db_path) + ".backup_"):
                backup_files.append(os.path.join(os.path.dirname(db_path), file))

        if not backup_files:
            console.print("[yellow]No backup files found")
            return

        # Sort by modification time (newest first)
        backup_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        console.print("[blue]📋 Available backups:")
        for i, backup in enumerate(backup_files[:5]):  # Show last 5 backups
            mtime = datetime.fromtimestamp(os.path.getmtime(backup))
            console.print(
                f"[blue]{i + 1}. {os.path.basename(backup)} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})"
            )

        if click.confirm("Rollback to most recent backup?"):
            try:
                shutil.copy2(backup_files[0], db_path)
                console.print("[green]✅ Database rolled back successfully")
            except Exception as e:
                console.print(f"[red]❌ Rollback failed: {e}")


@database.command("verify")
@click.pass_context
def database_verify(ctx):
    """Verify database integrity after migration."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[blue]🔍 Verifying database integrity...")

        with sqlite3.connect(db_path) as conn:
            # Check old vs new counts
            try:
                old_count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
                new_count = conn.execute(
                    "SELECT COUNT(*) FROM downloads_enhanced"
                ).fetchone()[0]

                if old_count == new_count:
                    console.print(f"[green]✅ Download counts match: {old_count}")
                else:
                    console.print(
                        f"[red]❌ Download count mismatch: {old_count} vs {new_count}"
                    )

            except Exception as e:
                console.print(f"[red]❌ Error checking counts: {e}")

            # Check table structure
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            required_tables = [
                "downloads",
                "downloads_enhanced",
                "collections",
                "track_collections",
                "failed_downloads_enhanced",
            ]
            missing_tables = [t for t in required_tables if t not in tables]

            if missing_tables:
                console.print(f"[red]❌ Missing tables: {missing_tables}")
            else:
                console.print("[green]✅ All required tables present")


@database.command("cleanup")
@click.option("--confirm", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def database_cleanup(ctx, confirm):
    """Clean up old database tables after successful migration."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[yellow]⚠️  WARNING: This will remove old database tables!")
        console.print(
            "[blue]This should only be done after verifying migration is complete."
        )

        if not confirm and not click.confirm(
            "Are you sure you want to cleanup old tables?"
        ):
            console.print("[yellow]Cleanup cancelled")
            return

        try:
            with sqlite3.connect(db_path) as conn:
                # Check if enhanced tables have data
                enhanced_count = conn.execute(
                    "SELECT COUNT(*) FROM downloads_enhanced"
                ).fetchone()[0]
                if enhanced_count == 0:
                    console.print("[red]❌ Enhanced tables are empty. Cleanup aborted.")
                    return

                # Drop old tables
                conn.execute("DROP TABLE IF EXISTS downloads")
                conn.execute("DROP TABLE IF EXISTS failed_downloads")

                console.print("[green]✅ Old tables cleaned up successfully")
                console.print("[blue]💡 You can now use enhanced database features")

        except Exception as e:
            console.print(f"[red]❌ Cleanup failed: {e}")


@database.command("inspect")
@click.option(
    "--table",
    help="Specific table to inspect (downloads_enhanced, collections, track_collections, failed_downloads_enhanced)",
)
@click.option(
    "--limit", type=int, default=10, help="Number of rows to show (default: 10)"
)
@click.option("--columns", help="Specific columns to show (comma-separated)")
@click.pass_context
def database_inspect(ctx, table, limit, columns):
    """Inspect database tables and their contents."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[blue]🔍 Database Inspector")

        with sqlite3.connect(db_path) as conn:
            # Get all tables
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            all_tables = [row[0] for row in cursor.fetchall()]

            if table:
                tables_to_show = [table] if table in all_tables else []
                if not tables_to_show:
                    console.print(f"[red]❌ Table '{table}' not found")
                    console.print(f"[blue]Available tables: {', '.join(all_tables)}")
                    return
            else:
                tables_to_show = all_tables

            for table_name in tables_to_show:
                console.print(f"\n[bold blue]📋 Table: {table_name}[/bold blue]")

                # Get table schema
                cursor = conn.execute(f"PRAGMA table_info({table_name})")
                schema = cursor.fetchall()

                if schema:
                    console.print("[dim]Schema:[/dim]")
                    for col in schema:
                        col_name, col_type = col[1], col[2]
                        nullable = "NULL" if col[3] == 0 else "NOT NULL"
                        console.print(
                            f"  [dim]{col_name}: {col_type} ({nullable})[/dim]"
                        )

                # Get row count
                cursor = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]
                console.print(f"[blue]Rows: {count}")

                if count > 0:
                    # Show sample data
                    if columns:
                        col_list = [col.strip() for col in columns.split(",")]
                        # Validate columns exist
                        valid_cols = [col[1] for col in schema]
                        invalid_cols = [
                            col for col in col_list if col not in valid_cols
                        ]
                        if invalid_cols:
                            console.print(f"[red]❌ Invalid columns: {invalid_cols}")
                            console.print(
                                f"[blue]Valid columns: {', '.join(valid_cols)}"
                            )
                            continue
                        select_cols = ", ".join(col_list)
                    else:
                        select_cols = "*"

                    cursor = conn.execute(
                        f"SELECT {select_cols} FROM {table_name} LIMIT {limit}"
                    )
                    rows = cursor.fetchall()

                    if rows:
                        console.print(
                            f"[blue]Sample data (first {len(rows)} rows):[/blue]"
                        )

                        # Get column names
                        if columns:
                            col_names = [col.strip() for col in columns.split(",")]
                        else:
                            col_names = [col[1] for col in schema]

                        # Create table
                        from rich.table import Table

                        sample_table = Table(
                            show_header=True, header_style="bold magenta"
                        )

                        for col_name in col_names:
                            sample_table.add_column(col_name, overflow="fold")

                        for row in rows:
                            # Truncate long values for display
                            display_row = []
                            for val in row:
                                if val is None:
                                    display_row.append("[dim]NULL[/dim]")
                                elif isinstance(val, str) and len(val) > 50:
                                    display_row.append(val[:47] + "...")
                                else:
                                    display_row.append(str(val))
                            sample_table.add_row(*display_row)

                        console.print(sample_table)

                        if count > limit:
                            console.print(
                                f"[dim]... and {count - limit} more rows[/dim]"
                            )


@database.command("query")
@click.argument("sql", required=True)
@click.pass_context
def database_query(ctx, sql):
    """Execute a custom SQL query on the database."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print(f"[blue]🔍 Executing SQL: {sql}[/blue]")

        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute(sql)
                rows = cursor.fetchall()

                if rows:
                    # Get column names
                    col_names = [description[0] for description in cursor.description]

                    # Create table
                    from rich.table import Table

                    result_table = Table(show_header=True, header_style="bold magenta")

                    for col_name in col_names:
                        result_table.add_column(col_name, overflow="fold")

                    for row in rows:
                        display_row = []
                        for val in row:
                            if val is None:
                                display_row.append("[dim]NULL[/dim]")
                            elif isinstance(val, str) and len(val) > 100:
                                display_row.append(val[:97] + "...")
                            else:
                                display_row.append(str(val))
                        result_table.add_row(*display_row)

                    console.print(result_table)
                    console.print(f"[blue]Found {len(rows)} rows[/blue]")
                else:
                    console.print("[yellow]No results found")

        except Exception as e:
            console.print(f"[red]❌ Query failed: {e}")


@database.command("backfill")
@click.option(
    "--scan-path", help="Path to scan for music files (default: downloads folder)"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be updated without making changes"
)
@click.option(
    "--limit", type=int, help="Limit number of files to process (for testing)"
)
@click.pass_context
def database_backfill(ctx, scan_path, dry_run, limit):
    """Backfill database with metadata from downloaded music files."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        # Determine scan path
        if scan_path:
            music_path = scan_path
        else:
            music_path = cfg.session.downloads.folder

        if not os.path.exists(music_path):
            console.print(f"[yellow]Music path does not exist: {music_path}")
            return

        console.print(f"[blue]🔍 Scanning music files in: {music_path}[/blue]")

        if dry_run:
            console.print("[blue]🔍 Dry run - no changes will be made[/blue]")

        # Import mutagen for metadata extraction
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            console.print(
                "[red]❌ mutagen library not found. Install with: pip install mutagen[/red]"
            )
            return

        # Find music files
        music_extensions = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}
        music_files = []

        for root, dirs, files in os.walk(music_path):
            for file in files:
                if any(file.lower().endswith(ext) for ext in music_extensions):
                    music_files.append(os.path.join(root, file))

        if not music_files:
            console.print("[yellow]No music files found[/yellow]")
            return

        console.print(f"[blue]Found {len(music_files)} music files[/blue]")

        # Process files
        updated_count = 0
        skipped_count = 0
        error_count = 0

        with sqlite3.connect(db_path) as conn:
            for i, file_path in enumerate(music_files, 1):
                try:
                    console.print(
                        f"[blue]Processing {i}/{len(music_files)}: {os.path.basename(file_path)}[/blue]"
                    )

                    # Extract metadata
                    audio_file = MutagenFile(file_path)
                    if audio_file is None:
                        console.print("[yellow]  ⚠️  Could not read metadata[/yellow]")
                        skipped_count += 1
                        continue

                    # Extract metadata fields
                    title = (
                        audio_file.get("title", ["Unknown"])[0]
                        if audio_file.get("title")
                        else "Unknown"
                    )
                    artist = (
                        audio_file.get("artist", ["Unknown"])[0]
                        if audio_file.get("artist")
                        else "Unknown"
                    )
                    album = (
                        audio_file.get("album", [None])[0]
                        if audio_file.get("album")
                        else None
                    )
                    album_artist = (
                        audio_file.get("albumartist", [None])[0]
                        if audio_file.get("albumartist")
                        else None
                    )
                    track_number = (
                        audio_file.get("tracknumber", [None])[0]
                        if audio_file.get("tracknumber")
                        else None
                    )
                    disc_number = (
                        audio_file.get("discnumber", [None])[0]
                        if audio_file.get("discnumber")
                        else None
                    )
                    year = (
                        audio_file.get("date", [None])[0]
                        if audio_file.get("date")
                        else None
                    )
                    genre = (
                        audio_file.get("genre", [None])[0]
                        if audio_file.get("genre")
                        else None
                    )
                    duration = (
                        int(audio_file.info.length)
                        if hasattr(audio_file, "info") and audio_file.info.length
                        else None
                    )

                    # Determine quality based on file format and bitrate
                    quality = "Unknown"
                    if hasattr(audio_file, "info"):
                        if audio_file.info.bitrate:
                            if audio_file.info.bitrate >= 320:
                                quality = "HIGH"
                            elif audio_file.info.bitrate >= 256:
                                quality = "MEDIUM"
                            else:
                                quality = "LOW"

                    # Determine source based on file path
                    source = "unknown"
                    if "tidal" in file_path.lower():
                        source = "tidal"
                    elif "qobuz" in file_path.lower():
                        source = "qobuz"
                    elif "deezer" in file_path.lower():
                        source = "deezer"
                    elif "soundcloud" in file_path.lower():
                        source = "soundcloud"

                    # Get file size
                    file_size = os.path.getsize(file_path)

                    # Try to find matching track in database by file path or similar metadata
                    # For now, we'll update tracks with "Unknown Track" title
                    cursor = conn.execute(
                        """
                        SELECT id FROM downloads_enhanced
                        WHERE title = 'Unknown Track' AND file_path = 'unknown'
                        LIMIT 1
                    """
                    )

                    track_id = cursor.fetchone()
                    if track_id:
                        track_id = track_id[0]

                        if not dry_run:
                            # Update the record
                            conn.execute(
                                """
                                UPDATE downloads_enhanced SET
                                    source = ?, title = ?, artist = ?, album = ?,
                                    album_artist = ?, track_number = ?, disc_number = ?,
                                    year = ?, genre = ?, duration = ?, quality = ?,
                                    file_path = ?, file_size = ?
                                WHERE id = ?
                            """,
                                (
                                    source,
                                    title,
                                    artist,
                                    album,
                                    album_artist,
                                    track_number,
                                    disc_number,
                                    year,
                                    genre,
                                    duration,
                                    quality,
                                    file_path,
                                    file_size,
                                    track_id,
                                ),
                            )

                        console.print(
                            f"[green]  ✅ Updated: {title} - {artist}[/green]"
                        )
                        updated_count += 1
                    else:
                        console.print(
                            "[yellow]  ⚠️  No matching track found in database[/yellow]"
                        )
                        skipped_count += 1

                except Exception as e:
                    console.print(f"[red]  ❌ Error processing {file_path}: {e}[/red]")
                    error_count += 1
                    continue

        # Commit changes
        if not dry_run:
            conn.commit()

        console.print("\n[blue]📊 Backfill Summary:[/blue]")
        console.print(f"[green]  ✅ Updated: {updated_count}[/green]")
        console.print(f"[yellow]  ⚠️  Skipped: {skipped_count}[/yellow]")
        console.print(f"[red]  ❌ Errors: {error_count}[/red]")

        if dry_run:
            console.print("[blue]💡 Run without --dry-run to apply changes[/blue]")


@database.command("sync")
@click.option(
    "--scan-path", help="Path to scan for music files (default: downloads folder)"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be updated without making changes"
)
@click.pass_context
def database_sync(ctx, scan_path, dry_run):
    """Sync database with existing music files - adds missing entries."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        # Determine scan path
        if scan_path:
            music_path = scan_path
        else:
            music_path = cfg.session.downloads.folder

        if not os.path.exists(music_path):
            console.print(f"[yellow]Music path does not exist: {music_path}[/yellow]")
            return

        console.print(
            f"[blue]🔍 Syncing database with music files in: {music_path}[/blue]"
        )

        if dry_run:
            console.print("[blue]🔍 Dry run - no changes will be made[/blue]")

        # Import mutagen for metadata extraction
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            console.print(
                "[red]❌ mutagen library not found. Install with: pip install mutagen[/red]"
            )
            return

        # Find music files
        music_extensions = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}
        music_files = []

        for root, dirs, files in os.walk(music_path):
            for file in files:
                if any(file.lower().endswith(ext) for ext in music_extensions):
                    music_files.append(os.path.join(root, file))

        if not music_files:
            console.print("[yellow]No music files found[/yellow]")
            return

        console.print(f"[blue]Found {len(music_files)} music files[/blue]")

        # Process files
        added_count = 0
        skipped_count = 0
        error_count = 0

        with sqlite3.connect(db_path) as conn:
            for i, file_path in enumerate(music_files, 1):
                try:
                    console.print(
                        f"[blue]Processing {i}/{len(music_files)}: {os.path.basename(file_path)}[/blue]"
                    )

                    # Extract metadata
                    audio_file = MutagenFile(file_path)
                    if audio_file is None:
                        console.print("[yellow]  ⚠️  Could not read metadata[/yellow]")
                        skipped_count += 1
                        continue

                    # Extract metadata fields
                    title = (
                        audio_file.get("title", ["Unknown"])[0]
                        if audio_file.get("title")
                        else "Unknown"
                    )
                    artist = (
                        audio_file.get("artist", ["Unknown"])[0]
                        if audio_file.get("artist")
                        else "Unknown"
                    )
                    album = (
                        audio_file.get("album", [None])[0]
                        if audio_file.get("album")
                        else None
                    )
                    album_artist = (
                        audio_file.get("albumartist", [None])[0]
                        if audio_file.get("albumartist")
                        else None
                    )
                    track_number = (
                        audio_file.get("tracknumber", [None])[0]
                        if audio_file.get("tracknumber")
                        else None
                    )
                    disc_number = (
                        audio_file.get("discnumber", [None])[0]
                        if audio_file.get("discnumber")
                        else None
                    )
                    year = (
                        audio_file.get("date", [None])[0]
                        if audio_file.get("date")
                        else None
                    )
                    genre = (
                        audio_file.get("genre", [None])[0]
                        if audio_file.get("genre")
                        else None
                    )
                    duration = (
                        int(audio_file.info.length)
                        if hasattr(audio_file, "info") and audio_file.info.length
                        else None
                    )

                    # Determine quality based on file format and bitrate
                    quality = "Unknown"
                    if hasattr(audio_file, "info"):
                        if audio_file.info.bitrate:
                            if audio_file.info.bitrate >= 320:
                                quality = "HIGH"
                            elif audio_file.info.bitrate >= 256:
                                quality = "MEDIUM"
                            else:
                                quality = "LOW"

                    # Determine source based on file path
                    source = "unknown"
                    if "tidal" in file_path.lower():
                        source = "tidal"
                    elif "qobuz" in file_path.lower():
                        source = "qobuz"
                    elif "deezer" in file_path.lower():
                        source = "deezer"
                    elif "soundcloud" in file_path.lower():
                        source = "soundcloud"

                    # Get file size
                    file_size = os.path.getsize(file_path)

                    # Generate a unique ID based on file path and metadata
                    import hashlib

                    unique_string = f"{source}:{title}:{artist}:{album}:{file_path}"
                    track_id = hashlib.md5(unique_string.encode()).hexdigest()

                    # Check if this track is already in the database
                    cursor = conn.execute(
                        "SELECT id FROM downloads_enhanced WHERE id = ?", (track_id,)
                    )
                    if cursor.fetchone():
                        console.print("[yellow]  ⚠️  Track already in database[/yellow]")
                        skipped_count += 1
                        continue

                    # Check if file path is already in database
                    cursor = conn.execute(
                        "SELECT id FROM downloads_enhanced WHERE file_path = ?",
                        (file_path,),
                    )
                    if cursor.fetchone():
                        console.print(
                            "[yellow]  ⚠️  File path already in database[/yellow]"
                        )
                        skipped_count += 1
                        continue

                    if not dry_run:
                        # Add new record to enhanced downloads table
                        conn.execute(
                            """
                            INSERT INTO downloads_enhanced
                            (id, source, title, artist, album, album_artist, track_number,
                             disc_number, year, genre, duration, quality, file_path, file_size,
                             download_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                track_id,
                                source,
                                title,
                                artist,
                                album,
                                album_artist,
                                track_number,
                                disc_number,
                                year,
                                genre,
                                duration,
                                quality,
                                file_path,
                                file_size,
                                datetime.now().isoformat(),
                            ),
                        )

                        # Also add to old downloads table for compatibility
                        conn.execute(
                            "INSERT OR IGNORE INTO downloads (id) VALUES (?)",
                            (track_id,),
                        )

                    console.print(f"[green]  ✅ Added: {title} - {artist}[/green]")
                    added_count += 1

                except Exception as e:
                    console.print(f"[red]  ❌ Error processing {file_path}: {e}[/red]")
                    error_count += 1
                    continue

        # Commit changes
        if not dry_run:
            conn.commit()

        console.print("\n[blue]📊 Sync Summary:[/blue]")
        console.print(f"[green]  ✅ Added: {added_count}[/green]")
        console.print(f"[yellow]  ⚠️  Skipped: {skipped_count}[/yellow]")
        console.print(f"[red]  ❌ Errors: {error_count}[/red]")

        if dry_run:
            console.print("[blue]💡 Run without --dry-run to apply changes[/blue]")


@database.command("clean")
@click.option(
    "--remove-orphaned",
    is_flag=True,
    help="Remove database entries for files that no longer exist",
)
@click.option(
    "--remove-duplicates", is_flag=True, help="Remove duplicate database entries"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be cleaned without making changes"
)
@click.pass_context
def database_clean(ctx, remove_orphaned, remove_duplicates, dry_run):
    """Clean up database: remove orphaned entries and duplicates."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[blue]🧹 Cleaning up database...[/blue]")

        if dry_run:
            console.print("[blue]🔍 Dry run - no changes will be made[/blue]")

        with sqlite3.connect(db_path) as conn:
            # Remove orphaned entries (files that no longer exist)
            if remove_orphaned:
                console.print("[blue]🔍 Checking for orphaned entries...[/blue]")

                cursor = conn.execute("SELECT id, file_path FROM downloads_enhanced")
                orphaned_count = 0

                for track_id, file_path in cursor.fetchall():
                    if not os.path.exists(file_path):
                        console.print(
                            f"[yellow]  ⚠️  Orphaned: {os.path.basename(file_path)}[/yellow]"
                        )
                        if not dry_run:
                            conn.execute(
                                "DELETE FROM downloads_enhanced WHERE id = ?",
                                (track_id,),
                            )
                            conn.execute(
                                "DELETE FROM downloads WHERE id = ?", (track_id,)
                            )
                        orphaned_count += 1

                console.print(
                    f"[blue]📊 Found {orphaned_count} orphaned entries[/blue]"
                )

            # Remove duplicates (same file path)
            if remove_duplicates:
                console.print("[blue]🔍 Checking for duplicate entries...[/blue]")

                cursor = conn.execute(
                    """
                    SELECT file_path, COUNT(*) as count
                    FROM downloads_enhanced
                    GROUP BY file_path
                    HAVING count > 1
                """
                )

                duplicate_count = 0
                for file_path, count in cursor.fetchall():
                    console.print(
                        f"[yellow]  ⚠️  Duplicate: {os.path.basename(file_path)} ({count} entries)[/yellow]"
                    )

                    if not dry_run:
                        # Keep the first entry, remove the rest
                        cursor2 = conn.execute(
                            """
                            SELECT id FROM downloads_enhanced
                            WHERE file_path = ?
                            ORDER BY download_date DESC
                        """,
                            (file_path,),
                        )

                        entries = cursor2.fetchall()
                        for i, (track_id,) in enumerate(entries):
                            if i > 0:  # Keep first, remove rest
                                conn.execute(
                                    "DELETE FROM downloads_enhanced WHERE id = ?",
                                    (track_id,),
                                )
                                conn.execute(
                                    "DELETE FROM downloads WHERE id = ?", (track_id,)
                                )
                                duplicate_count += 1

                console.print(
                    f"[blue]📊 Found {duplicate_count} duplicate entries to remove[/blue]"
                )

            if not dry_run:
                conn.commit()
                console.print("[green]✅ Database cleaned successfully![/green]")
            else:
                console.print("[blue]💡 Run without --dry-run to apply changes[/blue]")


@database.command("status")
@click.pass_context
def database_status(ctx):
    """Show comprehensive database status and statistics."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path
        music_path = cfg.session.downloads.folder

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[blue]📊 Database Status Report[/blue]")
        console.print(f"[blue]Database path: {db_path}[/blue]")
        console.print(f"[blue]Music folder: {music_path}[/blue]")

        with sqlite3.connect(db_path) as conn:
            # Total tracks in database
            cursor = conn.execute("SELECT COUNT(*) FROM downloads_enhanced")
            total_tracks = cursor.fetchone()[0]
            console.print(f"[green]Total tracks in database: {total_tracks}[/green]")

            # Tracks by source
            cursor = conn.execute(
                """
                SELECT source, COUNT(*)
                FROM downloads_enhanced
                GROUP BY source
                ORDER BY COUNT(*) DESC
            """
            )
            console.print("\n[blue]Tracks by source:[/blue]")
            for source, count in cursor.fetchall():
                console.print(f"  {source}: {count}")

            # Check for orphaned entries
            cursor = conn.execute("SELECT id, file_path FROM downloads_enhanced")
            orphaned_count = 0
            for track_id, file_path in cursor.fetchall():
                if not os.path.exists(file_path):
                    orphaned_count += 1

            if orphaned_count > 0:
                console.print(
                    f"\n[yellow]⚠️  Orphaned entries: {orphaned_count}[/yellow]"
                )
            else:
                console.print("\n[green]✅ No orphaned entries found[/green]")

            # Check for duplicates
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT file_path, COUNT(*) as count
                    FROM downloads_enhanced
                    GROUP BY file_path
                    HAVING count > 1
                )
            """
            )
            duplicate_count = cursor.fetchone()[0]

            if duplicate_count > 0:
                console.print(
                    f"[yellow]⚠️  Duplicate entries: {duplicate_count}[/yellow]"
                )
            else:
                console.print("[green]✅ No duplicate entries found[/green]")

            # Recent downloads
            cursor = conn.execute(
                """
                SELECT title, artist, download_date
                FROM downloads_enhanced
                ORDER BY download_date DESC
                LIMIT 5
            """
            )
            console.print("\n[blue]Recent downloads:[/blue]")
            for title, artist, date in cursor.fetchall():
                console.print(f"  {title} - {artist} ({date[:10]})")


@database.command("organize-files")
@click.option(
    "--source-path", help="Path to organize files from (default: main downloads folder)"
)
@click.option(
    "--target-path", help="Path to organize files to (default: downloads folder)"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be moved without making changes"
)
@click.pass_context
def database_organize_files(ctx, source_path, target_path, dry_run):
    """Organize files from main folder into proper playlist subfolders."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        # Set default paths
        if not source_path:
            source_path = os.path.join(cfg.session.downloads.folder, "Tidal")
        if not target_path:
            target_path = cfg.session.downloads.folder

        if not os.path.exists(source_path):
            console.print(f"[red]Source path does not exist: {source_path}")
            return

        console.print(f"[blue]📁 Organizing files from: {source_path}")
        console.print(f"[blue]📁 Target path: {target_path}")

        if dry_run:
            console.print("[yellow]🔍 DRY RUN MODE - No files will be moved")

        # Get all playlist names from database
        db_path = cfg.session.database.downloads_path
        if not os.path.exists(db_path):
            console.print(f"[red]Database does not exist: {db_path}")
            return

        # Initialize database components needed for this function
        from ..db import Collections, Dummy, EnhancedDownloads

        c = cfg.session.database
        if c.downloads_enabled:
            enhanced_downloads_db = EnhancedDownloads(c.downloads_path)
            collections_db = Collections(c.downloads_path)
        else:
            enhanced_downloads_db = Dummy()
            collections_db = Dummy()

        # Get existing playlist folders
        playlists_dir = os.path.join(target_path, "Tidal", "playlists")
        existing_playlists = []
        if os.path.exists(playlists_dir):
            existing_playlists = [
                d
                for d in os.listdir(playlists_dir)
                if os.path.isdir(os.path.join(playlists_dir, d))
            ]

        console.print(
            f"[blue]📋 Found {len(existing_playlists)} existing playlist folders"
        )

        # Get all files in source path
        music_files = []
        for ext in ["*.flac", "*.mp3", "*.m4a", "*.ogg"]:
            music_files.extend(glob.glob(os.path.join(source_path, ext)))

        console.print(f"[blue]🎵 Found {len(music_files)} music files to organize")

        if not music_files:
            console.print("[yellow]No music files found to organize")
            return

        # Process each file
        moved_count = 0
        skipped_count = 0
        error_count = 0

        # Simple distribution strategy: assign files to playlists based on their index
        for i, file_path in enumerate(music_files):
            try:
                filename = os.path.basename(file_path)
                console.print(f"[blue]Processing: {filename}")

                playlist_name = None

                # Try to match with database entries using file size first
                try:
                    file_size = os.path.getsize(file_path)

                    # Look for matching entry in enhanced downloads
                    with sqlite3.connect(enhanced_downloads_db.path) as conn:
                        cursor = conn.execute(
                            "SELECT id, source_playlist_id, title, artist FROM downloads_enhanced WHERE file_size = ?",
                            (file_size,),
                        )
                        matches = cursor.fetchall()

                        if matches:
                            # Found matching database entry
                            track_id, playlist_id, title, artist = matches[0]

                            # Try to find playlist name from playlist_id
                            if playlist_id:
                                with sqlite3.connect(collections_db.path) as conn2:
                                    cursor2 = conn2.execute(
                                        "SELECT name FROM collections WHERE collection_id = ?",
                                        (playlist_id,),
                                    )
                                    playlist_result = cursor2.fetchone()
                                    if playlist_result:
                                        playlist_name = playlist_result[0]

                except Exception as e:
                    console.print(f"  ⚠️  Database lookup failed: {e}")

                # If no database match, try filename-based matching
                if not playlist_name:
                    # Check if filename contains track number pattern
                    if re.match(r"^\d+\.\s+", filename):
                        # Extract the part after track number
                        track_part = re.sub(r"^\d+\.\s+", "", filename)
                        # Remove file extension
                        track_part = os.path.splitext(track_part)[0]

                        # Try to match with existing playlist names (case insensitive)
                        for playlist in existing_playlists:
                            if playlist.lower() in track_part.lower():
                                playlist_name = playlist
                                break

                # If still no match, distribute evenly among existing playlists
                if not playlist_name and existing_playlists:
                    playlist_index = i % len(existing_playlists)
                    playlist_name = existing_playlists[playlist_index]
                elif not playlist_name:
                    playlist_name = "Mixed"

                # Create target directory
                target_dir = os.path.join(
                    target_path, "Tidal", "playlists", playlist_name
                )

                if not dry_run:
                    os.makedirs(target_dir, exist_ok=True)

                # Move file
                target_file = os.path.join(target_dir, filename)

                if os.path.exists(target_file):
                    console.print(
                        f"  ⚠️  Skipped: File already exists in {playlist_name}"
                    )
                    skipped_count += 1
                else:
                    if dry_run:
                        console.print(f"  📁 Would move to: {playlist_name}/")
                    else:
                        shutil.move(file_path, target_file)
                        console.print(f"  ✅ Moved to: {playlist_name}/")
                    moved_count += 1

            except Exception as e:
                console.print(f"  ❌ Error: {e!s}")
                error_count += 1

        # Summary
        console.print("\n[green]📊 Organization Summary:")
        console.print(f"  ✅ Moved: {moved_count}")
        console.print(f"  ⚠️  Skipped: {skipped_count}")
        console.print(f"  ❌ Errors: {error_count}")

        if dry_run:
            console.print(
                "\n[yellow]🔍 This was a dry run. Run without --dry-run to actually move files."
            )


@database.command("populate-collections")
@click.pass_context
def database_populate_collections(ctx):
    """Populate collections from TIDAL playlists and albums."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[red]❌ Database is disabled in config[/red]")
            return

        console.print("[blue]🔄 Populating collections from TIDAL...")

        async def populate():
            try:
                async with Main(cfg) as main:
                    # Get TIDAL client
                    tidal_client = await main.get_logged_in_client("tidal")
                    if not tidal_client:
                        console.print("[red]❌ TIDAL client not available[/red]")
                        return

                    # Get user playlists
                    playlists = await tidal_client.get_user_playlists()

                    collections_added = 0
                    tracks_linked = 0

                    for playlist in playlists:
                        # Add playlist to collections
                        main.database.add_collection(
                            collection_id=playlist["id"],
                            collection_type="playlist",
                            source="tidal",
                            name=playlist["name"],
                            description=None,  # TIDAL API doesn't provide descriptions
                            track_count=None,  # We'll get this from tracks
                            is_recommended=False,
                        )
                        collections_added += 1

                        # Skip getting tracks for now - we'll populate them later
                        # when we have a working method
                        pass

                    console.print(
                        f"[green]✅ Added {collections_added} collections and "
                        f"linked {tracks_linked} tracks[/green]"
                    )

            except Exception as e:
                console.print(f"[red]❌ Error populating collections: {e}[/red]")

        asyncio.run(populate())


@database.command("stats")
@click.pass_context
def database_stats(ctx):
    """Show comprehensive database statistics."""
    with ctx.obj["config"] as cfg:
        if not cfg.session.database.downloads_enabled:
            console.print("[yellow]Database is disabled in configuration")
            return

        db_path = cfg.session.database.downloads_path

        if not os.path.exists(db_path):
            console.print("[yellow]Database file does not exist")
            return

        console.print("[blue]📊 Database Statistics[/blue]")

        with sqlite3.connect(db_path) as conn:
            # Table overview
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            console.print(f"\n[bold]📋 Tables ({len(tables)}):[/bold]")
            for table in tables:
                cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                console.print(f"  [blue]{table}:[/blue] {count} rows")

            # Enhanced downloads stats
            if "downloads_enhanced" in tables:
                console.print("\n[bold]🎵 Enhanced Downloads Analysis:[/bold]")

                # Sources
                cursor = conn.execute(
                    "SELECT source, COUNT(*) FROM downloads_enhanced GROUP BY source"
                )
                sources = cursor.fetchall()
                if sources:
                    console.print("  [blue]By Source:[/blue]")
                    for source, count in sources:
                        console.print(f"    {source}: {count}")

                # Quality distribution
                cursor = conn.execute(
                    "SELECT quality, COUNT(*) FROM downloads_enhanced WHERE quality IS NOT NULL GROUP BY quality"
                )
                qualities = cursor.fetchall()
                if qualities:
                    console.print("  [blue]By Quality:[/blue]")
                    for quality, count in qualities:
                        console.print(f"    {quality}: {count}")

                # Recent downloads
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM downloads_enhanced
                    WHERE download_date >= date('now', '-7 days')
                """
                )
                recent = cursor.fetchone()[0]
                console.print(f"  [blue]Downloads last 7 days:[/blue] {recent}")

            # Collections stats
            if "collections" in tables:
                cursor = conn.execute(
                    "SELECT collection_type, COUNT(*) FROM collections GROUP BY collection_type"
                )
                collection_types = cursor.fetchall()
                if collection_types:
                    console.print("\n[bold]📋 Collections Analysis:[/bold]")
                    for col_type, count in collection_types:
                        console.print(f"  [blue]{col_type}:[/blue] {count}")

            # File size stats
            if "downloads_enhanced" in tables:
                cursor = conn.execute(
                    """
                    SELECT
                        COUNT(*) as total_files,
                        SUM(file_size) as total_size,
                        AVG(file_size) as avg_size,
                        MIN(file_size) as min_size,
                        MAX(file_size) as max_size
                    FROM downloads_enhanced
                    WHERE file_size IS NOT NULL
                """
                )
                size_stats = cursor.fetchone()
                if size_stats and size_stats[0] > 0:
                    total_files, total_size, avg_size, min_size, max_size = size_stats
                    console.print("\n[bold]💾 File Size Statistics:[/bold]")
                    console.print(f"  [blue]Total files:[/blue] {total_files}")
                    console.print(
                        f"  [blue]Total size:[/blue] {total_size:,} bytes ({total_size / 1024 / 1024 / 1024:.2f} GB)"
                    )
                    console.print(f"  [blue]Average size:[/blue] {avg_size:,.0f} bytes")
                    console.print(
                        f"  [blue]Size range:[/blue] {min_size:,} - {max_size:,} bytes"
                    )


if __name__ == "__main__":
    rip()
