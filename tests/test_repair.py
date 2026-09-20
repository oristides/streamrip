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
    """`rip repair <path> --delete` actually removes white-noise files."""
    from click.testing import CliRunner

    from streamrip.rip.cli import rip

    bad = tmp_path / "noise.flac"
    _make_white_noise(bad)
    ok = tmp_path / "ok.flac"
    _make_sine(ok)

    runner = CliRunner()
    result = runner.invoke(
        rip,
        ["--no-progress", "repair", str(tmp_path), "--delete"],
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
