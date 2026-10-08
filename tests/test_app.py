"""Smoke tests for the Streamlit UI using Streamlit's AppTest harness (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import clipper

APP = str(Path(__file__).resolve().parent.parent / "app.py")
URL = "https://youtu.be/dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _isolated_service(monkeypatch, tmp_path):
    """Fresh shared service per test, with its cache in tmp and no real downloads."""
    monkeypatch.setenv("CLIPPER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(clipper, "check_ffmpeg", lambda *_: "/usr/bin/ffmpeg")
    calls = []

    def fake_download(source_url, start, end, output, **kwargs):
        calls.append((source_url, start, end))
        Path(output).write_bytes(b"\x00" * 1024)
        return clipper.ClipResult(Path(output), "My: Video?", source_url, start, end)

    monkeypatch.setattr(clipper, "download_section", fake_download)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield calls
    st.cache_resource.clear()


def _submit(at: AppTest, url: str, start: str, end: str) -> AppTest:
    at.text_input[0].set_value(url)
    at.text_input[1].set_value(start)
    at.text_input[2].set_value(end)
    at.button[0].click()
    return at.run(timeout=30)


def test_renders_form():
    at = AppTest.from_file(APP).run(timeout=30)
    assert not at.exception
    assert at.title[0].value.endswith("YouTube Clipper")
    assert len(at.text_input) == 3


def test_invalid_timestamp_shows_error(_isolated_service):
    at = _submit(AppTest.from_file(APP).run(timeout=30), URL, "1:00", "0:30")
    assert not at.exception
    assert "must be after start" in at.error[0].value
    assert _isolated_service == []


def test_invalid_url_shows_error():
    at = _submit(AppTest.from_file(APP).run(timeout=30), "https://vimeo.com/1", "0", "10")
    assert "Not a YouTube URL" in at.error[0].value


def test_successful_clip_is_cached_and_shared(_isolated_service):
    at = _submit(AppTest.from_file(APP).run(timeout=30), URL, "0:05", "0:10")
    assert not at.exception
    assert not at.error
    clip = at.session_state["clip"]
    assert clip.path.read_bytes() == b"\x00" * 1024
    assert not clip.from_cache
    assert "data" not in dir(clip)  # bytes are not held in session state

    # A second user asking for the same clip gets it from the cache: no download.
    other = _submit(AppTest.from_file(APP).run(timeout=30), URL, "5", "10")
    assert other.session_state["clip"].from_cache
    assert len(_isolated_service) == 1


def test_clipper_error_is_shown(monkeypatch):
    def fail(*args, **kwargs):
        raise clipper.AgeRestrictedError("This video is age-restricted")

    monkeypatch.setattr(clipper, "download_section", fail)
    at = _submit(AppTest.from_file(APP).run(timeout=30), URL, "0", "10")
    assert "age-restricted" in at.error[0].value


def test_per_user_rate_limit(monkeypatch):
    monkeypatch.setenv("CLIPPER_USER_CLIPS_PER_HOUR", "1")
    at = _submit(AppTest.from_file(APP).run(timeout=30), URL, "0", "10")
    assert not at.error
    at = _submit(at, URL, "20", "30")
    assert "limit of 1 new clips per hour" in at.error[0].value
