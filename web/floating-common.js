/* Shared interaction for alert.html and panel.html. */
const kind = document.body.dataset.kind;
const isAlert = kind === 'alert';
const api = () => window.pywebview.api;
const listLimit = isAlert ? 8 : 12;

function escapeHtml(value) {
  return (value || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function formatRemaining(hours) {
  if (hours === null || hours === undefined) return '';
  const past = hours < 0;
  const value = Math.abs(hours);
  const prefix = past ? '已过期 ' : '剩 ';
  if (value < 1) return prefix + Math.max(1, Math.round(value * 60)) + ' 分';
  const days = Math.floor(value / 24);
  const remainingHours = Math.floor(value % 24);
  if (days > 0) return prefix + days + ' 天 ' + remainingHours + ' 小时';
  return prefix + value.toFixed(1) + ' 小时';
}

let mainVisible = false;
async function refreshMainVisible() {
  try {
    const result = await api().is_main_visible();
    mainVisible = !!(result && result.visible);
  } catch (_) { mainVisible = false; }
}

async function onToggleMain(event) {
  if (event) event.stopPropagation();
  try {
    await api()[mainVisible ? 'hide_main' : 'show_main']();
  } catch (_) {}
  setTimeout(refreshMainVisible, 80);
}
async function onMinimize(event) {
  if (event) event.stopPropagation();
  try { await api()[isAlert ? 'minimize_alert' : 'minimize_panel'](); } catch (_) {}
}
async function onClose(event) {
  if (event) event.stopPropagation();
  try { await api()[isAlert ? 'dismiss_alert' : 'dismiss_panel'](); } catch (_) {}
}

function update(items) {
  const list = document.getElementById('list');
  document.getElementById('count').textContent = items ? `(${items.length})` : '';
  if (!items || !items.length) { list.innerHTML = ''; return; }

  let html = '';
  items.slice(0, listLimit).forEach(item => {
    const remaining = formatRemaining(item.hours_left);
    const detail = isAlert
      ? `<span class="time">${remaining}</span>`
      : (remaining ? `<span class="due">${escapeHtml(remaining)}</span>` : '');
    html += `<div class="item">
      <input type="checkbox" data-uid="${escapeHtml(item.uid)}" />
      <span class="title">${escapeHtml(item.title)}</span>${detail}
    </div>`;
  });
  if (items.length > listLimit) {
    html += `<div class="more">...还有 ${items.length - listLimit} 条</div>`;
  }
  list.innerHTML = html;
  list.querySelectorAll('input[type=checkbox]').forEach(checkbox => {
    checkbox.addEventListener('change', async event => {
      event.stopPropagation();
      checkbox.disabled = true;
      try { await api().toggle_done(checkbox.dataset.uid, true); } catch (_) {}
    });
  });
}

window[isAlert ? '__updateUrgent' : '__updateNormal'] = update;
window.addEventListener('pywebviewready', async () => {
  await api()[isAlert ? 'alert_ready' : 'panel_ready']();
  await refreshMainVisible();
  setInterval(refreshMainVisible, 1000);
});
