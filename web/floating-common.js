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
      let failed = false;
      try {
        await api().toggle_done(checkbox.dataset.uid, true);
      } catch (_) {
        failed = true;
      }
      /* 保存失败不能把这条滑走：滑走等于界面单方面宣布"已完成"，
         而主面板读的是后端状态，两边就此不一致（浮窗在说谎）。
         失败时把勾退回并解禁，浮窗保持原样，交给下一轮刷新收敛。 */
      if (failed) {
        checkbox.checked = false;
        checkbox.disabled = false;
        return;
      }
      /* 勾完这条就被移出浮窗：向右滑走（横向位移 + 淡出），动画结束后删掉这个 DOM 节点。
         纯 CSS 做不到这件事——窗口高度和行距由 controller._calc_height 与 flex 排布决定，
         不删节点就会留一条空行，下面的任务也不会补位。
         位移只有 X，不影响任何纵向几何；动画结束后 fill 随节点一起回收，不需要常驻 will-change。 */
      const row = checkbox.closest('.item');
      if (!row) return;
      let anim;
      try {
        anim = row.animate(
          [
            { transform: 'translateX(0)', opacity: 1 },
            { transform: 'translateX(18px)', opacity: 0.25, offset: 0.55 },
            { transform: 'translateX(46px)', opacity: 0 },
          ],
          { duration: 240, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', fill: 'forwards' }
        );
      } catch (_) {
        row.remove();          // 不支持 Web Animations 就直接移除，不留下卡住的行
        return;
      }
      anim.addEventListener('finish', () => row.remove());
    });
  });
}

window[isAlert ? '__updateUrgent' : '__updateNormal'] = update;
window.addEventListener('pywebviewready', async () => {
  await api()[isAlert ? 'alert_ready' : 'panel_ready']();
  await refreshMainVisible();
  setInterval(refreshMainVisible, 1000);
});
