# 🎬 YouTube Clipper

[![CI](https://github.com/artrisk1/clipper/actions/workflows/ci.yml/badge.svg)](https://github.com/artrisk1/clipper/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)

Extract a specific clip from a YouTube video using start and end timestamps,
**without downloading the full video**. You can use it from the command line or
through a Streamlit web UI that lets you preview and download the clip.

Built on [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and [`ffmpeg`](https://ffmpeg.org/).

---

## Table of contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Local installation](#local-installation)
- [CLI usage](#cli-usage)
- [Web UI (Streamlit)](#web-ui-streamlit)
- [Serving many users](#serving-many-users)
- [Running tests and linting](#running-tests-and-linting)
- [GitHub setup and CI](#github-setup-and-ci)
- [Deploying to Streamlit Community Cloud](#deploying-to-streamlit-community-cloud)
- [Troubleshooting](#troubleshooting)
- [Design decisions and limitations](#design-decisions-and-limitations)
- [Legal notice](#legal-notice)

---

## How it works

```
URL ──► validate ──► yt-dlp: fetch metadata (title, duration, stream URLs)
                         │
                         ├─ check end ≤ duration, reject playlists and live streams
                         ▼
        yt-dlp download_ranges ──► ffmpeg -ss START -t LEN -i <stream URL>
                         │           (input seeking = HTTP Range requests,
                         │            so only the needed bytes are downloaded)
                         ▼
        temp dir ──► merge audio+video ──► move finished clip to --output
                    (temp dir is always deleted, even on error or Ctrl+C)
```

There are two cutting modes:

| Mode | Flag | Speed | Accuracy |
|---|---|---|---|
| **Precise** (default) | — | Slower, because the clip is re-encoded with ffmpeg's default encoder (H.264 for MP4) | Starts and ends on the exact frame you asked for |
| **Fast** | `--fast` | Very fast, using a stream copy with no re-encode | The start snaps to the **previous keyframe**, so the clip can begin a few seconds early |

## Project structure

```
.
├── clipper.py               # Core library + CLI (validation, yt-dlp/ffmpeg logic)
├── app.py                   # Streamlit web interface
├── service.py               # Shared queue, cache, rate limits for many users
├── requirements.txt         # Runtime dependencies (also read by Streamlit Cloud)
├── requirements-dev.txt     # + pytest, ruff
├── packages.txt             # apt packages for Streamlit Cloud (ffmpeg)
├── pyproject.toml           # ruff + pytest configuration
├── tests/
│   ├── test_clipper.py      # unit tests + real ffmpeg integration tests (offline)
│   ├── test_service.py      # queue/cache/rate-limit tests, incl. concurrency
│   └── test_app.py          # Streamlit UI tests (AppTest)
├── .github/workflows/ci.yml # Lint + test on every push / PR
└── .gitignore
```

## Prerequisites

| Requirement | Why | How you get it |
|---|---|---|
| Python **3.10+** | Runtime | [python.org](https://www.python.org/downloads/) |
| **ffmpeg** and **ffprobe** | Cutting, merging, and re-encoding | See [Install ffmpeg](#1-install-ffmpeg) |
| A **JavaScript runtime** | Current yt-dlp versions need one to solve YouTube's player challenges | Installed automatically by `pip install -r requirements.txt` (the `deno` package) |

### 1. Install ffmpeg

<details open>
<summary><b>Linux</b></summary>

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# Fedora (enable RPM Fusion first for the full build)
sudo dnf install -y ffmpeg

# Arch / Manjaro
sudo pacman -S ffmpeg
```
</details>

<details open>
<summary><b>macOS</b></summary>

```bash
# Homebrew (https://brew.sh)
brew install ffmpeg
```
</details>

<details open>
<summary><b>Windows</b></summary>

Pick **one** of the following:

```powershell
# winget (built into Windows 10/11)
winget install --id Gyan.FFmpeg -e

# Chocolatey
choco install ffmpeg

# Scoop
scoop install ffmpeg
```

**Manual install:** download a "release essentials" build from
<https://www.gyan.dev/ffmpeg/builds/> and extract it to `C:\ffmpeg`. Then add
`C:\ffmpeg\bin` to your **PATH** (Start → "Edit the system environment
variables" → Environment Variables → `Path` → New). **Open a new terminal**
after changing PATH.
</details>

Then check that both tools are available:

```bash
ffmpeg -version
ffprobe -version
```

## Local installation

```bash
# 1. Clone
git clone https://github.com/artrisk1/clipper.git
cd clipper

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows (PowerShell / cmd)

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Check that it works
python clipper.py --help
```

> **Keep yt-dlp up to date.** YouTube changes frequently, and old yt-dlp
> versions stop working. If downloads start failing, first run
> `pip install -U "yt-dlp[default]"`.

## CLI usage

```
python clipper.py --url URL --start START --end END [--output FILE] [options]
```

**Timestamp formats.** All of the following are accepted:

| Input | Meaning |
|---|---|
| `90`, `12.5` | seconds |
| `1:30` | MM:SS |
| `01:02:03`, `1:02:03.250` | HH:MM:SS(.mmm) |

Minutes and seconds after the first colon must be below 60, so `1:75` is
rejected instead of being silently treated as 2:15.

### Examples

```bash
# Clip 0:43 → 1:05 into chorus.mp4 (frame-accurate)
python clipper.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
                  --start 0:43 --end 1:05 --output chorus.mp4

# Seconds work too; short links, Shorts, and embed URLs are accepted
python clipper.py --url "https://youtu.be/dQw4w9WgXcQ" --start 43 --end 65 -o chorus.mp4

# Fast mode (no re-encode) at max 720p, from a long video
python clipper.py --url "https://youtu.be/VIDEO_ID" \
                  --start 01:15:00 --end 01:20:30 --fast --max-height 720 -o talk.mp4

# Default output name: <video_id>_<start>-<end>.mp4 in the current directory
python clipper.py --url "https://youtu.be/dQw4w9WgXcQ" --start 10 --end 20

# MKV / WebM containers
python clipper.py --url "https://youtu.be/dQw4w9WgXcQ" --start 10 --end 20 -o clip.mkv

# Debug output from yt-dlp/ffmpeg
python clipper.py --url "https://youtu.be/dQw4w9WgXcQ" --start 10 --end 20 -v
```

### Options

| Option | Description |
|---|---|
| `--url` | YouTube video URL (**required**). Playlist parameters are ignored, so only that one video is fetched. |
| `--start`, `--end` | Clip boundaries (**required**). |
| `-o`, `--output` | Output file: `.mp4` (default), `.mkv` or `.webm`. Parent folders are created if needed. |
| `--fast` | Stream copy without re-encoding (see [How it works](#how-it-works)). |
| `--max-height PX` | Cap the resolution, e.g. `720`. |
| `--cookies FILE` | Netscape-format `cookies.txt` for age-restricted or members-only videos (see [Troubleshooting](#troubleshooting)). |
| `--ffmpeg-location PATH` | Use an ffmpeg binary or folder that isn't on PATH. |
| `--overwrite` | Replace an existing output file. Without it, the tool refuses to overwrite. |
| `-v`, `--verbose` | Show debug logs. |

**Exit codes:** `0` success · `1` clipping error (invalid input, unavailable
video, missing ffmpeg, and so on) · `2` invalid command-line usage · `130`
interrupted with Ctrl+C.

### Using it as a library

```python
from clipper import clip_video, ClipperError

try:
    result = clip_video("https://youtu.be/dQw4w9WgXcQ", "0:43", "1:05", "chorus.mp4")
    print(result.path, result.title, result.duration)
except ClipperError as exc:
    print(f"Failed: {exc}")
```

All expected failures raise a subclass of `ClipperError`:
`InvalidURLError`, `InvalidTimestampError`, `FFmpegNotFoundError`,
`AgeRestrictedError`, `AccessBlockedError`, `VideoUnavailableError`, or
`DownloadFailedError`.

## Web UI (Streamlit)

```bash
streamlit run app.py
```

Then open <http://localhost:8501>. Paste a URL, set the start and end times,
pick a quality, and click **Create clip**. The clip appears in a video player,
along with a **Download MP4** button.

If a clip has already been made by anyone (same video, times, quality and
mode), it is served instantly from the server's cache. The **Server status**
panel at the bottom shows running jobs, the queue, and the YouTube request
budget.

## Serving many users

The web app is designed to run on **one free server** (such as Streamlit
Community Cloud) and stay usable when many people use it at once. Every browser
session goes through one shared `ClipService` (`service.py`):

```
request ─► validate ─► disk cache hit? ──yes──► serve instantly (no YouTube call, no CPU)
                           │ no
                           ▼
            per-user limit (new clips / hour)
                           ▼
            same clip already being made? ──yes──► wait for it and share the result
                           │ no
                           ▼
            server-wide YouTube budget (fetches / hour)
                           ▼
            FIFO job queue (max N ffmpeg jobs at once; others see their position)
                           ▼
            yt-dlp + ffmpeg (capped threads, fast x264 preset) ─► cache ─► serve
```

| Problem with many users | What handles it |
|---|---|
| Several ffmpeg encodes at once exhaust CPU and RAM and crash the app | **Job queue**: only `CLIPPER_MAX_CONCURRENT_JOBS` jobs run. Others wait in order and see their queue position. Once the queue is full, new requests are turned away immediately with "server at capacity", instead of piling up. |
| Popular clips get made over and over | **Disk cache** (LRU, with a TTL and a size cap). It is keyed by video, start, end, quality and mode, so a cache hit needs no YouTube request and no CPU. |
| Two people request the same clip at the same moment | **Single-flight**: the second request waits for the first job instead of starting a duplicate. |
| Each new clip of the same video re-fetches its metadata | **Metadata cache** (10 minutes). If a cached stream URL has expired, the metadata is re-fetched automatically. |
| One user spams requests | **Per-user limit** on *new* clips per hour (cache hits don't count). |
| YouTube bot-blocks the server's IP, which breaks the app for everyone | **Server-wide YouTube budget** per hour. This is the most important limit, because all users share one IP address. |
| Each session holds video bytes in RAM | Sessions store only a **file path**. Streamlit de-duplicates identical media, so many viewers of one clip share one copy in memory, and the download file is read only when someone clicks the button. |
| ffmpeg uses every core for each job | `-threads` is capped per job, and precise mode uses the `veryfast` x264 preset. In a local benchmark (20 s of 720p, 2 threads) it encoded 2.8× faster than the default `medium`. On real footage, expect somewhat larger files at similar quality. |

### Configuration

All settings are environment variables. On Streamlit Cloud, set them as
root-level **Secrets** (see [deployment](#deploying-to-streamlit-community-cloud)).

| Variable | Default | Meaning |
|---|---|---|
| `CLIPPER_MAX_CONCURRENT_JOBS` | `2` | ffmpeg jobs that may run at the same time |
| `CLIPPER_MAX_QUEUE` | `20` | Requests allowed to wait. Beyond this, "server at capacity" |
| `CLIPPER_QUEUE_TIMEOUT_S` | `300` | Longest wait in the queue before giving up |
| `CLIPPER_FFMPEG_THREADS` | `2` | CPU threads per ffmpeg job |
| `CLIPPER_ENCODER_PRESET` | `veryfast` | x264 preset for precise mode (`ultrafast` … `medium`) |
| `CLIPPER_CACHE_DIR` | `<tmp>/clipper-cache` | Where finished clips are cached |
| `CLIPPER_CACHE_MB` | `1024` | Cache size budget. Least recently used clips are evicted first |
| `CLIPPER_CACHE_TTL_HOURS` | `24` | Maximum age of a cached clip |
| `CLIPPER_INFO_CACHE_TTL_S` | `600` | How long video metadata is reused |
| `CLIPPER_USER_CLIPS_PER_HOUR` | `10` | New (uncached) clips per user session per hour (`0` = unlimited) |
| `CLIPPER_GLOBAL_FETCHES_PER_HOUR` | `100` | YouTube downloads per hour for the whole server (`0` = unlimited) |
| `CLIPPER_MAX_CLIP_SECONDS` | `600` | Longest clip allowed |
| `CLIPPER_MAX_FILE_MB` | `100` | Largest clip file allowed |

**Tuning rule of thumb:** `MAX_CONCURRENT_JOBS × FFMPEG_THREADS ≈ CPU cores`.
If YouTube starts showing bot checks, lower `GLOBAL_FETCHES_PER_HOUR`. If
memory is tight, lower `MAX_FILE_MB` and `MAX_CONCURRENT_JOBS`.

### Limits of this design

- **Single process only.** Queues, caches and limits live in memory, so they
  are not shared between replicas and they reset when the app restarts (the
  disk cache also disappears when the container is replaced). This fits free
  single-container hosting.
- **Per-user limits are per browser session.** Opening a new tab gives a new
  session. Real per-person limits need logins. The server-wide YouTube budget
  is what actually protects the server.
- **Capacity is bounded by the hardware.** Free hosting has little CPU, so
  under load, expect queueing rather than parallelism. Fast mode (no re-encode)
  is far cheaper than precise mode.
- **Scaling beyond one machine** means replacing the classes in `service.py`
  with shared equivalents: a Redis-backed queue, limiter and lock, shared or
  object storage for the cache, and separate worker processes running the
  downloads. The `ClipService.get_clip()` interface is meant to stay the same,
  but that requires paid infrastructure and is not built here.

## Running tests and linting

```bash
pip install -r requirements-dev.txt

ruff check .            # lint
ruff format --check .   # formatting (run `ruff format .` to fix)
pytest -v               # tests
```

The test suite needs **no internet access**:

- **Unit tests** cover timestamp parsing, URL validation, error mapping, the
  yt-dlp options that get passed (including `download_ranges`), temp-file
  cleanup, and the CLI.
- **The integration test** generates a 20-second video with ffmpeg, serves it
  from a local HTTP server that supports Range requests, and runs the **real**
  yt-dlp + ffmpeg section download. It checks that the clip is 4.5 s ± 0.15 s
  long and that no temporary files are left behind. It is skipped if ffmpeg is
  not installed.
- **The service tests** check concurrency directly: that the job cap is never
  exceeded, the queue is FIFO, it rejects requests when full, aborted waiters
  give up their place, simultaneous identical requests run only once, cache
  LRU/TTL eviction works, and rate-limit windows behave correctly. Another
  real-ffmpeg test runs 6 simultaneous users through the service.
- **The UI tests** drive `app.py` with Streamlit's `AppTest`.
- **An optional live YouTube test** runs only when you opt in:
  `CLIPPER_NETWORK_TESTS=1 pytest -m network`.

## GitHub setup and CI

1. **Create the repository** on GitHub (e.g. `clipper`). Don't add a README or
   `.gitignore` there, because they are already in this project.
2. **Push the code:**
   ```bash
   git init                       # skip if already a git repo
   git add .
   git commit -m "Initial commit: YouTube clipper"
   git branch -M main
   git remote add origin https://github.com/<your-username>/clipper.git
   git push -u origin main
   ```
3. **CI runs automatically.** `.github/workflows/ci.yml` runs on every push and
   pull request:
   - `lint`: `ruff check` and `ruff format --check`
   - `test`: installs ffmpeg and runs `pytest` on Python 3.10–3.13

   You can follow the runs in the **Actions** tab.
4. **Update the badge** at the top of this README so it points to
   `<your-username>/<repo>`.
5. *(Recommended)* **Protect `main`:** go to Settings → Branches → Add rule →
   enable "Require status checks to pass" and select the `Lint (ruff)` and
   `Tests (…)` jobs.

## Deploying to Streamlit Community Cloud

Streamlit Community Cloud deploys straight from your GitHub repository and
redeploys automatically on every push to the branch you choose.

The repository already contains everything the deploy needs:
- `requirements.txt`: Python packages, including `deno`, which supplies the JS
  runtime yt-dlp needs
- `packages.txt`: system packages installed with `apt` (here, `ffmpeg`)

**Steps:**

1. Push the repository to GitHub (see above). It can be public or private.
2. Go to **<https://share.streamlit.io>** and **sign in with GitHub**. Authorize
   Streamlit to access your repositories when asked.
3. Click **Create app**, then choose **"Deploy a public app from GitHub"**.
4. Fill in:
   - **Repository:** `<your-username>/clipper`
   - **Branch:** `main`
   - **Main file path:** `app.py`
   - **App URL:** pick a subdomain (optional)
5. Open **Advanced settings** and set **Python version** to `3.12`. If you want
   to change the limits (see [Configuration](#configuration)), add them under
   **Secrets** as root-level keys, for example:
   ```toml
   CLIPPER_MAX_CONCURRENT_JOBS = "2"
   CLIPPER_GLOBAL_FETCHES_PER_HOUR = "60"
   CLIPPER_MAX_CLIP_SECONDS = "300"
   ```
6. Click **Deploy**. The first build installs ffmpeg and the dependencies, which
   takes a few minutes. You can follow the logs from **Manage app** (bottom
   right of the app).
7. After that, every `git push` to `main` redeploys the app automatically.

> ⚠️ **Hosted deployments may be blocked by YouTube.** YouTube often shows
> "Sign in to confirm you're not a bot" to requests from cloud and datacenter
> IP ranges, including Streamlit Cloud and GitHub Actions. When that happens,
> the app shows a clear `AccessBlockedError` message. Running locally is the
> reliable option. **Do not upload your personal YouTube cookies to a public
> deployment.** Anyone using the app would be acting as your account, and the
> account could be flagged.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ffmpeg and ffprobe not found on PATH` | Install ffmpeg (see above) and **open a new terminal**. On Windows, check that `...\ffmpeg\bin` is on PATH, or pass `--ffmpeg-location`. |
| `No JavaScript runtime (deno/node/bun) found` | Run `pip install deno` (included in `requirements.txt`). Node.js works only if it is version ≥ 22. |
| `YouTube blocked this request (bot check or rate limit)` | Common on cloud IPs and VPNs. Wait and retry, run from a home connection, or use `--cookies` (see the next row). |
| `This video is age-restricted…` | You need a signed-in, age-verified account. Export cookies from **your own** logged-in browser, e.g. `yt-dlp --cookies-from-browser firefox --cookies cookies.txt --skip-download "<video URL>"` (Firefox is the most reliable; recent Chrome versions on Windows encrypt cookies in a way yt-dlp may not read), then pass `--cookies cookies.txt`. Treat that file like a password: never commit it (it is in `.gitignore`) and never share it. |
| `Video unavailable: Private video` / members-only | The video can't be accessed without permission. |
| `End (...) is past the end of the video` | The end time is later than the video's length. Use a smaller value. |
| `Live streams cannot be clipped` | Wait for the broadcast to finish and be processed into a regular video. |
| Clip starts a few seconds early | You used `--fast` (keyframe cut). Drop `--fast` for a frame-accurate cut. |
| Precise mode is slow on long clips | Re-encoding takes time roughly proportional to clip length × resolution. Use `--max-height 720` or `--fast`. |
| Download suddenly fails for all videos | YouTube changed something. Run `pip install -U "yt-dlp[default]"`. |

## Design decisions and limitations

- **Section downloading uses yt-dlp's `download_ranges`** instead of
  downloading the full file and then trimming it. yt-dlp passes `-ss`/`-t` to
  ffmpeg as *input* options, so ffmpeg seeks through the remote stream using
  HTTP Range requests. For a 5-minute clip from a 3-hour video, you transfer
  roughly 5 minutes of data. It isn't byte-exact: ffmpeg also reads the stream
  index and data back to the preceding keyframe.
- **Precise by default.** Re-encoding costs CPU, but most users expect a clip
  to start exactly where they asked. `--fast` is available when speed matters
  more.
- **H.264 is preferred for MP4** so the clip previews in every browser,
  including the Streamlit player.
- **Canonical URLs.** Every accepted URL form is reduced to
  `watch?v=<id>`, which removes `list=` and `t=` parameters. This prevents
  whole playlists from being downloaded by accident.
- **Temporary files** live in a private `tempfile.TemporaryDirectory`. Only the
  finished clip is moved to the destination, so partial files never appear
  next to your output.
- **Many users.** See [Serving many users](#serving-many-users).
- **yt-dlp is pinned with a minimum version (`>=`), not an exact version.**
  Exact pins make builds reproducible, but an outdated yt-dlp stops working
  with YouTube within weeks, which is the bigger risk for this tool.

## Legal notice

This tool is for personal, educational, and other lawful uses, such as
clipping your own uploads, Creative Commons content, or short excerpts for
commentary where fair use or fair dealing applies. Downloading content may be
restricted by [YouTube's Terms of Service](https://www.youtube.com/t/terms)
and by copyright law in your country. You are responsible for how you use it.
