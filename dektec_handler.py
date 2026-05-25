"""
Dektec SDI input integration.

Detection strategy (tried in order):
  1. DEKTEC_INPUT_URI config (e.g. 'dektec:0:0' or '0:0' or a serial:port)
  2. Auto-detect 'dektec' device in the configured FFmpeg build
  3. 'decklink' FFmpeg device — Blackmagic fallback
  4. V4L2 /dev/videoN        — generic capture fallback

FFmpeg DekTec input form:  -f dektec -i <card_index_or_serial>:<port>
"""
import glob
import subprocess
import logging
import os
import time
from config import DEKTEC_INPUT_URI, DEKTEC_FFMPEG_FORMAT, FFMPEG_PATH

logger = logging.getLogger(__name__)

# Cached detection result: None = not checked, False = not found
_cached_format = None
_cached_uri    = None
_cache_ready   = False

# Cached input list (re-scanned at most once per 60 s)
_inputs_cache      = None
_inputs_cache_time = 0.0


def reset_cache():
    """Invalidate all cached detection results (call after user changes input)."""
    global _cached_format, _cached_uri, _cache_ready, _inputs_cache, _inputs_cache_time
    _cached_format = None
    _cached_uri    = None
    _cache_ready   = False
    _inputs_cache  = None
    _inputs_cache_time = 0.0


def _ffmpeg(*args, timeout=5):
    """Run the configured FFmpeg binary and return combined stdout+stderr."""
    try:
        r = subprocess.run(
            [FFMPEG_PATH] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout + r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''


def _device_available(fmt):
    """Return True if the configured FFmpeg build lists fmt as an input device."""
    output = _ffmpeg('-hide_banner', '-devices')
    return fmt in output


def _find_v4l2_device():
    for i in range(8):
        dev = f'/dev/video{i}'
        if os.path.exists(dev):
            return dev
    return None


def detect_input():
    """
    Return (ffmpeg_format, input_uri) for the best available capture source.
    ffmpeg_format may be None for stream URIs.
    Results are cached after first call.
    """
    global _cached_format, _cached_uri, _cache_ready
    if _cache_ready:
        return _cached_format, _cached_uri

    # ------------------------------------------------------------------ #
    # 1. DEKTEC_INPUT_URI config                                          #
    # ------------------------------------------------------------------ #
    uri = (DEKTEC_INPUT_URI or '').strip()
    if uri:
        # Stream URL (udp://, rtp://, etc.) — no device format needed
        if uri.startswith(('udp://', 'rtp://', 'rtmp://', 'srt://')):
            _cached_format, _cached_uri = None, uri
            _cache_ready = True
            logger.info(f'Using stream URI from config: {uri}')
            return _cached_format, _cached_uri

        # 'dektec:card:port' or 'dektec:serial:port'
        if uri.lower().startswith('dektec:'):
            card_port = uri[len('dektec:'):]   # e.g. '0:0' or '2215330929817:0'
            _cached_format, _cached_uri = 'dektec', card_port
            _cache_ready = True
            logger.info(f'DekTec input from config: -f dektec -i {card_port}')
            return _cached_format, _cached_uri

        # Bare 'card:port' (e.g. '0:0')
        if ':' in uri and not uri.startswith('/'):
            _cached_format, _cached_uri = DEKTEC_FFMPEG_FORMAT or 'dektec', uri
            _cache_ready = True
            logger.info(f'DekTec input from config (bare): -f {_cached_format} -i {uri}')
            return _cached_format, _cached_uri

    # ------------------------------------------------------------------ #
    # 2. Auto-detect via FFmpeg device list                               #
    # ------------------------------------------------------------------ #
    if _device_available('dektec'):
        _cached_format, _cached_uri = 'dektec', '0:0'
        _cache_ready = True
        logger.info('DekTec FFmpeg device auto-detected')
        return _cached_format, _cached_uri

    if _device_available('decklink'):
        _cached_format, _cached_uri = 'decklink', 'DeckLink SDI'
        _cache_ready = True
        logger.info('DeckLink device detected as DekTec fallback')
        return _cached_format, _cached_uri

    v4l2 = _find_v4l2_device()
    if v4l2:
        _cached_format, _cached_uri = 'v4l2', v4l2
        _cache_ready = True
        logger.warning(f'Using V4L2 fallback: {v4l2}')
        return _cached_format, _cached_uri

    logger.warning('No live capture device found')
    _cached_format, _cached_uri = None, None
    _cache_ready = True
    return None, None


def get_ffmpeg_input_args(source_hint=None):
    """
    Return the list of FFmpeg args needed to open the live input, e.g.:
      ['-f', 'dektec', '-i', '0:0']
    source_hint overrides auto-detection when provided.
    """
    hint = (source_hint or '').strip()

    # Stream URL passed directly
    if hint.startswith(('udp://', 'rtp://', 'rtmp://', 'srt://')):
        return ['-i', hint]

    # Explicit 'dektec:card:port'
    if hint.lower().startswith('dektec:'):
        card_port = hint[len('dektec:'):]
        return ['-f', 'dektec', '-i', card_port]

    # Bare 'card:port' with optional format prefix stripped
    if hint and ':' in hint and not hint.startswith('/'):
        fmt = DEKTEC_FFMPEG_FORMAT or 'dektec'
        return ['-f', fmt, '-i', hint]

    # No usable hint — fall back to detection
    fmt, uri = detect_input()
    if not uri:
        return None
    if fmt:
        return ['-f', fmt, '-i', uri]
    return ['-i', uri]


def dektec_status():
    """Return a dict describing detected capture hardware."""
    fmt, uri = detect_input()
    return {
        'detected': bool(uri),
        'format': fmt,
        'uri': uri,
        'config_uri': DEKTEC_INPUT_URI,
        'ffmpeg_path': FFMPEG_PATH,
    }


# Keep old name for any callers that imported it directly
def status():
    return dektec_status()


def _probe_dektec_port(card, port):
    """
    Try to open DekTec card:port briefly via FFmpeg.
    Returns ('present', has_signal) or ('absent', False).

    TimeoutExpired → device opened but hung waiting for signal → present.
    Quick exit with a 'no device' error message → absent.
    Quick exit without error → signal was present and brief read succeeded.
    """
    try:
        r = subprocess.run(
            [FFMPEG_PATH, '-hide_banner', '-loglevel', 'verbose',
             '-f', 'dektec', '-i', f'{card}:{port}',
             '-t', '0.5', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=5
        )
        out = (r.stdout + r.stderr).lower()
        absent_keywords = (
            'no dta device', 'invalid device', 'no device found',
            'cannot open device', 'device not found',
            'dta error: no', 'dta error: invalid',
        )
        if any(kw in out for kw in absent_keywords):
            return 'absent', False
        return 'present', (r.returncode == 0)
    except subprocess.TimeoutExpired:
        # FFmpeg opened the device but hung waiting for signal — device is present
        return 'present', False
    except (FileNotFoundError, OSError):
        return 'absent', False


def list_inputs():
    """
    Return a list of detected SDI/capture input candidates, cached for 60 s.
    Each entry: {'label': str, 'value': str, 'type': str, 'signal': bool|None}

    DekTec ports are discovered by probing card 0-1 port 0-7 via FFmpeg.
    Non-existent ports return immediately; present-but-no-signal ports hit
    the 5 s timeout — so worst case is about 5 s per present-no-signal port.
    """
    global _inputs_cache, _inputs_cache_time

    if _inputs_cache is not None and (time.time() - _inputs_cache_time) < 60:
        return _inputs_cache

    inputs = []

    if _device_available('dektec'):
        for card in range(4):
            card_found = False
            for port in range(8):
                status, has_signal = _probe_dektec_port(card, port)
                if status == 'absent':
                    break  # no more ports on this card
                card_found = True
                label = f'DekTec card {card} port {port}'
                if has_signal:
                    label += ' — signal detected'
                inputs.append({
                    'label': label,
                    'value': f'dektec:{card}:{port}',
                    'type': 'dektec',
                    'signal': has_signal,
                })
            if not card_found:
                break  # no more cards

    if _device_available('decklink'):
        inputs.append({
            'label': 'Blackmagic DeckLink SDI',
            'value': 'decklink:DeckLink SDI',
            'type': 'decklink',
            'signal': None,
        })

    v4l2 = _find_v4l2_device()
    if v4l2:
        inputs.append({
            'label': f'V4L2 {v4l2}',
            'value': v4l2,
            'type': 'v4l2',
            'signal': None,
        })

    _inputs_cache = inputs
    _inputs_cache_time = time.time()
    return inputs
