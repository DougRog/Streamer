/* ================================================================== */
/*  Streamer — FullCalendar 6 schedule application                   */
/* ================================================================== */

let calendar;
let currentEvent = null;

const eventModal     = new bootstrap.Modal(document.getElementById('eventModal'));
const repeatDayModal = new bootstrap.Modal(document.getElementById('repeatDayModal'));

// ------------------------------------------------------------------ //
//  Calendar init                                                      //
// ------------------------------------------------------------------ //
document.addEventListener('DOMContentLoaded', () => {
  // Pre-select channel from ?channel=N
  const params = new URLSearchParams(location.search);
  const urlCh = params.get('channel');
  if (urlCh) document.getElementById('channel-filter').value = urlCh;

  calendar = new FullCalendar.Calendar(document.getElementById('calendar'), {
    initialView:   'timeGridWeek',
    headerToolbar: {
      left:   'prev,next today',
      center: 'title',
      right:  'dayGridMonth,timeGridWeek,timeGridDay,listWeek',
    },
    timeZone:      'America/New_York',
    height:        'auto',
    nowIndicator:  true,
    editable:      true,
    selectable:    true,
    scrollTime:    '06:00:00',
    slotDuration:  '00:30:00',
    snapDuration:  '00:05:00',
    eventMinHeight: 28,

    events: fetchEvents,

    // Drag to reschedule
    eventDrop(info) {
      const ep = info.event.extendedProps;
      if (ep.isRecurring) {
        showDropScopePopup(info, ep);
      } else {
        patchEntry(ep.entryId, { start_time: info.event.start.toISOString() })
          .then(() => calendar.refetchEvents());
      }
    },

    // Resize → duration change
    eventResize(info) {
      const ep  = info.event.extendedProps;
      const dur = Math.round((info.event.end - info.event.start) / 1000);
      if (ep.isRecurring) {
        apiCreateOverride(ep.entryId, ep.occurrenceDate,
                          info.event.start, info.event.end, dur)
          .then(() => calendar.refetchEvents());
      } else {
        patchEntry(ep.entryId, { duration: dur })
          .then(() => calendar.refetchEvents());
      }
    },

    // Click on empty slot → new event
    dateClick(info) {
      openNewEventModal(info.dateStr);
    },

    // Click on event → edit
    eventClick(info) {
      openEditModal(info.event);
    },

    // Custom event rendering
    eventContent(arg) {
      const ep = arg.event.extendedProps;
      const icons = [];
      if (ep.isRecurring)  icons.push('<i class="bi bi-arrow-repeat type-icon"></i>');
      if (ep.isOverride)   icons.push('<i class="bi bi-pencil-square type-icon"></i>');
      if (ep.loopEnabled)  icons.push('<i class="bi bi-arrow-clockwise type-icon"></i>');
      if (ep.entryType === 'live')      icons.push('<i class="bi bi-camera-video type-icon"></i>');
      if (ep.entryType === 'recording') icons.push('<i class="bi bi-record-circle type-icon"></i>');
      return {
        html: `<div style="padding:3px 5px;overflow:hidden;height:100%">
                 <div style="font-size:0.75rem;font-weight:600;line-height:1.3;
                             overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                   ${escHtml(arg.event.title)}
                 </div>
                 <div style="font-size:0.68rem;opacity:0.75;display:flex;gap:4px;align-items:center;margin-top:1px">
                   ${escHtml(ep.channelName || '')}
                   ${icons.join('')}
                 </div>
               </div>`,
      };
    },
  });

  calendar.render();

  // Duration → human-readable hint
  document.getElementById('f-duration').addEventListener('input', updateDurationHint);

  // Type radio → toggle panels + highlight selected
  document.querySelectorAll('input[name=entryType]').forEach(r => {
    r.addEventListener('change', e => {
      toggleTypePanels(e.target.value);
      highlightTypeLabel(e.target.value);
    });
  });

  // Edit-scope radios → highlight
  document.querySelectorAll('input[name=editMode]').forEach(r => {
    r.addEventListener('change', e => highlightScopeLabel(e.target.value));
  });
});

// ------------------------------------------------------------------ //
//  Event source                                                       //
// ------------------------------------------------------------------ //
function fetchEvents(info, successCb, failureCb) {
  const channelId = document.getElementById('channel-filter').value;
  let url = `/api/schedule?start=${info.startStr}&end=${info.endStr}`;
  if (channelId) url += `&channel_id=${channelId}`;
  fetch(url)
    .then(r => r.json())
    .then(successCb)
    .catch(failureCb);
}

function filterChanged() {
  calendar.refetchEvents();
}

// ------------------------------------------------------------------ //
//  New event modal                                                    //
// ------------------------------------------------------------------ //
function openNewEventModal(dateStr, assetPath, assetDuration) {
  resetForm();
  document.getElementById('eventModalTitle').textContent = 'New Event';
  document.getElementById('btn-delete-event').classList.add('d-none');
  document.getElementById('editScopeRow').classList.add('d-none');
  currentEvent = null;

  if (dateStr) {
    const dt = new Date(dateStr);
    document.getElementById('f-start').value = toInputValue(dt);
  }

  // Pre-fill asset if passed
  if (assetPath) {
    document.getElementById('f-asset-path').value = assetPath;
    if (assetDuration > 0)
      document.getElementById('f-duration').value = secsToHMS(Math.round(assetDuration));
  }

  const ch = document.getElementById('channel-filter').value;
  if (ch) document.getElementById('f-channel').value = ch;

  updateDurationHint();
  eventModal.show();
}

// ------------------------------------------------------------------ //
//  Edit modal                                                         //
// ------------------------------------------------------------------ //
function openEditModal(fcEvent) {
  currentEvent = fcEvent;
  const ep = fcEvent.extendedProps;

  resetForm();
  document.getElementById('eventModalTitle').textContent = 'Edit Event';
  document.getElementById('btn-delete-event').classList.remove('d-none');

  if (ep.isRecurring) {
    document.getElementById('editScopeRow').classList.remove('d-none');
  }

  document.getElementById('f-entry-id').value        = ep.entryId;
  document.getElementById('f-occurrence-date').value  = ep.occurrenceDate || '';
  document.getElementById('f-title').value            = fcEvent.title;
  document.getElementById('f-channel').value          = ep.channelId;
  document.getElementById('f-start').value            = toInputValue(fcEvent.start);
  document.getElementById('f-duration').value         = secsToHMS(ep.duration);
  document.getElementById('f-rec-path').value         = ep.recordingPath || '';
  document.getElementById('f-asset-path').value       = ep.assetPath || '';
  document.getElementById('f-live-source').value      = ep.liveSource || (LIVE_PRESETS[0] && LIVE_PRESETS[0].url) || '';
  _populatePresetDropdown(ep.liveSource);
  document.getElementById('f-rrule').value            = ep.rrule || '';
  document.getElementById('f-notes').value            = ep.notes || '';
  document.getElementById('f-color').value            = fcEvent.backgroundColor || '#6366f1';
  document.getElementById('f-loop-enabled').checked   = !!ep.loopEnabled;
  toggleLoopHint();

  const typeVal = ep.entryType || 'file';
  document.querySelector(`input[name=entryType][value=${typeVal}]`).checked = true;
  toggleTypePanels(typeVal);
  highlightTypeLabel(typeVal);
  syncRruleChip(ep.rrule || '');
  updateDurationHint();

  // Auto-expand notes section if there's content
  if (ep.notes) {
    document.getElementById('advancedFields').classList.add('show');
  }

  eventModal.show();
}

// ------------------------------------------------------------------ //
//  Save                                                               //
// ------------------------------------------------------------------ //
function saveEvent() {
  const entryId     = document.getElementById('f-entry-id').value;
  const occDate     = document.getElementById('f-occurrence-date').value;
  const editMode    = document.querySelector('input[name=editMode]:checked')?.value || 'all';
  const entryType   = document.querySelector('input[name=entryType]:checked').value;
  const isRecurring = currentEvent?.extendedProps?.isRecurring;

  const startVal = document.getElementById('f-start').value;
  if (!startVal) { showToast('Start time is required', 'warning'); return; }

  const payload = {
    channel_id:   parseInt(document.getElementById('f-channel').value),
    title:        document.getElementById('f-title').value.trim(),
    entry_type:   entryType,
    asset_path:   document.getElementById('f-asset-path').value.trim() || null,
    live_source:  document.getElementById('f-live-source').value.trim() || (LIVE_PRESETS[0] && LIVE_PRESETS[0].url) || '',
    start_time:   easternToISO(startVal),
    duration:     (entryType === 'file' && document.getElementById('f-loop-enabled').checked)
                    ? 3600    // nominal; scheduler runs it indefinitely until preempted
                    : hmsToSecs(document.getElementById('f-duration').value),
    recording_path: entryType === 'recording'
                    ? (document.getElementById('f-rec-path').value.trim() || null)
                    : null,
    rrule:        document.getElementById('f-rrule').value.trim() || null,
    color:        document.getElementById('f-color').value,
    notes:        document.getElementById('f-notes').value.trim(),
    loop_enabled: entryType === 'file' && document.getElementById('f-loop-enabled').checked,
  };

  if (!payload.title)      { showToast('Title is required', 'warning'); return; }
  if (!payload.channel_id) { showToast('Channel is required', 'warning'); return; }
  if (!payload.duration)   { showToast('Duration is required', 'warning'); return; }

  let promise;
  if (!entryId) {
    promise = apiPost('/api/schedule', payload);
  } else if (isRecurring && editMode === 'this' && occDate) {
    promise = apiPost(`/api/schedule/${entryId}/override`,
                      { ...payload, occurrence_date: occDate });
  } else {
    promise = apiPut(`/api/schedule/${entryId}`, payload);
  }

  promise.then(d => {
    if (d.error) { showToast('Save failed: ' + d.error, 'danger'); return; }
    eventModal.hide();
    calendar.refetchEvents();
    showToast(entryId ? 'Event updated' : 'Event created', 'success');
  }).catch(() => showToast('Network error', 'danger'));
}

// ------------------------------------------------------------------ //
//  Delete                                                             //
// ------------------------------------------------------------------ //
function deleteEvent() {
  const entryId  = document.getElementById('f-entry-id').value;
  const occDate  = document.getElementById('f-occurrence-date').value;
  const editMode = document.querySelector('input[name=editMode]:checked')?.value || 'all';
  const isRec    = currentEvent?.extendedProps?.isRecurring;
  if (!entryId) return;

  const scope = (isRec && editMode === 'this') ? 'this' : 'all';
  if (scope === 'all' && !confirm('Delete all occurrences of this event?')) return;

  let url = `/api/schedule/${entryId}?scope=${scope}`;
  if (scope === 'this' && occDate) url += `&date=${occDate}`;

  fetch(url, { method: 'DELETE' })
    .then(() => {
      eventModal.hide();
      calendar.refetchEvents();
      showToast(scope === 'this' ? 'Occurrence removed' : 'Event deleted', 'info');
    });
}

// ------------------------------------------------------------------ //
//  Drop scope popup (for dragging recurring events)                  //
// ------------------------------------------------------------------ //
function showDropScopePopup(info, ep) {
  // Remove any existing popup
  document.querySelector('.drop-scope-popup')?.remove();

  const rect = info.el.getBoundingClientRect();
  const popup = document.createElement('div');
  popup.className = 'drop-scope-popup';
  popup.style.cssText = `
    position:fixed;top:${rect.bottom + 6}px;left:${rect.left}px;
    background:var(--bg-elevated);border:1px solid var(--border-md);
    border-radius:var(--radius-md);padding:8px;z-index:9999;
    box-shadow:var(--shadow);min-width:200px`;
  popup.innerHTML = `
    <div style="font-size:0.78rem;color:var(--text-sub);margin-bottom:6px;padding:0 4px">
      Move recurring event:
    </div>
    <button class="btn btn-sm btn-ghost w-100 text-start mb-1"
            onclick="applyDropThis(event, '${ep.entryId}', '${ep.occurrenceDate}')">
      <i class="bi bi-calendar-event me-1"></i>This occurrence only
    </button>
    <button class="btn btn-sm btn-ghost w-100 text-start"
            onclick="applyDropAll(event, '${ep.entryId}')">
      <i class="bi bi-arrow-repeat me-1"></i>All occurrences
    </button>`;

  document.body.appendChild(popup);

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', function close(e) {
      if (!popup.contains(e.target)) { popup.remove(); calendar.refetchEvents(); }
      document.removeEventListener('click', close);
    });
  }, 0);

  // Store drop info on popup for the callback
  popup._dropInfo = info;
}

function applyDropThis(e, entryId, occDate) {
  e.stopPropagation();
  const popup = e.target.closest('.drop-scope-popup');
  const info = popup._dropInfo;
  popup.remove();
  apiCreateOverride(entryId, occDate, info.event.start, info.event.end)
    .then(() => calendar.refetchEvents());
}
function applyDropAll(e, entryId) {
  e.stopPropagation();
  const popup = e.target.closest('.drop-scope-popup');
  const info = popup._dropInfo;
  popup.remove();
  patchEntry(entryId, { start_time: info.event.start.toISOString() })
    .then(() => calendar.refetchEvents());
}

// ------------------------------------------------------------------ //
//  Repeat Day                                                         //
// ------------------------------------------------------------------ //
function openRepeatDayModal() {
  // Default date to the currently viewed week's Monday (or today)
  const d = calendar.getDate();
  // calendar.getDate() returns a JS Date; get the Eastern day-of-week via Intl
  const etDayStr = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(d);
  const etDow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].indexOf(etDayStr);
  const daysToMon = (etDow + 6) % 7;
  const monday = new Date(d.getTime() - daysToMon * 86400000);
  document.getElementById('rd-date').value = toDateString(monday);
  document.getElementById('rd-until').value = '';
  document.getElementById('rd-preview-list').innerHTML =
    '<div class="text-muted" style="font-size:0.82rem">Select a date to preview events.</div>';
  document.getElementById('btn-repeat-apply').disabled = true;
  loadRepeatPreview();
  repeatDayModal.show();
}

function loadRepeatPreview() {
  const chId = document.getElementById('rd-channel').value;
  const date  = document.getElementById('rd-date').value;
  if (!chId || !date) return;

  const dayStart = easternToISO(date + 'T00:00');
  const dayEnd   = easternToISO(date + 'T23:59');

  fetch(`/api/schedule?start=${dayStart}&end=${dayEnd}&channel_id=${chId}`)
    .then(r => r.json())
    .then(events => {
      // Only show one-time (non-recurring) events — those are what will be made weekly
      const oneTime = events.filter(e => !e.extendedProps.isRecurring && !e.extendedProps.isOverride);
      const btn = document.getElementById('btn-repeat-apply');

      if (!oneTime.length) {
        document.getElementById('rd-preview-list').innerHTML =
          `<div class="text-muted" style="font-size:0.82rem">
             No one-time events on this day.
             ${events.length ? `(${events.length} already-recurring events were found and will be skipped.)` : ''}
           </div>`;
        btn.disabled = true;
        return;
      }

      document.getElementById('rd-preview-list').innerHTML = oneTime.map(ev => {
        const start = new Date(ev.start);
        const pad = n => String(n).padStart(2,'0');
        const timeStr = `${pad(start.getUTCHours())}:${pad(start.getUTCMinutes())}`;
        const dur = ev.extendedProps.duration;
        const h = Math.floor(dur/3600), m = Math.floor((dur%3600)/60);
        const durStr = h ? `${h}h ${m}m` : `${m}m`;
        return `<div class="repeat-preview-item">
          <span class="time">${timeStr}</span>
          <span style="color:#fff;flex:1">${escHtml(ev.title)}</span>
          <span class="text-muted">${durStr}</span>
          <span class="pill pill-live ms-1" style="font-size:0.65rem">→ weekly</span>
        </div>`;
      }).join('');

      btn.disabled = false;
    });
}

function applyRepeatDay() {
  const chId  = document.getElementById('rd-channel').value;
  const date  = document.getElementById('rd-date').value;
  const until = document.getElementById('rd-until').value?.replace(/-/g, '');

  const body = { channel_id: parseInt(chId), date };
  if (until) body.until = until;

  fetch('/api/schedule/repeat-day', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { showToast('Error: ' + (d.error || 'unknown'), 'danger'); return; }
      repeatDayModal.hide();
      calendar.refetchEvents();
      const day = new Date(date).toLocaleDateString('en-US', {weekday:'long', timeZone:'UTC'});
      showToast(
        d.count
          ? `${d.count} event${d.count > 1 ? 's' : ''} on ${day} will now repeat weekly`
          : 'No events were updated',
        d.count ? 'success' : 'info'
      );
    })
    .catch(() => showToast('Network error', 'danger'));
}


// ------------------------------------------------------------------ //
//  Helpers — API                                                      //
// ------------------------------------------------------------------ //
function apiPost(url, body) {
  return fetch(url, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
}
function apiPut(url, body) {
  return fetch(url, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }).then(r => r.json());
}
function patchEntry(entryId, patch) {
  return fetch(`/api/schedule/${entryId}`)
    .then(r => r.json())
    .then(existing => apiPut(`/api/schedule/${entryId}`, { ...existing, ...patch }));
}
function apiCreateOverride(parentId, occDate, newStart, newEnd, forceDur) {
  const dur = forceDur || Math.round((newEnd - newStart) / 1000);
  return apiPost(`/api/schedule/${parentId}/override`, {
    occurrence_date: occDate,
    start_time: newStart.toISOString(),
    duration: dur,
  });
}

// ------------------------------------------------------------------ //
//  Helpers — UI                                                       //
// ------------------------------------------------------------------ //
function setRrule(val, chipEl) {
  document.getElementById('f-rrule').value = val;
  document.querySelectorAll('.rrule-chip').forEach(c => c.classList.remove('active'));
  if (chipEl) chipEl.classList.add('active');
}

function syncRruleChip(val) {
  document.querySelectorAll('.rrule-chip').forEach(c => {
    c.classList.toggle('active',
      c.getAttribute('onclick').includes(`'${val}'`));
  });
}

function resetForm() {
  ['f-entry-id','f-occurrence-date','f-title','f-start',
   'f-asset-path','f-rrule','f-notes','f-rec-path'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  document.getElementById('f-live-source').value = (LIVE_PRESETS[0] && LIVE_PRESETS[0].url) || '';
  _populatePresetDropdown(document.getElementById('f-live-source').value);
  document.getElementById('f-duration').value      = '1:00:00';
  document.getElementById('f-color').value         = '#6366f1';
  document.getElementById('f-loop-enabled').checked = false;
  toggleLoopHint();
  document.querySelector('input[name=entryType][value=file]').checked = true;
  document.querySelector('input[name=editMode][value=this]').checked  = true;
  toggleTypePanels('file');
  highlightTypeLabel('file');
  highlightScopeLabel('this');
  document.querySelectorAll('.rrule-chip').forEach(c => c.classList.remove('active'));
  document.getElementById('advancedFields').classList.remove('show');
  updateDurationHint();
}

function _populatePresetDropdown(currentUrl) {
  const sel = document.getElementById('f-preset-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">— select a preset —</option>' +
    (LIVE_PRESETS || []).map(p =>
      `<option value="${escHtml(p.url)}" ${p.url === currentUrl ? 'selected' : ''}>${escHtml(p.name)}</option>`
    ).join('');
}

function onModalPresetSelect() {
  const url = document.getElementById('f-preset-select').value;
  if (url) document.getElementById('f-live-source').value = url;
}

function toggleTypePanels(type) {
  document.getElementById('filePicker').classList.toggle('d-none', type !== 'file');
  document.getElementById('liveSource').classList.toggle('d-none', type === 'file');
  document.getElementById('recordingPath').classList.toggle('d-none', type !== 'recording');
  // Duration doesn't apply to loop files; still show it for recording/live
  if (type !== 'file') {
    document.getElementById('f-loop-enabled').checked = false;
    toggleLoopHint();
  }
}

function toggleLoopHint() {
  const on = document.getElementById('f-loop-enabled').checked;
  document.getElementById('loop-hint').style.display = on ? 'block' : 'none';
  document.getElementById('duration-col').classList.toggle('d-none', on);
  document.getElementById('loop-duration-col').classList.toggle('d-none', !on);
}

function highlightTypeLabel(type) {
  ['file','live','rec'].forEach(t => {
    const el = document.getElementById(`type-${t}-label`);
    if (el) el.style.borderColor = '';
  });
  const active = document.getElementById(`type-${type}-label`);
  if (active) active.style.borderColor = 'var(--accent)';
}

function highlightScopeLabel(scope) {
  ['this','all'].forEach(s => {
    const el = document.getElementById(`scope-${s}-label`);
    if (el) el.style.borderColor = '';
  });
  const active = document.getElementById(`scope-${scope}-label`);
  if (active) active.style.borderColor = 'var(--accent)';
}

function updateDurationHint() { /* no-op: duration now shown as H:MM:SS in the field */ }

/** Convert seconds to "H:MM:SS" display string. */
function secsToHMS(s) {
  const sec = Math.max(0, Math.round(s || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const ss = sec % 60;
  return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
}

/** Parse "H:MM:SS", "M:SS", plain seconds, or plain number → integer seconds. */
function hmsToSecs(str) {
  const s = String(str || '').trim();
  if (!s) return 0;
  if (/^\d+$/.test(s)) return parseInt(s, 10);
  const parts = s.split(':').map(p => parseInt(p, 10) || 0);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return 0;
}

function validateAssetPath() {
  const path = (document.getElementById('f-asset-path').value || '').trim();
  const el   = document.getElementById('path-validation');
  if (!path) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = '<i class="bi bi-arrow-repeat spin me-1"></i>Checking…';
  el.style.color = 'var(--text-muted)';
  fetch('/api/validate_path', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.exists) {
        el.innerHTML = '<i class="bi bi-x-circle-fill me-1"></i>File not found on server';
        el.style.color = 'var(--danger,#ef4444)';
        return;
      }
      el.style.color = 'var(--success)';
      let html = '<i class="bi bi-check-circle-fill me-1"></i>File found';
      const parts = [];
      if (d.video_codec)  parts.push(d.video_codec.toUpperCase());
      if (d.video_width && d.video_height) parts.push(`${d.video_width}×${d.video_height}`);
      if (d.video_fps)    parts.push(`${parseFloat(d.video_fps).toFixed(2)} fps`);
      if (d.audio_codec)  parts.push(d.audio_codec.toUpperCase());
      if (d.has_scte)     parts.push('SCTE-35');
      if (parts.length)
        html += ` <span class="text-muted" style="font-size:0.78rem">— ${parts.join(' · ')}</span>`;
      if (d.duration_seconds) {
        const hms = secsToHMS(Math.round(d.duration_seconds));
        html += ` <button type="button" class="btn btn-xs btn-ghost ms-2"
                          onclick="document.getElementById('f-duration').value='${hms}';
                                   this.textContent='✓ Applied'"
                          style="font-size:0.72rem;padding:1px 6px">
                    Use ${hms}
                  </button>`;
      }
      el.innerHTML = html;
    })
    .catch(() => {
      el.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>Validation failed';
      el.style.color = 'var(--warning,#f59e0b)';
    });
}

// ------------------------------------------------------------------ //
//  Eastern timezone helpers                                          //
// ------------------------------------------------------------------ //
const _ET_FMT_FULL = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
});
const _ET_FMT_DATE = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric', month: '2-digit', day: '2-digit',
});

function _etParts(dt, fmt) {
  const p = {};
  fmt.formatToParts(dt).forEach(({type, value}) => { p[type] = value; });
  return p;
}

/** Format a JS Date as an Eastern "YYYY-MM-DDTHH:MM" string for datetime-local inputs. */
function toInputValue(dt) {
  const p = _etParts(dt, _ET_FMT_FULL);
  const hh = p.hour === '24' ? '00' : p.hour;
  return `${p.year}-${p.month}-${p.day}T${hh}:${p.minute}`;
}

/** Format a JS Date as an Eastern "YYYY-MM-DD" string for date inputs. */
function toDateString(dt) {
  const p = _etParts(dt, _ET_FMT_DATE);
  return `${p.year}-${p.month}-${p.day}`;
}

/**
 * Convert an Eastern datetime-local string ("YYYY-MM-DDTHH:MM") to a UTC ISO string.
 * Uses the Intl API to find the correct UTC offset for that ET moment, handling DST.
 */
function easternToISO(dtStr) {
  const tentative = new Date(dtStr + ':00Z');   // parse as UTC tentatively
  const p = _etParts(tentative, _ET_FMT_FULL);  // what ET time is that UTC?
  const hh = p.hour === '24' ? '00' : p.hour;
  const etAsUtc = new Date(`${p.year}-${p.month}-${p.day}T${hh}:${p.minute}:00Z`);
  const offsetMs = tentative - etAsUtc;          // ET is behind UTC → positive
  return new Date(tentative.getTime() + offsetMs).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
