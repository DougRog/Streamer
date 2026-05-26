"""
Live input source helper.

Parses source strings and returns FFmpeg input arguments:
  udp://…, rtp://…, rtmp://…, srt://…  →  -i <url>
  dektec:<card>:<port>                  →  -f dektec -i <card>:<port>
  <card>:<port>                         →  -f dektec -i <card>:<port>
  /dev/videoN                           →  -f v4l2 -i /dev/videoN
"""
import logging
from config import DEKTEC_FFMPEG_FORMAT

logger = logging.getLogger(__name__)

_STREAM_SCHEMES = ('udp://', 'rtp://', 'rtmp://', 'srt://')


def get_ffmpeg_input_args(source_hint):
    """
    Return the FFmpeg input args for the given source string, e.g.:
      ['-i', 'udp://239.1.1.1:5000']
      ['-f', 'dektec', '-i', '0:0']
    Returns None if the hint is empty or unrecognised.
    """
    hint = (source_hint or '').strip()
    if not hint:
        return None

    if hint.startswith(_STREAM_SCHEMES):
        return ['-i', hint]

    if hint.lower().startswith('dektec:'):
        return ['-f', 'dektec', '-i', hint[len('dektec:'):]]

    if hint.startswith('/dev/'):
        return ['-f', 'v4l2', '-i', hint]

    if ':' in hint:
        return ['-f', DEKTEC_FFMPEG_FORMAT or 'dektec', '-i', hint]

    logger.warning('Unrecognised live source: %s', hint)
    return None
