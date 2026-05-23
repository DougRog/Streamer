/* ------------------------------------------------------------------ */
/*  FullCalendar 6 – Streamer MCR schedule                            */
/* ------------------------------------------------------------------ */

let calendar;
let currentEvent = null;   // FC event object being edited
let allAssets = [];

const eventModal  = new bootstrap.Modal(document.getElementById('eventModal'));
const assetModal  = new bootstrap.Modal(document.getElementById('assetModal'));

// ------------------------------------------------------------------ //
//  Calendar init                                                      //
// ------------------------------------------------------------------ //
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('calendar');

  // Pre-select channel from query string ?channel=N
  const urlCh = new URLSearchParams(location.search).get('channel');
  if (urlCh) {
    const sel = document.getElementById('channel-filter');
    if (sel) sel.value = urlCh;
  }

  calendar = new FullCalendar.Calendar(el, {
    initialView: 'timeGridWeek',
    headerToolbar: {
      left:   'prev,next today',
      center: 'title',
      right:  'dayGridMonth,timeGridWeek,timeGridDay,listWeek',
    },
    height:      'auto',
    nowIndicator: true,
    editable:     true,
    selectable:   true,
    scrollTime:   '07:00:00',
    slotDuration: '00:30:00',

    events: fetchEvents,

    // Click on empty slot → new event
    dateClick(info) {
      openNewEventModal(info.dateStr);
    },

    // Click on existing event → edit
    eventClick(info) {
      openEditModal(info.event);
    },

    // Drag to reschedule
    eventDrop(info) {
      const ep = info.event.extendedProps;
      const newStart = info.event.start;
      const newEnd   = info.event.end;

      if (ep.isRecurring) {
        // Ask scope
        const scope = confirm(
          'Move all occurrences? (OK = all, Cancel = this one only)'
        ) ? 'all' : 'this';
        if (scope === 'this') {
          createOverride(ep.entryId, ep.occurrenceDate, newStart, newEnd, ep);
        } else {
          updateEntry(ep.entryId, { start_time: newStart.toISOString() });
        }
      } else {
        updateEntry(ep.entryId, { start_time: newStart.toISOString() });
      }
    },

    // Resize → update duration
    eventResize(info) {
      const ep  = info.event.extendedProps;
      const dur = Math.round((info.event.end - info.event.start) / 1000);
      if (ep.isRecurring) {
        createOverride(ep.entryId, ep.occurrenceDate,
                       info.event.start, info.event.end, ep, dur);
      } else {
        updateEntry(ep.entryId, { duration: dur });
      }
    },

    // Render channel name as subtitle
    eventContent(arg) {
      const ep = arg.event.extendedProps;
      return {
        html: `<div class="fc-event-main-frame p-1">
                 <div class="fc-event-title fw-bold">${arg.event.title}</div>
                 <div style="font-size:0.7rem;opacity:0.8;">
                   ${ep.channelName || ''}
                   ${ep.isRecurring ? ' <i class="bi bi-arrow-repeat"></i>' : ''}
                   ${ep.isOverride  ? ' <i class="bi bi-pencil-square"></i>' : ''}
                   ${ep.entryType === 'live' ? ' <i class="bi bi-camera-video"></i>' : ''}
                   ${ep.entryType === 'recording' ? ' <i class="bi bi-record-circle"></i>' : ''}
                 </div>
               </div>`,
      };
    },
  });

  calendar.render();

  // Load assets for the picker
  fetch('/api/assets')
    .then(r => r.json())
    .then(data => { allAssets = data; });
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
//  Modal – new event                                                  //
// ------------------------------------------------------------------ //
function openNewEventModal(dateStr) {
  resetForm();
  document.getElementById('eventModalTitle').textContent = 'New Event';
  document.getElementById('btn-delete-event').classList.add('d-none');
  document.getElementById('editModeRow').classList.add('d-none');
  currentEvent = null;

  if (dateStr) {
    // dateStr may be full ISO or just date — normalise
    const dt = new Date(dateStr);
    document.getElementById('f-start').value = toLocalInputValue(dt);
  }

  // Pre-select channel filter
  const ch = document.getElementById('channel-filter').value;
  if (ch) document.getElementById('f-channel').value = ch;

  eventModal.show();
}

// ------------------------------------------------------------------ //
//  Modal – edit existing event                                        //
// ------------------------------------------------------------------ //
function openEditModal(fcEvent) {
  currentEvent = fcEvent;
  const ep = fcEvent.extendedProps;

  resetForm();
  document.getElementById('eventModalTitle').textContent = 'Edit Event';
  document.getElementById('btn-delete-event').classList.remove('d-none');

  if (ep.isRecurring) {
    document.getElementById('editModeRow').classList.remove('d-none');
  }

  document.getElementById('f-entry-id').value       = ep.entryId;
  document.getElementById('f-occurrence-date').value = ep.occurrenceDate || '';
  document.getElementById('f-title').value          = fcEvent.title;
  document.getElementById('f-channel').value        = ep.channelId;
  document.getElementById('f-start').value          = toLocalInputValue(fcEvent.start);
  document.getElementById('f-duration').value       = ep.duration;
  document.getElementById('f-asset-path').value     = ep.assetPath || '';
  document.getElementById('f-live-source').value    = ep.liveSource || 'dektec:0:0';
  document.getElementById('f-rrule').value          = ep.rrule || '';
  document.getElementById('f-notes').value          = ep.notes || '';

  // Set type radio
  const typeVal = ep.entryType || 'file';
  document.querySelector(`input[name=entryType][value=${typeVal}]`).checked = true;
  toggleTypePanels(typeVal);

  // Color
  document.getElementById('f-color').value = fcEvent.backgroundColor || '#3788d8';

  eventModal.show();
}

// ------------------------------------------------------------------ //
//  Save event                                                         //
// ------------------------------------------------------------------ //
function saveEvent() {
  const entryId      = document.getElementById('f-entry-id').value;
  const occDate      = document.getElementById('f-occurrence-date').value;
  const editMode     = document.querySelector('input[name=editMode]:checked')?.value || 'all';
  const entryType    = document.querySelector('input[name=entryType]:checked').value;
  const isRecurring  = currentEvent?.extendedProps?.isRecurring;

  const startInput = document.getElementById('f-start').value;
  const startISO   = new Date(startInput).toISOString();

  const payload = {
    channel_id: parseInt(document.getElementById('f-channel').value),
    title:      document.getElementById('f-title').value.trim(),
    entry_type: entryType,
    asset_path: document.getElementById('f-asset-path').value.trim() || null,
    live_source: document.getElementById('f-live-source').value.trim() || 'dektec:0:0',
    start_time: startISO,
    duration:   parseInt(document.getElementById('f-duration').value),
    rrule:      document.getElementById('f-rrule').value.trim() || null,
    color:      document.getElementById('f-color').value,
    notes:      document.getElementById('f-notes').value.trim(),
  };

  if (!payload.title || !payload.channel_id || !payload.duration) {
    alert('Title, channel and duration are required.');
    return;
  }

  let promise;

  if (!entryId) {
    // Create new
    promise = fetch('/api/schedule', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
  } else if (isRecurring && editMode === 'this' && occDate) {
    // Create/update an override for this occurrence
    promise = fetch(`/api/schedule/${entryId}/override`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ...payload, occurrence_date: occDate }),
    });
  } else {
    // Update the main entry
    promise = fetch(`/api/schedule/${entryId}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
  }

  promise
    .then(r => r.json())
    .then(d => {
      if (d.error) { alert('Error: ' + d.error); return; }
      eventModal.hide();
      calendar.refetchEvents();
    })
    .catch(e => alert('Network error: ' + e));
}

// ------------------------------------------------------------------ //
//  Delete event                                                       //
// ------------------------------------------------------------------ //
function deleteEvent() {
  const entryId  = document.getElementById('f-entry-id').value;
  const occDate  = document.getElementById('f-occurrence-date').value;
  const editMode = document.querySelector('input[name=editMode]:checked')?.value || 'all';
  const isRec    = currentEvent?.extendedProps?.isRecurring;

  if (!entryId) return;

  const scope = (isRec && editMode === 'this') ? 'this' : 'all';
  let url = `/api/schedule/${entryId}?scope=${scope}`;
  if (scope === 'this' && occDate) url += `&date=${occDate}`;

  if (!confirm(scope === 'all' ? 'Delete all occurrences?' : 'Remove this occurrence?')) return;

  fetch(url, { method: 'DELETE' })
    .then(r => r.json())
    .then(() => {
      eventModal.hide();
      calendar.refetchEvents();
    });
}

// ------------------------------------------------------------------ //
//  Asset picker                                                       //
// ------------------------------------------------------------------ //
function openAssetPicker() {
  renderAssetList(allAssets);
  assetModal.show();
}

function filterAssets() {
  const q = document.getElementById('asset-search').value.toLowerCase();
  const filtered = allAssets.filter(a => a.filename.toLowerCase().includes(q));
  renderAssetList(filtered);
}

function renderAssetList(assets) {
  const html = `
    <table class="table table-dark table-hover table-sm mb-0">
      <thead><tr>
        <th>Filename</th><th>Duration</th><th>Res</th><th>SCTE</th>
      </tr></thead>
      <tbody>
        ${assets.map(a => `
          <tr style="cursor:pointer" onclick="selectAsset('${escHtml(a.path)}', ${a.duration_seconds || 0})">
            <td class="font-monospace small">${escHtml(a.filename)}</td>
            <td>${a.duration}</td>
            <td>${a.video_width ? a.video_width + '×' + a.video_height : '—'}</td>
            <td>${a.has_scte ? '<span class="badge bg-warning text-dark">SCTE</span>' : ''}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
  document.getElementById('asset-list').innerHTML = html;
}

function selectAsset(path, durationSec) {
  document.getElementById('f-asset-path').value = path;
  if (durationSec > 0)
    document.getElementById('f-duration').value = Math.round(durationSec);
  assetModal.hide();
}

// ------------------------------------------------------------------ //
//  Helpers                                                            //
// ------------------------------------------------------------------ //

function setRrule(val) {
  document.getElementById('f-rrule').value = val;
}

function resetForm() {
  ['f-entry-id','f-occurrence-date','f-title','f-start','f-asset-path',
   'f-live-source','f-rrule','f-notes'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('f-duration').value = 3600;
  document.getElementById('f-color').value = '#3788d8';
  document.querySelector('input[name=entryType][value=file]').checked = true;
  document.querySelector('input[name=editMode][value=this]').checked = true;
  toggleTypePanels('file');
}

document.querySelectorAll('input[name=entryType]').forEach(r => {
  r.addEventListener('change', e => toggleTypePanels(e.target.value));
});

function toggleTypePanels(type) {
  document.getElementById('filePicker').classList.toggle('d-none', type !== 'file');
  document.getElementById('liveSource').classList.toggle('d-none', type === 'file');
}

function updateEntry(entryId, patch) {
  // First fetch existing entry so we can merge
  fetch(`/api/schedule/${entryId}`)
    .then(r => r.json())
    .then(existing => {
      const merged = Object.assign({}, existing, patch);
      return fetch(`/api/schedule/${entryId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(merged),
      });
    })
    .then(() => calendar.refetchEvents())
    .catch(e => { calendar.refetchEvents(); });
}

function createOverride(parentId, occDate, newStart, newEnd, ep, forceDuration) {
  const dur = forceDuration || Math.round((newEnd - newStart) / 1000);
  fetch(`/api/schedule/${parentId}/override`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      occurrence_date: occDate,
      start_time: newStart.toISOString(),
      duration: dur,
    }),
  }).then(() => calendar.refetchEvents());
}

function toLocalInputValue(dt) {
  // Convert Date to local datetime-local value string
  const pad = n => String(n).padStart(2, '0');
  return `${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())}`
       + `T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
