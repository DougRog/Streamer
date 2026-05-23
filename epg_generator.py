"""
XMLTV EPG and M3U playlist generator.

Generates a 7-day (configurable) rolling EPG window by expanding every
recurring and one-time ScheduleEntry for each channel, applying overrides,
and serialising to XMLTV XML and/or M3U8.

Output files (uploaded via sftp_uploader):
  epg.xml      — XMLTV Electronic Programme Guide
  playlist.m3u — IPTV playlist pointing at the multicast URLs
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import logging

from scheduler import expand_occurrences

logger = logging.getLogger(__name__)

# XMLTV category heuristics by entry_type
_CATEGORY_MAP = {
    'file':      'Entertainment',
    'live':      'News',
    'recording': 'News',
}


def generate_xmltv(app, days=7):
    """
    Return XMLTV XML as a unicode string covering the next `days` days.
    Runs inside the provided Flask app context.
    """
    with app.app_context():
        from models import Channel, ScheduleEntry, AppSetting

        source_name = AppSetting.get('epg.source_name', 'Streamer MCR')
        now = datetime.utcnow()
        end = now + timedelta(days=days)

        root = ET.Element('tv')
        root.set('generator-info-name', 'Streamer MCR')
        root.set('source-info-name', source_name)

        channels = Channel.query.order_by(Channel.name).all()

        # ---- Channel declarations ----
        for ch in channels:
            ch_el = ET.SubElement(root, 'channel', id=f'ch-{ch.id}')
            dn = ET.SubElement(ch_el, 'display-name', lang='en')
            dn.text = ch.name
            url_el = ET.SubElement(ch_el, 'url')
            url_el.text = f'udp://{ch.multicast_addr}:{ch.multicast_port}'

        # ---- Pre-build override lookup ----
        override_map = {}
        for ov in ScheduleEntry.query.filter(
            ScheduleEntry.parent_id.isnot(None)
        ).all():
            if ov.override_date:
                override_map[(ov.parent_id, ov.override_date.isoformat())] = ov

        # ---- Programme entries ----
        programmes = []   # collect (start_str, Element) for sorting

        for ch in channels:
            entries = ScheduleEntry.query.filter_by(
                channel_id=ch.id, parent_id=None
            ).all()

            for entry in entries:
                for occ_start, occ_end in expand_occurrences(entry, now, end):
                    date_str = occ_start.date().isoformat()
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

                    ET.SubElement(prog, 'length', units='seconds').text = \
                        str(eff.duration)

                    if eff.entry_type in ('live', 'recording'):
                        ET.SubElement(prog, 'live')

                    programmes.append((start_str, prog))

        # Sort all programmes chronologically then attach to root
        programmes.sort(key=lambda x: x[0])
        for _, prog_el in programmes:
            root.append(prog_el)

        # Pretty-print (Python 3.9+)
        try:
            ET.indent(root, space='  ')
        except AttributeError:
            pass

        xml_body = ET.tostring(root, encoding='unicode', xml_declaration=False)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_body


def generate_m3u(app):
    """
    Return an M3U playlist as a unicode string containing all channels.
    The x-tvg-url points at the EPG public URL from settings.
    """
    with app.app_context():
        from models import Channel, AppSetting

        epg_url     = AppSetting.get('epg.public_url', '')
        source_name = AppSetting.get('epg.source_name', 'Streamer MCR')
        channels    = Channel.query.order_by(Channel.name).all()

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
