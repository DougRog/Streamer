const channelModal = new bootstrap.Modal(document.getElementById('channelModal'));

function openCreateModal() {
  document.getElementById('channelModalTitle').textContent = 'New Channel';
  document.getElementById('cf-id').value      = '';
  document.getElementById('cf-name').value    = '';
  document.getElementById('cf-addr').value    = '239.1.1.1';
  document.getElementById('cf-port').value    = '5000';
  document.getElementById('cf-bitrate').value = '6M';
  document.getElementById('cf-slate').checked = true;
  document.getElementById('cf-color').value   = '#6366f1';
  document.getElementById('cf-notes').value   = '';
  document.querySelector('input[name=cf-slate-type][value=color]').checked = true;
  document.getElementById('cf-slate-path').value = '';
  toggleSlateOptions();
  toggleSlateAsset();
  channelModal.show();
}

function editChannel(id) {
  fetch(`/api/channels/${id}`)
    .then(r => r.json())
    .then(ch => {
      document.getElementById('channelModalTitle').textContent = 'Edit Channel';
      document.getElementById('cf-id').value      = ch.id;
      document.getElementById('cf-name').value    = ch.name;
      document.getElementById('cf-addr').value    = ch.multicast_addr;
      document.getElementById('cf-port').value    = ch.multicast_port;
      document.getElementById('cf-bitrate').value = ch.video_bitrate || '6M';
      document.getElementById('cf-slate').checked = ch.slate_enabled;
      document.getElementById('cf-color').value   = ch.color || '#6366f1';
      document.getElementById('cf-notes').value   = ch.notes || '';

      const slateType = ch.slate_type || 'color';
      document.querySelector(`input[name=cf-slate-type][value=${slateType}]`).checked = true;
      document.getElementById('cf-slate-path').value = ch.slate_asset_path || '';
      toggleSlateOptions();
      toggleSlateAsset();
      channelModal.show();
    });
}

function toggleSlateOptions() {
  const enabled = document.getElementById('cf-slate').checked;
  document.getElementById('slate-options').style.display = enabled ? '' : 'none';
}

function toggleSlateAsset() {
  const isFile = document.querySelector('input[name=cf-slate-type]:checked')?.value === 'file';
  document.getElementById('slate-file-row').classList.toggle('d-none', !isFile);
  ['color','file'].forEach(t => {
    const el = document.getElementById(`slate-${t}-label`);
    if (el) el.style.borderColor = '';
  });
  const checked = document.querySelector('input[name=cf-slate-type]:checked')?.value;
  const active = document.getElementById(`slate-${checked}-label`);
  if (active) active.style.borderColor = 'var(--accent)';
}

function openSlateAssetPicker() {
  const p = prompt('Enter slate file path (e.g. /mnt/MasterControlMedia/slate.mxf):');
  if (p) document.getElementById('cf-slate-path').value = p;
}

function saveChannel() {
  const id = document.getElementById('cf-id').value;
  const slateEnabled = document.getElementById('cf-slate').checked;
  const slateType = document.querySelector('input[name=cf-slate-type]:checked')?.value || 'color';

  const payload = {
    name:             document.getElementById('cf-name').value.trim(),
    multicast_addr:   document.getElementById('cf-addr').value.trim(),
    multicast_port:   parseInt(document.getElementById('cf-port').value),
    video_bitrate:    document.getElementById('cf-bitrate').value,
    slate_enabled:    slateEnabled,
    slate_type:       slateType,
    slate_asset_path: slateType === 'file'
                        ? document.getElementById('cf-slate-path').value.trim() || null
                        : null,
    color:            document.getElementById('cf-color').value,
    notes:            document.getElementById('cf-notes').value.trim(),
  };

  if (!payload.name)           { showToast('Channel name is required', 'warning'); return; }
  if (!payload.multicast_addr) { showToast('Multicast address is required', 'warning'); return; }
  if (slateEnabled && slateType === 'file' && !payload.slate_asset_path) {
    showToast('Select a slate file or switch to Black + tone', 'warning'); return;
  }

  const method = id ? 'PUT' : 'POST';
  const url    = id ? `/api/channels/${id}` : '/api/channels';

  fetch(url, { method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) })
    .then(r => r.json())
    .then(d => {
      if (d.error) { showToast('Error: ' + d.error, 'danger'); return; }
      channelModal.hide();
      showToast(id ? 'Channel updated' : 'Channel created', 'success');
      setTimeout(() => location.reload(), 600);
    })
    .catch(() => showToast('Network error', 'danger'));
}

function startChannel(id) {
  fetch(`/api/channels/${id}/start`, {method:'POST'})
    .then(() => { showToast('Channel started', 'success'); setTimeout(() => location.reload(), 600); });
}

function stopChannel(id) {
  fetch(`/api/channels/${id}/stop`, {method:'POST'})
    .then(() => { showToast('Channel stopped', 'info'); setTimeout(() => location.reload(), 600); });
}

function deleteChannel(id, name) {
  if (!confirm(`Delete channel "${name}" and all its schedule entries?`)) return;
  fetch(`/api/channels/${id}`, {method:'DELETE'})
    .then(() => { showToast(`"${name}" deleted`, 'info'); setTimeout(() => location.reload(), 600); });
}
