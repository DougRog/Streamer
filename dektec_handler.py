"""
Live input source helper.

Parses source strings and returns FFmpeg input arguments:
  udp://…, rtp://…, rtmp://…, srt://…  →  -i <url>
  dektec:<card>:<port>                  →  -f dektec -i <card>:<port>
  <card>:<port>                         →  -f dektec -i <card>:<port>
  /dev/videoN                           →  -f v4l2 -i /dev/videoN

For UDP/RTP multicast inputs, localaddr (from MULTICAST_INTERFACE) is appended
so FFmpeg joins the multicast group on the correct interface.  overrun_nonfatal
prevents buffer-overflow errors from dropping the session.
"""
import logging
from config import DEKTEC_FFMPEG_FORMAT, MULTICAST_INTERFACE

logger = logging.getLogger(__name__)

_STREAM_SCHEMES = ('udp://', 'rtp://', 'rtmp://', 'srt://')


def _add_udp_input_opts(url: str) -> str:
    """Append localaddr and overrun_nonfatal to a UDP/RTP input URL if not already set."""
    base, _, qs = url.partition('?')
    params: dict = {}
    if qs:
        for part in qs.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                params[k.lower()] = v
            elif part:
                params[part.lower()] = ''

    if MULTICAST_INTERFACE and 'localaddr' not in params:
        params['localaddr'] = MULTICAST_INTERFACE
    if 'overrun_nonfatal' not in params:
        params['overrun_nonfatal'] = '1'

    qs_out = '&'.join(f'{k}={v}' if v else k for k, v in params.items())
    return f'{base}?{qs_out}' if qs_out else base


def get_ffmpeg_input_args(source_hint):
    """
    Return the FFmpeg input args for the given source string, e.g.:
      ['-i', 'udp://239.1.1.1:5000?localaddr=10.1.224.15&overrun_nonfatal=1']
      ['-f', 'dektec', '-i', '0:0']
    Returns None if the hint is empty or unrecognised.
    """
    hint = (source_hint or '').strip()
    if not hint:
        return None

    if hint.startswith(_STREAM_SCHEMES):
        if hint.startswith(('udp://', 'rtp://')):
            hint = _add_udp_input_opts(hint)
        return ['-i', hint]

    if hint.lower().startswith('dektec:'):
        return ['-f', 'dektec', '-i', hint[len('dektec:'):]]

    if hint.startswith('/dev/'):
        return ['-f', 'v4l2', '-i', hint]

    if ':' in hint:
        return ['-f', DEKTEC_FFMPEG_FORMAT or 'dektec', '-i', hint]

    logger.warning('Unrecognised live source: %s', hint)
    return None
