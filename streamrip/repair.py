"""Detect and remove white-noise audio files.

White noise in this context is the symptom of a previous streamrip bug where
compressed DASH fragments were interpreted by ffmpeg as raw PCM. The result
is an audio file with a zero-crossing rate close to 0.5 (random noise crosses
zero ~50% of the time). Real music typically has ZCR well below 0.15.

Because the original fMP4 segments and init segment are gone by the time the
corrupted file is on disk, the audio cannot be reconstructed. The only
recovery action is to delete the bad file and re-download the track.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("streamrip.repair")

# Zero-crossing rate at or above this value is treated as white noise.
# Pure white noise has ZCR ~= 0.5; real music almost always stays below 0.20.
_NOISE_ZCR_THRESHOLD = 0.4

_AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aac"}


@dataclass(slots=True)
class AudioStats:
    """Audio statistics extracted from a file via ffmpeg."""

    path: Path
    zcr: float | None = None  # zero-crossing rate (0.0 - ~0.5)


@dataclass(slots=True)
class DbEntry:
    """A row from downloads_enhanced that matches a corrupted file."""

    id: str
    source: str
    title: str
    artist: str
    file_path: str


def classify_zcr(zcr: float | None) -> str:
    """Return 'noise' if ZCR looks like white noise, otherwise 'ok'."""
    if zcr is None:
        return "ok"
    return "noise" if zcr >= _NOISE_ZCR_THRESHOLD else "ok"


def is_white_noise(path: Path, *, threshold: float = _NOISE_ZCR_THRESHOLD) -> bool:
    """Return True if the file looks like white noise based on zero-crossing rate."""
    zcr = _extract_zcr(path)
    return classify_zcr(zcr) == "noise" and (zcr is not None and zcr >= threshold)


def _extract_zcr(path: Path) -> float | None:
    """Run ffmpeg astats on a file and return its zero-crossing rate."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(path),
                "-af",
                "astats=metadata=1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("ffmpeg failed for %s: %s", path, e)
        return None

    rates: list[float] = []
    for line in result.stderr.splitlines():
        if "Zero crossings rate:" in line:
            try:
                rates.append(float(line.split(":", 1)[1].strip()))
            except ValueError:
                continue
    return rates[-1] if rates else None


def _iter_audio_files(root: Path) -> list[Path]:
    """Recursively find audio files under root."""
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in _AUDIO_EXTENSIONS:
            out.append(path)
    return out


def scan_for_white_noise(
    root: Path | str,
    *,
    max_workers: int = 4,
) -> list[AudioStats]:
    """Scan ``root`` recursively and return AudioStats for files that look like
    white noise.

    Files that fail to analyze are skipped (their zcr is None and they are not
    returned).
    """
    root = Path(root)
    files = _iter_audio_files(root)
    if not files:
        return []

    results: list[AudioStats] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_path = {pool.submit(_extract_zcr, p): p for p in files}
        for fut in as_completed(future_to_path):
            path = future_to_path[fut]
            zcr = fut.result()
            if zcr is None:
                logger.debug("Skipping %s (could not extract zcr)", path)
                continue
            stats = AudioStats(path=path, zcr=zcr)
            if classify_zcr(zcr) == "noise":
                results.append(stats)
    return results


def remove_white_noise_files(
    stats: list[AudioStats],
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Delete the files referenced by ``stats`` that look like white noise.

    Files whose ``zcr`` is below the noise threshold are skipped — the caller
    is expected to pass stats from :func:`scan_for_white_noise`, but this
    function defensively re-checks each entry to avoid deleting good audio.

    Returns the list of paths that were actually removed. With ``dry_run=True``
    nothing is deleted and the returned list is empty, but the caller can still
    see what would have been removed via the input ``stats``.
    """
    if dry_run:
        return []

    removed: list[Path] = []
    for s in stats:
        if classify_zcr(s.zcr) != "noise":
            continue
        try:
            s.path.unlink()
            removed.append(s.path)
            logger.info("Removed white-noise file: %s", s.path)
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("Failed to remove %s: %s", s.path, e)
    return removed


def repair_directory(
    root: Path | str,
    *,
    dry_run: bool = False,
    max_workers: int = 4,
) -> dict:
    """Convenience helper: scan + optionally remove.

    Returns a dict with ``scanned`` (total audio files seen) and ``removed``
    (paths actually deleted).
    """
    root = Path(root)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to repair audio files")

    audio_files = _iter_audio_files(root)
    noise = scan_for_white_noise(root, max_workers=max_workers)
    removed = remove_white_noise_files(noise, dry_run=dry_run)

    return {
        "scanned": len(audio_files),
        "noise": noise,
        "removed": removed,
        "dry_run": dry_run,
    }


def find_db_entries_for_paths(
    db_path: Path | str,
    file_paths: list[Path] | list[str],
    *,
    table: str = "downloads_enhanced",
) -> list[DbEntry]:
    """Look up DB rows whose ``file_path`` matches one of ``file_paths``.

    Returns an empty list if the DB file or table does not exist. Only the
    columns needed for display are fetched.
    """
    db_path = Path(db_path)
    if not db_path.exists() or not file_paths:
        return []

    paths = [str(p) for p in file_paths]
    placeholders = ",".join("?" * len(paths))

    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name=?",
                (table,),
            )
            if cur.fetchone() is None:
                logger.debug("Table %s not present in %s", table, db_path)
                return []

            cur = conn.execute(
                f"SELECT id, source, title, artist, file_path "
                f"FROM {table} WHERE file_path IN ({placeholders})",
                paths,
            )
            return [
                DbEntry(
                    id=row[0],
                    source=row[1] or "",
                    title=row[2] or "",
                    artist=row[3] or "",
                    file_path=row[4] or "",
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning("Could not read DB %s: %s", db_path, e)
        return []


def remove_db_entries_for_paths(
    db_path: Path | str,
    file_paths: list[Path] | list[str],
    *,
    table: str = "downloads_enhanced",
    dry_run: bool = False,
) -> list[str]:
    """DELETE rows from ``table`` whose ``file_path`` matches one of the given paths.

    Returns the IDs of rows that were (or would be, in dry-run mode) removed.
    Gracefully handles a missing DB file, missing table, or empty input by
    returning an empty list.
    """
    db_path = Path(db_path)
    if not db_path.exists() or not file_paths:
        return []

    paths = [str(p) for p in file_paths]
    placeholders = ",".join("?" * len(paths))

    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name=?",
                (table,),
            )
            if cur.fetchone() is None:
                logger.debug("Table %s not present in %s", table, db_path)
                return []

            cur = conn.execute(
                f"SELECT id FROM {table} WHERE file_path IN ({placeholders})",
                paths,
            )
            ids = [row[0] for row in cur.fetchall()]

            if dry_run or not ids:
                return []

            conn.execute(
                f"DELETE FROM {table} WHERE file_path IN ({placeholders})",
                paths,
            )
            conn.commit()
            logger.info("Removed %d rows from %s in %s", len(ids), table, db_path)
            return ids
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning("Could not update DB %s: %s", db_path, e)
        return []


@dataclass(slots=True)
class RepairReport:
    """Aggregate result of a single :func:`repair_path` invocation."""

    noise: list[AudioStats]
    db_entries: list[DbEntry]
    removed_files: list[Path]
    removed_db_ids: list[str]
    apply: bool


def repair_path(
    path: Path | str,
    *,
    db_path: Path | str | None = None,
    apply: bool = False,
    max_workers: int = 4,
) -> RepairReport:
    """Scan ``path``, report white-noise files, and (if ``apply``) delete the
    files AND remove their rows from the downloads DB.

    This is the one-shot helper used by ``rip repair PATH [--apply]``. By
    default it returns the report without changing anything on disk so the
    CLI can present a dry-run summary; with ``apply=True`` the file deletions
    and DB row removals are committed.

    The DB step is always tied to file deletion — there's no way to delete
    files but leave the DB rows (or vice versa). The two operations belong
    together because the whole point is to make the tracks re-downloadable.
    """
    path = Path(path)
    if db_path is None:
        db_path = str(Path.home() / ".config" / "streamrip" / "downloads.db")

    noise = scan_for_white_noise(path, max_workers=max_workers)
    db_entries = find_db_entries_for_paths(db_path, [str(s.path) for s in noise])

    removed_files: list[Path] = []
    removed_db_ids: list[str] = []
    if apply:
        removed_files = remove_white_noise_files(noise, dry_run=False)
        if db_entries:
            removed_db_ids = remove_db_entries_for_paths(
                db_path,
                [e.file_path for e in db_entries],
                dry_run=False,
            )

    return RepairReport(
        noise=noise,
        db_entries=db_entries,
        removed_files=removed_files,
        removed_db_ids=removed_db_ids,
        apply=apply,
    )
