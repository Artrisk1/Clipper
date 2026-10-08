"""Shared, thread-safe services that let one server handle many users.

Streamlit runs every browser session as a thread inside a single Python
process, so plain in-process primitives are shared by all users. This module
provides them, independent of Streamlit so they can be unit-tested:

* :class:`ClipCache`   - disk cache of finished clips (LRU + TTL + size cap), so
  popular clips are produced once and served instantly afterwards.
* :class:`SingleFlight` - identical requests that arrive while a clip is being
  made wait for that one job instead of starting duplicates.
* :class:`JobQueue`    - FIFO queue capping how many ffmpeg jobs run at once,
  with queue positions for the UI and a bounded waiting line.
* :class:`RateLimiter` - sliding-window limits, used per user and server-wide.
  The server-wide limit matters most: every YouTube fetch comes from the same
  IP, and too many trigger YouTube's bot checks for *everyone*.
* :class:`TTLCache`    - short-lived cache of video metadata (fewer YouTube calls
  when users try several timestamps on the same video).

:class:`ClipService` wires these together. Everything is in-process, which
fits free single-container hosting (Streamlit Community Cloud). To scale past
one machine, replace these classes with Redis-backed equivalents and move the
download into worker processes; the ``ClipService`` interface can stay the same.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import clipper

logger = logging.getLogger("clipper.service")

T = TypeVar("T")
StatusFn = Callable[[str], None]


class ServiceBusyError(clipper.ClipperError):
    """The job queue is full or the wait timed out."""


class RateLimitedError(clipper.ClipperError):
    """A per-user or server-wide request limit was hit."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


@dataclass(frozen=True)
class ServiceConfig:
    max_concurrent_jobs: int = 2
    max_queue: int = 20
    queue_timeout_s: float = 300
    cache_dir: Path = Path(tempfile.gettempdir()) / "clipper-cache"
    cache_max_mb: float = 1024
    cache_ttl_s: float = 24 * 3600
    user_clips_per_hour: int = 10
    global_fetches_per_hour: int = 100
    ffmpeg_threads: int = 2
    encoder_preset: str = "veryfast"
    info_cache_ttl_s: float = 600
    max_clip_seconds: float = 600
    max_file_mb: float = 100

    @classmethod
    def from_env(cls) -> ServiceConfig:
        """Read ``CLIPPER_*`` environment variables (also set by Streamlit secrets)."""
        d = cls()
        return cls(
            max_concurrent_jobs=max(
                1, int(_env_float("CLIPPER_MAX_CONCURRENT_JOBS", d.max_concurrent_jobs))
            ),
            max_queue=max(0, int(_env_float("CLIPPER_MAX_QUEUE", d.max_queue))),
            queue_timeout_s=_env_float("CLIPPER_QUEUE_TIMEOUT_S", d.queue_timeout_s),
            cache_dir=Path(os.environ.get("CLIPPER_CACHE_DIR") or d.cache_dir),
            cache_max_mb=_env_float("CLIPPER_CACHE_MB", d.cache_max_mb),
            cache_ttl_s=_env_float("CLIPPER_CACHE_TTL_HOURS", d.cache_ttl_s / 3600) * 3600,
            user_clips_per_hour=int(
                _env_float("CLIPPER_USER_CLIPS_PER_HOUR", d.user_clips_per_hour)
            ),
            global_fetches_per_hour=int(
                _env_float("CLIPPER_GLOBAL_FETCHES_PER_HOUR", d.global_fetches_per_hour)
            ),
            ffmpeg_threads=max(1, int(_env_float("CLIPPER_FFMPEG_THREADS", d.ffmpeg_threads))),
            encoder_preset=os.environ.get("CLIPPER_ENCODER_PRESET") or d.encoder_preset,
            info_cache_ttl_s=_env_float("CLIPPER_INFO_CACHE_TTL_S", d.info_cache_ttl_s),
            max_clip_seconds=_env_float("CLIPPER_MAX_CLIP_SECONDS", d.max_clip_seconds),
            max_file_mb=_env_float("CLIPPER_MAX_FILE_MB", d.max_file_mb),
        )


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClipKey:
    """Everything that determines the output bytes of a clip."""

    video_id: str
    start: float
    end: float
    max_height: int | None
    precise: bool

    @property
    def digest(self) -> str:
        raw = f"{self.video_id}|{self.start:.3f}|{self.end:.3f}|{self.max_height}|{self.precise}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class CachedClip:
    path: Path
    title: str
    start: float
    end: float
    size_bytes: int
    from_cache: bool = False

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1_048_576


class ClipCache:
    """Disk cache of finished clips with LRU eviction, a TTL and a size budget.

    Each entry is ``<digest>.mp4`` plus a ``<digest>.json`` sidecar. A hit
    refreshes the file's mtime, which is what LRU eviction sorts by. Being on
    disk (not in RAM) means a cache of hundreds of MB costs no memory, and the
    cache survives app reruns (though not container restarts).
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        ttl_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        # Leftovers from a crash mid-put.
        for tmp in self.root.glob("*.tmp"):
            tmp.unlink(missing_ok=True)

    def _paths(self, digest: str) -> tuple[Path, Path]:
        return self.root / f"{digest}.mp4", self.root / f"{digest}.json"

    def get(self, key: ClipKey) -> CachedClip | None:
        video, meta = self._paths(key.digest)
        with self._lock:
            try:
                stat = video.stat()
                data = json.loads(meta.read_text())
            except (OSError, ValueError):
                return None
            if self._clock() - data.get("created", 0) > self.ttl_s:
                self._remove(key.digest)
                return None
            now = self._clock()
            os.utime(video, (now, now))  # mark as recently used
            return CachedClip(
                video, data["title"], data["start"], data["end"], stat.st_size, from_cache=True
            )

    def put(self, key: ClipKey, src: Path, title: str) -> CachedClip:
        video, meta = self._paths(key.digest)
        with self._lock:
            tmp = self.root / f"{key.digest}.mp4.tmp"
            shutil.move(str(src), str(tmp))  # may cross filesystems...
            os.replace(tmp, video)  # ...but the final rename is atomic
            now = self._clock()
            os.utime(video, (now, now))
            meta.write_text(
                json.dumps({"title": title, "start": key.start, "end": key.end, "created": now})
            )
            self._evict(keep=key.digest)
            size = video.stat().st_size
        return CachedClip(video, title, key.start, key.end, size)

    def _remove(self, digest: str) -> None:
        for p in self._paths(digest):
            p.unlink(missing_ok=True)

    def _entries(self) -> list[tuple[float, int, str]]:
        entries = []
        for video in self.root.glob("*.mp4"):
            try:
                st = video.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, video.stem))
        return entries

    def _evict(self, keep: str | None = None) -> None:
        """Drop expired entries, then least-recently-used ones until under budget."""
        now = self._clock()
        entries = []
        for mtime, size, digest in self._entries():
            meta = self.root / f"{digest}.json"
            try:
                created = json.loads(meta.read_text()).get("created", 0)
            except (OSError, ValueError):
                created = 0
            if now - created > self.ttl_s and digest != keep:
                self._remove(digest)
            else:
                entries.append((mtime, size, digest))
        total = sum(size for _, size, _ in entries)
        for _, size, digest in sorted(entries):  # oldest first
            if total <= self.max_bytes:
                break
            if digest == keep:
                continue
            self._remove(digest)
            total -= size

    def stats(self) -> dict[str, float]:
        with self._lock:
            entries = self._entries()
        return {"entries": len(entries), "mb": sum(s for _, s, _ in entries) / 1_048_576}


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` events per ``window_s`` per key.

    ``limit <= 0`` disables the limit.
    """

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self.window_s:
            events.popleft()
        return events

    def acquire(self, key: str = "global") -> float:
        """Record an event. Returns 0 if allowed, else seconds until a slot frees up."""
        if self.limit <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return max(0.0, self.window_s - (now - events[0]))
            events.append(now)
            # Keep memory bounded when many distinct users come and go.
            if len(self._events) > 10_000:
                for k in [k for k, v in self._events.items() if not v]:
                    del self._events[k]
            return 0.0

    def usage(self, key: str = "global") -> int:
        with self._lock:
            return len(self._prune(key, self._clock()))


class JobQueue:
    """FIFO admission control: at most ``max_concurrent`` jobs run at once.

    Up to ``max_waiting`` callers may queue; beyond that new requests are
    rejected immediately with :class:`ServiceBusyError` (fail fast instead of
    piling up threads). Waiters get their queue position via ``on_wait``.
    """

    def __init__(self, max_concurrent: int, max_waiting: int, poll_s: float = 1.0) -> None:
        self.max_concurrent = max_concurrent
        self.max_waiting = max_waiting
        self._poll_s = poll_s
        self._cond = threading.Condition()
        self._waiting: deque[object] = deque()
        self._active = 0

    def run(
        self,
        fn: Callable[[], T],
        on_wait: Callable[[int], None] | None = None,
        timeout_s: float | None = None,
    ) -> T:
        ticket = object()
        with self._cond:
            if len(self._waiting) >= self.max_waiting and self._active >= self.max_concurrent:
                raise ServiceBusyError(
                    "The server is at capacity right now. Please try again in a minute."
                )
            self._waiting.append(ticket)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        admitted = False
        try:
            while True:
                with self._cond:
                    if self._waiting[0] is ticket and self._active < self.max_concurrent:
                        self._waiting.popleft()
                        self._active += 1
                        admitted = True
                        self._cond.notify_all()
                        break
                    position = self._waiting.index(ticket) + 1
                    self._cond.wait(timeout=self._poll_s)
                if deadline is not None and time.monotonic() > deadline:
                    raise ServiceBusyError("Timed out waiting in the queue. Please try again.")
                if on_wait:
                    on_wait(position)  # outside the lock: may raise (e.g. user left)
        finally:
            if not admitted:
                with self._cond:
                    if ticket in self._waiting:
                        self._waiting.remove(ticket)
                    self._cond.notify_all()
        try:
            return fn()
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def stats(self) -> dict[str, int]:
        with self._cond:
            return {"active": self._active, "waiting": len(self._waiting)}


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.aborted = False


class SingleFlight:
    """Run at most one ``fn`` per key at a time; concurrent callers share its result.

    If the leader is aborted by a non-error exception (a Streamlit session
    closing raises ``StopException``, a ``BaseException``), followers do not
    see that; one of them simply retries as the new leader.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

    def do(
        self,
        key: str,
        fn: Callable[[], T],
        on_wait: Callable[[], None] | None = None,
        poll_s: float = 1.0,
    ) -> T:
        while True:
            with self._lock:
                flight = self._flights.get(key)
                leader = flight is None
                if leader:
                    flight = self._flights[key] = _Flight()
            assert flight is not None
            if leader:
                return self._lead(key, flight, fn)
            while not flight.done.wait(timeout=poll_s):
                if on_wait:
                    on_wait()
            if flight.aborted:
                continue  # leader went away; try again (possibly as leader)
            if flight.error is not None:
                raise flight.error
            return flight.result

    def _lead(self, key: str, flight: _Flight, fn: Callable[[], T]) -> T:
        try:
            flight.result = fn()
            return flight.result
        except Exception as exc:
            flight.error = exc
            raise
        except BaseException:
            flight.aborted = True
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
            flight.done.set()

    def in_flight(self) -> int:
        with self._lock:
            return len(self._flights)


class TTLCache(MutableMapping):
    """Small thread-safe LRU mapping whose entries expire after ``ttl_s``."""

    def __init__(
        self, ttl_s: float, maxsize: int = 256, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = ttl_s
        self.maxsize = maxsize
        self._clock = clock
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def __getitem__(self, key: Any) -> Any:
        with self._lock:
            stored_at, value = self._data[key]
            if self._clock() - stored_at > self.ttl_s:
                del self._data[key]
                raise KeyError(key)
            self._data.move_to_end(key)
            return value

    def __setitem__(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __delitem__(self, key: Any) -> None:
        with self._lock:
            del self._data[key]

    def __iter__(self) -> Iterator[Any]:
        with self._lock:
            return iter(list(self._data))

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #
class ClipService:
    """Cache → de-duplicate → rate-limit → queue → download, in that order.

    Cheap checks run first: a cache hit costs no YouTube request, no CPU and
    no rate-limit budget.
    """

    def __init__(
        self,
        config: ServiceConfig,
        downloader: Callable[..., clipper.ClipResult] | None = None,
    ) -> None:
        self.config = config
        self._download = downloader or clipper.download_section
        self.cache = ClipCache(
            config.cache_dir, int(config.cache_max_mb * 1_048_576), config.cache_ttl_s
        )
        self.flights = SingleFlight()
        self.queue = JobQueue(config.max_concurrent_jobs, config.max_queue)
        self.user_limiter = RateLimiter(config.user_clips_per_hour, 3600)
        self.global_limiter = RateLimiter(config.global_fetches_per_hour, 3600)
        self.info_cache = TTLCache(config.info_cache_ttl_s)
        self._work_dir = config.cache_dir / "work"
        self._work_dir.mkdir(parents=True, exist_ok=True)

    def get_clip(
        self,
        url: str,
        start: str | float,
        end: str | float,
        *,
        user_id: str,
        max_height: int | None = 720,
        precise: bool = True,
        status: StatusFn | None = None,
    ) -> CachedClip:
        notify = status or (lambda _m: None)
        video_id = clipper.extract_video_id(url)
        start_s, end_s = clipper.parse_timestamp(start), clipper.parse_timestamp(end)
        clipper.validate_range(start_s, end_s, self.config.max_clip_seconds)
        key = ClipKey(video_id, start_s, end_s, max_height, precise)

        if hit := self.cache.get(key):
            logger.info("cache hit %s", key.digest)
            return hit

        if wait := self.user_limiter.acquire(user_id):
            raise RateLimitedError(
                f"You've reached the limit of {self.config.user_clips_per_hour} new clips per "
                f"hour. Try again in {_human(wait)} (clips already made by anyone are not "
                "counted)."
            )

        return self.flights.do(
            key.digest,
            lambda: self._produce(key, notify),
            on_wait=lambda: notify("Someone is already making this exact clip; waiting for it…"),
        )

    def _produce(self, key: ClipKey, notify: StatusFn) -> CachedClip:
        if hit := self.cache.get(key):  # finished while we were waiting
            return hit
        if wait := self.global_limiter.acquire():
            raise RateLimitedError(
                "This server has reached its hourly YouTube request budget (this protects it "
                f"from being blocked by YouTube). Try again in {_human(wait)}."
            )
        return self.queue.run(
            lambda: self._make(key, notify),
            on_wait=lambda pos: notify(f"Waiting in queue… position {pos}"),
            timeout_s=self.config.queue_timeout_s,
        )

    def _make(self, key: ClipKey, notify: StatusFn) -> CachedClip:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="job-", dir=self._work_dir) as tmp:
            result = self._download(
                clipper.canonical_url(key.video_id),
                key.start,
                key.end,
                Path(tmp) / "clip.mp4",
                precise=key.precise,
                max_height=key.max_height,
                status=notify,
                threads=self.config.ffmpeg_threads,
                encoder_preset=self.config.encoder_preset,
                info_cache=self.info_cache,
            )
            size_mb = result.path.stat().st_size / 1_048_576
            if size_mb > self.config.max_file_mb:
                raise clipper.ClipperError(
                    f"The clip is {size_mb:.0f} MB, above this server's "
                    f"{self.config.max_file_mb:.0f} MB limit. Choose a shorter range or lower "
                    "quality."
                )
            clip = self.cache.put(key, result.path, result.title)
        logger.info(
            "made %s (%.1f MB) in %.1fs", key.digest, clip.size_mb, time.monotonic() - started
        )
        return clip

    def stats(self) -> dict[str, Any]:
        return {
            **self.queue.stats(),
            "max_concurrent": self.config.max_concurrent_jobs,
            "cache": self.cache.stats(),
            "youtube_fetches_last_hour": self.global_limiter.usage(),
            "youtube_fetch_budget": self.config.global_fetches_per_hour,
        }


def _human(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
