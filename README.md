# Streamer MCR

A Python/Flask broadcast playout system for master control rooms. Schedules
MXF and live SDI sources to multicast MPEG-TS outputs at 720p 59.94 fps,
with SCTE-35 passthrough, a web calendar interface, and automated EPG/M3U
SFTP upload.

---

## Features

- **Multi-channel playout** — create as many channels as needed, each with its
  own multicast address and bitrate
- **File playout** — MXF, MP4, MOV, MTS, M2TS, TS from a configurable media
  pool; SCTE-35 data streams passed through verbatim
- **Live SDI input** — Dektec, DeckLink, or V4L2 capture with auto-detection
  fallback chain
- **Live recording** — record SDI input to MXF while simultaneously streaming
  to multicast; SCTE-35 preserved
- **Recurring schedules** — iCalendar RRULE strings (e.g. `FREQ=WEEKLY;BYDAY=MO,WE,FR`)
  with per-occurrence exdates and overrides
- **Repeat-day** — promote a full day's one-time schedule to weekly repeat in
  one click
- **Slate** — keeps the multicast alive with a black/silent stream when nothing
  is scheduled
- **XMLTV EPG** — rolling window (default 7 days), uploaded automatically on a
  configurable interval
- **M3U playlist** — auto-generated with TVG metadata for IPTV clients
- **SFTP upload** — EPG XML + M3U written to a remote server via paramiko
- **Media Library** — scans the media pool, shows codec, resolution, duration,
  and SCTE-35 presence
- **Web UI** — Bootstrap 5 dark theme, FullCalendar 6 with drag-drop scheduling

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.9+ | f-strings, `ET.indent`, `datetime.fromisoformat` |
| FFmpeg | Must be on `PATH`; libx264 and AAC support required |
| paramiko ≥ 3.4 | SFTP upload |
| Dektec SDK (optional) | Only needed for Dektec SDI capture |

Python packages are listed in `requirements.txt`.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) set environment variables — see Configuration below
export MEDIA_POOL_PATH=/mnt/MasterControlMedia
export PORT=5001

# 3. Run
python run.py
```

Open `http://localhost:5001` in a browser.

The SQLite database (`streamer.db`) is created automatically on first run.

---

## Configuration

All settings have defaults. Override via environment variables or via the
**Settings** page in the UI (stored in the database; env vars always take
priority).

### Core

| Variable | Default | Description |
|---|---|---|
| `MEDIA_POOL_PATH` | `/mnt/MasterControlMedia` | Root directory scanned for media files |
| `RECORDING_PATH` | `/mnt/MasterControlMedia/recordings` | MXF recording output |
| `DATABASE_URL` | `sqlite:///streamer.db` | SQLAlchemy connection string |
| `SECRET_KEY` | `change-me-in-production-abc123` | Flask session secret — change this |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `5001` | HTTP port |
| `DEBUG` | `false` | Flask debug mode (`true`/`false`) |

### Video Output

| Variable | Default | Description |
|---|---|---|
| `MULTICAST_TTL` | `4` | Multicast TTL |

Output is fixed at **1280×720, 59.94 fps, libx264, AAC 192k** per channel.
Per-channel bitrate is configurable in the Channels UI (default `6M`).

### SDI Capture

| Variable | Default | Description |
|---|---|---|
| `DEKTEC_INPUT_URI` | `dektec:0:0` | Override the auto-detected capture source |
| `DEKTEC_FFMPEG_FORMAT` | `dektec` | FFmpeg `-f` format for Dektec input |

Auto-detection order: Dektec → DeckLink → V4L2 → `DEKTEC_INPUT_URI`.

### SFTP / EPG

These can also be set via the **Settings** page in the web UI.

| Variable | Default | Description |
|---|---|---|
| `SFTP_HOST` | _(empty)_ | SFTP server hostname or IP |
| `SFTP_PORT` | `22` | SFTP port |
| `SFTP_USERNAME` | _(empty)_ | SFTP username |
| `SFTP_PASSWORD` | _(empty)_ | SFTP password |
| `SFTP_PATH` | `/` | Remote directory for `epg.xml` and `playlist.m3u` |
| `EPG_WINDOW_DAYS` | `7` | Days of EPG to generate |
| `EPG_INTERVAL_MINUTES` | `30` | Auto-upload interval (0 = disabled) |
| `EPG_SOURCE_NAME` | `Streamer MCR` | Station name in XMLTV and M3U |
| `EPG_PUBLIC_URL` | _(empty)_ | Public URL of `epg.xml` (used in M3U `x-tvg-url`) |

---

## Architecture

```
run.py
└── app.py  (Flask factory, all routes)
    ├── models.py          Channel, ScheduleEntry, MediaAsset, SCTEEvent, AppSetting
    ├── stream_manager.py  FFmpeg subprocess lifecycle, one process per channel
    ├── scheduler.py       Background thread — ticks every 5 s, drives playout
    ├── media_pool.py      Watchdog file scanner, ffprobe metadata extraction
    ├── dektec_handler.py  SDI capture source detection
    ├── scte_handler.py    SCTE-35 parser (FFmpeg log + threefive)
    ├── epg_generator.py   XMLTV and M3U generation
    └── sftp_uploader.py   paramiko SFTP upload
```

### Scheduling model

- Each `ScheduleEntry` belongs to a channel and has a `start_time`, `duration`,
  and optional `rrule` (iCalendar RRULE string).
- Override entries have `parent_id` + `override_date`; they shadow the parent
  occurrence for that specific date.
- Exdates (JSON list of date strings) skip individual recurrences without
  creating an override.

### Playout loop

Every 5 seconds the scheduler calls `get_current_item()` for each active
channel, compares the result against what FFmpeg is currently playing, and
starts a new process if they differ. A crashed FFmpeg process is automatically
restarted.

### SCTE-35

File streams use `-map 0:d? -c:d copy` so data PID streams (including SCTE-35)
are passed through unchanged into the MPEG-TS output. Events are also parsed
from FFmpeg stderr and stored in the `scte_events` table.

---

## API Reference

### Channels

| Method | Path | Description |
|---|---|---|
| GET | `/api/channels` | List all channels |
| POST | `/api/channels` | Create channel |
| GET/PUT/DELETE | `/api/channels/<id>` | Get / update / delete |
| POST | `/api/channels/<id>/start` | Mark active (scheduler will start playout) |
| POST | `/api/channels/<id>/stop` | Mark inactive, stop FFmpeg |
| GET | `/api/channels/<id>/stream` | Current FFmpeg status + log tail |

### Schedule

| Method | Path | Description |
|---|---|---|
| GET | `/api/schedule?start=&end=` | FullCalendar event list |
| POST | `/api/schedule` | Create entry |
| GET/PUT/DELETE | `/api/schedule/<id>` | Get / update / delete |
| POST | `/api/schedule/<id>/override` | Create per-day override |
| POST | `/api/schedule/repeat-day` | Apply `FREQ=WEEKLY` to all one-time entries on a day |

### EPG & SFTP

| Method | Path | Description |
|---|---|---|
| GET | `/api/epg` | Download XMLTV XML (`?days=N`) |
| GET | `/api/epg/m3u` | Download M3U playlist |
| POST | `/api/epg/upload` | Trigger immediate SFTP upload (background) |
| GET | `/api/epg/status` | Last upload status |
| POST | `/api/sftp/test` | Test SFTP connectivity |

### Settings

| Method | Path | Description |
|---|---|---|
| GET | `/api/settings` | All settings (password masked) |
| POST | `/api/settings` | Update settings |

### Media Library

| Method | Path | Description |
|---|---|---|
| GET | `/api/assets` | List media assets (`?q=` for search) |
| POST | `/api/assets/scan` | Trigger media pool rescan |
| GET | `/api/assets/scan/status` | Scan progress |

---

## Pages

| URL | Description |
|---|---|
| `/` | Dashboard — channel status, EPG panel, SCTE-35 log |
| `/calendar` | FullCalendar schedule view with drag-drop editing |
| `/channels` | Channel management |
| `/library` | Media Library |
| `/settings` | SFTP and EPG configuration |
