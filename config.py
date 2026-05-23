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

# Multicast
MULTICAST_TTL = int(os.environ.get('MULTICAST_TTL', '4'))

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

# SSAI / SCTE-35
SCTE35_PID = 500
SCTE35_LOG_EVENTS = True

# Media scanning
MEDIA_EXTENSIONS = {'.mxf', '.mp4', '.mov', '.mts', '.m2ts', '.ts'}
SCAN_WORKER_THREADS = 4
