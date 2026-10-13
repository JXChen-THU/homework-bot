/* Shared interaction for alert.html and panel.html. */
const kind = document.body.dataset.kind;
const isAlert = kind === 'alert';
const api = () => window.pywebview.api;
/* ★ 不再有 listLimit：任务条数不再截断，全部渲染进 DOM，超出的部分靠 #list
   滚动查看（见 panel.css / alert.css 的 #list）。
   窗口高度上限仍由 Python 的 _calc_height 决定（紧急 4 行 / 待办 6 行的高度），
   所以"不限条数"不会让窗口无限长高。 */

/* ------------------------------------------------------------------ 主题兜底
   主题解析的正主是 head 里的 theme-boot.js（首帧前就定下来，避免闪色）。
   这里只做兜底：万一 theme-boot.js 没加载成功，浮窗也必须跟着系统亮暗走，
   不能停在浅色。逻辑与 theme-boot.js 完全一致（同一套三态规则）。 */
if (!window.hwTheme) {
  (function () {
    'use strict';
    var KEY = 'hwThemeMode';
    var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    function mode() {
      try { var v = localStorage.getItem(KEY); return (v === 'light' || v === 'dark') ? v : 'auto'; }
      catch (e) { return 'auto'; }
    }
    function resolved(m) {
      if (m === 'light' || m === 'dark') return m;
      return (mq && mq.matches) ? 'dark' : 'light';
    }
    function apply() {
      var m = mode(), dark = resolved(m) === 'dark', root = document.documentElement;
      if (dark) root.setAttribute('data-lg-dark', ''); else root.removeAttribute('data-lg-dark');
      root.setAttribute('data-lg-mode', m);
      return dark ? 'dark' : 'light';
    }
    if (mq) {
      var onChange = function () { if (mode() === 'auto') apply(); };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
    /* 主窗口在设置里改主题 → 本窗口立刻跟着变。
       同源窗口共享 localStorage，改动会在其他窗口触发 storage 事件。 */
    window.addEventListener('storage', function (e) {
      if (e && e.key && e.key !== KEY) return;
      apply();
    });
    window.hwTheme = {
      apply: apply, mode: mode, resolved: function () { return resolved(mode()); },
      setMode: function (m) {
        try { if (m === 'light' || m === 'dark' || m === 'auto') localStorage.setItem(KEY, m); } catch (e) {}
        return apply();
      }
    };
    apply();
  })();
}

function escapeHtml(value) {
  // 必须先 String()：任务字段里有数字（uid / hours_left 之类），
  // 数字是真值却带 .replace，`(value || '').replace` 会直接 TypeError。
  return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* 倒计时口径与主窗口 main.js 的 fmtRemaining 对齐：
   过期就说「已过期」，不足一分钟说「即将到期」，绝不再输出「剩 1 分」
   ——那会让已经过期的任务看起来还剩一分钟。 */
function formatRemaining(hours) {
  if (hours === null || hours === undefined) return '';
  if (hours <= 0) return '已过期';
  if (hours < 1 / 60) return '即将到期';
  if (hours < 1) return '剩 ' + Math.max(1, Math.round(hours * 60)) + ' 分';
  const days = Math.floor(hours / 24);
  const remainingHours = Math.floor(hours % 24);
  if (days > 0) return '剩 ' + days + ' 天 ' + remainingHours + ' 小时';
  return '剩 ' + hours.toFixed(1) + ' 小时';
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
  items.forEach(item => {
    const remaining = formatRemaining(item.hours_left);
    const detail = isAlert
      ? `<span class="time">${remaining}</span>`
      : (remaining ? `<span class="due">${escapeHtml(remaining)}</span>` : '');
    html += `<div class="item">
      <input type="checkbox" data-uid="${escapeHtml(item.uid)}" />
      <span class="title">${escapeHtml(item.title)}</span>${detail}
    </div>`;
  });
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
      /* 这条已经完成，行由后端重绘移除 —— toggle_done 返回之前 Python 侧
         就调了 layout_all()，list.innerHTML 早把这一行换成了新节点，
         所以这里不做位移动画：动画只会跑在游离节点上，用户看到的是"瞬移消失"。
         窗口高度由 controller._calc_height 按后端数据重算，也不需要前端补位。 */
    });
  });
}

window[isAlert ? '__updateUrgent' : '__updateNormal'] = update;
window.addEventListener('pywebviewready', async () => {
  await api()[isAlert ? 'alert_ready' : 'panel_ready']();
  await refreshMainVisible();
  setInterval(refreshMainVisible, 1000);
});
