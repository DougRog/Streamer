import os
import json
import logging
from datetime import datetime, timedelta, date

from flask import Flask, render_template, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy

import config
from models import db, Channel, ScheduleEntry, MediaAsset, SCTEEvent
from stream_manager import stream_manager
from media_pool import scan_pool, scan_status
from dektec_handler import status as dektec_status
from scheduler import init_scheduler, expand_occurrences, get_current_item, get_next_item

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = config.DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    stream_manager.init_app(app)

    with app.app_context():
        db.create_all()
        os.makedirs(config.MEDIA_POOL_PATH, exist_ok=True)
        os.makedirs(config.RECORDING_PATH, exist_ok=True)

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
        return render_template(
            'index.html',
            channels=channels,
            stream_manager=stream_manager,
            recent_scte=recent_scte,
            dektec=dektec_status(),
        )

    @app.route('/calendar')
    def calendar():
        channels = Channel.query.order_by(Channel.name).all()
        return render_template('calendar.html', channels=channels)

    @app.route('/channels')
    def channels():
        channels = Channel.query.order_by(Channel.name).all()
        return render_template('channels.html', channels=channels)

    @app.route('/assets')
    def assets():
        assets = MediaAsset.query.order_by(MediaAsset.filename).all()
        return render_template(
            'assets.html',
            assets=assets,
            scan=scan_status(),
            pool_path=config.MEDIA_POOL_PATH,
        )

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
                      'video_bitrate', 'slate_enabled', 'color', 'notes'):
            if field in data:
                setattr(ch, field, data[field])
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

        # Normalise to naive UTC
        if start.tzinfo:
            start = start.replace(tzinfo=None)
        if end.tzinfo:
            end = end.replace(tzinfo=None)

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
                date_str = occ_start.date().isoformat()
                override = override_map.get((entry.id, date_str))

                if override:
                    ov_start = datetime.combine(
                        override.override_date, override.start_time.time()
                    )
                    ov_end = ov_start + timedelta(seconds=override.duration)
                    ev = _fc_event(override, ov_start, ov_end,
                                   isOverride=True, parentId=entry.id,
                                   occurrenceDate=date_str)
                else:
                    ev = _fc_event(entry, occ_start, occ_end,
                                   isRecurring=bool(entry.rrule),
                                   occurrenceDate=date_str)
                events.append(ev)

        return jsonify(events)

    def _fc_event(entry, start, end, **extra_props):
        return {
            'id': f'entry-{entry.id}-{start.date().isoformat()}',
            'title': entry.title,
            'start': start.isoformat(),
            'end': end.isoformat(),
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
    #  Assets API                                                          #
    # ------------------------------------------------------------------ #

    @app.route('/api/assets', methods=['GET'])
    def api_list_assets():
        q = request.args.get('q', '').strip()
        query = MediaAsset.query
        if q:
            query = query.filter(MediaAsset.filename.ilike(f'%{q}%'))
        assets = query.order_by(MediaAsset.filename).all()
        return jsonify([a.to_dict() for a in assets])

    @app.route('/api/assets/scan', methods=['POST'])
    def api_scan_assets():
        ok, msg = scan_pool(app)
        return jsonify({'ok': ok, 'message': msg})

    @app.route('/api/assets/scan/status', methods=['GET'])
    def api_scan_status():
        return jsonify(scan_status())

    @app.route('/api/assets/<int:aid>', methods=['GET'])
    def api_get_asset(aid):
        return jsonify(MediaAsset.query.get_or_404(aid).to_dict())

    # ------------------------------------------------------------------ #
    #  Dektec / Live / Recording API                                       #
    # ------------------------------------------------------------------ #

    @app.route('/api/dektec/status', methods=['GET'])
    def api_dektec_status():
        return jsonify(dektec_status())

    @app.route('/api/recording/start', methods=['POST'])
    def api_start_recording():
        data = request.get_json(force=True) or {}
        source = data.get('live_source', 'dektec:0:0')
        channel_id = data.get('channel_id')
        channel = Channel.query.get(channel_id) if channel_id else None
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
            entry.rrule = data['rrule'] or None
        if 'exdates' in data:
            entry.set_exdates(data['exdates'])
        if 'color' in data:
            entry.color = data['color']
        if 'notes' in data:
            entry.notes = data['notes']

        if not entry.title:
            raise ValueError('title is required')
        if not entry.channel_id:
            raise ValueError('channel_id is required')
        if not entry.start_time:
            raise ValueError('start_time is required')

    return app


application = create_app()
