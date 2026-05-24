"""
XMLTV EPG and M3U playlist generator.

Generates a rolling EPG window by expanding every recurring and one-time
ScheduleEntry for each channel (or a single channel), applying overrides,
and serialising to XMLTV XML and/or M3U8.

All times in XMLTV output are UTC (+0000). Override lookup uses the
schedule timezone (Eastern) so day keys match the broadcast day.

Output files (uploaded via sftp_uploader):
  epg.xml          — all-channel XMLTV
  ch{id}.xml       — per-channel XMLTV
  playlist.m3u     — all-channel M3U
  ch{id}.m3u       — per-channel M3U
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import logging

from scheduler import expand_occurrences, _sched_date

logger = logging.getLogger(__name__)

_CATEGORY_MAP = {
    'file':      'Entertainment',
    'live':      'News',
    'recording': 'News',
}


def generate_xmltv(app, days=7, channel_id=None):
    """
    Return XMLTV XML as a unicode string.
    If channel_id is given, only include that channel.
    """
    with app.app_context():
        from models import Channel, ScheduleEntry, AppSetting

        source_name = AppSetting.get('epg.source_name', 'Streamer MCR')
        now = datetime.utcnow()
        end = now + timedelta(days=days)

        root = ET.Element('tv')
        root.set('generator-info-name', 'Streamer MCR')
        root.set('source-info-name', source_name)

        q = Channel.query.order_by(Channel.name)
        if channel_id:
            q = q.filter_by(id=channel_id)
        channels = q.all()

        for ch in channels:
            ch_el = ET.SubElement(root, 'channel', id=f'ch-{ch.id}')
            ET.SubElement(ch_el, 'display-name', lang='en').text = ch.name
            ET.SubElement(ch_el, 'url').text = f'udp://{ch.multicast_addr}:{ch.multicast_port}'

        # Override lookup keyed by (parent_id, schedule-tz date string)
        override_map = {}
        parent_ids = [e.id for e in ScheduleEntry.query.filter_by(parent_id=None).all()]
        for ov in ScheduleEntry.query.filter(ScheduleEntry.parent_id.isnot(None)).all():
            if ov.override_date:
                override_map[(ov.parent_id, ov.override_date.isoformat())] = ov

        programmes = []

        for ch in channels:
            entries = ScheduleEntry.query.filter_by(
                channel_id=ch.id, parent_id=None
            ).all()

            for entry in entries:
                for occ_start, occ_end in expand_occurrences(entry, now, end):
                    # Use schedule-tz date to match override_date in DB
                    date_str = _sched_date(occ_start).isoformat()
                    override = override_map.get((entry.id, date_str))

                    if override:
                        eff = override
                        prog_start = datetime.combine(
                            override.override_date, override.start_time.time()
                        )
                        prog_end = prog_start + timedelta(seconds=override.duration)
                    else:
                        eff = entry
                        prog_start, prog_end = occ_start, occ_end

                    start_str = prog_start.strftime('%Y%m%d%H%M%S') + ' +0000'
                    stop_str  = prog_end.strftime('%Y%m%d%H%M%S')   + ' +0000'

                    prog = ET.Element('programme',
                                      start=start_str, stop=stop_str,
                                      channel=f'ch-{ch.id}')
                    ET.SubElement(prog, 'title', lang='en').text = eff.title
                    if eff.notes:
                        ET.SubElement(prog, 'desc', lang='en').text = eff.notes
                    ET.SubElement(prog, 'category', lang='en').text = \
                        _CATEGORY_MAP.get(eff.entry_type, 'Entertainment')
                    ET.SubElement(prog, 'length', units='seconds').text = str(eff.duration)
                    if eff.entry_type in ('live', 'recording'):
                        ET.SubElement(prog, 'live')

                    programmes.append((start_str, prog))

        programmes.sort(key=lambda x: x[0])
        for _, prog_el in programmes:
            root.append(prog_el)

        try:
            ET.indent(root, space='  ')
        except AttributeError:
            pass

        xml_body = ET.tostring(root, encoding='unicode', xml_declaration=False)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_body


def generate_m3u(app, channel_id=None):
    """
    Return an M3U playlist as a unicode string.
    If channel_id is given, only include that channel.
    """
    with app.app_context():
        from models import Channel, AppSetting

        epg_url     = AppSetting.get('epg.public_url', '')
        source_name = AppSetting.get('epg.source_name', 'Streamer MCR')

        q = Channel.query.order_by(Channel.name)
        if channel_id:
            q = q.filter_by(id=channel_id)
        channels = q.all()

        lines = [f'#EXTM3U x-tvg-url="{epg_url}"', '']

        for ch in channels:
            multicast_url = f'udp://{ch.multicast_addr}:{ch.multicast_port}'
            lines.append(
                f'#EXTINF:-1 tvg-id="ch-{ch.id}" tvg-name="{ch.name}" '
                f'tvg-logo="" group-title="{source_name}",{ch.name}'
            )
            lines.append(multicast_url)
            lines.append('')

        return '\n'.join(lines)
