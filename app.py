import os
import json
import logging
from datetime import datetime, timedelta, date, timezone

from flask import Flask, render_template, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy

from dateutil import tz as tzlib

import config
from models import db, Channel, ScheduleEntry, SCTEEvent, AppSetting
from stream_manager import stream_manager
from dektec_handler import dektec_status, list_inputs as list_dektec_inputs, reset_cache as reset_dektec_cache
from scheduler import (init_scheduler, expand_occurrences,
                       get_current_item, get_next_item, _sched_date)
import sftp_uploader

_UTC = tzlib.tzutc()
_SCHEDULE_TZ = tzlib.gettz(config.SCHEDULE_TIMEZONE)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


def _seed_settings(app):
    """Pre-populate AppSetting from config.py defaults — only if not already set."""
    defaults = {
        'sftp.host':            config.SFTP_HOST,
        'sftp.port':            str(config.SFTP_PORT),
        'sftp.username':        config.SFTP_USERNAME,
        'sftp.password':        config.SFTP_PASSWORD,
        'sftp.path':            config.SFTP_PATH,
        'epg.window_days':      str(config.EPG_WINDOW_DAYS),
        'epg.interval_minutes': str(config.EPG_INTERVAL_MINUTES),
        'epg.source_name':      config.EPG_SOURCE_NAME,
        'epg.public_url':       config.EPG_PUBLIC_URL,
    }
    with app.app_context():
        changed = False
        for key, value in defaults.items():
            if value and db.session.get(AppSetting, key) is None:
                db.session.add(AppSetting(key=key, value=value))
                changed = True
        if changed:
            db.session.commit()


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = config.DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    stream_manager.init_app(app)

    with app.app_context():
        db.create_all()
        os.makedirs(config.RECORDING_PATH, exist_ok=True)
        _seed_settings(app)
        # Schema migrations for columns added after initial deploy
        from sqlalchemy import text
        for stmt in [
            'ALTER TABLE schedule_entries ADD COLUMN loop_enabled BOOLEAN NOT NULL DEFAULT 0',
            'ALTER TABLE channels ADD COLUMN slate_type VARCHAR(20) NOT NULL DEFAULT "color"',
            'ALTER TABLE channels ADD COLUMN slate_asset_path VARCHAR(500)',
            'ALTER TABLE channels ADD COLUMN epg_filename VARCHAR(100)',
            'ALTER TABLE schedule_entries ADD COLUMN recording_path VARCHAR(500)',
        ]:
            try:
                db.session.execute(text(stmt))
                db.session.commit()
            except Exception:
                db.session.rollback()

    init_scheduler(app, stream_manager)

    # ------------------------------------------------------------------ #
    #  Page routes                                                         #
    # ------------------------------------------------------------------ #

    @app.route('/')
    def index():
        with app.app_context():
            channels = Channel.query.order_by(Channel.name).all()
            recent_scte = SCTEEvent.query.order_by(
                SCTEEvent.detected_at.desc()
            ).limit(20).all()
        saved_sdi = AppSetting.get('dektec.input', '') or config.DEKTEC_INPUT_URI or ''
        return render_template(
            'index.html',
            channels=channels,
            stream_manager=stream_manager,
            recent_scte=recent_scte,
            dektec_input=saved_sdi,
        )

    @app.route('/calendar')
    def calendar():
        channels = Channel.query.order_by(Channel.name).all()
        saved_sdi = AppSetting.get('dektec.input', '') or config.DEKTEC_INPUT_URI or 'dektec:0:0'
        return render_template('calendar.html', channels=channels, dektec_input=saved_sdi)

    @app.route('/channels')
    def channels():
        channels = Channel.query.order_by(Channel.name).all()
        return render_template('channels.html', channels=channels)

    # ------------------------------------------------------------------ #
    #  Channel API                                                         #
    # ------------------------------------------------------------------ #

    @app.route('/api/channels', methods=['GET'])
    def api_list_channels():
        channels = Channel.query.order_by(Channel.name).all()
        return jsonify([c.to_dict() for c in channels])

    @app.route('/api/channels', methods=['POST'])
    def api_create_channel():
        data = request.get_json(force=True)
        if not data.get('name'):
            return jsonify({'error': 'name required'}), 400
        if Channel.query.filter_by(name=data['name']).first():
            return jsonify({'error': 'Channel name already exists'}), 409
        ch = Channel(
            name=data['name'],
            multicast_addr=data.get('multicast_addr', '239.1.1.1'),
            multicast_port=int(data.get('multicast_port', 5000)),
            video_bitrate=data.get('video_bitrate', '6M'),
            slate_enabled=data.get('slate_enabled', True),
            slate_type=data.get('slate_type', 'color'),
            slate_asset_path=data.get('slate_asset_path') or None,
            epg_filename=data.get('epg_filename') or None,
            color=data.get('color', '#3788d8'),
            notes=data.get('notes', ''),
        )
        db.session.add(ch)
        db.session.commit()
        return jsonify(ch.to_dict()), 201

    @app.route('/api/channels/<int:cid>', methods=['GET'])
    def api_get_channel(cid):
        ch = Channel.query.get_or_404(cid)
        d = ch.to_dict()
        d['stream'] = stream_manager.get_channel_info(cid)
        return jsonify(d)

    @app.route('/api/channels/<int:cid>', methods=['PUT'])
    def api_update_channel(cid):
        ch = Channel.query.get_or_404(cid)
        data = request.get_json(force=True)
        for field in ('name', 'multicast_addr', 'multicast_port',
                      'video_bitrate', 'slate_enabled', 'color', 'notes',
                      'slate_type', 'slate_asset_path', 'epg_filename'):
            if field in data:
                null_if_empty = field in ('slate_asset_path', 'epg_filename')
                setattr(ch, field, data[field] or None if null_if_empty else data[field])
        db.session.commit()
        return jsonify(ch.to_dict())

    @app.route('/api/channels/<int:cid>', methods=['DELETE'])
    def api_delete_channel(cid):
        ch = Channel.query.get_or_404(cid)
        stream_manager.stop_channel(cid)
        db.session.delete(ch)
        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/channels/<int:cid>/start', methods=['POST'])
    def api_start_channel(cid):
        ch = Channel.query.get_or_404(cid)
        ch.is_active = True
        db.session.commit()
        return jsonify({'ok': True, 'channel': ch.to_dict()})

    @app.route('/api/channels/<int:cid>/stop', methods=['POST'])
    def api_stop_channel(cid):
        ch = Channel.query.get_or_404(cid)
        ch.is_active = False
        db.session.commit()
        stream_manager.stop_channel(cid)
        return jsonify({'ok': True})

    @app.route('/api/channels/<int:cid>/stream', methods=['GET'])
    def api_channel_stream(cid):
        return jsonify(stream_manager.get_channel_info(cid))

    # ------------------------------------------------------------------ #
    #  Schedule API                                                        #
    # ------------------------------------------------------------------ #

    @app.route('/api/schedule', methods=['GET'])
    def api_schedule():
        """
        Returns FullCalendar-compatible event objects for [start, end].
        Query params: start, end (ISO8601), channel_id (optional filter).
        """
        try:
            start = datetime.fromisoformat(request.args['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(request.args['end'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            return jsonify({'error': 'start and end ISO params required'}), 400

        # Normalise to naive UTC (convert, don't just strip)
        if start.tzinfo:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
        if end.tzinfo:
            end = end.astimezone(timezone.utc).replace(tzinfo=None)

        channel_filter = request.args.get('channel_id', type=int)
        q = ScheduleEntry.query.filter_by(parent_id=None)
        if channel_filter:
            q = q.filter_by(channel_id=channel_filter)
        entries = q.all()

        # Build override lookup: (parent_id, date_str) → ScheduleEntry
        override_map = {}
        for ov in ScheduleEntry.query.filter(ScheduleEntry.parent_id.isnot(None)).all():
            if ov.override_date:
                override_map[(ov.parent_id, ov.override_date.isoformat())] = ov

        events = []
        for entry in entries:
            occs = expand_occurrences(entry, start, end)
            for occ_start, occ_end in occs:
                # Use schedule-tz date to match override_date stored in DB
                date_str = _sched_date(occ_start).isoformat()
                override = override_map.get((entry.id, date_str))

                if override:
                    ov_start = datetime.combine(
                        override.override_date, override.start_time.time()
                    )
                    ov_end = ov_start + timedelta(seconds=override.duration)
                    ev = _fc_event(override, ov_start, ov_end,
                                   isOverride=True, parentId=entry.id,
                                   occurrenceDate=date_str)  # already ET date
                else:
                    ev = _fc_event(entry, occ_start, occ_end,
                                   isRecurring=bool(entry.rrule),
                                   occurrenceDate=date_str)  # ET date
                events.append(ev)

        return jsonify(events)

    def _fc_event(entry, start, end, **extra_props):
        # Use schedule-tz date so occurrence keys match the broadcast day
        sched_date = _sched_date(start)
        return {
            'id': f'entry-{entry.id}-{sched_date.isoformat()}',
            'title': entry.title,
            'start': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end':   end.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'color': entry.effective_color(),
            'extendedProps': {
                'entryId': entry.id,
                'channelId': entry.channel_id,
                'channelName': entry.channel.name if entry.channel else '',
                'entryType': entry.entry_type,
                'assetPath': entry.asset_path or '',
                'liveSource': entry.live_source or '',
                'duration': entry.duration,
                'rrule': entry.rrule or '',
                'notes': entry.notes or '',
                'loopEnabled': bool(entry.loop_enabled),
                'recordingPath': entry.recording_path or '',
                **extra_props,
            },
        }

    @app.route('/api/schedule', methods=['POST'])
    def api_create_entry():
        data = request.get_json(force=True)
        try:
            entry = _entry_from_data(data)
            db.session.add(entry)
            db.session.commit()
            return jsonify(entry.to_dict()), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/schedule/<int:eid>', methods=['GET'])
    def api_get_entry(eid):
        return jsonify(ScheduleEntry.query.get_or_404(eid).to_dict())

    @app.route('/api/schedule/<int:eid>', methods=['PUT'])
    def api_update_entry(eid):
        entry = ScheduleEntry.query.get_or_404(eid)
        data = request.get_json(force=True)
        try:
            _apply_entry_data(entry, data)
            db.session.commit()
            return jsonify(entry.to_dict())
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/schedule/<int:eid>', methods=['DELETE'])
    def api_delete_entry(eid):
        scope = request.args.get('scope', 'this')   # 'this' | 'all'
        occ_date = request.args.get('date')          # YYYY-MM-DD when scope='this'
        entry = ScheduleEntry.query.get_or_404(eid)

        if scope == 'all':
            # Delete overrides too
            ScheduleEntry.query.filter_by(parent_id=eid).delete()
            db.session.delete(entry)
        elif scope == 'this' and occ_date and entry.rrule:
            exdates = entry.get_exdates()
            if occ_date not in exdates:
                exdates.append(occ_date)
                entry.set_exdates(exdates)
        else:
            db.session.delete(entry)

        db.session.commit()
        return jsonify({'ok': True})

    @app.route('/api/schedule/<int:eid>/override', methods=['POST'])
    def api_create_override(eid):
        """
        Create a one-off override for a specific occurrence of a recurring entry.
        Body: { occurrence_date, start_time, duration, title?, asset_path?, notes? }
        """
        parent = ScheduleEntry.query.get_or_404(eid)
        data = request.get_json(force=True)
        try:
            occ_date = date.fromisoformat(data['occurrence_date'])
        except (KeyError, ValueError):
            return jsonify({'error': 'occurrence_date required (YYYY-MM-DD)'}), 400

        # Remove existing override for this date
        ScheduleEntry.query.filter_by(parent_id=eid, override_date=occ_date).delete()

        # Build override start_time: date from occ_date, time from data or parent
        if 'start_time' in data:
            try:
                ov_start = datetime.fromisoformat(data['start_time'])
            except ValueError:
                ov_start = datetime.combine(occ_date, parent.start_time.time())
        else:
            ov_start = datetime.combine(occ_date, parent.start_time.time())

        override = ScheduleEntry(
            channel_id=parent.channel_id,
            title=data.get('title', parent.title),
            entry_type=data.get('entry_type', parent.entry_type),
            asset_path=data.get('asset_path', parent.asset_path),
            live_source=data.get('live_source', parent.live_source),
            start_time=ov_start,
            duration=int(data.get('duration', parent.duration)),
            color=data.get('color', parent.color),
            notes=data.get('notes', parent.notes),
            parent_id=eid,
            override_date=occ_date,
        )
        db.session.add(override)
        db.session.commit()
        return jsonify(override.to_dict()), 201

    # ------------------------------------------------------------------ #
    #  Dektec / Live / Recording API                                       #
    # ------------------------------------------------------------------ #

    @app.route('/api/dektec/status', methods=['GET'])
    def api_dektec_status():
        return jsonify(dektec_status())

    @app.route('/api/dektec/inputs', methods=['GET'])
    def api_dektec_inputs():
        inputs = list_dektec_inputs()
        saved = AppSetting.get('dektec.input', '') or config.DEKTEC_INPUT_URI or ''
        return jsonify({'inputs': inputs, 'saved': saved})

    @app.route('/api/dektec/input', methods=['POST'])
    def api_dektec_input():
        data = request.get_json(force=True) or {}
        value = (data.get('value') or '').strip()
        if not value:
            return jsonify({'error': 'value required'}), 400
        AppSetting.set('dektec.input', value)
        reset_dektec_cache()
        return jsonify({'ok': True})

    @app.route('/api/validate_path', methods=['POST'])
    def api_validate_path():
        data = request.get_json(force=True) or {}
        path = (data.get('path') or '').strip()
        if not path:
            return jsonify({'exists': False})
        if not os.path.isfile(path):
            return jsonify({'exists': False})
        from media_pool import probe_file, _extract_asset_info
        try:
            probe_data = probe_file(path)
            if probe_data:
                info = _extract_asset_info(path, probe_data)
                return jsonify({'exists': True, **info})
        except Exception:
            pass
        return jsonify({'exists': True})

    @app.route('/api/recording/start', methods=['POST'])
    def api_start_recording():
        data = request.get_json(force=True) or {}
        default_sdi = AppSetting.get('dektec.input', '') or config.DEKTEC_INPUT_URI or 'dektec:0:0'
        source = data.get('live_source') or default_sdi
        channel_id = data.get('channel_id')
        channel = db.session.get(Channel, channel_id) if channel_id else None
        ok, result = stream_manager.start_recording(source, channel)
        if ok:
            return jsonify({'ok': True, 'path': result})
        return jsonify({'ok': False, 'error': result}), 500

    @app.route('/api/recording/stop', methods=['POST'])
    def api_stop_recording():
        data = request.get_json(force=True) or {}
        rec_id = data.get('recording_id')
        ok = stream_manager.stop_recording(rec_id)
        return jsonify({'ok': ok})

    @app.route('/api/recording/active', methods=['GET'])
    def api_active_recordings():
        return jsonify(stream_manager.active_recordings())

    # ------------------------------------------------------------------ #
    #  Repeat-day API                                                      #
    # ------------------------------------------------------------------ #

    @app.route('/api/schedule/repeat-day', methods=['POST'])
    def api_repeat_day():
        """
        Apply FREQ=WEEKLY to every one-time entry on the given day for a channel.
        Body: { channel_id, date (YYYY-MM-DD), until? (YYYYMMDD, no dashes) }
        """
        from datetime import time as dtime
        data = request.get_json(force=True)
        channel_id = data.get('channel_id')
        day_str = data.get('date')
        until_str = data.get('until')

        if not channel_id or not day_str:
            return jsonify({'error': 'channel_id and date required'}), 400

        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            return jsonify({'error': 'Invalid date — use YYYY-MM-DD'}), 400

        day_start = datetime.combine(day_date, dtime.min)
        day_end = datetime.combine(day_date, dtime.max)

        rrule_str = 'FREQ=WEEKLY'
        if until_str:
            rrule_str += f';UNTIL={until_str}T235959Z'

        entries = ScheduleEntry.query.filter_by(
            channel_id=int(channel_id),
            parent_id=None,
        ).filter(
            ScheduleEntry.rrule.is_(None),
            ScheduleEntry.start_time >= day_start,
            ScheduleEntry.start_time <= day_end,
        ).all()

        updated = []
        for entry in entries:
            entry.rrule = rrule_str
            updated.append(entry.id)
        db.session.commit()

        return jsonify({'ok': True, 'count': len(updated), 'updated': updated,
                        'rrule': rrule_str})

    # ------------------------------------------------------------------ #
    #  Settings page + API                                                 #
    # ------------------------------------------------------------------ #

    @app.route('/settings')
    def settings():
        s = AppSetting.all_dict()
        return render_template('settings.html', settings=s,
                               epg_status=sftp_uploader.get_status())

    @app.route('/api/settings', methods=['GET'])
    def api_get_settings():
        d = AppSetting.all_dict()
        if d.get('sftp.password'):
            d['sftp.password'] = '••••••'
        return jsonify(d)

    @app.route('/api/settings', methods=['POST'])
    def api_update_settings():
        data = request.get_json(force=True)
        allowed = {
            'sftp.host', 'sftp.port', 'sftp.username', 'sftp.password',
            'sftp.path', 'epg.window_days', 'epg.interval_minutes',
            'epg.source_name', 'epg.public_url',
        }
        AppSetting.bulk_set({k: v for k, v in data.items() if k in allowed})
        return jsonify({'ok': True})

    # ------------------------------------------------------------------ #
    #  EPG API                                                             #
    # ------------------------------------------------------------------ #

    @app.route('/api/epg', methods=['GET'])
    def api_epg_xml():
        from epg_generator import generate_xmltv
        days = request.args.get('days', 7, type=int)
        xml = generate_xmltv(app, days=days)
        return xml, 200, {'Content-Type': 'application/xml; charset=utf-8'}

    @app.route('/api/epg/m3u', methods=['GET'])
    def api_epg_m3u():
        from epg_generator import generate_m3u
        m3u = generate_m3u(app)
        return m3u, 200, {
            'Content-Type': 'application/x-mpegurl; charset=utf-8',
            'Content-Disposition': 'attachment; filename="playlist.m3u"',
        }

    @app.route('/api/epg/channel/<int:cid>', methods=['GET'])
    def api_epg_channel_xml(cid):
        Channel.query.get_or_404(cid)
        from epg_generator import generate_xmltv
        days = request.args.get('days', 7, type=int)
        xml = generate_xmltv(app, days=days, channel_id=cid)
        return xml, 200, {'Content-Type': 'application/xml; charset=utf-8'}

    @app.route('/api/epg/channel/<int:cid>/m3u', methods=['GET'])
    def api_epg_channel_m3u(cid):
        ch = Channel.query.get_or_404(cid)
        from epg_generator import generate_m3u
        m3u = generate_m3u(app, channel_id=cid)
        slug = ch.name.lower().replace(' ', '_')
        return m3u, 200, {
            'Content-Type': 'application/x-mpegurl; charset=utf-8',
            'Content-Disposition': f'attachment; filename="ch{cid}_{slug}.m3u"',
        }

    @app.route('/api/epg/upload', methods=['POST'])
    def api_epg_upload():
        import threading
        threading.Thread(target=sftp_uploader.upload_epg, args=(app,),
                         daemon=True).start()
        return jsonify({'ok': True, 'message': 'Upload started in background'})

    @app.route('/api/epg/status', methods=['GET'])
    def api_epg_status():
        return jsonify(sftp_uploader.get_status())

    @app.route('/api/sftp/test', methods=['POST'])
    def api_sftp_test():
        ok, msg = sftp_uploader.test_connection(app)
        return jsonify({'ok': ok, 'message': msg})

    # ------------------------------------------------------------------ #
    #  SCTE events API                                                     #
    # ------------------------------------------------------------------ #

    @app.route('/api/scte', methods=['GET'])
    def api_scte_events():
        channel_id = request.args.get('channel_id', type=int)
        limit = min(int(request.args.get('limit', 50)), 500)
        q = SCTEEvent.query.order_by(SCTEEvent.detected_at.desc())
        if channel_id:
            q = q.filter_by(channel_id=channel_id)
        return jsonify([e.to_dict() for e in q.limit(limit).all()])

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _entry_from_data(data):
        entry = ScheduleEntry()
        _apply_entry_data(entry, data)
        return entry

    def _apply_entry_data(entry, data):
        if 'channel_id' in data:
            entry.channel_id = int(data['channel_id'])
        if 'title' in data:
            entry.title = data['title']
        if 'entry_type' in data:
            entry.entry_type = data['entry_type']
        if 'asset_path' in data:
            entry.asset_path = data['asset_path']
        if 'live_source' in data:
            entry.live_source = data['live_source']
        if 'start_time' in data:
            try:
                st = datetime.fromisoformat(str(data['start_time']).replace('Z', '+00:00'))
                entry.start_time = st.replace(tzinfo=None)
            except ValueError as e:
                raise ValueError(f'Invalid start_time: {e}')
        if 'duration' in data:
            entry.duration = int(data['duration'])
        if 'rrule' in data:
            val = (data['rrule'] or '').strip()
            if val.upper().startswith('RRULE:'):
                val = val[6:].strip()
            # Auto-fix partial RRULE saved without FREQ
            if val and 'FREQ=' not in val.upper():
                if any(k in val.upper() for k in ('BYDAY=', 'BYMONTHDAY=', 'BYWEEKNO=')):
                    val = 'FREQ=WEEKLY;' + val
            entry.rrule = val or None
        if 'exdates' in data:
            entry.set_exdates(data['exdates'])
        if 'color' in data:
            entry.color = data['color']
        if 'notes' in data:
            entry.notes = data['notes']
        if 'loop_enabled' in data:
            entry.loop_enabled = bool(data['loop_enabled'])
        if 'recording_path' in data:
            entry.recording_path = (data['recording_path'] or '').strip() or None

        if not entry.title:
            raise ValueError('title is required')
        if not entry.channel_id:
            raise ValueError('channel_id is required')
        if not entry.start_time:
            raise ValueError('start_time is required')

    return app


application = create_app()
