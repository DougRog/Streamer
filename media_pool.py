"""
Scans /mnt/MasterControlMedia for media files, runs ffprobe to extract
metadata, and persists results in MediaAsset.
"""
import os
import json
import logging
import subprocess
import threading
from datetime import datetime
from config import MEDIA_POOL_PATH, MEDIA_EXTENSIONS, SCAN_WORKER_THREADS

logger = logging.getLogger(__name__)

_scan_lock = threading.Lock()
_scan_in_progress = False
_scan_progress = {'total': 0, 'done': 0, 'current': ''}


def scan_status():
    return {
        'in_progress': _scan_in_progress,
        'total': _scan_progress['total'],
        'done': _scan_progress['done'],
        'current': _scan_progress['current'],
    }


def probe_file(path):
    """Run ffprobe and return parsed info dict, or None on failure."""
    cmd = [
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_format', '-show_streams',
        path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning(f'ffprobe failed for {path}: {e}')
        return None


def _has_scte(probe_data):
    """Return True if any stream looks like SCTE-35 data."""
    for stream in probe_data.get('streams', []):
        codec = stream.get('codec_name', '').lower()
        codec_tag = stream.get('codec_tag_string', '').lower()
        if 'scte' in codec or 'scte' in codec_tag:
            return True
        # MPEG-TS data streams with PID in typical SCTE range
        if stream.get('codec_type') == 'data':
            return True
    return False


def _extract_asset_info(path, probe_data):
    """Map ffprobe output → dict for MediaAsset fields."""
    fmt = probe_data.get('format', {})
    streams = probe_data.get('streams', [])

    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)

    fps_str = None
    width = height = None
    color_space = None
    if video:
        r_frame_rate = video.get('r_frame_rate', '')
        if '/' in r_frame_rate:
            num, den = r_frame_rate.split('/')
            if int(den) > 0:
                fps_val = int(num) / int(den)
                fps_str = f'{fps_val:.3f}'
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

    file_size = None
    raw_size = fmt.get('size')
    if raw_size:
        try:
            file_size = int(raw_size)
        except ValueError:
            try:
                file_size = os.path.getsize(path)
            except OSError:
                pass

    return {
        'duration_seconds': duration,
        'file_size': file_size,
        'has_scte': _has_scte(probe_data),
        'video_codec': video.get('codec_name') if video else None,
        'audio_codec': audio.get('codec_name') if audio else None,
        'video_width': width,
        'video_height': height,
        'video_fps': fps_str,
        'color_space': color_space,
    }


def _scan_worker(app, paths, results):
    """Thread worker: probe a list of file paths and store results."""
    global _scan_progress
    from models import db, MediaAsset

    with app.app_context():
        for path in paths:
            _scan_progress['current'] = os.path.basename(path)
            try:
                probe_data = probe_file(path)
                if not probe_data:
                    _scan_progress['done'] += 1
                    continue

                info = _extract_asset_info(path, probe_data)
                asset = MediaAsset.query.filter_by(path=path).first()
                if not asset:
                    asset = MediaAsset(
                        filename=os.path.basename(path),
                        path=path,
                        discovered_at=datetime.utcnow(),
                    )
                    db.session.add(asset)

                for k, v in info.items():
                    setattr(asset, k, v)
                asset.last_scanned = datetime.utcnow()
                db.session.commit()
            except Exception as e:
                logger.error(f'Error scanning {path}: {e}')
                db.session.rollback()
            finally:
                _scan_progress['done'] += 1


def scan_pool(app):
    """Start a background scan of the media pool. Thread-safe."""
    global _scan_in_progress, _scan_progress

    with _scan_lock:
        if _scan_in_progress:
            return False, 'Scan already in progress'
        _scan_in_progress = True

    def _run():
        global _scan_in_progress, _scan_progress
        try:
            all_paths = []
            for root, dirs, files in os.walk(MEDIA_POOL_PATH):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in MEDIA_EXTENSIONS:
                        all_paths.append(os.path.join(root, fname))

            _scan_progress = {'total': len(all_paths), 'done': 0, 'current': ''}
            logger.info(f'Media scan: found {len(all_paths)} files')

            # Remove DB entries for files that no longer exist
            with app.app_context():
                from models import db, MediaAsset
                existing = MediaAsset.query.all()
                for asset in existing:
                    if not os.path.exists(asset.path):
                        db.session.delete(asset)
                db.session.commit()

            # Single worker thread — avoids SQLite concurrent-write lock contention
            _scan_worker(app, all_paths, [])

            logger.info('Media scan complete')
        except Exception as e:
            logger.error(f'Scan failed: {e}')
        finally:
            _scan_in_progress = False

    threading.Thread(target=_run, daemon=True).start()
    return True, 'Scan started'
