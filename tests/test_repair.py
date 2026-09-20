"""Tests for the repair module (white-noise detection / removal)."""

from pathlib import Path

import pytest

from streamrip.repair import (
    AudioStats,
    classify_zcr,
    is_white_noise,
    remove_white_noise_files,
    scan_for_white_noise,
)


def _require_ffmpeg():
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for repair tests")


def _make_sine(path: Path, duration: float = 2.0) -> Path:
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}:sample_rate=44100",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _make_white_noise(path: Path, duration: float = 2.0) -> Path:
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=white:amplitude=0.3:duration={duration}:sample_rate=44100",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_classify_zcr_thresholds():
    assert classify_zcr(0.5) == "noise"
    assert classify_zcr(0.45) == "noise"
    assert classify_zcr(0.4) == "noise"
    assert classify_zcr(0.39) == "ok"
    assert classify_zcr(0.1) == "ok"
    assert classify_zcr(0.0) == "ok"


def test_is_white_noise_true_for_noise():
    _require_ffmpeg()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = _make_white_noise(Path(td) / "noise.flac")
        assert is_white_noise(path) is True


def test_is_white_noise_false_for_sine():
    _require_ffmpeg()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = _make_sine(Path(td) / "tone.flac")
        assert is_white_noise(path) is False


def test_scan_for_white_noise_returns_only_noise(tmp_path):
    _require_ffmpeg()
    _make_sine(tmp_path / "ok1.flac")
    _make_sine(tmp_path / "ok2.flac")
    _make_white_noise(tmp_path / "bad.flac")

    results = scan_for_white_noise(tmp_path)

    paths = {r.path.name for r in results}
    assert "bad.flac" in paths
    assert "ok1.flac" not in paths
    assert "ok2.flac" not in paths


def test_scan_returns_audio_stats(tmp_path):
    _require_ffmpeg()
    _make_white_noise(tmp_path / "noise.flac")

    results = scan_for_white_noise(tmp_path)
    assert len(results) == 1
    stats = results[0]
    assert isinstance(stats, AudioStats)
    assert stats.zcr is not None
    assert stats.zcr > 0.4


def test_scan_is_recursive(tmp_path):
    _require_ffmpeg()
    sub = tmp_path / "subdir"
    sub.mkdir()
    _make_sine(sub / "ok.flac")
    _make_white_noise(sub / "noise.flac")

    results = scan_for_white_noise(tmp_path)
    names = {r.path.name for r in results}
    assert "noise.flac" in names
    assert "ok.flac" not in names


def test_remove_white_noise_files_deletes_only_noise(tmp_path):
    _require_ffmpeg()
    ok = tmp_path / "ok.flac"
    bad = tmp_path / "bad.flac"
    _make_sine(ok)
    _make_white_noise(bad)

    stats = [AudioStats(path=ok, zcr=0.05), AudioStats(path=bad, zcr=0.5)]
    removed = remove_white_noise_files(stats, dry_run=False)

    assert removed == [bad]
    assert ok.exists()
    assert not bad.exists()


def test_remove_white_noise_files_dry_run_keeps_files(tmp_path):
    _require_ffmpeg()
    bad = tmp_path / "bad.flac"
    _make_white_noise(bad)

    stats = [AudioStats(path=bad, zcr=0.5)]
    removed = remove_white_noise_files(stats, dry_run=True)

    assert removed == []
    assert bad.exists()


def test_remove_returns_empty_when_no_noise(tmp_path):
    _require_ffmpeg()
    ok = tmp_path / "ok.flac"
    _make_sine(ok)
    stats = [AudioStats(path=ok, zcr=0.05)]
    removed = remove_white_noise_files(stats, dry_run=False)
    assert removed == []
    assert ok.exists()


def test_remove_skips_missing_files(tmp_path):
    missing = tmp_path / "ghost.flac"
    stats = [AudioStats(path=missing, zcr=0.5)]
    removed = remove_white_noise_files(stats, dry_run=False)
    assert removed == []


def test_scan_handles_non_audio_dirs(tmp_path):
    """Scanner only picks up audio extensions."""
    _require_ffmpeg()
    (tmp_path / "readme.txt").write_text("not audio")
    (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    results = scan_for_white_noise(tmp_path)
    assert results == []


def test_repair_cli_command_registered():
    """The `repair` subcommand should be wired into the rip CLI."""
    from streamrip.rip.cli import rip

    names = {c for c in rip.commands.keys()}
    assert "repair" in names


def test_repair_cli_runs_dry_run_by_default(tmp_path, monkeypatch):
    """`rip repair <path>` defaults to a dry run (no deletions)."""
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)

    runner = CliRunner()
    result = runner.invoke(
        rip,
        ["--no-progress", "repair", str(tmp_path)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert bad.exists(), "dry run should not delete"


def test_repair_cli_with_delete_removes_files(tmp_path):
    """`rip repair <path> --apply` actually removes white-noise files."""
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    ok = tmp_path / "ok.flac"
    _make_sine(ok)

    runner = CliRunner()
    result = runner.invoke(
        rip,
        ["--no-progress", "repair", str(tmp_path), "--apply"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert not bad.exists()
    assert ok.exists()


def test_repair_cli_rejects_nonexistent_path(tmp_path):
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    runner = CliRunner()
    result = runner.invoke(
        rip,
        ["--no-progress", "repair", str(tmp_path / "nope")],
        catch_exceptions=False,
    )
    assert result.exit_code != 0


def test_find_db_entries_returns_matches(tmp_path):
    """For each white-noise path, look up matching DB rows by file_path."""
    import sqlite3

    from streamrip.repair import find_db_entries_for_paths

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    conn.executemany(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        [
            ("123", "tidal", "Good Song", "Artist A", "/music/good.flac"),
            ("456", "tidal", "Bad Song", "Artist B", "/music/bad.m4a"),
            ("789", "tidal", "Other", "Artist C", "/music/other.flac"),
        ],
    )
    conn.commit()
    conn.close()

    noise_paths = [
        tmp_path / "bad.m4a",  # won't exist on disk; db lookup uses the literal path
    ]
    # The function looks up by absolute path string. Use real-ish absolute paths:
    noise_paths = ["/music/bad.m4a", "/music/missing.flac"]

    matches = find_db_entries_for_paths(db_path, noise_paths)

    assert len(matches) == 1
    entry = matches[0]
    assert entry.id == "456"
    assert entry.title == "Bad Song"
    assert entry.file_path == "/music/bad.m4a"


def test_find_db_entries_handles_missing_db(tmp_path):
    from streamrip.repair import find_db_entries_for_paths

    result = find_db_entries_for_paths(tmp_path / "no.db", ["/x"])
    assert result == []


def test_find_db_entries_handles_missing_table(tmp_path):
    import sqlite3

    from streamrip.repair import find_db_entries_for_paths

    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other (id TEXT)")
    conn.commit()
    conn.close()

    result = find_db_entries_for_paths(db_path, ["/x"])
    assert result == []


def test_remove_db_entries_for_paths_removes_matching_rows(tmp_path):
    """remove_db_entries_for_paths should DELETE rows whose file_path matches."""
    import sqlite3

    from streamrip.repair import remove_db_entries_for_paths

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE downloads_enhanced (id TEXT, file_path TEXT)")
    conn.executemany(
        "INSERT INTO downloads_enhanced VALUES (?, ?)",
        [
            ("123", "/music/good.flac"),
            ("456", "/music/bad.m4a"),
            ("789", "/music/other.flac"),
        ],
    )
    conn.commit()
    conn.close()

    removed = remove_db_entries_for_paths(db_path, ["/music/bad.m4a"])
    assert removed == ["456"]

    # Verify the row is gone
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced ORDER BY id").fetchall()
    conn.close()
    assert rows == [("123",), ("789",)]


def test_remove_db_entries_handles_missing_db(tmp_path):
    from streamrip.repair import remove_db_entries_for_paths

    result = remove_db_entries_for_paths(tmp_path / "no.db", ["/x"])
    assert result == []


def test_remove_db_entries_handles_empty_input(tmp_path):
    from streamrip.repair import remove_db_entries_for_paths

    result = remove_db_entries_for_paths(tmp_path / "x.db", [])
    assert result == []


def test_remove_db_entries_dry_run_keeps_rows(tmp_path):
    import sqlite3

    from streamrip.repair import remove_db_entries_for_paths

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE downloads_enhanced (id TEXT, file_path TEXT)")
    conn.execute("INSERT INTO downloads_enhanced VALUES ('1', '/a.flac')")
    conn.commit()
    conn.close()

    removed = remove_db_entries_for_paths(db_path, ["/a.flac"], dry_run=True)
    assert removed == []

    # Row must still be present
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == [("1",)]


def test_repair_cli_clean_db_removes_rows(tmp_path):
    """`rip repair --apply` should delete matching DB rows AND files together."""
    import sqlite3

    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    # Set up DB with a row matching a noise file
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    conn.execute(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        ("42", "tidal", "Bad Song", "Artist X", str(bad)),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(
        rip,
        [
            "--no-progress",
            "repair",
            str(tmp_path),
            "--db-path",
            str(db_path),
            "--apply",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    # Row must be gone
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == []
    # File must be gone too
    assert not bad.exists()


def test_repair_cli_clean_db_dry_run_by_default(tmp_path):
    """Without --apply, files AND DB rows stay intact."""
    import sqlite3

    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    conn.execute(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        ("42", "tidal", "Bad Song", "Artist X", str(bad)),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(
        rip,
        [
            "--no-progress",
            "repair",
            str(tmp_path),
            "--db-path",
            str(db_path),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    # Row must still be there in dry-run
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == [("42",)]
    # File must still exist
    assert bad.exists()


def test_repair_cli_apply_deletes_files_and_db_rows(tmp_path):
    """`rip repair --apply` does the whole thing in one go (no --delete / --clean-db flags)."""
    import sqlite3

    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    ok = tmp_path / "ok.flac"
    _make_sine(ok)
    conn.execute(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        ("42", "tidal", "Bad Song", "Artist X", str(bad)),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(
        rip,
        [
            "--no-progress",
            "repair",
            str(tmp_path),
            "--db-path",
            str(db_path),
            "--apply",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    # File gone, ok file still there
    assert not bad.exists()
    assert ok.exists()
    # DB row gone
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == []


def test_repair_cli_apply_no_corruption_is_noop(tmp_path):
    """If there's nothing to repair, --apply does nothing and exits clean."""
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    runner = CliRunner()
    result = runner.invoke(
        rip,
        ["--no-progress", "repair", str(tmp_path), "--apply"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "No white-noise files found" in result.output


def test_repair_path_helper_does_everything(tmp_path):
    """repair_path() should scan, delete files, and clean DB in one call."""
    import sqlite3

    from streamrip.repair import repair_path

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    conn.execute(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        ("42", "tidal", "Bad Song", "Artist X", str(bad)),
    )
    conn.commit()
    conn.close()

    result = repair_path(tmp_path, db_path=db_path, apply=True, max_workers=2)

    assert not bad.exists()
    assert result.removed_files == [bad]

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == []
    assert result.removed_db_ids == ["42"]


def test_repair_path_helper_dry_run_changes_nothing(tmp_path):
    import sqlite3

    from streamrip.repair import repair_path

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads_enhanced (id TEXT, source TEXT, title TEXT, "
        "artist TEXT, file_path TEXT)"
    )
    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    conn.execute(
        "INSERT INTO downloads_enhanced VALUES (?, ?, ?, ?, ?)",
        ("42", "tidal", "Bad Song", "Artist X", str(bad)),
    )
    conn.commit()
    conn.close()

    result = repair_path(tmp_path, db_path=db_path, apply=False, max_workers=2)

    assert bad.exists()
    assert result.removed_files == []
    assert result.removed_db_ids == []

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM downloads_enhanced").fetchall()
    conn.close()
    assert rows == [("42",)]
