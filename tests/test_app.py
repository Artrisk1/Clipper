"""Smoke tests for the Streamlit UI using Streamlit's AppTest harness (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import clipper

APP = str(Path(__file__).resolve().parent.parent / "app.py")
URL = "https://youtu.be/dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _ffmpeg_ok(monkeypatch):
    monkeypatch.setattr(clipper, "check_ffmpeg", lambda *_: "/usr/bin/ffmpeg")


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


def test_invalid_timestamp_shows_error():
    at = AppTest.from_file(APP).run(timeout=30)
    at = _submit(at, URL, "1:00", "0:30")
    assert not at.exception
    assert "must be after start" in at.error[0].value


def test_invalid_url_shows_error():
    at = AppTest.from_file(APP).run(timeout=30)
    at = _submit(at, "https://vimeo.com/1", "0", "10")
    assert "Not a YouTube URL" in at.error[0].value


def test_successful_clip_offers_download(monkeypatch):
    def fake_clip(url, start, end, output, **kwargs):
        Path(output).write_bytes(b"\x00" * 1024)
        return clipper.ClipResult(Path(output), "My: Video?", url, start, end)

    monkeypatch.setattr(clipper, "clip_video", fake_clip)
    at = AppTest.from_file(APP).run(timeout=30)
    at = _submit(at, URL, "0:05", "0:10")
    assert not at.exception
    assert not at.error
    clip = at.session_state["clip"]
    assert clip["filename"] == "My_Video_00-00-05-00-00-10.mp4"
    assert clip["data"] == b"\x00" * 1024


def test_clipper_error_is_shown(monkeypatch):
    def fail(*args, **kwargs):
        raise clipper.AgeRestrictedError("This video is age-restricted")

    monkeypatch.setattr(clipper, "clip_video", fail)
    at = AppTest.from_file(APP).run(timeout=30)
    at = _submit(at, URL, "0", "10")
    assert "age-restricted" in at.error[0].value
