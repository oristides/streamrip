import asyncio
import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from streamrip.client.downloadable import TidalDownloadable, reconstruct_dash_audio
from streamrip.client.tidal import parse_dash_manifest

SAMPLE_DASH_MPD = """\
<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet>
      <Representation id="0" codecs="mp4a.40.2">
        <SegmentTemplate
          initialization="https://cdn.example/init.mp4"
          media="https://cdn.example/chunk_$Number$.m4s"
          startNumber="1">
          <SegmentTimeline>
            <S d="44100" r="2"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


class _FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, _chunk_size: int):
        yield self._body


class _FakeResponse:
    def __init__(self, *, headers=None, body: bytes = b"dash-segment"):
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, bodies: dict[str, bytes] | None = None):
        self.bodies = bodies or {}

    def head(self, url: str):
        return _FakeResponse(
            headers={"Content-Type": "audio/mp4"},
            body=self.bodies.get(url, b""),
        )

    def get(self, url: str):
        return _FakeResponse(
            headers={"Content-Type": "audio/mp4"},
            body=self.bodies.get(url, b"dash-segment"),
        )


def test_dash_parser_puts_initialization_segment_first():
    manifest_b64 = base64.b64encode(SAMPLE_DASH_MPD.encode()).decode()
    parsed = parse_dash_manifest(manifest_b64)

    assert parsed["urls"][0] == "https://cdn.example/init.mp4"
    assert parsed["urls"][1:] == [
        "https://cdn.example/chunk_1.m4s",
        "https://cdn.example/chunk_2.m4s",
        "https://cdn.example/chunk_3.m4s",
    ]
    assert "mp4a" in parsed["codecs"]


def test_dash_download_raises_when_reconstruction_fails(tmp_path, monkeypatch):
    session = _FakeSession()
    downloadable = TidalDownloadable(
        session=session,
        url=["https://example.com/seg1", "https://example.com/seg2"],
        codec="flac",
    )

    class _FailedRun:
        def __init__(self):
            self.returncode = 1
            self.stderr = "ffmpeg failed"

    monkeypatch.setattr(
        "streamrip.client.downloadable.shutil.which",
        lambda _: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "streamrip.client.downloadable.subprocess.run",
        lambda *args, **kwargs: _FailedRun(),
    )

    output_path = tmp_path / "track.flac"
    with pytest.raises(
        ValueError,
        match="Failed to reconstruct DASH segments",
    ):
        asyncio.run(downloadable.download(output_path, lambda _: None))


def test_dash_download_never_invokes_raw_pcm_ffmpeg(tmp_path, monkeypatch):
    captured_cmds: list[list[str]] = []

    class _FailedRun:
        def __init__(self):
            self.returncode = 1
            self.stderr = "ffmpeg failed"

    def fake_run(cmd, *args, **kwargs):
        captured_cmds.append(list(cmd))
        return _FailedRun()

    monkeypatch.setattr(
        "streamrip.client.downloadable.shutil.which",
        lambda _: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "streamrip.client.downloadable.subprocess.run",
        fake_run,
    )

    downloadable = TidalDownloadable(
        session=_FakeSession(),
        url=["https://example.com/init", "https://example.com/seg1"],
        codec="aac",
    )
    with pytest.raises(ValueError, match="Failed to reconstruct DASH segments"):
        asyncio.run(downloadable.download(tmp_path / "track.m4a", lambda _: None))

    pcm_formats = {"s16le", "s32le", "s24le", "f32le", "f64le", "u8"}
    for cmd in captured_cmds:
        assert not pcm_formats.intersection(cmd)


def _require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required for DASH reconstruction tests")


def _make_sine_dash(tmp_path: Path) -> tuple[list[Path], Path]:
    _require_ffmpeg()
    dash_dir = tmp_path / "dash"
    dash_dir.mkdir()
    mpd = dash_dir / "stream.mpd"
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3:sample_rate=44100",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "dash",
            "-dash_segment_type",
            "mp4",
            "-seg_duration",
            "1",
            str(mpd),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not generate DASH fixture: {result.stderr}")

    parsed = parse_dash_manifest(base64.b64encode(mpd.read_bytes()).decode())
    segment_files = []
    for url in parsed["urls"]:
        name = Path(url.split("?")[0]).name
        path = dash_dir / name
        if not path.exists():
            pytest.skip(f"DASH fixture missing segment {name}")
        segment_files.append(path)
    return segment_files, dash_dir


def _zero_crossing_rate(path: Path) -> float:
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
        check=False,
    )
    rates = [
        float(line.split(":", 1)[1])
        for line in result.stderr.splitlines()
        if "Zero crossings rate:" in line
    ]
    assert rates, result.stderr
    return rates[-1]


def test_reconstruct_dash_audio_keeps_tone_not_noise(tmp_path):
    segment_files, _ = _make_sine_dash(tmp_path)
    output = tmp_path / "reconstructed.m4a"
    reconstruct_dash_audio([str(p) for p in segment_files], output)

    assert output.exists() and output.stat().st_size > 0
    # A 440 Hz sine has a low zero-crossing rate. Treating compressed
    # fragments as PCM produces ~0.5 (white noise).
    assert _zero_crossing_rate(output) < 0.15


def test_tidal_downloadable_reconstructs_real_dash_segments(tmp_path):
    segment_files, dash_dir = _make_sine_dash(tmp_path)
    bodies = {
        f"https://cdn.example/{path.name}": path.read_bytes() for path in segment_files
    }
    urls = [f"https://cdn.example/{path.name}" for path in segment_files]
    downloadable = TidalDownloadable(
        session=_FakeSession(bodies),
        url=urls,
        codec="mp4a.40.2",
    )
    output = tmp_path / "track.flac"
    saved = asyncio.run(downloadable.download(output, lambda _: None))

    assert saved.suffix == ".m4a"
    assert saved.exists()
    assert _zero_crossing_rate(saved) < 0.15
    # Keep fixture directory referenced so pytest doesn't warn on unused vars.
    assert dash_dir.exists()


def test_reconstruct_dash_audio_raises_for_white_noise_output(tmp_path):
    """If the reconstructed file is white noise, raise rather than return it.

    This protects users from the original DASH-PCM bug returning as soon as
    the new reconstructor is in place: any future regression that produces
    PCM garbage would land in the user's library otherwise.
    """
    import shutil

    from streamrip.client.downloadable import reconstruct_dash_audio

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")

    # Generate white-noise .wav segments and pretend they are DASH fragments.
    segment_dir = tmp_path / "segs"
    segment_dir.mkdir()
    seg_files = []
    for i in range(3):
        seg = segment_dir / f"seg_{i}.m4s"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=white:amplitude=0.3:duration=1:sample_rate=44100",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "mp4",
                str(seg),
            ],
            check=True,
            capture_output=True,
        )
        seg_files.append(seg)

    output = tmp_path / "out.m4a"
    with pytest.raises(ValueError, match="white noise"):
        reconstruct_dash_audio([str(p) for p in seg_files], output)

    # Output must NOT have been left behind on disk
    assert not output.exists()


def test_reconstruct_dash_audio_accepts_real_music(tmp_path):
    """A valid DASH reconstruction should not be flagged as white noise."""
    import shutil

    from streamrip.client.downloadable import reconstruct_dash_audio

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")

    segment_files, _ = _make_sine_dash(tmp_path)
    output = tmp_path / "ok.m4a"
    # Should not raise
    reconstruct_dash_audio([str(p) for p in segment_files], output)
    assert output.exists()
    assert _zero_crossing_rate(output) < 0.15
