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
// 每个站点为什么需要登录，以及点了「登录」之后会发生什么
const LOGIN_HINTS = {
  no_credentials: {
    text: '未填账号密码，无法自动登录',
    btn: '登录',
  },
  qr_code: {
    text: '需要微信扫码，无法自动登录',
    btn: '扫码登录',
  },
  manual_required: {
    text: '自动登录已尝试，需要你确认一次验证码',
    btn: '去验证',
  },
  bad_credentials: {
    text: '自动登录失败，账号或密码可能不对，请点按钮重新填写',
    btn: '重新填账号密码',
  },
  session_unstable: {
    text: '登录成功了但会话没能保存下来，请再点一次登录',
    btn: '重试登录',
  },
  default: {
    text: '登录状态已失效',
    btn: '登录',
  },
};

function renderBanners(errors, needLogin) {
  let html = '';

  if (needLogin && needLogin.length) {
    html += '<div class="banner warn"><div>需要登录以下站点：</div>';
    needLogin.forEach(s => {
      const hint = LOGIN_HINTS[s.reason] || LOGIN_HINTS.default;
      // data-reason 决定按钮点下去该做什么：只有 bad_credentials 是「填凭据」，
      // 其余原因才需要弹出登录窗口
      html += '<div class="login-row">'
        + `<button data-login-site="${escapeHtml(s.site_id)}" data-reason="${escapeHtml(s.reason || '')}">${hint.btn} ${escapeHtml(s.site_name)}</button>`
        + `<span class="login-hint">${hint.text}</span>`
        + '</div>';
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
      const reason = btn.dataset.reason;

      // 「重新填账号密码」承诺的是填凭据，不是弹浏览器窗口：
      // 直接打开凭据弹窗。openCredDialog 内部对查不到的站点有 `|| {}` 兜底，
      // 没打开过设置面板（sitesFullInfo 为空）也能正常使用。
      if (reason === 'bad_credentials') {
        openCredDialog(siteId);
        return;
      }

      btn.disabled = true;
      btn.textContent = '正在打开登录窗口...';

      try {
        const r = await window.pywebview.api.login(siteId);
        if (r && r.ok) {
          // load() 会重新渲染提示条，按钮随 innerHTML 一起被替换，不必手动恢复
          await load();
          return;
        }
        // 后端只返回 {ok: ok}，没成功就是「未完成」，让用户能再点
        btn.disabled = false;
        btn.textContent = '登录未完成，点击重试';
      } catch (e) {
        // pywebview 会把 Python 异常变成 rejected promise；
        // 不在这里恢复，按钮会永久停在 disabled 的「正在打开登录窗口...」
        btn.disabled = false;
        btn.textContent = '登录出错，点击重试';
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
      try {
        await window.pywebview.api.toggle_done(uid, done);
      } catch (err) {
        // 后端没保存成功，勾选状态要退回，否则界面和真实数据不一致
        e.target.checked = !done;
        alert('保存勾选状态失败：' + err);
        return;
      }

      const it = items.find(x => x.uid === uid);
      if (it) it.done = done;
      render();
    });
  });

  content.querySelectorAll('[data-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const uid = btn.dataset.del;
      if (!confirm('确定要删除这条手动任务吗？')) return;
      let r;
      try {
        r = await window.pywebview.api.delete_task(uid);
      } catch (err) {
        alert('删除失败：' + err);
        return;
      }
      if (r && r.error) {
        // delete_task 确实会返回 {"error": ...}
        alert('删除失败：' + r.error);
        return;
      }
      items = items.filter(x => x.uid !== uid);
      render();
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
    // get_homeworks 只返回 {items, errors, need_login}，没有 error 字段；
    // 抓取失败会走 catch（pywebview 把后端异常变成 rejected promise），
    // 单个站点的问题则在 res.errors 里
    const res = await window.pywebview.api.get_homeworks();
    items = res.items || [];
    renderBanners(res.errors || [], res.need_login || []);
    render();
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
  try {
    await window.pywebview.api.reset_floating();
  } catch (e) {
    alert('重置浮窗失败：' + e);
  }
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

  let r;
  try {
    r = await window.pywebview.api.add_task(title, course, due, url);
  } catch (err) {
    modalError.textContent = '添加失败：' + err;
    return;
  }
  if (r && r.error) {
    // add_task 确实会返回 {"error": ...}
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
  let res;
  try {
    res = await window.pywebview.api.list_sites();
  } catch (err) {
    // 不处理会一直停在「加载中...」
    list.innerHTML = `<div style="padding:20px;text-align:center;color:#e74c3c;">读取网站列表失败：${escapeHtml(err)}</div>`;
    return;
  }
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
    // 雨课堂靠微信扫码，不需要账号密码；其余站点都支持「填账号密码 → 后台静默登录」
    if (s.type !== 'yuketang') {
      if (s.credentials_broken) {
        // 换了电脑或 Windows 账户，密文解不开
        credBadge = '<span class="cred broken">密码需重填</span>';
      } else if (!s.has_credentials) {
        credBadge = '<span class="cred">未填账号密码</span>';
      }
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
  try {
    await window.pywebview.api.apply_sites_changes(siteDraft);
  } catch (err) {
    applyHint.textContent = '应用失败：' + err;
    applyHint.className = 'apply-hint warn';
    return;
  }
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
  const schoolWrap = document.getElementById('cred-school-wrap');
  const bindingWrap = document.getElementById('cred-binding-wrap');

  document.getElementById('cred-user').value = '';
  document.getElementById('cred-pass').value = '';
  document.getElementById('cred-school').value = '';
  document.getElementById('cred-binding-user').value = '';
  document.getElementById('cred-binding-pass').value = '';
  schoolWrap.style.display = 'none';
  bindingWrap.style.display = 'none';

  // 先从站点列表里拿类型信息，决定显示哪些字段
  const info = (sitesFullInfo || []).find(x => x.id === siteId) || {};
  if (info.needs_school) schoolWrap.style.display = '';
  if (info.type === 'tyche') bindingWrap.style.display = '';

  let r;
  try {
    r = await window.pywebview.api.get_site_credentials(siteId);
  } catch (err) {
    // 读不到已保存的凭据也要让弹窗打开，否则用户以为按钮没反应
    r = null;
    credError.textContent = '读取已保存的账号密码失败：' + err;
  }
  if (r && r.ok) {
    document.getElementById('cred-user').value = r.username || '';
    document.getElementById('cred-pass').value = r.password || '';
    document.getElementById('cred-school').value = r.school || '';
    document.getElementById('cred-binding-user').value = r.basic_user || '';
    document.getElementById('cred-binding-pass').value = r.basic_pass || '';
    if (r.undecryptable) {
      credError.textContent =
        '已保存的账号密码在当前电脑/Windows 账户下无法解密，请重新填写。';
    }
  } else if (r && r.error) {
    // get_site_credentials 确实会返回 {"error": "站点不存在"}
    credError.textContent = r.error;
  }

  // 不同站点的账号叫法不一样
  const labels = {
    thuoj: ['学号', '清华账号密码'],
    learn: ['学号', '清华账号密码'],
    smartestu: ['学号 / 工号', '密码'],
    unipus: ['手机号 / 邮箱', '密码'],
    tyche: ['网站账号', '网站密码'],
  };
  const lb = labels[info.type] || ['账号', '密码'];
  document.getElementById('cred-user-label').textContent = lb[0];
  document.getElementById('cred-pass-label').textContent = lb[1];

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
  const school = document.getElementById('cred-school').value.trim();
  const bu = document.getElementById('cred-binding-user').value.trim();
  const bp = document.getElementById('cred-binding-pass').value;
  if (!u) { credError.textContent = '请填写账号'; return; }
  if (!p) { credError.textContent = '请填写密码'; return; }
  if (document.getElementById('cred-school-wrap').style.display !== 'none' && !school) {
    credError.textContent = '请填写学校全名';
    return;
  }
  let r;
  try {
    r = await window.pywebview.api.save_site_credentials(credSiteId, u, p, school, bu, bp);
  } catch (err) {
    credError.textContent = '保存失败：' + err;
    return;
  }
  if (r && r.error) {
    // save_site_credentials 确实会返回 {"error": "站点不存在"}
    credError.textContent = r.error;
    return;
  }
  credBg.classList.remove('open');
  await refreshSitesPanel();
  settingsForceRefresh = true;
  // 从顶部提示条点进来时设置面板本就没打开，没人会去关面板触发刷新，
  // 提示条会一直停在旧文案上。这里主动刷新一次：顺带用新账密重新登录
  // （改完密码本来也需要重登才能生效），代价是这次刷新要等抓取完成。
  if (!settingsBg.classList.contains('open')) {
    settingsForceRefresh = false;
    await load();
  }
});

// ============================================================
// 开机自启动开关
// ============================================================
const autostartSwitch = document.getElementById('autostart-switch');
const autostartStatus = document.getElementById('autostart-status');

async function refreshAutostart() {
  let r;
  try {
    r = await window.pywebview.api.get_autostart();
  } catch (e) {
    autostartStatus.textContent = '读取失败';
    autostartStatus.className = 'status';
    return;
  }
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
  let r;
  try {
    r = await window.pywebview.api.set_autostart(target);
  } catch (e) {
    r = { error: String(e) };
  } finally {
    // 无论成功失败都要恢复点击，否则开关被永久锁死
    autostartSwitch.style.pointerEvents = '';
  }
  if (r && r.error) {
    // set_autostart 确实会返回 {"error": ...}
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
