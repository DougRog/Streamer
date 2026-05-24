"""
Scans MEDIA_POOL_PATH for media files, runs ffprobe to extract
metadata, and persists results in MediaAsset.
"""
import os
import json
import shutil
import logging
import subprocess
import threading
from datetime import datetime
from config import MEDIA_POOL_PATH, MEDIA_EXTENSIONS

logger = logging.getLogger(__name__)

_scan_lock = threading.Lock()
_scan_in_progress = False
_scan_progress = {'total': 0, 'done': 0, 'current': ''}
_scan_errors: list = []   # surfaced to the status API so the UI can show them


def scan_status():
    return {
        'in_progress': _scan_in_progress,
        'total': _scan_progress['total'],
        'done': _scan_progress['done'],
        'current': _scan_progress['current'],
        'errors': list(_scan_errors[-20:]),
    }


def probe_file(path):
    """Run ffprobe and return parsed info dict, or None on failure."""
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        logger.error('ffprobe not found in PATH — install the ffmpeg package')
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
    except json.JSONDecodeError as e:
        logger.warning(f'ffprobe bad JSON for {os.path.basename(path)}: {e}')
        return None
    except FileNotFoundError:
        logger.error('ffprobe not found — install the ffmpeg package')
        return None


def _has_scte(probe_data):
    """Return True if any stream looks like SCTE-35 data."""
    for stream in probe_data.get('streams', []):
        codec = stream.get('codec_name', '').lower()
        codec_tag = stream.get('codec_tag_string', '').lower()
        if 'scte' in codec or 'scte' in codec_tag:
            return True
        if stream.get('codec_type') == 'data':
            return True
    return False


def _extract_asset_info(path, probe_data):
    """Map ffprobe output → dict for MediaAsset fields."""
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
        'file_size':        file_size,
        'has_scte':         _has_scte(probe_data),
        'video_codec':      video.get('codec_name') if video else None,
        'audio_codec':      audio.get('codec_name') if audio else None,
        'video_width':      width,
        'video_height':     height,
        'video_fps':        fps_str,
        'color_space':      color_space,
    }


def _scan_worker(app, paths):
    """Probe each path and upsert a MediaAsset record. Runs single-threaded."""
    global _scan_progress, _scan_errors
    from models import db, MediaAsset

    saved = skipped = 0
    with app.app_context():
        for path in paths:
            fname = os.path.basename(path)
            _scan_progress['current'] = fname
            try:
                probe_data = probe_file(path)
                if not probe_data:
                    skipped += 1
                    _scan_errors.append(f'probe failed: {fname}')
                    logger.warning(f'Scan skipped (no probe data): {fname}')
                    continue

                info = _extract_asset_info(path, probe_data)
                asset = MediaAsset.query.filter_by(path=path).first()
                if not asset:
                    asset = MediaAsset(
                        filename=fname,
                        path=path,
                        discovered_at=datetime.utcnow(),
                    )
                    db.session.add(asset)

                for k, v in info.items():
                    setattr(asset, k, v)
                asset.last_scanned = datetime.utcnow()
                db.session.commit()
                saved += 1
                logger.debug(f'Scan saved: {fname}')

            except Exception as e:
                logger.error(f'Scan error for {fname}: {e}', exc_info=True)
                _scan_errors.append(f'error: {fname}: {e}')
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                _scan_progress['done'] += 1

    logger.info(f'Scan worker done: {saved} saved, {skipped} skipped out of {len(paths)} files')
    return saved


def scan_pool(app):
    """Start a background scan of the media pool. Thread-safe."""
    global _scan_in_progress, _scan_progress, _scan_errors

    if not shutil.which('ffprobe'):
        msg = 'ffprobe not found in PATH — install the ffmpeg package (apt install ffmpeg)'
        logger.error(msg)
        return False, msg

    with _scan_lock:
        if _scan_in_progress:
            return False, 'Scan already in progress'
        _scan_in_progress = True
        _scan_errors = []

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
            logger.info(f'Media scan: found {len(all_paths)} files in {MEDIA_POOL_PATH}')

            # Prune DB entries whose files no longer exist
            with app.app_context():
                from models import db, MediaAsset
                for asset in MediaAsset.query.all():
                    if not os.path.exists(asset.path):
                        logger.info(f'Removing missing file from library: {asset.filename}')
                        db.session.delete(asset)
                db.session.commit()

            _scan_worker(app, all_paths)

            # Log final tally
            with app.app_context():
                from models import MediaAsset
                count = MediaAsset.query.count()
                logger.info(
                    f'Scan complete: {count} assets in library '
                    f'({len(_scan_errors)} error(s))'
                )
        except Exception as e:
            logger.error(f'Scan failed: {e}', exc_info=True)
        finally:
            _scan_in_progress = False

    threading.Thread(target=_run, daemon=True, name='media-scan').start()
    return True, 'Scan started'
