"""
SCTE-35 detection and event logging.

Responsibilities:
  1. ffprobe check: does an asset contain SCTE-35 data streams?
  2. Parse SCTE-35 base64/hex payloads from FFmpeg log lines.
  3. Persist events to SCTEEvent table for SSAI visibility.
"""
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# FFmpeg logs SCTE-35 splice info like:
#   [mpegts @...] SCTE-35 message, splice_insert event_id=12345
_SCTE_LOG_RE = re.compile(
    r'SCTE-?35.*?event_id=(\d+)|'
    r'splice_insert.*?event_id=(\d+)|'
    r'time_signal',
    re.IGNORECASE
)

# Hex payload pattern from verbose FFmpeg output
_HEX_RE = re.compile(r'([0-9a-fA-F]{20,})')


def parse_ffmpeg_log_line(line, channel_id, app):
    """
    Called from the FFmpeg stderr reader thread.
    If the line contains SCTE-35 information, persist it and return True.
    """
    if 'scte' not in line.lower() and 'splice' not in line.lower():
        return False

    try:
        _store_event_from_log(line, channel_id, app)
    except Exception as e:
        logger.debug(f'SCTE parse error: {e}')
    return True


def _store_event_from_log(line, channel_id, app):
    event_id = None
    m = re.search(r'event_id[=:](\d+)', line, re.IGNORECASE)
    if m:
        event_id = int(m.group(1))

    event_type = 'unknown'
    if 'splice_insert' in line.lower():
        event_type = 'splice_insert'
    elif 'time_signal' in line.lower():
        event_type = 'time_signal'
    elif 'splice_null' in line.lower():
        event_type = 'splice_null'

    pts = None
    pts_m = re.search(r'pts[=: ]+(\d+)', line, re.IGNORECASE)
    if pts_m:
        pts = int(pts_m.group(1))

    out_of_network = 'out_of_network_indicator=1' in line or 'out of network' in line.lower()

    hex_payload = None
    hex_m = _HEX_RE.search(line)
    if hex_m:
        hex_payload = hex_m.group(1)

    with app.app_context():
        from models import db, SCTEEvent
        ev = SCTEEvent(
            channel_id=channel_id,
            event_id=event_id,
            event_type=event_type,
            pts_time=pts,
            out_of_network=out_of_network,
            raw_hex=hex_payload,
            detected_at=datetime.utcnow(),
        )
        db.session.add(ev)
        db.session.commit()
        logger.info(
            f'[SCTE-35] channel={channel_id} type={event_type} '
            f'event_id={event_id} oon={out_of_network}'
        )


def try_parse_threefive(hex_payload):
    """
    Optional: use the threefive library to fully decode a SCTE-35 payload.
    Returns a dict or None if threefive is unavailable/fails.
    """
    try:
        import threefive
        cue = threefive.Cue(hex_payload)
        cue.decode()
        return cue.get()
    except Exception:
        return None
