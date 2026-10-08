"""Streamlit web UI for the YouTube clipper.

Run locally with:  streamlit run app.py
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import streamlit as st

import clipper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipper.app")

# Limits protect the (small) server from memory exhaustion: the finished clip is
# held in memory so it can be previewed and downloaded. Override with env vars.
MAX_CLIP_SECONDS = float(os.environ.get("CLIPPER_MAX_CLIP_SECONDS", 600))
MAX_FILE_MB = float(os.environ.get("CLIPPER_MAX_FILE_MB", 200))

QUALITY_OPTIONS = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

st.set_page_config(page_title="YouTube Clipper", page_icon="🎬", layout="centered")


@st.cache_data(show_spinner=False)
def ffmpeg_status() -> str | None:
    """Return an error message if ffmpeg is missing, else None (checked once per server)."""
    try:
        clipper.check_ffmpeg()
    except clipper.FFmpegNotFoundError as exc:
        return str(exc)
    return None


def safe_filename(title: str, start: float, end: float) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60] or "clip"
    stamp = f"{clipper.format_timestamp(start)}-{clipper.format_timestamp(end)}".replace(":", "-")
    return f"{stem}_{stamp}.mp4"


def run_clip(url: str, start_s: float, end_s: float, max_height: int, precise: bool) -> None:
    """Clip into a temp dir, load the bytes into session state, and delete the file."""
    with (
        st.status("Clipping video…", expanded=True) as status,
        tempfile.TemporaryDirectory(prefix="clipper-app-") as tmp,
    ):
        try:
            result = clipper.clip_video(
                url,
                start_s,
                end_s,
                Path(tmp) / "clip.mp4",
                precise=precise,
                max_height=max_height,
                max_clip_seconds=MAX_CLIP_SECONDS,
                status=st.write,
            )
            size_mb = result.path.stat().st_size / 1_048_576
            if size_mb > MAX_FILE_MB:
                raise clipper.ClipperError(
                    f"The clip is {size_mb:.0f} MB, above this server's {MAX_FILE_MB:.0f} MB "
                    "limit. Choose a shorter range or lower quality."
                )
            st.session_state.clip = {
                "data": result.path.read_bytes(),
                "filename": safe_filename(result.title, result.start, result.end),
                "title": result.title,
                "start": result.start,
                "end": result.end,
                "size_mb": size_mb,
            }
            status.update(label="Clip ready!", state="complete", expanded=False)
        except clipper.ClipperError as exc:
            status.update(label="Could not create the clip", state="error", expanded=False)
            st.session_state.error = str(exc)
        except Exception:  # noqa: BLE001 - last-resort guard so the UI never shows a raw trace
            log.exception("Unexpected error while clipping %s", url)
            status.update(label="Unexpected error", state="error", expanded=False)
            st.session_state.error = "An unexpected error occurred. Please try again later."
    # Leaving the `with` block deletes the temporary directory and its files.


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
    try:
        clipper.extract_video_id(url)
        start_s = clipper.parse_timestamp(start_text)
        end_s = clipper.parse_timestamp(end_text)
        clipper.validate_range(start_s, end_s, MAX_CLIP_SECONDS)
    except clipper.ClipperError as exc:
        st.session_state.error = str(exc)
    else:
        run_clip(url, start_s, end_s, QUALITY_OPTIONS[quality], precise)

if error := st.session_state.get("error"):
    st.error(error)

if clip := st.session_state.get("clip"):
    st.subheader(clip["title"])
    st.caption(
        f"{clipper.format_timestamp(clip['start'])} → {clipper.format_timestamp(clip['end'])}"
        f" · {clip['end'] - clip['start']:.1f} s · {clip['size_mb']:.1f} MB"
    )
    st.video(clip["data"], format="video/mp4")
    col_dl, col_clear = st.columns([3, 1])
    col_dl.download_button(
        "⬇️ Download MP4",
        data=clip["data"],
        file_name=clip["filename"],
        mime="video/mp4",
        type="primary",
        width="stretch",
    )
    if col_clear.button("Clear", width="stretch"):
        # Drop the clip bytes from server memory.
        st.session_state.pop("clip", None)
        st.rerun()

st.divider()
st.caption(
    f"Limits on this server: clips up to {clipper.format_timestamp(MAX_CLIP_SECONDS)} and "
    f"{MAX_FILE_MB:.0f} MB. Only clip videos you have the right to download."
)
