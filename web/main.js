// ============================================================
// 工具函数
// ============================================================
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

const content = document.getElementById('content');
const banners = document.getElementById('banners');
const refreshBtn = document.getElementById('refresh-btn');
const addBtn = document.getElementById('add-btn');
const modalBg = document.getElementById('modal-bg');
const modalError = document.getElementById('modal-error');
const loadingHint = document.getElementById('loading-hint');

let items = [];
let currentTab = 'todo';

// ============================================================
// 时间计算
// ============================================================
function remainingMs(dateStr) {
  if (!dateStr) return null;
  const now = new Date();
  const due = new Date(dateStr.replace(' ', 'T'));
  if (isNaN(due)) return null;
  return due - now;
}

function fmtRemaining(ms) {
  if (ms === null) return '';
  if (ms <= 0) return '已过期';
  const h = ms / 3600000;
  if (h < 24) return '还剩 ' + h.toFixed(1) + ' 小时';
  const d = Math.floor(h / 24);
  const rh = Math.floor(h % 24);
  return '还剩 ' + d + ' 天 ' + rh + ' 小时';
}

// ============================================================
// 顶部提示条
// ============================================================
function renderBanners(errors, needLogin) {
  let html = '';

  if (needLogin && needLogin.length) {
    html += '<div class="banner warn"><div>需要登录以下站点：</div>';
    needLogin.forEach(s => {
      html += `<button data-login-site="${escapeHtml(s.site_id)}">登录 ${escapeHtml(s.site_name)}</button>`;
    });
    html += '</div>';
  }

  if (errors && errors.length) {
    html += '<div class="banner err">';
    errors.forEach(e => {
      html += `<div>⚠️ ${escapeHtml(e.site_name)}：${escapeHtml(e.message)}</div>`;
    });
    html += '</div>';
  }

  banners.innerHTML = html;

  banners.querySelectorAll('[data-login-site]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const siteId = btn.dataset.loginSite;
      btn.disabled = true;
      const originalText = btn.textContent;
      btn.textContent = '请在弹出的浏览器里登录...';

      const r = await window.pywebview.api.login(siteId);

      if (r.ok) {
        await load();
      } else if (r.error === 'need_credentials') {
        btn.disabled = false;
        btn.textContent = originalText;
        alert('这个网站需要先填写账号密码。\n\n请点击右上角「设置」，在网站列表中找到它，点「填账号密码」填好后再回来登录。');
      } else {
        btn.disabled = false;
        btn.textContent = '登录未完成，重试';
      }
    });
  });
}

// ============================================================
// 任务排序
// ============================================================
function sortItems(list) {
  return [...list].sort((a, b) => {
    if (!a.end && !b.end) return 0;
    if (!a.end) return 1;
    if (!b.end) return -1;
    return new Date(a.end.replace(' ', 'T')) - new Date(b.end.replace(' ', 'T'));
  });
}

// ============================================================
// 主列表渲染
// ============================================================
function render() {
  const todo = sortItems(items.filter(x => !x.done));
  const done = sortItems(items.filter(x => x.done));

  document.getElementById('cnt-todo').textContent = todo.length;
  document.getElementById('cnt-done').textContent = done.length;

  const shown = currentTab === 'todo' ? todo : done;

  if (!shown.length) {
    if (currentTab === 'todo') {
      content.innerHTML = `
        <div class="empty">
          <div class="big">🎉</div>
          <div class="msg">所有任务都已完成，太棒了！</div>
        </div>`;
    } else {
      content.innerHTML = `<div class="empty">还没有已完成的任务</div>`;
    }
    return;
  }

  content.innerHTML = '<div class="list">' + shown.map(it => {
    const ms = remainingMs(it.end);
    const expired = !it.done && ms !== null && ms <= 0;
    const urgent  = !it.done && ms !== null && ms > 0 && ms < 24*3600*1000;

    const cls = it.done ? 'done' : (expired ? 'expired' : (urgent ? 'urgent' : ''));

    const badge = it.manual
      ? '<span class="badge manual">手动</span>'
      : `<span class="badge">${escapeHtml(it.site_name)}</span>`;

    const courseBadge = it.course ? `<span class="badge">${escapeHtml(it.course)}</span>` : '';
    const dueText = it.end ? `截止：${escapeHtml(it.end)}` : '无截止时间';
    const delBtn = it.manual ? `<button class="del" data-del="${escapeHtml(it.uid)}">删除</button>` : '';

    return `
      <div class="item ${cls}">
        <input type="checkbox" ${it.done ? 'checked' : ''} data-uid="${escapeHtml(it.uid)}" />
        <div class="info">
          <div class="title">
            ${badge}${courseBadge}
            <span class="text">${escapeHtml(it.title)}</span>
          </div>
          <div class="meta">${dueText}</div>
        </div>
        <div class="days">${it.done ? '已完成' : fmtRemaining(ms)}</div>
        ${delBtn}
      </div>
    `;
  }).join('') + '</div>';

  content.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', async (e) => {
      const uid = e.target.dataset.uid;
      const done = e.target.checked;
      await window.pywebview.api.toggle_done(uid, done);

      const it = items.find(x => x.uid === uid);
      if (it) it.done = done;
      render();
    });
  });

  content.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const uid = btn.dataset.del;
      if (!confirm('确定要删除这条手动任务吗？')) return;
      const r = await window.pywebview.api.delete_task(uid);
      if (!r.error) {
        items = items.filter(x => x.uid !== uid);
        render();
      }
    });
  });
}

// ============================================================
// 刷新数据
// ============================================================
async function load() {
  refreshBtn.disabled = true;
  loadingHint.textContent = '任务刷新中，请等待…';

  try {
    const res = await window.pywebview.api.get_homeworks();
    if (res.error) {
      banners.innerHTML = `<div class="banner err">抓取失败：${escapeHtml(res.error)}</div>`;
    } else {
      items = res.items || [];
      renderBanners(res.errors || [], res.need_login || []);
      render();
    }
  } catch (e) {
    banners.innerHTML = `<div class="banner err">出错了：${escapeHtml(e)}</div>`;
  } finally {
    refreshBtn.disabled = false;
    loadingHint.textContent = '';
  }
}

// ============================================================
// 标签页切换
// ============================================================
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    currentTab = t.dataset.tab;
    render();
  });
});

refreshBtn.addEventListener('click', load);

// ============================================================
// 「重置浮窗」按钮
// ============================================================
document.getElementById('reset-floating-btn').addEventListener('click', async () => {
  await window.pywebview.api.reset_floating();
});

// ============================================================
// 添加任务弹窗
// ============================================================
addBtn.addEventListener('click', () => {
  document.getElementById('m-title').value = '';
  document.getElementById('m-course').value = '';
  document.getElementById('m-due').value = '';
  document.getElementById('m-url').value = '';
  modalError.textContent = '';
  modalBg.classList.add('open');
  setTimeout(() => document.getElementById('m-title').focus(), 50);
});

document.getElementById('m-cancel').addEventListener('click', () => {
  modalBg.classList.remove('open');
});

modalBg.addEventListener('click', (e) => {
  if (e.target === modalBg) modalBg.classList.remove('open');
});

document.getElementById('m-ok').addEventListener('click', async () => {
  const title = document.getElementById('m-title').value.trim();
  const course = document.getElementById('m-course').value.trim();
  const dueRaw = document.getElementById('m-due').value;
  const url = document.getElementById('m-url').value.trim();

  if (!title) { modalError.textContent = '请填写标题'; return; }
  if (!dueRaw) { modalError.textContent = '请填写截止时间'; return; }

  const due = dueRaw.replace('T', ' ') + (dueRaw.length === 16 ? ':00' : '');

  const r = await window.pywebview.api.add_task(title, course, due, url);
  if (r.error) {
    modalError.textContent = r.error;
    return;
  }
  modalBg.classList.remove('open');
});

// ============================================================
// 设置面板：网站管理
// ============================================================
const settingsBg = document.getElementById('settings-bg');
const applyHint = document.getElementById('apply-hint');
let sitesFullInfo = [];
let siteDraft = [];
let settingsForceRefresh = false;

async function openSettings() {
  settingsBg.classList.add('open');
  settingsForceRefresh = false;
  applyHint.textContent = '';
  applyHint.className = 'apply-hint';
  await refreshSitesPanel();
  await refreshAutostart();
}

async function refreshSitesPanel() {
  const list = document.getElementById('site-list');
  list.innerHTML = '<div style="padding:20px;text-align:center;color:#95a5a6;">加载中...</div>';
  const res = await window.pywebview.api.list_sites();
  sitesFullInfo = res.sites || [];
  siteDraft = sitesFullInfo.map(s => ({
    id: s.id, enabled: s.enabled, deleted: false, clear_session: false,
  }));
  renderSites();
  updateApplyHint();
}

function renderSites() {
  const list = document.getElementById('site-list');
  if (!sitesFullInfo.length) {
    list.innerHTML = '<div style="padding:20px;text-align:center;color:#95a5a6;">还没有配置任何网站</div>';
    return;
  }

  const draftMap = {};
  siteDraft.forEach(d => { draftMap[d.id] = d; });

  list.innerHTML = sitesFullInfo.map(s => {
    const d = draftMap[s.id] || {};
    const enabled = d.enabled;
    const deleted = d.deleted;
    const clearing = d.clear_session;

    const rowCls = ['site-row'];
    if (!enabled) rowCls.push('disabled');
    if (deleted) rowCls.push('deleted');

    const clearBtnCls = clearing ? 'pending' : '';
    const clearBtnText = clearing ? '将清除（点撤销）' : '清除登录';
    const delBtnCls = deleted ? 'pending' : 'danger';
    const delBtnText = deleted ? '将删除（点撤销）' : '删除';

    let credBadge = '';
    let credBtn = '';
    if (s.type === 'tyche') {
      if (!s.has_credentials) credBadge = '<span class="cred">未填账号密码</span>';
      credBtn = `<button class="cred-btn" data-cred="${escapeHtml(s.id)}">填账号密码</button>`;
    }

    return `
    <div class="${rowCls.join(' ')}">
      <div class="switch ${enabled && !deleted ? 'on' : ''}" data-toggle="${escapeHtml(s.id)}"></div>
      <div class="name">${escapeHtml(s.name)}</div>
      <div class="type">${escapeHtml(s.type)}</div>
      <div class="sess ${s.has_session ? 'ok' : 'no'}">${s.has_session ? '已登录' : '未登录'}</div>
      ${credBadge}
      <div class="spacer"></div>
      ${credBtn}
      <button class="${clearBtnCls}" data-clear="${escapeHtml(s.id)}">${clearBtnText}</button>
      <button class="${delBtnCls}" data-del="${escapeHtml(s.id)}">${delBtnText}</button>
    </div>`;
  }).join('');

  list.querySelectorAll('[data-toggle]').forEach(el => {
    el.addEventListener('click', () => {
      const it = siteDraft.find(x => x.id === el.dataset.toggle);
      if (!it || it.deleted) return;
      it.enabled = !it.enabled;
      renderSites(); updateApplyHint();
    });
  });

  list.querySelectorAll('[data-clear]').forEach(el => {
    el.addEventListener('click', () => {
      const it = siteDraft.find(x => x.id === el.dataset.clear);
      if (!it || it.deleted) return;
      it.clear_session = !it.clear_session;
      renderSites(); updateApplyHint();
    });
  });

  list.querySelectorAll('[data-del]').forEach(el => {
    el.addEventListener('click', () => {
      const it = siteDraft.find(x => x.id === el.dataset.del);
      if (!it) return;
      it.deleted = !it.deleted;
      if (it.deleted) it.clear_session = false;
      renderSites(); updateApplyHint();
    });
  });

  list.querySelectorAll('[data-cred]').forEach(el => {
    el.addEventListener('click', () => openCredDialog(el.dataset.cred));
  });
}

function updateApplyHint() {
  const changed = siteDraft.some(d => {
    const orig = sitesFullInfo.find(s => s.id === d.id);
    if (!orig) return true;
    return orig.enabled !== d.enabled || d.deleted || d.clear_session;
  });
  if (changed) {
    applyHint.textContent = '有未应用的改动';
    applyHint.className = 'apply-hint warn';
  } else {
    applyHint.textContent = '';
    applyHint.className = 'apply-hint';
  }
}

document.getElementById('apply-sites-btn').addEventListener('click', async () => {
  await window.pywebview.api.apply_sites_changes(siteDraft);
  settingsForceRefresh = true;
  await refreshSitesPanel();
  applyHint.textContent = '✓ 已应用';
  applyHint.className = 'apply-hint ok';
  setTimeout(() => {
    if (applyHint.textContent === '✓ 已应用') {
      applyHint.textContent = '';
      applyHint.className = 'apply-hint';
    }
  }, 2000);
});

// ============================================================
// 凭据编辑弹窗
// ============================================================
const credBg = document.getElementById('cred-bg');
const credError = document.getElementById('cred-error');
let credSiteId = '';

async function openCredDialog(siteId) {
  credSiteId = siteId;
  credError.textContent = '';
  document.getElementById('cred-user').value = '';
  document.getElementById('cred-pass').value = '';
  const r = await window.pywebview.api.get_site_credentials(siteId);
  if (r && r.ok) {
    document.getElementById('cred-user').value = r.basic_user || '';
    document.getElementById('cred-pass').value = r.basic_pass || '';
  }
  credBg.classList.add('open');
  setTimeout(() => document.getElementById('cred-user').focus(), 50);
}

document.getElementById('cred-cancel').addEventListener('click', () => {
  credBg.classList.remove('open');
});
credBg.addEventListener('click', (e) => {
  if (e.target === credBg) credBg.classList.remove('open');
});

document.getElementById('cred-ok').addEventListener('click', async () => {
  const u = document.getElementById('cred-user').value.trim();
  const p = document.getElementById('cred-pass').value;
  if (!u) { credError.textContent = '请填写账号'; return; }
  if (!p) { credError.textContent = '请填写密码'; return; }
  const r = await window.pywebview.api.save_site_credentials(credSiteId, u, p);
  if (r.error) {
    credError.textContent = r.error;
    return;
  }
  credBg.classList.remove('open');
  await refreshSitesPanel();
  settingsForceRefresh = true;
});

// ============================================================
// 开机自启动开关
// ============================================================
const autostartSwitch = document.getElementById('autostart-switch');
const autostartStatus = document.getElementById('autostart-status');

async function refreshAutostart() {
  const r = await window.pywebview.api.get_autostart();
  if (r && r.enabled) {
    autostartSwitch.classList.add('on');
    autostartStatus.textContent = '已开启';
    autostartStatus.className = 'status on';
  } else {
    autostartSwitch.classList.remove('on');
    autostartStatus.textContent = '未开启';
    autostartStatus.className = 'status';
  }
}

autostartSwitch.addEventListener('click', async () => {
  const isOn = autostartSwitch.classList.contains('on');
  const target = !isOn;
  autostartSwitch.style.pointerEvents = 'none';
  const r = await window.pywebview.api.set_autostart(target);
  autostartSwitch.style.pointerEvents = '';
  if (r && r.error) {
    alert('设置失败：' + r.error);
    return;
  }
  await refreshAutostart();
});

// ============================================================
// 设置面板打开/关闭
// ============================================================
document.getElementById('settings-btn').addEventListener('click', openSettings);

function closeSettings() {
  settingsBg.classList.remove('open');
  if (settingsForceRefresh) {
    settingsForceRefresh = false;
    load();
  }
}

document.getElementById('s-close').addEventListener('click', closeSettings);
settingsBg.addEventListener('click', (e) => {
  if (e.target === settingsBg) closeSettings();
});

// ============================================================
// 与 Python 的双向通讯
// ============================================================
window.__syncFromCache = function(newItems) {
  if (!Array.isArray(newItems)) return;
  items = newItems;
  render();
};

window.addEventListener('pywebviewready', load);

setInterval(() => {
  if (!items.length) return;
  document.querySelectorAll('.item').forEach(el => {
    const cb = el.querySelector('input[type=checkbox]');
    if (!cb) return;
    const it = items.find(x => x.uid === cb.dataset.uid);
    if (!it || it.done) return;
    const daysEl = el.querySelector('.days');
    if (daysEl) daysEl.textContent = fmtRemaining(remainingMs(it.end));
  });
}, 60 * 1000);
