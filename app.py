"""Streamlit web UI for the YouTube clipper.

Run locally with:  streamlit run app.py

All sessions share one :class:`service.ClipService` (queue, cache, rate limits);
see service.py for how that keeps a single small server usable by many people.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import streamlit as st

import clipper
from service import CachedClip, ClipService, ServiceConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipper.app")

QUALITY_OPTIONS = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

st.set_page_config(page_title="YouTube Clipper", page_icon="🎬", layout="centered")


@st.cache_resource
def get_service() -> ClipService:
    """One service per server process, shared by every user session."""
    return ClipService(ServiceConfig.from_env())


@st.cache_data(show_spinner=False)
def ffmpeg_status() -> str | None:
    """Return an error message if ffmpeg is missing, else None (checked once per server)."""
    try:
        clipper.check_ffmpeg()
    except clipper.FFmpegNotFoundError as exc:
        return str(exc)
    return None


def user_id() -> str:
    """A random per-browser-session ID used for per-user rate limiting."""
    if "user_id" not in st.session_state:
        st.session_state.user_id = uuid.uuid4().hex
    return st.session_state.user_id


def safe_filename(title: str, start: float, end: float) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60] or "clip"
    stamp = f"{clipper.format_timestamp(start)}-{clipper.format_timestamp(end)}".replace(":", "-")
    return f"{stem}_{stamp}.mp4"


def run_clip(
    service: ClipService, url: str, start: str, end: str, max_height: int, precise: bool
) -> None:
    with st.status("Clipping video…", expanded=True) as status:
        message = st.empty()  # one line that updates in place (queue position etc.)
        try:
            clip = service.get_clip(
                url,
                start,
                end,
                user_id=user_id(),
                max_height=max_height,
                precise=precise,
                status=message.write,
            )
        except clipper.ClipperError as exc:
            status.update(label="Could not create the clip", state="error", expanded=False)
            st.session_state.error = str(exc)
            return
        except Exception:  # noqa: BLE001 - last-resort guard so the UI never shows a raw trace
            log.exception("Unexpected error while clipping %s", url)
            status.update(label="Unexpected error", state="error", expanded=False)
            st.session_state.error = "An unexpected error occurred. Please try again later."
            return
        label = "Clip ready! (served from cache ⚡)" if clip.from_cache else "Clip ready!"
        status.update(label=label, state="complete", expanded=False)
        # Only the path is stored per session; the bytes stay on disk in the shared cache.
        st.session_state.clip = clip


def show_clip(clip: CachedClip) -> None:
    path = Path(clip.path)
    if not path.exists():
        st.info("This clip has expired from the server cache. Please create it again.")
        st.session_state.pop("clip", None)
        return
    st.subheader(clip.title)
    st.caption(
        f"{clipper.format_timestamp(clip.start)} → {clipper.format_timestamp(clip.end)}"
        f" · {clip.end - clip.start:.1f} s · {clip.size_mb:.1f} MB"
    )
    # Streamlit de-duplicates identical media, so many viewers of one cached clip
    # share a single in-memory copy.
    st.video(str(path), format="video/mp4")
    col_dl, col_clear = st.columns([3, 1])
    col_dl.download_button(
        "⬇️ Download MP4",
        # Deferred: the file is read only when the button is clicked.
        data=path.read_bytes,
        file_name=safe_filename(clip.title, clip.start, clip.end),
        mime="video/mp4",
        type="primary",
        on_click="ignore",
        width="stretch",
    )
    if col_clear.button("Clear", width="stretch"):
        st.session_state.pop("clip", None)
        st.rerun()


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
st.title("🎬 YouTube Clipper")
st.caption(
    "Paste a YouTube link, choose start and end times, and download just that part. "
    "Only the selected section is fetched, not the whole video."
)

if missing := ffmpeg_status():
    st.error(f"Server configuration problem: {missing}")
    st.stop()

service = get_service()
config = service.config

with st.form("clip_form"):
    url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=…")
    col_start, col_end = st.columns(2)
    start_text = col_start.text_input("Start", value="0:00", help="Seconds, MM:SS or HH:MM:SS")
    end_text = col_end.text_input("End", value="0:30", help="Seconds, MM:SS or HH:MM:SS")
    col_q, col_p = st.columns(2)
    quality = col_q.selectbox("Max quality", list(QUALITY_OPTIONS), index=1)
    precise = col_p.toggle(
        "Frame-accurate cut",
        value=True,
        help="Re-encodes the clip so it starts exactly on your timestamp. Turn off for a "
        "faster cut that may start a few seconds early (nearest keyframe).",
    )
    submitted = st.form_submit_button("✂️ Create clip", type="primary", width="stretch")

if submitted:
    st.session_state.pop("clip", None)
    st.session_state.pop("error", None)
    run_clip(service, url, start_text, end_text, QUALITY_OPTIONS[quality], precise)

if error := st.session_state.get("error"):
    st.error(error)

if clip := st.session_state.get("clip"):
    show_clip(clip)

st.divider()
with st.expander("Server status"):
    stats = service.stats()
    c1, c2, c3 = st.columns(3)
    c1.metric("Jobs running", f"{stats['active']} / {stats['max_concurrent']}")
    c2.metric("Waiting in queue", stats["waiting"])
    c3.metric(
        "YouTube fetches (1 h)",
        f"{stats['youtube_fetches_last_hour']} / {stats['youtube_fetch_budget']}",
    )
    st.caption(f"Cache: {stats['cache']['entries']} clips, {stats['cache']['mb']:.0f} MB")
st.caption(
    f"Limits: clips up to {clipper.format_timestamp(config.max_clip_seconds)} and "
    f"{config.max_file_mb:.0f} MB; {config.user_clips_per_hour} new clips per user per hour. "
    "Only clip videos you have the right to download."
)
