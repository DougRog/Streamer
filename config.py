import os

# Paths
MEDIA_POOL_PATH = os.environ.get('MEDIA_POOL_PATH', '/mnt/MasterControlMedia')
RECORDING_PATH = os.environ.get('RECORDING_PATH', '/mnt/MasterControlMedia/recordings')
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///streamer.db')

# Flask
SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me-in-production-abc123')
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 5001))
DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

# Video output defaults
DEFAULT_VIDEO_WIDTH = 1280
DEFAULT_VIDEO_HEIGHT = 720
DEFAULT_VIDEO_FPS = '60000/1001'    # 59.94fps
DEFAULT_VIDEO_BITRATE = '6M'
DEFAULT_AUDIO_BITRATE = '192k'
DEFAULT_GOP_SIZE = 120              # 2 seconds at 59.94fps
# Set DEFAULT_VIDEO_CODEC to match what your FFmpeg build supports.
# Set DEFAULT_VIDEO_PRESET to '' to disable -preset (needed for hardware encoders
# or custom builds without libx264 preset support).
DEFAULT_VIDEO_CODEC  = os.environ.get('DEFAULT_VIDEO_CODEC',  'libx264')
DEFAULT_VIDEO_PRESET = os.environ.get('DEFAULT_VIDEO_PRESET', 'fast')

# Multicast
MULTICAST_TTL       = int(os.environ.get('MULTICAST_TTL', '4'))
MULTICAST_INTERFACE = os.environ.get('MULTICAST_INTERFACE', '10.1.224.15')

# Dektec — override DEKTEC_INPUT_URI to use a specific capture source
# Examples:
#   dektec:0:0         → Dektec ffmpeg device, card 0 port 0
#   decklink:0         → Blackmagic DeckLink card 0
#   /dev/video0        → V4L2 capture device
#   udp://...          → Existing UDP stream
DEKTEC_INPUT_URI = os.environ.get('DEKTEC_INPUT_URI', 'dektec:0:0')
DEKTEC_FFMPEG_FORMAT = os.environ.get('DEKTEC_FFMPEG_FORMAT', 'dektec')

# Scheduler
SCHEDULER_INTERVAL = 5          # seconds between playout checks
PRETRANSITION_SECONDS = 3       # seconds before end to begin next item
SLATE_RESTART_DELAY = 2         # seconds between slate restart on channel with no content
SCHEDULE_TIMEZONE = os.environ.get('SCHEDULE_TIMEZONE', 'America/New_York')

# SSAI / SCTE-35
SCTE35_PID = 500
SCTE35_LOG_EVENTS = True

# Media scanning
MEDIA_EXTENSIONS           = {'.mxf', '.mp4', '.mov', '.mts', '.m2ts', '.ts'}
SCAN_WORKER_THREADS        = 4
MEDIA_SCAN_INTERVAL_HOURS  = int(os.environ.get('MEDIA_SCAN_INTERVAL_HOURS', '1'))
FFPROBE_PATH = os.environ.get('FFPROBE_PATH', '/home/lilly/ffmpeg-build/ffprobe')
FFMPEG_PATH  = os.environ.get('FFMPEG_PATH',  '/home/lilly/ffmpeg-build/ffmpeg')

# PID tracking file — records PIDs of FFmpeg processes owned by this app so
# orphans from a previous run can be reaped on restart without touching
# unrelated FFmpeg processes on the same machine.
STREAMER_PID_FILE = os.environ.get('STREAMER_PID_FILE', '/tmp/streamer_ffmpeg.pids')

# EPG / SFTP — env vars take priority; also configurable via Settings UI.
# These become the seed values for the DB on first run.
SFTP_HOST     = os.environ.get('SFTP_HOST', '')
SFTP_PORT     = int(os.environ.get('SFTP_PORT', '22'))
SFTP_USERNAME = os.environ.get('SFTP_USERNAME', '')
SFTP_PASSWORD = os.environ.get('SFTP_PASSWORD', '')
SFTP_PATH     = os.environ.get('SFTP_PATH', '/')

EPG_WINDOW_DAYS      = int(os.environ.get('EPG_WINDOW_DAYS', '7'))
EPG_INTERVAL_MINUTES = int(os.environ.get('EPG_INTERVAL_MINUTES', '30'))
EPG_SOURCE_NAME      = os.environ.get('EPG_SOURCE_NAME', 'Streamer MCR')
EPG_PUBLIC_URL       = os.environ.get('EPG_PUBLIC_URL', '')
