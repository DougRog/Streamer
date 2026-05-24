from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()


class Channel(db.Model):
    __tablename__ = 'channels'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    multicast_addr = db.Column(db.String(50), nullable=False, default='239.1.1.1')
    multicast_port = db.Column(db.Integer, nullable=False, default=5000)
    video_bitrate = db.Column(db.String(10), default='6M')
    is_active = db.Column(db.Boolean, default=False)
    slate_enabled = db.Column(db.Boolean, default=True)
    color = db.Column(db.String(20), default='#3788d8')
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    schedule_entries = db.relationship(
        'ScheduleEntry', backref='channel', lazy='dynamic',
        foreign_keys='ScheduleEntry.channel_id',
        cascade='all, delete-orphan'
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'multicast_addr': self.multicast_addr,
            'multicast_port': self.multicast_port,
            'multicast_url': f'udp://{self.multicast_addr}:{self.multicast_port}',
            'video_bitrate': self.video_bitrate,
            'is_active': self.is_active,
            'slate_enabled': self.slate_enabled,
            'color': self.color,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ScheduleEntry(db.Model):
    __tablename__ = 'schedule_entries'

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    # Types: 'file', 'live', 'recording' (live input → record to MXF + optionally stream)
    entry_type = db.Column(db.String(20), nullable=False, default='file')
    asset_path = db.Column(db.String(500))
    live_source = db.Column(db.String(100), default='dektec:0:0')
    start_time = db.Column(db.DateTime, nullable=False)
    duration = db.Column(db.Integer, nullable=False, default=3600)   # seconds
    # RRULE string, e.g. FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
    rrule = db.Column(db.String(500))
    # JSON list of date strings to skip: ["2024-03-25", ...]
    exdates = db.Column(db.Text, default='[]')
    # Override linkage
    parent_id = db.Column(db.Integer, db.ForeignKey('schedule_entries.id'), nullable=True)
    override_date = db.Column(db.Date, nullable=True)
    color = db.Column(db.String(20))
    notes = db.Column(db.Text)
    loop_enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    overrides = db.relationship(
        'ScheduleEntry', backref=db.backref('parent', remote_side=[id]),
        lazy='dynamic'
    )

    def get_exdates(self):
        try:
            return json.loads(self.exdates or '[]')
        except (ValueError, TypeError):
            return []

    def set_exdates(self, dates):
        self.exdates = json.dumps(list(dates))

    def effective_color(self):
        if self.color:
            return self.color
        if self.channel:
            return self.channel.color
        return '#3788d8'

    def to_dict(self):
        return {
            'id': self.id,
            'channel_id': self.channel_id,
            'title': self.title,
            'entry_type': self.entry_type,
            'asset_path': self.asset_path,
            'live_source': self.live_source,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'duration': self.duration,
            'rrule': self.rrule,
            'exdates': self.get_exdates(),
            'parent_id': self.parent_id,
            'override_date': self.override_date.isoformat() if self.override_date else None,
            'color': self.effective_color(),
            'notes': self.notes,
            'loop_enabled': bool(self.loop_enabled),
        }


class MediaAsset(db.Model):
    __tablename__ = 'media_assets'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    path = db.Column(db.String(500), nullable=False, unique=True)
    duration_seconds = db.Column(db.Float)
    file_size = db.Column(db.BigInteger)
    has_scte = db.Column(db.Boolean, default=False)
    video_codec = db.Column(db.String(50))
    audio_codec = db.Column(db.String(50))
    video_width = db.Column(db.Integer)
    video_height = db.Column(db.Integer)
    video_fps = db.Column(db.String(20))
    color_space = db.Column(db.String(50))
    discovered_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_scanned = db.Column(db.DateTime)

    def formatted_duration(self):
        if not self.duration_seconds:
            return 'Unknown'
        total = int(self.duration_seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f'{h:02d}:{m:02d}:{s:02d}'

    def formatted_size(self):
        if not self.file_size:
            return 'Unknown'
        size = float(self.file_size)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024:
                return f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} PB'

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'path': self.path,
            'duration_seconds': self.duration_seconds,
            'duration': self.formatted_duration(),
            'file_size': self.file_size,
            'size': self.formatted_size(),
            'has_scte': self.has_scte,
            'video_codec': self.video_codec,
            'audio_codec': self.audio_codec,
            'video_width': self.video_width,
            'video_height': self.video_height,
            'video_fps': self.video_fps,
            'color_space': self.color_space,
            'discovered_at': self.discovered_at.isoformat() if self.discovered_at else None,
            'last_scanned': self.last_scanned.isoformat() if self.last_scanned else None,
        }


class AppSetting(db.Model):
    """Key/value store for runtime configuration (SFTP, EPG, etc.)."""
    __tablename__ = 'app_settings'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def get(cls, key, default=''):
        row = cls.query.get(key)
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.get(key)
        if row:
            row.value = str(value) if value is not None else ''
            row.updated_at = datetime.utcnow()
        else:
            db.session.add(cls(key=key, value=str(value) if value is not None else '',
                               updated_at=datetime.utcnow()))
        db.session.commit()

    @classmethod
    def bulk_set(cls, mapping):
        for k, v in mapping.items():
            row = cls.query.get(k)
            if row:
                row.value = str(v) if v is not None else ''
                row.updated_at = datetime.utcnow()
            else:
                db.session.add(cls(key=k, value=str(v) if v is not None else '',
                                   updated_at=datetime.utcnow()))
        db.session.commit()

    @classmethod
    def all_dict(cls):
        return {r.key: r.value for r in cls.query.all()}


class SCTEEvent(db.Model):
    __tablename__ = 'scte_events'

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey('channels.id'))
    event_id = db.Column(db.Integer)
    event_type = db.Column(db.String(50))
    pts_time = db.Column(db.BigInteger)
    duration_ticks = db.Column(db.BigInteger)
    splice_immediate = db.Column(db.Boolean, default=False)
    avail_num = db.Column(db.Integer)
    avails_expected = db.Column(db.Integer)
    out_of_network = db.Column(db.Boolean, default=False)
    raw_hex = db.Column(db.Text)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'channel_id': self.channel_id,
            'event_id': self.event_id,
            'event_type': self.event_type,
            'pts_time': self.pts_time,
            'out_of_network': self.out_of_network,
            'detected_at': self.detected_at.isoformat() if self.detected_at else None,
        }
