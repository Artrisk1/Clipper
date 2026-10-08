"""Tests for service.py: cache, rate limits, queue and de-duplication under concurrency."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import clipper
import service
from service import (
    ClipCache,
    ClipKey,
    ClipService,
    JobQueue,
    RateLimitedError,
    RateLimiter,
    ServiceBusyError,
    ServiceConfig,
    SingleFlight,
    TTLCache,
)

VID = "dQw4w9WgXcQ"
URL = f"https://youtu.be/{VID}"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _key(start: float = 0, end: float = 10) -> ClipKey:
    return ClipKey(VID, float(start), float(end), 720, True)


def _src(tmp_path: Path, name: str, size: int) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


# --------------------------------------------------------------------------- #
# ClipCache
# --------------------------------------------------------------------------- #


def test_cache_put_get(tmp_path):
    cache = ClipCache(tmp_path / "c", max_bytes=10_000, ttl_s=60)
    assert cache.get(_key()) is None
    stored = cache.put(_key(), _src(tmp_path, "a.mp4", 100), "Title")
    assert not stored.from_cache
    hit = cache.get(_key())
    assert hit is not None and hit.from_cache
    assert hit.title == "Title" and hit.size_bytes == 100 and hit.path.read_bytes() == b"x" * 100


def test_cache_key_distinguishes_options():
    base = ClipKey(VID, 0.0, 10.0, 720, True)
    assert base.digest == ClipKey(VID, 0.0, 10.0, 720, True).digest
    assert base.digest != ClipKey(VID, 0.0, 10.0, 1080, True).digest
    assert base.digest != ClipKey(VID, 0.0, 10.0, 720, False).digest
    assert base.digest != ClipKey(VID, 0.0, 10.5, 720, True).digest


def test_cache_ttl_expiry(tmp_path):
    clock = FakeClock()
    cache = ClipCache(tmp_path / "c", max_bytes=10_000, ttl_s=60, clock=clock)
    cache.put(_key(), _src(tmp_path, "a.mp4", 10), "t")
    clock.now += 61
    assert cache.get(_key()) is None
    assert not list((tmp_path / "c").glob("*.mp4"))


def test_cache_lru_eviction(tmp_path):
    clock = FakeClock()
    cache = ClipCache(tmp_path / "c", max_bytes=250, ttl_s=3600, clock=clock)
    cache.put(_key(0, 1), _src(tmp_path, "src_a", 100), "a")
    clock.now += 1
    cache.put(_key(0, 2), _src(tmp_path, "src_b", 100), "b")
    clock.now += 1
    assert cache.get(_key(0, 1))  # touch "a" so "b" becomes least recently used
    clock.now += 1
    cache.put(_key(0, 3), _src(tmp_path, "src_c", 100), "c")  # 300 > 250: evict one
    assert cache.get(_key(0, 1)) is not None
    assert cache.get(_key(0, 2)) is None
    assert cache.get(_key(0, 3)) is not None
    assert cache.stats()["entries"] == 2


def test_cache_keeps_new_entry_even_if_over_budget(tmp_path):
    cache = ClipCache(tmp_path / "c", max_bytes=50, ttl_s=3600)
    cache.put(_key(), _src(tmp_path, "big", 100), "big")
    assert cache.get(_key()) is not None


# --------------------------------------------------------------------------- #
# RateLimiter / TTLCache
# --------------------------------------------------------------------------- #


def test_rate_limiter_window():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_s=60, clock=clock)
    assert limiter.acquire("u") == 0
    assert limiter.acquire("u") == 0
    assert limiter.acquire("u") == pytest.approx(60)
    assert limiter.acquire("other") == 0  # keys are independent
    clock.now += 30
    assert limiter.acquire("u") == pytest.approx(30)
    clock.now += 30
    assert limiter.acquire("u") == 0
    assert limiter.usage("u") == 1


def test_rate_limiter_disabled():
    limiter = RateLimiter(limit=0, window_s=60)
    assert all(limiter.acquire() == 0 for _ in range(100))


def test_ttl_cache():
    clock = FakeClock()
    cache = TTLCache(ttl_s=10, maxsize=2, clock=clock)
    cache["a"], cache["b"] = 1, 2
    assert cache.get("a") == 1
    cache["c"] = 3  # evicts least recently used ("b")
    assert "b" not in cache and set(cache) == {"a", "c"}
    clock.now += 11
    assert cache.get("a") is None


# --------------------------------------------------------------------------- #
# JobQueue
# --------------------------------------------------------------------------- #


def test_job_queue_caps_concurrency():
    queue = JobQueue(max_concurrent=2, max_waiting=50, poll_s=0.01)
    lock = threading.Lock()
    running = peak = 0

    def job():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return True

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: queue.run(job), range(10)))
    assert all(results)
    assert peak == 2
    assert queue.stats() == {"active": 0, "waiting": 0}


def test_job_queue_is_fifo():
    queue = JobQueue(max_concurrent=1, max_waiting=10, poll_s=0.01)
    gate = threading.Event()
    order = []
    blocker = threading.Thread(target=queue.run, args=(gate.wait,))
    blocker.start()
    while queue.stats()["active"] == 0:
        time.sleep(0.001)
    threads = []
    for i in range(5):
        t = threading.Thread(target=queue.run, args=(lambda i=i: order.append(i),))
        t.start()
        threads.append(t)
        while queue.stats()["waiting"] < i + 1:  # enqueue in a known order
            time.sleep(0.001)
    gate.set()
    for t in [blocker, *threads]:
        t.join(5)
    assert order == [0, 1, 2, 3, 4]


def test_job_queue_rejects_when_full_and_reports_position():
    queue = JobQueue(max_concurrent=1, max_waiting=1, poll_s=0.01)
    gate = threading.Event()
    positions = []
    t1 = threading.Thread(target=queue.run, args=(gate.wait,))
    t1.start()
    while queue.stats()["active"] == 0:
        time.sleep(0.001)
    t2 = threading.Thread(target=queue.run, args=(lambda: None, positions.append))
    t2.start()
    while queue.stats()["waiting"] == 0:
        time.sleep(0.001)
    with pytest.raises(ServiceBusyError, match="capacity"):
        queue.run(lambda: None)
    time.sleep(0.05)
    gate.set()
    t1.join(5)
    t2.join(5)
    assert positions and set(positions) == {1}


def test_job_queue_timeout_and_abort_free_their_place():
    queue = JobQueue(max_concurrent=1, max_waiting=5, poll_s=0.01)
    gate = threading.Event()
    t = threading.Thread(target=queue.run, args=(gate.wait,))
    t.start()
    while queue.stats()["active"] == 0:
        time.sleep(0.001)
    with pytest.raises(ServiceBusyError, match="Timed out"):
        queue.run(lambda: None, timeout_s=0.05)

    class UserLeft(BaseException):  # like Streamlit's StopException
        pass

    def on_wait(_pos):
        raise UserLeft

    with pytest.raises(UserLeft):
        queue.run(lambda: None, on_wait=on_wait)
    assert queue.stats() == {"active": 1, "waiting": 0}
    gate.set()
    t.join(5)
    assert queue.run(lambda: "ok") == "ok"


def test_job_queue_releases_slot_on_job_error():
    queue = JobQueue(max_concurrent=1, max_waiting=5)
    with pytest.raises(ValueError):
        queue.run(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert queue.stats()["active"] == 0


# --------------------------------------------------------------------------- #
# SingleFlight
# --------------------------------------------------------------------------- #


def test_single_flight_dedupes_concurrent_calls():
    flights = SingleFlight()
    calls = 0
    gate = threading.Event()

    def work():
        nonlocal calls
        calls += 1
        gate.wait(5)
        return "result"

    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(flights.do, "k", work, None, 0.01) for _ in range(8)]
        time.sleep(0.1)
        gate.set()
        results = [f.result(5) for f in futures]
    assert results == ["result"] * 8
    assert calls == 1
    assert flights.in_flight() == 0


def test_single_flight_shares_errors():
    flights = SingleFlight()
    gate = threading.Event()

    def work():
        gate.wait(5)
        raise clipper.VideoUnavailableError("gone")

    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(flights.do, "k", work, None, 0.01) for _ in range(3)]
        time.sleep(0.1)
        gate.set()
        for f in futures:
            with pytest.raises(clipper.VideoUnavailableError):
                f.result(5)


def test_single_flight_follower_retries_when_leader_aborts():
    flights = SingleFlight()
    leader_started = threading.Event()
    release = threading.Event()

    class UserLeft(BaseException):
        pass

    def aborted_leader():
        leader_started.set()
        release.wait(5)
        raise UserLeft

    def leader():
        with pytest.raises(UserLeft):
            flights.do("k", aborted_leader)

    t = threading.Thread(target=leader)
    t.start()
    leader_started.wait(5)
    result = {}
    follower = threading.Thread(
        target=lambda: result.setdefault("v", flights.do("k", lambda: "retried", None, 0.01))
    )
    follower.start()
    time.sleep(0.05)
    release.set()
    t.join(5)
    follower.join(5)
    assert result["v"] == "retried"


# --------------------------------------------------------------------------- #
# ClipService
# --------------------------------------------------------------------------- #


def _config(tmp_path, **overrides) -> ServiceConfig:
    return ServiceConfig(cache_dir=tmp_path / "cache", **overrides)


class Downloader:
    """Fake clipper.download_section that records calls."""

    def __init__(self, delay: float = 0.0, fail: Exception | None = None) -> None:
        self.calls = []
        self.kwargs = []
        self.delay = delay
        self.fail = fail
        self._lock = threading.Lock()

    def __call__(self, source_url, start, end, output, **kwargs):
        with self._lock:
            self.calls.append((source_url, start, end))
            self.kwargs.append(kwargs)
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        Path(output).write_bytes(b"v" * 64)
        return clipper.ClipResult(Path(output), "Title", source_url, start, end)


def test_service_caches_results(tmp_path):
    dl = Downloader()
    svc = ClipService(_config(tmp_path), downloader=dl)
    first = svc.get_clip(URL, "0:05", "0:10", user_id="a")
    second = svc.get_clip(f"https://www.youtube.com/watch?v={VID}", 5, 10, user_id="b")
    assert not first.from_cache and second.from_cache
    assert first.path == second.path
    assert len(dl.calls) == 1
    assert dl.calls[0][0] == f"https://www.youtube.com/watch?v={VID}"
    # Scaling knobs are passed through to the downloader.
    kw = dl.kwargs[0]
    assert kw["threads"] == 2 and kw["encoder_preset"] == "veryfast"
    assert kw["info_cache"] is svc.info_cache
    # Work directories are cleaned up.
    assert not list((tmp_path / "cache" / "work").iterdir())


def test_service_dedupes_simultaneous_identical_requests(tmp_path):
    dl = Downloader(delay=0.2)
    svc = ClipService(_config(tmp_path), downloader=dl)
    with ThreadPoolExecutor(10) as pool:
        clips = list(pool.map(lambda i: svc.get_clip(URL, 0, 10, user_id=f"u{i}"), range(10)))
    assert len(dl.calls) == 1
    assert len({c.path for c in clips}) == 1


def test_service_limits_concurrency_for_distinct_requests(tmp_path):
    dl = Downloader(delay=0.05)
    svc = ClipService(_config(tmp_path, max_concurrent_jobs=2), downloader=dl)
    svc.queue._poll_s = 0.01
    peak = 0
    stop = threading.Event()

    def watch():
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, svc.queue.stats()["active"])
            time.sleep(0.002)

    watcher = threading.Thread(target=watch)
    watcher.start()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: svc.get_clip(URL, i, i + 5, user_id=f"u{i}"), range(8)))
    stop.set()
    watcher.join()
    assert len(dl.calls) == 8
    assert peak == 2


def test_service_global_youtube_budget(tmp_path):
    dl = Downloader()
    svc = ClipService(_config(tmp_path, global_fetches_per_hour=2), downloader=dl)
    svc.get_clip(URL, 0, 1, user_id="a")
    svc.get_clip(URL, 0, 2, user_id="b")
    with pytest.raises(RateLimitedError, match="hourly YouTube request budget"):
        svc.get_clip(URL, 0, 3, user_id="c")
    # Cache hits still work when the budget is exhausted.
    assert svc.get_clip(URL, 0, 1, user_id="c").from_cache
    assert svc.stats()["youtube_fetches_last_hour"] == 2


def test_service_per_user_limit_ignores_cache_hits(tmp_path):
    svc = ClipService(_config(tmp_path, user_clips_per_hour=1), downloader=Downloader())
    svc.get_clip(URL, 0, 1, user_id="a")
    assert svc.get_clip(URL, 0, 1, user_id="a").from_cache
    with pytest.raises(RateLimitedError, match="limit of 1 new clips"):
        svc.get_clip(URL, 0, 2, user_id="a")
    svc.get_clip(URL, 0, 2, user_id="b")  # other users unaffected


def test_service_rejects_oversized_clip_and_cleans_up(tmp_path):
    svc = ClipService(_config(tmp_path, max_file_mb=0.00001), downloader=Downloader())
    with pytest.raises(clipper.ClipperError, match="limit"):
        svc.get_clip(URL, 0, 10, user_id="a")
    assert svc.cache.stats()["entries"] == 0
    assert not list((tmp_path / "cache" / "work").iterdir())


def test_service_validates_before_spending_budget(tmp_path):
    dl = Downloader()
    svc = ClipService(_config(tmp_path, max_clip_seconds=60), downloader=dl)
    with pytest.raises(clipper.InvalidURLError):
        svc.get_clip("https://vimeo.com/1", 0, 10, user_id="a")
    with pytest.raises(clipper.InvalidTimestampError, match="maximum"):
        svc.get_clip(URL, 0, 61, user_id="a")
    assert dl.calls == [] and svc.user_limiter.usage("a") == 0


def test_service_errors_are_not_cached(tmp_path):
    dl = Downloader(fail=clipper.AccessBlockedError("blocked"))
    svc = ClipService(_config(tmp_path), downloader=dl)
    with pytest.raises(clipper.AccessBlockedError):
        svc.get_clip(URL, 0, 10, user_id="a")
    dl.fail = None
    assert not svc.get_clip(URL, 0, 10, user_id="a").from_cache


def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPPER_MAX_CONCURRENT_JOBS", "4")
    monkeypatch.setenv("CLIPPER_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CLIPPER_CACHE_TTL_HOURS", "2")
    monkeypatch.setenv("CLIPPER_USER_CLIPS_PER_HOUR", "not-a-number")
    cfg = ServiceConfig.from_env()
    assert cfg.max_concurrent_jobs == 4
    assert cfg.cache_dir == tmp_path
    assert cfg.cache_ttl_s == 7200
    assert cfg.user_clips_per_hour == ServiceConfig().user_clips_per_hour


def test_human_duration():
    assert service._human(10) == "1 minute"
    assert service._human(600) == "10 minutes"
