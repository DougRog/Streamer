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
)

logger = logging.getLogger(__name__)


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
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            logger.info(f'[ch{self.channel_id}] FFmpeg PID={self.process.pid} stopped')
        except (ProcessLookupError, PermissionError, OSError) as e:
            logger.debug(f'stop error: {e}')

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
        try:
            for raw in self.process.stderr:
                line = raw.decode('utf-8', errors='replace').rstrip()
                with self._lock:
                    self._log.append(line)
                    if len(self._log) > 200:
                        self._log.pop(0)

                if self._app and ('scte' in line.lower() or 'splice' in line.lower()):
                    parse_ffmpeg_log_line(line, self.channel_id, self._app)

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

    # ------------------------------------------------------------------ #
    #  Channel streams                                                     #
    # ------------------------------------------------------------------ #

    def start_file_stream(self, channel, entry, occurrence_start):
        loop = bool(getattr(entry, 'loop_enabled', False))
        file_dur = None
        if loop and entry.asset_path and self._app:
            try:
                with self._app.app_context():
                    from models import MediaAsset
                    asset = MediaAsset.query.filter_by(path=entry.asset_path).first()
                    if asset and asset.duration_seconds:
                        file_dur = asset.duration_seconds
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
        out_path = os.path.join(RECORDING_PATH, f'recording_{ts}.mxf')
        cmd = self._recording_cmd(args, out_path, channel)
        rec_id = f'rec_{ts}'
        proc = StreamProcess(-1, -1, cmd, f'Recording → {out_path}')
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
               f'?pkt_size=1316&ttl={MULTICAST_TTL}&reuse=1')
        if MULTICAST_INTERFACE:
            url += f'&localaddr={MULTICAST_INTERFACE}'
        return url

    def _file_cmd(self, channel, asset_path, occurrence_start, duration,
                  loop=False, file_duration=None):
        now = datetime.utcnow()
        slot_elapsed = max(0.0, (now - occurrence_start).total_seconds())

        if loop:
            fd = file_duration or duration
            seek = (slot_elapsed % fd) if fd > 0 else 0.0
        else:
            seek = slot_elapsed

        cmd = [FFMPEG_PATH, '-hide_banner', '-loglevel', 'level+warning']
        if loop:
            cmd += ['-stream_loop', '-1']
        if seek > 2.0:
            cmd += ['-ss', f'{seek:.3f}']
        cmd += ['-re', '-copyts', '-i', asset_path]
        # Map video, audio, optional data (SCTE-35)
        cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
        cmd += self._common_video_args(channel)
        cmd += ['-f', 'mpegts', self._multicast_url(channel)]
        return cmd

    def _live_cmd(self, channel, input_args):
        cmd = [FFMPEG_PATH, '-hide_banner', '-loglevel', 'level+warning']
        cmd += input_args
        cmd += ['-copyts']
        cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
        cmd += self._common_video_args(channel)
        cmd += ['-f', 'mpegts', self._multicast_url(channel)]
        return cmd

    def _recording_cmd(self, input_args, out_path, channel=None):
        cmd = [FFMPEG_PATH, '-hide_banner', '-loglevel', 'level+warning']
        cmd += input_args

        if channel:
            # Output 1: transcoded multicast
            cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
            cmd += self._common_video_args(channel)
            cmd += ['-f', 'mpegts', self._multicast_url(channel)]
            # Output 2: lossless MXF record preserving SCTE-35
            cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map', '0:d?']
            cmd += ['-c:v', 'copy', '-c:a', 'copy', '-c:d', 'copy']
            cmd += ['-f', 'mxf', out_path]
        else:
            # Record only — copy everything verbatim
            cmd += ['-map', '0', '-c', 'copy', '-f', 'mxf', out_path]

        return cmd

    def _slate_cmd(self, channel):
        """FFmpeg slate → multicast. Uses a looped file if configured, else black+tone."""
        slate_type  = getattr(channel, 'slate_type', 'color') or 'color'
        slate_path  = getattr(channel, 'slate_asset_path', '') or ''

        if slate_type == 'file' and slate_path:
            return [
                FFMPEG_PATH, '-hide_banner', '-loglevel', 'error',
                '-stream_loop', '-1',
                '-re', '-i', slate_path,
                '-map', '0:v:0', '-map', '0:a:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', '500k',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '64k', '-ar', '48000',
                '-f', 'mpegts', self._multicast_url(channel),
            ]

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
            '-f', 'mpegts', self._multicast_url(channel),
        ]
        return cmd


# Module-level singleton
stream_manager = StreamManager()
