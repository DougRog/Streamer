"""
On-demand file probing via ffprobe. Called when a file is added to the schedule,
not via background scanning.
"""
import os
import json
import shutil
import logging
import subprocess
from config import FFPROBE_PATH

logger = logging.getLogger(__name__)


def _ffprobe_bin():
    if os.path.isfile(FFPROBE_PATH) and os.access(FFPROBE_PATH, os.X_OK):
        return FFPROBE_PATH
    return shutil.which('ffprobe')


def probe_file(path):
    """Run ffprobe and return parsed info dict, or None on failure."""
    ffprobe = _ffprobe_bin()
    if not ffprobe:
        logger.error(f'ffprobe not found at {FFPROBE_PATH!r} or in PATH')
        return None

    cmd = [
        ffprobe, '-v', 'quiet',
        '-print_format', 'json',
        '-show_format', '-show_streams',
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            err = (result.stderr or '').strip()[:200]
            logger.warning(f'ffprobe exit {result.returncode} for {os.path.basename(path)}: {err}')
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning(f'ffprobe timed out for {os.path.basename(path)}')
        return None
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning(f'ffprobe error for {os.path.basename(path)}: {e}')
        return None


def _has_scte(probe_data):
    for stream in probe_data.get('streams', []):
        codec = stream.get('codec_name', '').lower()
        codec_tag = stream.get('codec_tag_string', '').lower()
        if 'scte' in codec or 'scte' in codec_tag:
            return True
        if stream.get('codec_type') == 'data':
            return True
    return False


def _extract_asset_info(path, probe_data):
    """Map ffprobe output → dict of media metadata."""
    fmt = probe_data.get('format', {})
    streams = probe_data.get('streams', [])

    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)

    fps_str = width = height = color_space = None
    if video:
        r_frame_rate = video.get('r_frame_rate', '')
        if '/' in r_frame_rate:
            num, den = r_frame_rate.split('/')
            if int(den) > 0:
                fps_str = f'{int(num) / int(den):.3f}'
        width = video.get('width')
        height = video.get('height')
        color_space = video.get('color_space')

    duration = None
    raw_dur = fmt.get('duration') or (video.get('duration') if video else None)
    if raw_dur:
        try:
            duration = float(raw_dur)
        except ValueError:
            pass

    return {
        'duration_seconds': duration,
        'has_scte':         _has_scte(probe_data),
        'video_codec':      video.get('codec_name') if video else None,
        'audio_codec':      audio.get('codec_name') if audio else None,
        'video_width':      width,
        'video_height':     height,
        'video_fps':        fps_str,
        'color_space':      color_space,
    }
