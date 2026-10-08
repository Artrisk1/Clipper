"""Tests for clipper.py. Network-free; ffmpeg integration tests skip when ffmpeg is absent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

import clipper

# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("90", 90.0),
        ("12.5", 12.5),
        ("1:30", 90.0),
        ("01:30", 90.0),
        ("0:01:30", 90.0),
        ("01:02:03", 3723.0),
        ("01:02:03.250", 3723.25),
        ("  1:05 ", 65.0),
        ("120:00", 7200.0),  # leading component may exceed 59
        (45, 45.0),
        (7.5, 7.5),
    ],
)
def test_parse_timestamp_valid(value, expected):
    assert clipper.parse_timestamp(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "abc",
        "1:75",
        "1:60:00",
        "-5",
        "1::2",
        "1:2:3:4",
        "1.5:30",
        "nan",
        "inf",
        -1,
        float("nan"),
        float("inf"),
        True,
        "1,5",
    ],
)
def test_parse_timestamp_invalid(value):
    with pytest.raises(clipper.InvalidTimestampError):
        clipper.parse_timestamp(value)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00"), (90, "00:01:30"), (3723.25, "01:02:03.250"), (59.9999, "00:01:00")],
)
def test_format_timestamp(seconds, expected):
    assert clipper.format_timestamp(seconds) == expected


def test_validate_range():
    clipper.validate_range(0, 10)
    with pytest.raises(clipper.InvalidTimestampError, match="after start"):
        clipper.validate_range(10, 10)
    with pytest.raises(clipper.InvalidTimestampError, match="after start"):
        clipper.validate_range(20, 10)
    with pytest.raises(clipper.InvalidTimestampError, match="maximum"):
        clipper.validate_range(0, 601, max_clip_seconds=600)


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&t=42s",
        f"https://www.youtube.com/watch?v={VID}&list=PL123&index=2",
        f"http://m.youtube.com/watch?feature=share&v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abc&t=10",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=share",
        f"www.youtube.com/watch?v={VID}",
        f"youtu.be/{VID}",
    ],
)
def test_extract_video_id_valid(url):
    assert clipper.extract_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://vimeo.com/123456",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch",
        "https://youtu.be/",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://evil-youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_extract_video_id_invalid(url):
    with pytest.raises(clipper.InvalidURLError):
        clipper.extract_video_id(url)


def test_canonical_url_strips_extras():
    assert clipper.canonical_url(VID) == f"https://www.youtube.com/watch?v={VID}"


# --------------------------------------------------------------------------- #
# Dependencies & error mapping
# --------------------------------------------------------------------------- #


def test_check_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(clipper.shutil, "which", lambda *a, **k: None)
    with pytest.raises(clipper.FFmpegNotFoundError, match="ffmpeg and ffprobe"):
        clipper.check_ffmpeg()


def test_check_ffmpeg_present(monkeypatch):
    monkeypatch.setattr(clipper.shutil, "which", lambda name, **k: f"/usr/bin/{name}")
    assert clipper.check_ffmpeg() == "/usr/bin/ffmpeg"


def test_detect_js_runtimes(monkeypatch):
    def fake_which(name, path=None):
        assert path  # must search beyond the default PATH
        return "/venv/bin/node" if name == "node" else None

    monkeypatch.setattr(clipper.shutil, "which", fake_which)
    assert clipper.detect_js_runtimes() == {"node": {"path": "/venv/bin/node"}}


@pytest.mark.parametrize(
    ("message", "exc_type"),
    [
        (
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate",
            clipper.AgeRestrictedError,
        ),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", clipper.AccessBlockedError),
        (
            "ERROR: unable to download: HTTP Error 429: Too Many Requests",
            clipper.AccessBlockedError,
        ),
        (
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
            clipper.VideoUnavailableError,
        ),
        ("ERROR: [youtube] x: Video unavailable", clipper.VideoUnavailableError),
        ("ERROR: ffprobe and ffmpeg not found. Please install", clipper.FFmpegNotFoundError),
        (
            "ERROR: Unable to download API page: ('Unable to connect to proxy', ...)",
            clipper.DownloadFailedError,
        ),
        ("ERROR: something unexpected", clipper.DownloadFailedError),
    ],
)
def test_map_download_error(message, exc_type):
    err = clipper.map_download_error(DownloadError(message))
    assert isinstance(err, exc_type)
    assert not str(err).startswith("ERROR:")
    assert "please report" not in str(err).lower()


def test_map_download_error_network_message():
    err = clipper.map_download_error(
        DownloadError("ERROR: x: ('Unable to connect to proxy'); please report this issue on ...")
    )
    assert str(err).startswith("Network error")


def test_build_format_selector():
    sel = clipper.build_format_selector(".mp4", 720)
    assert sel.startswith("bv*[height<=720][ext=mp4][vcodec^=avc1]+ba[ext=m4a]")
    assert sel.endswith("/b")
    assert "[height" not in clipper.build_format_selector(".mkv")


# --------------------------------------------------------------------------- #
# clip_video with a fake yt-dlp (verifies options + cleanup, no network)
# --------------------------------------------------------------------------- #


class FakeYDL:
    instances: list[FakeYDL] = []
    info: dict = {"id": VID, "title": "Test video", "duration": 300}
    fail_with: Exception | None = None

    def __init__(self, params):
        self.params = params
        self.tmp_dir = Path(params["paths"]["home"])
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.url = url
        if FakeYDL.fail_with:
            raise FakeYDL.fail_with
        return dict(FakeYDL.info)

    def process_ie_result(self, info, download=True):
        (self.tmp_dir / "clip.mp4").write_bytes(b"fake-video")
        (self.tmp_dir / "clip.f137.mp4.part").write_bytes(b"partial")
        return info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.instances = []
    FakeYDL.info = {"id": VID, "title": "Test video", "duration": 300}
    FakeYDL.fail_with = None
    monkeypatch.setattr(clipper.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(clipper, "check_ffmpeg", lambda *_: "/usr/bin/ffmpeg")
    return FakeYDL


def test_clip_video_downloads_only_section(fake_ydl, tmp_path):
    out = tmp_path / "sub" / "clip.mp4"
    result = clipper.clip_video(f"https://youtu.be/{VID}?t=5&list=PLx", "0:05", "10.5", out)

    assert result.path == out.resolve()
    assert out.read_bytes() == b"fake-video"
    assert result.title == "Test video"
    assert result.duration == pytest.approx(5.5)

    ydl = fake_ydl.instances[0]
    assert ydl.url == f"https://www.youtube.com/watch?v={VID}"
    params = ydl.params
    ranges = list(params["download_ranges"]({}, None))
    assert ranges == [{"start_time": 5.0, "end_time": 10.5}]
    assert params["force_keyframes_at_cuts"] is True
    assert params["noplaylist"] is True
    assert params["merge_output_format"] == "mp4"
    # Temporary directory (with its .part leftovers) is gone.
    assert not ydl.tmp_dir.exists()


def test_clip_video_fast_mode(fake_ydl, tmp_path):
    clipper.clip_video(f"https://youtu.be/{VID}", 0, 3, tmp_path / "c.mkv", precise=False)
    params = fake_ydl.instances[0].params
    assert params["force_keyframes_at_cuts"] is False
    assert params["merge_output_format"] == "mkv"


def test_clip_video_default_output_name(fake_ydl, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = clipper.clip_video(f"https://youtu.be/{VID}", "1:00", "1:30")
    assert result.path.name == f"{VID}_00-01-00-00-01-30.mp4"


def test_clip_video_end_past_duration(fake_ydl, tmp_path):
    fake_ydl.info = {"id": VID, "title": "Short", "duration": 60}
    with pytest.raises(clipper.InvalidTimestampError, match="past the end"):
        clipper.clip_video(f"https://youtu.be/{VID}", "0:30", "2:00", tmp_path / "c.mp4")
    assert not (tmp_path / "c.mp4").exists()
    assert not fake_ydl.instances[0].tmp_dir.exists()


def test_clip_video_rejects_live(fake_ydl, tmp_path):
    fake_ydl.info = {"id": VID, "title": "Live", "is_live": True}
    with pytest.raises(clipper.ClipperError, match="Live"):
        clipper.clip_video(f"https://youtu.be/{VID}", 0, 10, tmp_path / "c.mp4")


def test_clip_video_maps_age_restriction(fake_ydl, tmp_path):
    fake_ydl.fail_with = DownloadError("ERROR: [youtube] x: Sign in to confirm your age")
    with pytest.raises(clipper.AgeRestrictedError):
        clipper.clip_video(f"https://youtu.be/{VID}", 0, 10, tmp_path / "c.mp4")
    assert not fake_ydl.instances[0].tmp_dir.exists()


def test_clip_video_refuses_overwrite(fake_ydl, tmp_path):
    out = tmp_path / "c.mp4"
    out.write_bytes(b"keep me")
    with pytest.raises(clipper.ClipperError, match="already exists"):
        clipper.clip_video(f"https://youtu.be/{VID}", 0, 10, out)
    assert out.read_bytes() == b"keep me"
    clipper.clip_video(f"https://youtu.be/{VID}", 0, 10, out, overwrite=True)
    assert out.read_bytes() == b"fake-video"


def test_clip_video_rejects_bad_extension(fake_ydl, tmp_path):
    with pytest.raises(clipper.ClipperError, match="Unsupported output extension"):
        clipper.clip_video(f"https://youtu.be/{VID}", 0, 10, tmp_path / "c.avi")


def test_clip_video_validates_before_network(fake_ydl, tmp_path):
    with pytest.raises(clipper.InvalidURLError):
        clipper.clip_video("https://vimeo.com/1", 0, 10, tmp_path / "c.mp4")
    with pytest.raises(clipper.InvalidTimestampError):
        clipper.clip_video(f"https://youtu.be/{VID}", "1:00", "0:30", tmp_path / "c.mp4")
    assert fake_ydl.instances == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_success(fake_ydl, tmp_path, caplog):
    out = tmp_path / "cli.mp4"
    code = clipper.main(
        ["--url", f"https://youtu.be/{VID}", "--start", "5", "--end", "0:10", "--output", str(out)]
    )
    assert code == 0
    assert out.exists()


def test_cli_error_exit_code(fake_ydl, tmp_path):
    code = clipper.main(
        [
            "--url",
            f"https://youtu.be/{VID}",
            "--start",
            "abc",
            "--end",
            "10",
            "-o",
            str(tmp_path / "x.mp4"),
        ]
    )
    assert code == 1


def test_cli_requires_arguments():
    with pytest.raises(SystemExit) as exc:
        clipper.main(["--url", f"https://youtu.be/{VID}"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# Integration: real yt-dlp + ffmpeg section download from a local HTTP server
# --------------------------------------------------------------------------- #

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)


class _RangeHandler(SimpleHTTPRequestHandler):
    """Static file handler with HTTP Range support (ffmpeg needs it to seek)."""

    def log_message(self, *args):
        pass

    def send_head(self):
        range_header = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not range_header or not os.path.isfile(path):
            self._remaining = None
            return super().send_head()
        size = os.path.getsize(path)
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        start = int(match.group(1) or 0)
        end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
        f = open(path, "rb")  # noqa: SIM115 - closed by the base handler
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0 and (chunk := source.read(min(65536, remaining))):
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg closes the connection once it has the bytes it needs


@pytest.fixture
def local_video_server(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=20:size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=20",
            "-c:v",
            "libx264",
            "-g",
            "250",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(src_dir / "source.mp4"),
        ],
        check=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_RangeHandler, directory=src_dir))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/source.mp4", src_dir / "source.mp4"
    server.shutdown()
    server.server_close()


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


@needs_ffmpeg
def test_integration_precise_section(local_video_server, tmp_path):
    url, source = local_video_server
    out = tmp_path / "out" / "clip.mp4"
    result = clipper.download_section(url, 5.0, 9.5, out, precise=True)
    assert result.path == out.resolve()
    assert _probe_duration(out) == pytest.approx(4.5, abs=0.15)
    # Only the clip remains; no leftover temp files beside it.
    assert [p.name for p in out.parent.iterdir()] == ["clip.mp4"]
    assert out.stat().st_size < source.stat().st_size


@needs_ffmpeg
def test_integration_fast_section(local_video_server, tmp_path):
    url, _ = local_video_server
    out = tmp_path / "fast.mkv"
    clipper.download_section(url, 5.0, 9.5, out, precise=False)
    assert out.exists() and _probe_duration(out) > 0


# --------------------------------------------------------------------------- #
# Optional live test against YouTube (opt-in: CLIPPER_NETWORK_TESTS=1)
# --------------------------------------------------------------------------- #


@needs_ffmpeg
@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("CLIPPER_NETWORK_TESTS") != "1",
    reason="set CLIPPER_NETWORK_TESTS=1 to hit YouTube",
)
def test_network_youtube_clip(tmp_path):
    # "Me at the zoo" - the first YouTube video, 19 s long.
    result = clipper.clip_video("https://youtu.be/jNQXAC9IVRw", 3, 8, tmp_path / "zoo.mp4")
    assert _probe_duration(result.path) == pytest.approx(5, abs=0.5)
