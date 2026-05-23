"""
SFTP upload for EPG XML and M3U playlist files.

Settings are read (in priority order):
  1. Environment variables: SFTP_HOST, SFTP_PORT, SFTP_USERNAME,
     SFTP_PASSWORD, SFTP_PATH
  2. app_settings table (set via the Settings UI)
  3. config.py module-level defaults (empty strings)

Credentials are never stored in source code.
"""
import logging
import os
from datetime import datetime

import paramiko

logger = logging.getLogger(__name__)

# Runtime state visible to the status API
_status = {
    'last_time':   None,   # datetime | None
    'last_ok':     None,   # bool | None
    'last_msg':    'No upload attempted yet',
    'last_files':  [],
    'in_progress': False,
}


# ------------------------------------------------------------------ #
#  Internal helpers                                                   #
# ------------------------------------------------------------------ #

def _settings(app):
    """Return SFTP config dict, env vars > DB > empty."""
    with app.app_context():
        from models import AppSetting
        db_get = AppSetting.get

    def pick(env_key, db_key, default=''):
        return os.environ.get(env_key) or db_get(db_key, '') or default

    return {
        'host':     pick('SFTP_HOST',     'sftp.host'),
        'port':     int(pick('SFTP_PORT', 'sftp.port', '22') or 22),
        'username': pick('SFTP_USERNAME', 'sftp.username'),
        'password': pick('SFTP_PASSWORD', 'sftp.password'),
        'path':     pick('SFTP_PATH',     'sftp.path', '/'),
    }


def _connect(cfg):
    """Open and return (transport, sftp) or raise."""
    transport = paramiko.Transport((cfg['host'], cfg['port']))
    transport.connect(username=cfg['username'], password=cfg['password'])
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def _close(transport):
    try:
        if transport and transport.is_active():
            transport.close()
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  Public API                                                         #
# ------------------------------------------------------------------ #

def upload_epg(app):
    """
    Generate XMLTV + M3U and upload both to the configured SFTP path.
    Returns (ok: bool, message: str).
    Thread-safe: sets _status['in_progress'] during the upload.
    """
    from epg_generator import generate_xmltv, generate_m3u

    cfg = _settings(app)
    if not cfg['host'] or not cfg['username']:
        msg = 'SFTP not configured — enter host and credentials in Settings'
        logger.warning(msg)
        _status.update(last_time=datetime.utcnow(), last_ok=False,
                       last_msg=msg, last_files=[])
        return False, msg

    if _status['in_progress']:
        return False, 'Upload already in progress'

    _status['in_progress'] = True
    transport = None
    try:
        with app.app_context():
            from models import AppSetting
            days = int(AppSetting.get('epg.window_days', '7') or 7)

        logger.info('EPG generation starting…')
        xml_content = generate_xmltv(app, days=days)
        m3u_content = generate_m3u(app)
        logger.info('EPG generation done — uploading via SFTP…')

        transport, sftp = _connect(cfg)

        base = cfg['path'].rstrip('/')
        uploaded = []
        for filename, content in [('epg.xml', xml_content),
                                   ('playlist.m3u', m3u_content)]:
            remote = f'{base}/{filename}'
            with sftp.file(remote, 'wb') as fh:
                fh.write(content.encode('utf-8'))
            uploaded.append(remote)
            logger.info(f'SFTP uploaded → {remote}')

        sftp.close()
        msg = (f'Uploaded {len(uploaded)} files at '
               f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC')
        _status.update(last_time=datetime.utcnow(), last_ok=True,
                       last_msg=msg, last_files=uploaded)
        return True, msg

    except Exception as exc:
        msg = f'SFTP upload failed: {exc}'
        logger.error(msg, exc_info=True)
        _status.update(last_time=datetime.utcnow(), last_ok=False,
                       last_msg=msg, last_files=[])
        return False, msg

    finally:
        _close(transport)
        _status['in_progress'] = False


def test_connection(app):
    """
    Quick connectivity check — connect, list the target path, disconnect.
    Returns (ok: bool, message: str).
    """
    cfg = _settings(app)
    if not cfg['host'] or not cfg['username']:
        return False, 'SFTP host/username not configured'

    transport = None
    try:
        transport, sftp = _connect(cfg)
        entries = sftp.listdir(cfg['path'])
        sftp.close()
        return True, (f"Connected to {cfg['host']} — "
                      f"{len(entries)} item(s) in {cfg['path']}")
    except Exception as exc:
        return False, str(exc)
    finally:
        _close(transport)


def get_status():
    """Return a JSON-serialisable copy of the current upload status."""
    s = dict(_status)
    if s['last_time']:
        s['last_time'] = s['last_time'].isoformat()
    return s
