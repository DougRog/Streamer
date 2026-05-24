"""
Playout scheduler — runs in a background thread, wakes every
SCHEDULER_INTERVAL seconds and drives each active channel to the
correct FFmpeg process based on the current schedule.

Recurring events use iCalendar RRULE strings (parsed by python-dateutil).
Per-day overrides shadow the recurring entry for that specific date.
"""
import logging
import threading
from datetime import datetime, timedelta, date

from dateutil import rrule as rrulelib

from config import SCHEDULER_INTERVAL, PRETRANSITION_SECONDS

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  RRULE expansion                                                     #
# ------------------------------------------------------------------ #

def expand_occurrences(entry, window_start, window_end):
    """
    Return list of (occ_start, occ_end) for 'entry' that overlap
    [window_start, window_end].
    """
    if not entry.rrule:
        occ_end = entry.start_time + timedelta(seconds=entry.duration)
        if entry.start_time < window_end and occ_end > window_start:
            return [(entry.start_time, occ_end)]
        return []

    dtstart = entry.start_time
    # Strip accidental "RRULE:" prefix that users sometimes type
    rrule_str = entry.rrule.strip()
    if rrule_str.upper().startswith('RRULE:'):
        rrule_str = rrule_str[6:].strip()

    try:
        rule = rrulelib.rrulestr(
            f'DTSTART:{dtstart.strftime("%Y%m%dT%H%M%S")}\nRRULE:{rrule_str}'
        )
    except Exception as e:
        logger.warning(
            f'Bad RRULE on entry {entry.id} ({rrule_str!r}): {e} '
            f'— showing as one-time event. Fix or delete this entry.'
        )
        # Fall back: treat as a one-time event at its original start_time
        occ_end = entry.start_time + timedelta(seconds=entry.duration)
        if entry.start_time < window_end and occ_end > window_start:
            return [(entry.start_time, occ_end)]
        return []

    exdate_set = set(entry.get_exdates())
    search_from = window_start - timedelta(seconds=entry.duration)
    raw_occs = rule.between(search_from, window_end, inc=True)

    result = []
    for occ_start in raw_occs:
        if occ_start.strftime('%Y-%m-%d') in exdate_set:
            continue
        occ_end = occ_start + timedelta(seconds=entry.duration)
        if occ_start < window_end and occ_end > window_start:
            result.append((occ_start, occ_end))
    return result


def get_current_item(channel_id, at_time, app):
    """
    Return (entry, occ_start, occ_end) for the item that should be
    on-air on channel_id at at_time, or (None, None, None).
    Override entries shadow their parent for the specific date.
    """
    with app.app_context():
        from models import ScheduleEntry

        # Only top-level (non-override) entries
        entries = ScheduleEntry.query.filter_by(
            channel_id=channel_id, parent_id=None
        ).all()

        window = timedelta(hours=12)
        candidates = []

        for entry in entries:
            occs = expand_occurrences(
                entry,
                at_time - window,
                at_time + timedelta(seconds=1),
            )
            for occ_start, occ_end in occs:
                if not (occ_start <= at_time < occ_end):
                    continue

                # Check for a day-level override
                override = ScheduleEntry.query.filter_by(
                    parent_id=entry.id,
                    override_date=occ_start.date(),
                ).first()

                if override:
                    ov_start = datetime.combine(override.override_date,
                                                override.start_time.time())
                    ov_end = ov_start + timedelta(seconds=override.duration)
                    if ov_start <= at_time < ov_end:
                        candidates.append((override, ov_start, ov_end))
                    # Even if the override doesn't cover this moment, the
                    # original recurring slot is displaced — so don't add it.
                else:
                    candidates.append((entry, occ_start, occ_end))

        if not candidates:
            return None, None, None
        # Most recently started wins; ties broken by creation date
        return max(candidates, key=lambda x: (x[1], x[0].created_at))


def get_next_item(channel_id, after_time, app, lookahead_seconds=300):
    """Return (entry, occ_start, occ_end) for the next scheduled item."""
    with app.app_context():
        from models import ScheduleEntry
        entries = ScheduleEntry.query.filter_by(
            channel_id=channel_id, parent_id=None
        ).all()

        window_end = after_time + timedelta(seconds=lookahead_seconds)
        candidates = []

        for entry in entries:
            occs = expand_occurrences(entry, after_time, window_end)
            for occ_start, occ_end in occs:
                if occ_start > after_time:
                    override = ScheduleEntry.query.filter_by(
                        parent_id=entry.id,
                        override_date=occ_start.date(),
                    ).first()
                    eff_entry = override if override else entry
                    candidates.append((eff_entry, occ_start, occ_end))

        if not candidates:
            return None, None, None
        return min(candidates, key=lambda x: x[1])


# ------------------------------------------------------------------ #
#  Scheduler engine                                                    #
# ------------------------------------------------------------------ #

class PlayoutScheduler:

    def __init__(self, app, stream_mgr):
        self._app = app
        self._sm = stream_mgr
        self._stop_event = threading.Event()
        self._thread = None
        self._playing: dict = {}
        self._last_epg_upload = None   # datetime of last successful EPG push
        self._slate_last_attempt: dict = {}  # channel_id → last start attempt time

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='playout-scheduler'
        )
        self._thread.start()
        logger.info('Playout scheduler started')

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.wait(SCHEDULER_INTERVAL):
            try:
                self._tick()
                self._epg_tick()
            except Exception as e:
                logger.exception(f'Scheduler tick error: {e}')

    def _epg_tick(self):
        """Upload EPG if the configured interval has elapsed."""
        try:
            with self._app.app_context():
                from models import AppSetting
                interval_min = int(AppSetting.get('epg.interval_minutes', '30') or 0)
        except Exception:
            return

        if interval_min <= 0:
            return  # auto-upload disabled

        now = datetime.utcnow()
        if (self._last_epg_upload is None or
                (now - self._last_epg_upload).total_seconds() >= interval_min * 60):
            logger.info('EPG upload interval elapsed — uploading…')
            from sftp_uploader import upload_epg
            ok, msg = upload_epg(self._app)
            self._last_epg_upload = now
            if not ok:
                logger.warning(f'Auto EPG upload: {msg}')

    def _tick(self):
        now = datetime.utcnow()
        with self._app.app_context():
            from models import Channel
            channels = Channel.query.filter_by(is_active=True).all()

        for channel in channels:
            self._handle_channel(channel, now)

    def _handle_channel(self, channel, now):
        entry, occ_start, occ_end = get_current_item(channel.id, now, self._app)
        prev = self._playing.get(channel.id)

        # Nothing scheduled → show slate if enabled, otherwise nothing
        if entry is None:
            if prev is not None:
                logger.info(f'[ch{channel.id}] No content scheduled; {"starting slate" if channel.slate_enabled else "stopping"}')
                self._playing.pop(channel.id, None)
                if channel.slate_enabled:
                    self._sm.start_slate(channel)
                    self._slate_last_attempt[channel.id] = now
                else:
                    self._sm.stop_channel(channel.id)
            elif channel.slate_enabled and not self._sm.get_channel_info(channel.id)['running']:
                # Restart a dead slate — but back off if it's crashing rapidly
                last = self._slate_last_attempt.get(channel.id)
                if last is None or (now - last).total_seconds() >= 30:
                    self._sm.start_slate(channel)
                    self._slate_last_attempt[channel.id] = now
            return

        key = (entry.id, occ_start)
        if prev == key:
            # Correct item already running
            if not self._sm.get_channel_info(channel.id)['running']:
                # FFmpeg crashed — restart
                logger.warning(f'[ch{channel.id}] FFmpeg died, restarting "{entry.title}"')
                self._launch(channel, entry, occ_start)
            return

        # New item or resuming after gap
        logger.info(
            f'[ch{channel.id}] Switch → "{entry.title}" '
            f'({entry.entry_type}) start={occ_start.isoformat()}'
        )
        self._launch(channel, entry, occ_start)
        self._playing[channel.id] = key

    def _launch(self, channel, entry, occ_start):
        if entry.entry_type == 'file':
            ok = self._sm.start_file_stream(channel, entry, occ_start)
        elif entry.entry_type in ('live', 'recording'):
            ok = self._sm.start_live_stream(channel, entry)
        else:
            logger.error(f'Unknown entry_type: {entry.entry_type}')
            ok = False
        if not ok:
            logger.error(f'[ch{channel.id}] Failed to start "{entry.title}"')


scheduler = None


def init_scheduler(app, stream_mgr):
    global scheduler
    scheduler = PlayoutScheduler(app, stream_mgr)
    scheduler.start()
    return scheduler
