const channelModal = new bootstrap.Modal(document.getElementById('channelModal'));

function openCreateModal() {
  document.getElementById('channelModalTitle').textContent = 'New Channel';
  document.getElementById('cf-id').value    = '';
  document.getElementById('cf-name').value  = '';
  document.getElementById('cf-addr').value  = '239.1.1.1';
  document.getElementById('cf-port').value  = '5000';
  document.getElementById('cf-bitrate').value = '6M';
  document.getElementById('cf-slate').checked = true;
  document.getElementById('cf-color').value = '#6366f1';
  document.getElementById('cf-notes').value = '';
  channelModal.show();
}

function editChannel(id) {
  fetch(`/api/channels/${id}`)
    .then(r => r.json())
    .then(ch => {
      document.getElementById('channelModalTitle').textContent = 'Edit Channel';
      document.getElementById('cf-id').value    = ch.id;
      document.getElementById('cf-name').value  = ch.name;
      document.getElementById('cf-addr').value  = ch.multicast_addr;
      document.getElementById('cf-port').value  = ch.multicast_port;
      document.getElementById('cf-bitrate').value = ch.video_bitrate || '6M';
      document.getElementById('cf-slate').checked = ch.slate_enabled;
      document.getElementById('cf-color').value = ch.color || '#6366f1';
      document.getElementById('cf-notes').value = ch.notes || '';
      channelModal.show();
    });
}

function saveChannel() {
  const id = document.getElementById('cf-id').value;
  const payload = {
    name:           document.getElementById('cf-name').value.trim(),
    multicast_addr: document.getElementById('cf-addr').value.trim(),
    multicast_port: parseInt(document.getElementById('cf-port').value),
    video_bitrate:  document.getElementById('cf-bitrate').value,
    slate_enabled:  document.getElementById('cf-slate').checked,
    color:          document.getElementById('cf-color').value,
    notes:          document.getElementById('cf-notes').value.trim(),
  };

  if (!payload.name)           { showToast('Channel name is required', 'warning'); return; }
  if (!payload.multicast_addr) { showToast('Multicast address is required', 'warning'); return; }

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
