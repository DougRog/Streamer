"""
Manages FFmpeg child processes — one per active channel plus
any in-progress recordings.

Key design decisions:
  • Each channel runs exactly one FFmpeg process at a time.
  • Transitions stop the old process then start the new one (brief gap
    is acceptable for SSAI/SCTE splice contexts).
  • SCTE-35 events parsed from FFmpeg stderr are forwarded to scte_handler.
  • When nothing is scheduled on an active channel, a slate (black + tone)
    keeps the multicast stream alive for downstream SSAI equipment.
"""
import os
import re
import signal
import logging
import threading
import subprocess
from datetime import datetime

from config import (
    DEFAULT_VIDEO_WIDTH, DEFAULT_VIDEO_HEIGHT, DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_BITRATE, DEFAULT_AUDIO_BITRATE, DEFAULT_GOP_SIZE,
    DEFAULT_VIDEO_CODEC, DEFAULT_VIDEO_PRESET,
    MULTICAST_TTL, MULTICAST_INTERFACE, RECORDING_PATH, FFMPEG_PATH,
    STREAMER_PID_FILE,
)

logger = logging.getLogger(__name__)

_pid_file_lock = threading.Lock()


def _pid_record(pid):
    with _pid_file_lock:
        try:
            with open(STREAMER_PID_FILE, 'a') as f:
                f.write(f'{pid}\n')
        except OSError:
            pass


def _pid_forget(pid):
    with _pid_file_lock:
        try:
            with open(STREAMER_PID_FILE, 'r') as f:
                lines = f.readlines()
            with open(STREAMER_PID_FILE, 'w') as f:
                for line in lines:
                    if line.strip() != str(pid):
                        f.write(line)
        except OSError:
            pass


def reap_orphans():
    """Kill FFmpeg processes left over from a previous Streamer run.

    Only PIDs recorded in STREAMER_PID_FILE are targeted, and each is
    verified to still be running our specific FFmpeg binary before being
    killed — so unrelated ffmpeg processes on the same host are never
    touched.
    """
    with _pid_file_lock:
        try:
            with open(STREAMER_PID_FILE, 'r') as f:
                pids = [int(l.strip()) for l in f if l.strip().isdigit()]
        except FileNotFoundError:
            return
        finally:
            try:
                os.unlink(STREAMER_PID_FILE)
            except OSError:
                pass

    for pid in pids:
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='replace')
            if FFMPEG_PATH not in cmdline:
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                logger.info(f'Reaped orphan FFmpeg PID={pid}')
            except (ProcessLookupError, OSError):
                pass
        except (FileNotFoundError, OSError):
            pass  # already gone


def _bitrate_val(bitrate_str):
    """Convert '6M' → 6, '500k' → 0.5 for arithmetic."""
    s = str(bitrate_str or '6M').strip().upper()
    if s.endswith('M'):
        return float(s[:-1])
    if s.endswith('K'):
        return float(s[:-1]) / 1000
    return float(s) / 1_000_000


class StreamProcess:
    """Wraps a single FFmpeg subprocess with stderr monitoring."""

    def __init__(self, channel_id, entry_id, command, description=''):
        self.channel_id = channel_id
        self.entry_id = entry_id
        self.command = command
        self.description = description
        self.process = None
        self.started_at = None
        self._log = []
        self._lock = threading.Lock()
        self._app = None   # set by StreamManager before starting

    def start(self, app=None):
        self._app = app
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            self.started_at = datetime.utcnow()
            _pid_record(self.process.pid)
            threading.Thread(target=self._tail_stderr, daemon=True).start()
            logger.info(
                f'[ch{self.channel_id}] FFmpeg PID={self.process.pid} "{self.description}"'
            )
            return True
        except Exception as e:
            logger.error(f'[ch{self.channel_id}] Failed to start FFmpeg: {e}')
            return False

    def stop(self):
        if not (self.process and self.process.poll() is None):
            return
        pid = self.process.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            logger.info(f'[ch{self.channel_id}] FFmpeg PID={pid} stopped')
        except (ProcessLookupError, PermissionError, OSError) as e:
            logger.debug(f'stop error: {e}')
        finally:
            _pid_forget(pid)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def get_log(self, n=30):
        with self._lock:
            return list(self._log[-n:])

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def _tail_stderr(self):
        from scte_handler import parse_ffmpeg_log_line
        # Skip pure FFmpeg progress-stat lines — they're noisy and unhelpful in the log tail
        _skip_re = re.compile(
            r'^\[info\]\s*(?:frame=|fps=|size=|time=|bitrate=|speed=)',
            re.IGNORECASE,
        )
        try:
            for raw in self.process.stderr:
                line = raw.decode('utf-8', errors='replace').rstrip()

                is_scte = 'scte' in line.lower() or 'splice' in line.lower()
                if is_scte and self._app:
                    parse_ffmpeg_log_line(line, self.channel_id, self._app)

                if not _skip_re.match(line):
                    with self._lock:
                        self._log.append(line)
                        if len(self._log) > 200:
                            self._log.pop(0)

                if re.search(r'\berror\b', line, re.IGNORECASE):
                    logger.warning(f'[ch{self.channel_id}] {line}')
        except Exception:
            pass


class StreamManager:
    """Singleton that owns all active FFmpeg processes."""

    def __init__(self):
        self._streams: dict = {}      # channel_id → StreamProcess
        self._recordings: dict = {}   # recording_id → StreamProcess
        self._lock = threading.Lock()
        self._app = None

    def init_app(self, app):
        self._app = app
        reap_orphans()

    # ------------------------------------------------------------------ #
    #  Channel streams                                                     #
    # ------------------------------------------------------------------ #

    def start_file_stream(self, channel, entry, occurrence_start):
        loop = bool(getattr(entry, 'loop_enabled', False))
        file_dur = None
        if loop and entry.asset_path:
            try:
                from media_pool import probe_file, _extract_asset_info
                probe_data = probe_file(entry.asset_path)
                if probe_data:
                    file_dur = _extract_asset_info(entry.asset_path, probe_data).get('duration_seconds')
            except Exception:
                pass
        cmd = self._file_cmd(channel, entry.asset_path, occurrence_start,
                             entry.duration, loop=loop, file_duration=file_dur)
        desc = f'File[loop]: {entry.title}' if loop else f'File: {entry.title}'
        return self._replace_channel_stream(channel.id, entry.id, cmd, desc)

    def start_live_stream(self, channel, entry):
        from dektec_handler import get_ffmpeg_input_args
        args = get_ffmpeg_input_args(entry.live_source)
        if args is None:
            logger.error('No live capture device available')
            return False
        cmd = self._live_cmd(channel, args)
        return self._replace_channel_stream(channel.id, entry.id, cmd,
                                            f'Live: {entry.title}')

    def start_recording_stream(self, channel, entry):
        """Live SDI → transcode to multicast AND record all streams to MXF."""
        from dektec_handler import get_ffmpeg_input_args
        args = get_ffmpeg_input_args(entry.live_source)
        if args is None:
            logger.error('No live capture device available')
            return False

        rec_path = (getattr(entry, 'recording_path', None) or '').strip()
        if not rec_path:
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            rec_path = os.path.join(RECORDING_PATH, f'recording_{ts}')
        if not rec_path.lower().endswith(('.ts', '.mts')):
            rec_path += '.ts'

        os.makedirs(os.path.dirname(os.path.abspath(rec_path)), exist_ok=True)
        cmd = self._recording_cmd(args, rec_path, channel)
        return self._replace_channel_stream(channel.id, entry.id, cmd,
                                            f'RecordAir: {entry.title} → {rec_path}')

    def start_slate(self, channel):
        """Black+tone slate keeps the multicast stream alive when nothing is scheduled."""
        cmd = self._slate_cmd(channel)
        return self._replace_channel_stream(channel.id, -1, cmd, 'Slate')

    def stop_channel(self, channel_id):
        with self._lock:
            proc = self._streams.pop(channel_id, None)
        if proc:
            proc.stop()

    # ------------------------------------------------------------------ #
    #  Recording                                                           #
    # ------------------------------------------------------------------ #

    def start_recording(self, live_source, channel=None):
        """
        Record from live_source to a timestamped MXF in RECORDING_PATH.
        If channel is provided the same input is also transcoded to multicast.
        Returns (ok, path_or_error).
        """
        from dektec_handler import get_ffmpeg_input_args
        args = get_ffmpeg_input_args(live_source)
        if args is None:
            return False, 'No live capture device'

        os.makedirs(RECORDING_PATH, exist_ok=True)
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        out_path = os.path.join(RECORDING_PATH, f'recording_{ts}.ts')
        cmd = self._recording_cmd(args, out_path, channel)
        rec_id = f'rec_{ts}'
        # channel_id=None so any SCTE-35 events are stored with NULL channel_id
        # rather than an invalid -1 that violates the FK constraint
        proc = StreamProcess(None, -1, cmd, f'Recording → {out_path}')
        if proc.start(self._app):
            with self._lock:
                self._recordings[rec_id] = proc
            return True, out_path
        return False, 'FFmpeg failed to start'

    def stop_recording(self, rec_id=None):
        with self._lock:
            if rec_id:
                proc = self._recordings.pop(rec_id, None)
            else:
                # Stop the most recently started recording
                if self._recordings:
                    rec_id, proc = next(reversed(self._recordings.items()))
                    del self._recordings[rec_id]
                else:
                    proc = None
        if proc:
            proc.stop()
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    def get_channel_info(self, channel_id):
        with self._lock:
            proc = self._streams.get(channel_id)
        if proc and proc.is_running():
            return {
                'running': True,
                'pid': proc.pid,
                'entry_id': proc.entry_id,
                'description': proc.description,
                'started_at': proc.started_at.isoformat() if proc.started_at else None,
                'log': proc.get_log(5),
            }
        return {'running': False}

    def active_channel_ids(self):
        with self._lock:
            return [cid for cid, p in self._streams.items() if p.is_running()]

    def active_recordings(self):
        with self._lock:
            return {
                rid: {
                    'pid': p.pid,
                    'description': p.description,
                    'started_at': p.started_at.isoformat() if p.started_at else None,
                }
                for rid, p in self._recordings.items()
                if p.is_running()
            }

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _replace_channel_stream(self, channel_id, entry_id, cmd, desc):
        self.stop_channel(channel_id)
        proc = StreamProcess(channel_id, entry_id, cmd, desc)
        if proc.start(self._app):
            with self._lock:
                self._streams[channel_id] = proc
            return True
        return False

    def _common_video_args(self, channel):
        br = channel.video_bitrate or DEFAULT_VIDEO_BITRATE
        bv = _bitrate_val(br)
        maxrate = f'{bv * 1.5:.1f}M'
        bufsize = f'{bv * 3:.1f}M'
        args = [
            '-vf', f'scale={DEFAULT_VIDEO_WIDTH}:{DEFAULT_VIDEO_HEIGHT}:flags=lanczos,'
                   f'fps={DEFAULT_VIDEO_FPS}',
            '-c:v', DEFAULT_VIDEO_CODEC,
        ]
        if DEFAULT_VIDEO_PRESET:
            args += ['-preset', DEFAULT_VIDEO_PRESET]
        args += ['-b:v', br, '-maxrate', maxrate, '-bufsize', bufsize]
        # x264opts is libx264-specific — skip for other codecs
        if DEFAULT_VIDEO_CODEC == 'libx264':
            args += ['-x264opts',
                     f'keyint={DEFAULT_GOP_SIZE}:min-keyint={DEFAULT_GOP_SIZE}:scenecut=-1']
        args += [
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', DEFAULT_AUDIO_BITRATE, '-ar', '48000',
            '-c:d', 'copy',
        ]
        return args

    def _multicast_url(self, channel):
        url = (f'udp://{channel.multicast_addr}:{channel.multicast_port}'
               f'?pkt_size=1316&ttl={MULTICAST_TTL}&reuse=1&overrun_nonfatal=1')
        if MULTICAST_INTERFACE:
            url += f'&localaddr={MULTICAST_INTERFACE}'
        return url

    def _mpegts_out_args(self, channel):
        """
        Stable MPEG-TS output args with fixed PIDs and periodic PAT/PMT.

        Fixed PIDs ensure VLC doesn't lose the stream when FFmpeg restarts for
        a content transition.  resend_headers re-injects PAT/PMT before every
        discontinuity so VLC re-locks within one PAT period (~100 ms).

          PID 0      — PAT  (fixed by MPEG-TS standard)
          PID 4096   — PMT
          PID 256    — video elementary stream
          PID 257    — audio elementary stream (+ any data PIDs)
        """
        return [
            '-f', 'mpegts',
            '-mpegts_service_id', '1',
            '-mpegts_pmt_start_pid', '4096',
            '-mpegts_start_pid', '256',
            '-mpegts_flags', 'resend_headers',
            self._multicast_url(channel),
        ]

    def _file_cmd(self, channel, asset_path, occurrence_start, duration,
                  loop=False, file_duration=None):
        now = datetime.utcnow()
        slot_elapsed = max(0.0, (now - occurrence_start).total_seconds())

        if loop:
            fd = file_duration or duration
            seek = (slot_elapsed % fd) if fd > 0 else 0.0
        else:
            seek = slot_elapsed

        cmd = [FFMPEG_PATH, '-hide_banner', '-nostdin', '-loglevel', 'level+info']
        if loop:
            cmd += ['-stream_loop', '-1']
        if seek > 2.0:
            cmd += ['-ss', f'{seek:.3f}']
        cmd += ['-re', '-copyts', '-i', asset_path]
        # Map video, audio, optional data (SCTE-35)
        cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
        cmd += self._common_video_args(channel)
        cmd += self._mpegts_out_args(channel)
        return cmd

    def _live_cmd(self, channel, input_args):
        cmd = [FFMPEG_PATH, '-hide_banner', '-nostdin', '-loglevel', 'level+info']
        cmd += input_args
        cmd += ['-copyts']
        cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
        cmd += self._common_video_args(channel)
        cmd += self._mpegts_out_args(channel)
        return cmd

    def _recording_cmd(self, input_args, out_path, channel=None):
        # MPEG-TS is used for recording — it preserves all streams verbatim
        # (SCTE-35, all audio tracks, CC) and is the native container for live
        # broadcast ingest.  MXF would fail on data/SCTE-35 streams with -c copy.
        cmd = [FFMPEG_PATH, '-hide_banner', '-nostdin', '-loglevel', 'level+info']
        cmd += input_args
        cmd += ['-copyts']

        if channel:
            # Output 1: transcoded multicast (re-encoded A/V + pass-through data)
            cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
            cmd += self._common_video_args(channel)
            cmd += self._mpegts_out_args(channel)
            # Output 2: raw MPEG-TS file — all streams verbatim, SCTE-35 intact
            cmd += ['-map', '0', '-c', 'copy', '-f', 'mpegts', out_path]
        else:
            # Record only — all streams verbatim
            cmd += ['-map', '0', '-c', 'copy', '-f', 'mpegts', out_path]

        return cmd

    def _slate_cmd(self, channel):
        """FFmpeg slate → multicast. Uses a looped file if configured, else black+tone."""
        slate_type  = getattr(channel, 'slate_type', 'color') or 'color'
        slate_path  = getattr(channel, 'slate_asset_path', '') or ''

        if slate_type == 'file' and slate_path:
            cmd = [
                FFMPEG_PATH, '-hide_banner', '-loglevel', 'error',
                '-stream_loop', '-1',
                '-re', '-i', slate_path,
                '-map', '0:v:0', '-map', '0:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '500k',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '64k', '-ar', '48000',
            ]
            cmd += self._mpegts_out_args(channel)
            return cmd

        # Default: black frame + 400 Hz tone
        cmd = [
            FFMPEG_PATH, '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi',
            '-i', f'color=c=black:s={DEFAULT_VIDEO_WIDTH}x{DEFAULT_VIDEO_HEIGHT}'
                  f':r={DEFAULT_VIDEO_FPS}',
            '-f', 'lavfi', '-i', 'sine=frequency=400:sample_rate=48000',
            '-map', '0:v', '-map', '1:a',
            '-c:v', DEFAULT_VIDEO_CODEC,
        ]
        if DEFAULT_VIDEO_PRESET:
            cmd += ['-preset', DEFAULT_VIDEO_PRESET]
        cmd += [
            '-b:v', '500k',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '64k', '-ar', '48000',
        ]
        cmd += self._mpegts_out_args(channel)
        return cmd


# Module-level singleton
stream_manager = StreamManager()
