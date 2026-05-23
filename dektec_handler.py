"""
Dektec SDI input integration.

Detection strategy (tried in order):
  1. 'dektec' ffmpeg device  — requires ffmpeg built with DtAPI / dtavcinput
  2. 'decklink' ffmpeg device — Blackmagic fallback if available
  3. V4L2 /dev/videoN        — generic capture fallback
  4. User-supplied URI        — from DEKTEC_INPUT_URI env var

The resolved input URI is cached after first detection so repeated calls
are cheap.
"""
import subprocess
import logging
import os
from config import DEKTEC_INPUT_URI, DEKTEC_FFMPEG_FORMAT

logger = logging.getLogger(__name__)

# Cached detection result: None = not checked, False = not found, str = URI
_cached_input_uri = None
_cached_format = None


def _check_ffmpeg_device(fmt):
    """Return True if ffmpeg lists 'fmt' as an available input device."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-devices'],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
        return fmt in output
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _find_v4l2_device():
    """Return first available V4L2 device path, or None."""
    for i in range(8):
        dev = f'/dev/video{i}'
        if os.path.exists(dev):
            return dev
    return None


def detect_input():
    """
    Auto-detect the best available capture source.
    Returns (ffmpeg_format, input_uri) or (None, None).
    """
    global _cached_input_uri, _cached_format

    if _cached_input_uri is not None:
        return _cached_format, _cached_input_uri

    # User-provided explicit URI always wins
    if DEKTEC_INPUT_URI and ':' in DEKTEC_INPUT_URI:
        # Looks like 'dektec:0:0' or 'udp://...'
        if DEKTEC_INPUT_URI.startswith(('udp://', 'rtp://', 'rtmp://', 'srt://')):
            _cached_format = None
            _cached_input_uri = DEKTEC_INPUT_URI
            return _cached_format, _cached_input_uri

    # Try Dektec FFmpeg device
    if _check_ffmpeg_device('dektec'):
        _cached_format = 'dektec'
        _cached_input_uri = '0:0'   # card:port
        logger.info('Dektec FFmpeg device detected')
        return _cached_format, _cached_input_uri

    # Try Blackmagic DeckLink
    if _check_ffmpeg_device('decklink'):
        _cached_format = 'decklink'
        _cached_input_uri = 'DeckLink SDI'
        logger.info('DeckLink device detected as Dektec fallback')
        return _cached_format, _cached_input_uri

    # Try V4L2
    v4l2_dev = _find_v4l2_device()
    if v4l2_dev:
        _cached_format = 'v4l2'
        _cached_input_uri = v4l2_dev
        logger.warning(f'Using V4L2 fallback: {v4l2_dev}')
        return _cached_format, _cached_input_uri

    logger.warning('No live capture device found')
    _cached_input_uri = False
    return None, None


def get_dektec_input_uri(source_hint=None):
    """
    Return a URI string suitable for `ffmpeg -i URI`.
    source_hint may be 'dektec:0:0', 'decklink:0', '/dev/video0', etc.
    If None, auto-detect.
    """
    if source_hint:
        # Already a full URI
        if source_hint.startswith(('udp://', 'rtp://', 'rtmp://', 'srt://', '/')):
            return source_hint
        # 'dektec:card:port' style
        if source_hint.startswith('dektec:'):
            return source_hint.split(':', 1)[1]  # strip the 'dektec:' prefix

    fmt, uri = detect_input()
    return uri if uri else None


def get_ffmpeg_input_args(source_hint=None):
    """
    Return the list of FFmpeg args needed to open the live input.
    e.g. ['-f', 'dektec', '-i', '0:0'] or ['-i', 'udp://...']
    """
    if source_hint and source_hint.startswith(('udp://', 'rtp://', 'rtmp://', 'srt://')):
        return ['-i', source_hint]

    fmt, uri = detect_input()
    if not fmt and not uri:
        return None

    if fmt:
        return ['-f', fmt, '-i', uri]
    return ['-i', uri]


def status():
    """Return a dict describing detected capture hardware."""
    fmt, uri = detect_input()
    return {
        'detected': bool(uri),
        'format': fmt,
        'uri': uri if uri else None,
        'config_uri': DEKTEC_INPUT_URI,
    }
