#!/usr/bin/env python3
"""YouTube Video Clipper.

Extract a time range from a YouTube video *without* downloading the whole file.

yt-dlp resolves the media stream URLs, then hands them to ffmpeg with input
seeking (``-ss``/``-t`` before ``-i``), so ffmpeg issues HTTP Range requests for
roughly the requested section instead of downloading the whole video. With
``precise=True`` (the default) the clip is re-encoded so it starts and ends on
the exact frame you asked for; ``precise=False`` stream-copies instead, which
is much faster but snaps the start to the previous keyframe.

Usage (CLI)::

    python clipper.py --url "https://youtu.be/dQw4w9WgXcQ" --start 0:43 --end 1:05 --output clip.mp4

The module is also imported by the Streamlit UI (``app.py``).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, download_range_func

__version__ = "1.0.0"

logger = logging.getLogger("clipper")

SUPPORTED_CONTAINERS = (".mp4", ".mkv", ".webm")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
# Path prefixes that are followed directly by the video ID.
_ID_PATH_PREFIXES = ("shorts", "embed", "live", "v", "e")
# JavaScript runtimes yt-dlp can use to solve YouTube's player challenges.
_JS_RUNTIMES = ("deno", "node", "bun")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ClipperError(Exception):
    """Base class for all errors with a user-presentable message."""


class InvalidURLError(ClipperError):
    """The URL is not a recognisable single YouTube video."""


class InvalidTimestampError(ClipperError):
    """A timestamp could not be parsed or the range is invalid."""


class FFmpegNotFoundError(ClipperError):
    """ffmpeg/ffprobe are not installed or not on PATH."""


class AgeRestrictedError(ClipperError):
    """The video requires a signed-in, age-verified account."""


class AccessBlockedError(ClipperError):
    """YouTube refused the request (bot check, rate limit, region block)."""


class VideoUnavailableError(ClipperError):
    """The video is private, removed, members-only or otherwise unavailable."""


class DownloadFailedError(ClipperError):
    """Any other failure while fetching or processing the clip."""


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def parse_timestamp(value: str | int | float) -> float:
    """Convert ``SS``, ``SS.mmm``, ``MM:SS`` or ``HH:MM:SS(.mmm)`` to seconds.

    Minutes and seconds after the first ``:`` must be below 60, so ``1:75`` is
    rejected rather than silently meaning 2:15. A bare number of seconds may be
    any size (``"90"`` == 1:30).
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise InvalidTimestampError(f"Invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise InvalidTimestampError(f"Timestamp must be a non-negative number, got {value!r}")
        return seconds

    text = str(value).strip()
    parts = text.split(":")
    if not text or len(parts) > 3:
        raise InvalidTimestampError(
            f"Invalid timestamp {value!r}. Use seconds (90), MM:SS (1:30) or HH:MM:SS (0:01:30)."
        )

    *head, last = parts
    if not all(re.fullmatch(r"\d+", p) for p in head) or not re.fullmatch(r"\d+(\.\d+)?", last):
        raise InvalidTimestampError(
            f"Invalid timestamp {value!r}. Use seconds (90), MM:SS (1:30) or HH:MM:SS (0:01:30)."
        )

    numbers = [int(p) for p in head] + [float(last)]
    # Every component after the leading one is a sexagesimal digit.
    if any(n >= 60 for n in numbers[1:]):
        raise InvalidTimestampError(f"Invalid timestamp {value!r}: minutes/seconds must be < 60.")

    seconds = 0.0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS`` (with ``.mmm`` when there is a fraction)."""
    millis = round(seconds * 1000)
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    base = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{base}.{ms:03d}" if ms else base


def validate_range(start: float, end: float, max_clip_seconds: float | None = None) -> None:
    """Check that ``start < end`` and, optionally, that the clip is not too long."""
    if start < 0 or end < 0:
        raise InvalidTimestampError("Timestamps cannot be negative.")
    if end <= start:
        raise InvalidTimestampError(
            f"End ({format_timestamp(end)}) must be after start ({format_timestamp(start)})."
        )
    if max_clip_seconds is not None and end - start > max_clip_seconds:
        raise InvalidTimestampError(
            f"Clip is {format_timestamp(end - start)} long; the maximum allowed is "
            f"{format_timestamp(max_clip_seconds)}."
        )


def extract_video_id(url: str) -> str:
    """Return the 11-character video ID from any common YouTube URL form.

    Accepts ``watch?v=``, ``youtu.be/``, ``/shorts/``, ``/embed/``, ``/live/``
    and mobile/music hosts. Playlists, channels and search pages are rejected.
    """
    if not url or not url.strip():
        raise InvalidURLError("Please provide a YouTube URL.")
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        raise InvalidURLError(f"Unsupported URL scheme: {parsed.scheme!r}")

    candidate: str | None = None
    segments = [s for s in parsed.path.split("/") if s]
    if host in _SHORT_HOSTS:
        candidate = segments[0] if segments else None
    elif host in _YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(segments) >= 2 and segments[0] in _ID_PATH_PREFIXES:
            candidate = segments[1]
    else:
        raise InvalidURLError(f"Not a YouTube URL: {url!r}")

    if not candidate or not _VIDEO_ID_RE.fullmatch(candidate):
        raise InvalidURLError(
            f"Could not find a video ID in {url!r}. Paste a link to a single video "
            "(playlists and channels are not supported)."
        )
    return candidate


def canonical_url(video_id: str) -> str:
    """Canonical watch URL; drops playlist/timestamp params so only one video is fetched."""
    return f"https://www.youtube.com/watch?v={video_id}"


def check_ffmpeg(ffmpeg_location: str | None = None) -> str:
    """Ensure ffmpeg and ffprobe are available. Returns the ffmpeg path."""
    if ffmpeg_location:
        loc = Path(ffmpeg_location)
        search_dir = str(loc if loc.is_dir() else loc.parent)
        ffmpeg = shutil.which("ffmpeg", path=search_dir)
        ffprobe = shutil.which("ffprobe", path=search_dir)
    else:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")

    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path]
    if missing:
        raise FFmpegNotFoundError(
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg "
            "(Linux: 'sudo apt install ffmpeg', macOS: 'brew install ffmpeg', "
            "Windows: 'winget install Gyan.FFmpeg') and restart your terminal."
        )
    return ffmpeg  # type: ignore[return-value]


def detect_js_runtimes() -> dict[str, dict]:
    """Return the JS runtimes found, in yt-dlp's ``js_runtimes`` format.

    Recent yt-dlp versions need a JavaScript runtime (deno >= 2.3 recommended;
    node >= 22 or bun also work) to solve YouTube's player challenges; without
    one, many formats are missing or downloads fail. Besides PATH, this checks
    the Python environment's scripts directory, where ``pip install deno`` puts
    the binary (that directory is not always on PATH, e.g. on Streamlit Cloud).
    """
    search_path = os.pathsep.join(
        filter(
            None,
            [
                sysconfig.get_path("scripts"),
                str(Path(sys.executable).parent),
                os.environ.get("PATH", ""),
            ],
        )
    )
    found: dict[str, dict] = {}
    for name in _JS_RUNTIMES:
        if path := shutil.which(name, path=search_path):
            found[name] = {"path": path}
    return found


# --------------------------------------------------------------------------- #
# yt-dlp integration
# --------------------------------------------------------------------------- #
def build_format_selector(container: str = ".mp4", max_height: int | None = None) -> str:
    """yt-dlp format string preferring streams that need no transcoding to ``container``."""
    h = f"[height<={int(max_height)}]" if max_height else ""
    if container == ".mp4":
        # H.264 first: it previews in every browser (the Streamlit player included).
        return (
            f"bv*{h}[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*{h}[ext=mp4]+ba[ext=m4a]/"
            f"b{h}[ext=mp4]/bv*{h}+ba/b{h}/b"
        )
    if container == ".webm":
        return f"bv*{h}[ext=webm]+ba[ext=webm]/b{h}[ext=webm]/bv*{h}+ba/b{h}/b"
    return f"bv*{h}+ba/b{h}/b"


class _YtDlpLogger:
    """Route yt-dlp output into :mod:`logging` instead of stdout/stderr."""

    def debug(self, msg: str) -> None:
        logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        logger.warning(msg)

    def error(self, msg: str) -> None:
        # Re-raised as a ClipperError; log at debug level to avoid printing it twice.
        logger.debug(msg)


# Substrings of yt-dlp error messages, checked in this order ("Sign in to confirm
# your age" must win over the generic "Sign in to confirm you're not a bot").
_AGE_MARKERS = ("confirm your age", "age-restricted", "age restricted", "inappropriate for some")
_BLOCKED_MARKERS = ("not a bot", "sign in to confirm", "http error 429", "too many requests")
_UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "has been removed",
    "members-only",
    "join this channel",
    "not available in your country",
    "this video is not available",
    "premieres in",
)
_NETWORK_MARKERS = (
    "unable to connect",
    "connection refused",
    "connection reset",
    "timed out",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo failed",
)


def map_download_error(exc: BaseException) -> ClipperError:
    """Translate a yt-dlp error into a specific, actionable :class:`ClipperError`."""
    msg = re.sub(r"^(ERROR:\s*)+", "", str(exc))
    msg = re.split(r";\s*please report this issue", msg, flags=re.IGNORECASE)[0].strip()
    low = msg.lower()

    if any(k in low for k in _AGE_MARKERS):
        return AgeRestrictedError(
            "This video is age-restricted and needs a signed-in, age-verified account. "
            "From the CLI you can pass --cookies with a cookies.txt exported from your browser."
        )
    if any(k in low for k in _BLOCKED_MARKERS):
        return AccessBlockedError(
            "YouTube blocked this request (bot check or rate limit). This is common on cloud/"
            "datacenter IPs. Try again later, run locally, or use --cookies from the CLI."
        )
    if any(k in low for k in _UNAVAILABLE_MARKERS):
        return VideoUnavailableError(f"Video unavailable: {msg}")
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return FFmpegNotFoundError(msg)
    if any(k in low for k in _NETWORK_MARKERS):
        return DownloadFailedError(
            "Network error: could not reach YouTube. Check your connection or proxy settings."
        )
    if "cannot be partially downloaded" in low:
        return DownloadFailedError(
            "The selected format cannot be section-downloaded. Try a different quality setting."
        )
    return DownloadFailedError(f"Download failed: {msg}")


@dataclass(frozen=True)
class ClipResult:
    path: Path
    title: str
    source_url: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def download_section(
    source_url: str,
    start: float,
    end: float,
    output: Path,
    *,
    precise: bool = True,
    max_height: int | None = None,
    cookies_file: str | None = None,
    ffmpeg_location: str | None = None,
    overwrite: bool = False,
    status: Callable[[str], None] | None = None,
) -> ClipResult:
    """Download only ``[start, end)`` of ``source_url`` into ``output``.

    Low-level: performs no URL validation (``clip_video`` does). All
    intermediate files live in a private temporary directory that is removed on
    success, failure or interruption; only the finished clip is moved to
    ``output``.
    """
    notify = status or (lambda _msg: None)
    output = Path(output).expanduser().resolve()
    container = output.suffix.lower()
    if container not in SUPPORTED_CONTAINERS:
        raise ClipperError(
            f"Unsupported output extension {output.suffix!r}; use one of "
            f"{', '.join(SUPPORTED_CONTAINERS)}."
        )
    if output.exists() and not overwrite:
        raise ClipperError(f"{output} already exists (use --overwrite to replace it).")
    validate_range(start, end)
    check_ffmpeg(ffmpeg_location)

    with tempfile.TemporaryDirectory(prefix="clipper-") as tmp:
        tmp_dir = Path(tmp)
        opts: dict = {
            "format": build_format_selector(container, max_height),
            "merge_output_format": container.lstrip("."),
            # Remux single-file fallbacks so the extension always matches.
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": container.lstrip(".")}
            ],
            "outtmpl": str(tmp_dir / "clip.%(ext)s"),
            "paths": {"home": str(tmp_dir), "temp": str(tmp_dir)},
            # The key part: only this time range is fetched.
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": precise,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": _YtDlpLogger(),
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "overwrites": True,
        }
        if runtimes := detect_js_runtimes():
            opts["js_runtimes"] = runtimes
        else:
            logger.warning(
                "No JavaScript runtime (deno/node/bun) found; YouTube downloads may fail. "
                "Run 'pip install deno' (see README)."
            )
        if cookies_file:
            opts["cookiefile"] = str(Path(cookies_file).expanduser())
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                notify("Fetching video information…")
                info = ydl.extract_info(source_url, download=False)
                if info is None:
                    raise DownloadFailedError("yt-dlp returned no information for this URL.")
                if info.get("_type") == "playlist":
                    raise InvalidURLError("Playlists are not supported; paste a single video URL.")
                if info.get("is_live"):
                    raise ClipperError("Live streams cannot be clipped until the broadcast ends.")

                duration = info.get("duration")
                if duration and end > duration:
                    raise InvalidTimestampError(
                        f"End ({format_timestamp(end)}) is past the end of the video "
                        f"({format_timestamp(duration)})."
                    )

                notify(f"Downloading {format_timestamp(start)} → {format_timestamp(end)}…")
                ydl.process_ie_result(info, download=True)
        except ClipperError:
            raise
        except DownloadError as exc:
            raise map_download_error(exc) from exc
        except OSError as exc:
            raise DownloadFailedError(f"File system error: {exc}") from exc

        produced = _find_output(tmp_dir, container)
        notify("Finalising…")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(output))

    logger.info("Saved clip to %s", output)
    return ClipResult(
        path=output,
        title=str(info.get("title") or info.get("id") or "clip"),
        source_url=source_url,
        start=start,
        end=end,
    )


def _find_output(tmp_dir: Path, container: str) -> Path:
    """Locate the finished file yt-dlp wrote (ignoring partial/fragment files)."""
    files = [
        p
        for p in tmp_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp")) and p.stat().st_size
    ]
    preferred = [p for p in files if p.suffix.lower() == container]
    candidates = preferred or files
    if not candidates:
        raise DownloadFailedError("ffmpeg finished but produced no output file.")
    return max(candidates, key=lambda p: p.stat().st_size)


def clip_video(
    url: str,
    start: str | float,
    end: str | float,
    output: str | Path | None = None,
    *,
    precise: bool = True,
    max_height: int | None = None,
    cookies_file: str | None = None,
    ffmpeg_location: str | None = None,
    overwrite: bool = False,
    max_clip_seconds: float | None = None,
    status: Callable[[str], None] | None = None,
) -> ClipResult:
    """Validate inputs and save the ``start``–``end`` section of a YouTube video.

    ``start``/``end`` accept seconds or ``[HH:]MM:SS[.mmm]`` strings. When
    ``output`` is omitted the clip is written to the current directory as
    ``<video_id>_<start>-<end>.mp4``.
    """
    video_id = extract_video_id(url)
    start_s, end_s = parse_timestamp(start), parse_timestamp(end)
    validate_range(start_s, end_s, max_clip_seconds)

    if output is None:
        stamp = f"{format_timestamp(start_s)}-{format_timestamp(end_s)}".replace(":", "-")
        output = Path.cwd() / f"{video_id}_{stamp}.mp4"

    return download_section(
        canonical_url(video_id),
        start_s,
        end_s,
        Path(output),
        precise=precise,
        max_height=max_height,
        cookies_file=cookies_file,
        ffmpeg_location=ffmpeg_location,
        overwrite=overwrite,
        status=status,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipper",
        description="Download only a specific section of a YouTube video.",
        epilog=(
            "Timestamps accept seconds (90, 12.5), MM:SS (1:30) or HH:MM:SS(.mmm) (01:02:03.5).\n"
            "Example: clipper.py --url https://youtu.be/dQw4w9WgXcQ --start 0:43 --end 1:05 "
            "--output chorus.mp4"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="YouTube video URL")
    parser.add_argument("--start", required=True, help="clip start time")
    parser.add_argument("--end", required=True, help="clip end time")
    parser.add_argument(
        "--output",
        "-o",
        help="output file (.mp4, .mkv or .webm). Default: <video_id>_<start>-<end>.mp4",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="stream-copy without re-encoding (much faster, but the start snaps to the "
        "previous keyframe, so the clip may begin a few seconds early)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        metavar="PX",
        help="limit video resolution, e.g. 720 or 1080",
    )
    parser.add_argument(
        "--cookies",
        metavar="FILE",
        help="Netscape cookies.txt for age-restricted/members videos. Keep this file private.",
    )
    parser.add_argument("--ffmpeg-location", metavar="PATH", help="ffmpeg binary or directory")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    parser.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp debug output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s" if args.verbose else "%(message)s",
    )

    try:
        result = clip_video(
            args.url,
            args.start,
            args.end,
            args.output,
            precise=not args.fast,
            max_height=args.max_height,
            cookies_file=args.cookies,
            ffmpeg_location=args.ffmpeg_location,
            overwrite=args.overwrite,
            status=lambda m: logger.info(m),
        )
    except ClipperError as exc:
        logger.error("Error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted; temporary files were removed.")
        return 130

    size_mb = result.path.stat().st_size / 1_048_576
    logger.info(
        "Done: %r [%s → %s] → %s (%.1f MB)",
        result.title,
        format_timestamp(result.start),
        format_timestamp(result.end),
        result.path,
        size_mb,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
