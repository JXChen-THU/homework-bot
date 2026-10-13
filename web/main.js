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
const progressArea = document.getElementById('progress');

let items = [];
let currentTab = 'todo';

// ============================================================
// 抓取进度文字区
//
// 这是「抓取时正在发生什么」的临时展示，不是终端也不是日志查看器：
// 抓取开始出现、抓完收起清空，把空间还给任务列表。
// 放到 #content 外面是必须的 —— render() 每次整块重建 #content，
// 放在里面会被每次重绘冲掉。
// ============================================================
const PROGRESS_MAX_LINES = 3;

function showProgress() {
  progressArea.innerHTML = '';
  progressArea.hidden = false;
}

function hideProgress() {
  progressArea.hidden = true;
  progressArea.innerHTML = '';
}

// Python 侧通过 evaluate_js 调这个；失败不能影响抓取，所以全程不抛。
// 站点名写成「[站点名] 结果」，站点名（含方括号）会单独用蓝色渲染。
function appendBracketLine(parent, text) {
  const m = text.match(/^(\[[^\]]*\])([\s\S]*)$/);
  if (!m) {
    parent.textContent = text;
    return;
  }
  const br = document.createElement('span');
  br.className = 'br';
  br.textContent = m[1];

  const rest = document.createElement('span');
  rest.textContent = m[2];

  parent.appendChild(br);
  parent.appendChild(rest);
}

window.__appendLog = function (line) {
  try {
    if (line == null) return;
    const text = String(line);
    if (!text) return;

    // 区域是隐藏的说明这轮已经结束，迟到的行直接丢弃，
    // 免得把「抓完就消失」变成一条残留的尾巴。
    if (progressArea.hidden) return;

    const div = document.createElement('div');
    div.className = 'line';
    appendBracketLine(div, text);
    progressArea.appendChild(div);

    // 最多 3 行：新的顶掉最旧的
    while (progressArea.children.length > PROGRESS_MAX_LINES) {
      progressArea.removeChild(progressArea.firstChild);
    }
    progressArea.scrollTop = progressArea.scrollHeight;
  } catch (_) { /* 进度是辅助信息，任何异常都不许冒泡到 Python */ }
};


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
// 每个站点为什么需要登录，以及点了「登录」之后会发生什么。
// key = 后端 need_login[].reason（也可能来自 login() 返回的 reason）。
// ★ 查表一律写 `LOGIN_HINTS[reason] || LOGIN_HINTS.default`：
//   后端将来新增 reason 时只会退到通用话术，绝不会渲染出 undefined。
//   唯一例外是按钮的失败话术（见下面点击处理），那里未收录时用更贴切的通用句。
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
  mfa: {
    text: '自动登录已尝试，需要你确认一次验证码',
    btn: '去验证',
  },
  no_adapter_support: {
    text: '该平台不支持自动登录，请手动登录',
    btn: '手动登录',
  },
  login_page_error: {
    text: '登录页打不开，请检查网络后重试',
    btn: '重试登录',
  },
  timeout: {
    text: '登录超时，请重试',
    btn: '重试登录',
  },
  exception: {
    text: '登录过程出错，请重试或手动登录',
    btn: '重试登录',
  },
  default: {
    text: '登录状态已失效',
    btn: '登录',
  },
};

// 这些原因点下去是「重新填凭据」（openCredDialog），其余原因一律弹浏览器登录窗口。
// 显式白名单而不是 if/else 兜底：将来后端新增 reason 时默认落到「弹登录窗口」
// 这个安全动作，不会被静默当成填凭据。
const CRED_DIALOG_REASONS = ['bad_credentials', 'no_credentials'];

// 首次运行引导（后端 gate，见 core.controller.get_homeworks 的启动闸门）
let gatePending = false;

// 单个站点那一行（按钮 + 话术 + 可选诊断）。两处渲染共用这一份：
// 「首次运行引导」里给的是"去填账密"，「需要登录」提示条里给的是"去登录"，
// 但按钮文案/话术/落点属性完全一样 —— 各写一遍早晚会出现"改了这边忘了那边"。
function loginRowHtml(s) {
  const hint = LOGIN_HINTS[s.reason] || LOGIN_HINTS.default;
  return '<div class="login-row">'
    + `<button data-login-site="${escapeHtml(s.site_id)}" data-reason="${escapeHtml(s.reason || '')}">${hint.btn} ${escapeHtml(s.site_name)}</button>`
    + `<span class="login-hint">${hint.text}</span>`
    + '</div>'
    // detail 是可选的诊断摘要，只有后端给了才多渲染一行小字。
    // 放在 .login-row 外面：那一行是 flex 横排，塞进去会和按钮挤在同一行。
    + (s.detail ? `<div class="need-login-detail">${escapeHtml(s.detail)}</div>` : '');
}

function needLoginRowsHtml(needLogin) {
  return (needLogin || []).map(loginRowHtml).join('');
}

// 把 need_login 那一段渲染出来（普通路径用）。
function needLoginHtml(needLogin) {
  if (!needLogin || !needLogin.length) return '';
  return '<div class="banner warn"><div>需要登录以下站点：</div>'
    + needLoginRowsHtml(needLogin) + '</div>';
}

// 首次运行引导：一个会话文件都没有时不抓取，先让用户填账号密码。
// 这里刻意把「为什么没有任务」说清楚 —— 否则用户看到空列表会以为程序坏了。
function gateHtml(gate, needLogin) {
  const msg = (gate && gate.message)
    || '这是第一次运行：还没有任何登录状态，请先在「设置」里填好账号密码。';
  const rows = needLoginRowsHtml(needLogin);

  return '<div class="banner setup">'
    + `<div class="setup-title">${escapeHtml(msg)}</div>`
    + '<div class="setup-steps">'
    + '<div>① 点下面任一「填账号密码」，把要用的站点填好（雨课堂只能扫码，先不用管）。</div>'
    + '<div>② 填完会自动开始抓取；也可以随时点右上角「刷新任务」。</div>'
    + '<div>③ 只想手动记作业的话，用「添加任务」即可，不必登录任何站点。</div>'
    + '</div>'
    + (rows ? '<div class="setup-sites"><div class="setup-sub">要抓取的站点：</div>' + rows + '</div>' : '')
    + '<div class="setup-actions">'
    + '<button class="setup-open-settings" data-open-settings="1">打开设置</button>'
    + '</div>'
    + '</div>';
}

function renderBanners(errors, needLogin, startup, gate) {
  let html = '';

  // 启动提示（例如数据目录不可写、已静默回退到 %LOCALAPPDATA%）。
  // 这类信息没有别的出口，不显示用户就只会看到「配置莫名丢了」。
  if (startup && startup.length) {
    startup.forEach(m => {
      const kind = m.kind === 'err' ? 'err' : 'warn';
      html += `<div class="banner ${kind}">`
        + `<div>${escapeHtml(m.text)}</div>`
        + (m.detail ? `<div class="startup-detail">${escapeHtml(m.detail)}</div>` : '')
        + '</div>';
    });
  }

  if (gate && gate.skipped) {
    html += gateHtml(gate, needLogin);
  } else {
    html += needLoginHtml(needLogin);
  }

  if (errors && errors.length) {
    html += '<div class="banner err">';
    errors.forEach(e => {
      html += `<div>${escapeHtml(e.site_name)}：${escapeHtml(e.message)}</div>`;
    });
    html += '</div>';
  }

  banners.innerHTML = html;

  banners.querySelectorAll('[data-open-settings]').forEach(btn => {
    btn.addEventListener('click', () => openSettings());
  });

  banners.querySelectorAll('[data-login-site]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const siteId = btn.dataset.loginSite;
      const reason = btn.dataset.reason;

      // 「重新填账号密码 / 登录」承诺的是填凭据，不是弹浏览器窗口：
      // 白名单里的原因直接打开凭据弹窗（openCredDialog 内部对查不到的站点
      // 有 `|| {}` 兜底，没打开过设置面板、sitesFullInfo 为空也能正常使用）。
      if (CRED_DIALOG_REASONS.indexOf(reason) !== -1) {
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
        // 后端返回 {ok, reason}。按 reason 说清楚这次卡在哪一步，
        // 用户才知道该去确认验证码、还是该检查网络。
        // 这里刻意不用 `|| LOGIN_HINTS.default`：未收录的 reason 说
        // 「登录未完成，点击重试」比 default 那句干巴巴的「登录」更贴合现场。
        // 用 text（原因）而不是 btn（动作）：btn 会拼出
        // 「未完成：去验证，点击重试」这种像命令的句子。
        const failReason = r && r.reason;
        const failHint = failReason ? LOGIN_HINTS[failReason] : null;
        btn.disabled = false;
        btn.textContent = failHint
          ? '未完成：' + failHint.text + '，点击重试'
          : '登录未完成，点击重试';
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
      if (gatePending) {
        // 首次运行：这里"空"是因为还没有登录状态，不是"任务都做完了"。
        // 用绿色那句「太棒了」会把用户误导成"没事可做"。
        content.innerHTML = `
          <div class="empty setup">
            <div class="msg">还没有登录任何站点</div>
            <div class="sub">在上面的引导里填好账号密码，或点「添加任务」手动记一条作业。</div>
            <div class="sub">填完会自动开始抓取。</div>
          </div>`;
      } else {
        content.innerHTML = `
          <div class="empty">
            <div class="msg">所有任务都已完成，太棒了！</div>
          </div>`;
      }
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

    // 原网页链接：适配器抓到的作业深链、以及手动任务里手填的「链接」,
    // 此前全链路产出但界面从不显示，等于白填/白抓。
    // 用 js_api 交给系统浏览器打开，而不是 <a target="_blank">
    // —— 后者是让 WebView2 自己导航，站点要求登录态，只会弹出空白页。
    const link = (it.url || '').trim();
    const linkable = /^https?:\/\//i.test(link);
    const titleHtml = linkable
      ? `<span class="text link" data-url="${escapeHtml(link)}" title="${escapeHtml(link)}">${escapeHtml(it.title)}</span>`
      : `<span class="text">${escapeHtml(it.title)}</span>`;

    return `
      <div class="item ${cls}">
        <input type="checkbox" ${it.done ? 'checked' : ''} data-uid="${escapeHtml(it.uid)}" />
        <div class="info">
          <div class="title">
            ${badge}${courseBadge}
            ${titleHtml}
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
      const box = e.target;
      const uid = box.dataset.uid;
      const done = box.checked;
      // 请求飞行期间锁住这个勾选框（与浮窗 floating-common.js 同一做法）：
      // 后端一次 toggle_done 要落盘，连点会发出多条互相打架的写入。
      box.disabled = true;
      try {
        await window.pywebview.api.toggle_done(uid, done);
      } catch (err) {
        // 后端没保存成功，勾选状态要退回，否则界面和真实数据不一致
        box.checked = !done;
        alert('保存勾选状态失败：' + err);
        return;
      } finally {
        // 失败回滚后必须解禁，否则这一行再也点不动。
        // 成功路径上 render() 已经把整行换成新节点，这里恢复的是游离节点，无害。
        box.disabled = false;
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

  content.querySelectorAll('.link[data-url]').forEach(el => {
    el.addEventListener('click', async () => {
      let r;
      try {
        r = await window.pywebview.api.open_url(el.dataset.url);
      } catch (err) {
        alert('打开链接失败：' + err);
        return;
      }
      if (r && r.error) {
        // open_url 用正常返回值表达失败（伪协议 / 没有可用浏览器）
        alert('打开链接失败：' + r.error);
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

  // 任何一次抓取都显示进度区（手动刷新、启动首次抓取都算）。
  // 无任务时也照常显示 —— 它反映的是抓取状态，与有没有任务无关。
  showProgress();

  try {
    // get_homeworks 返回 {items, errors, need_login, startup, gate}，没有 error 字段；
    // 抓取失败会走 catch（pywebview 把后端异常变成 rejected promise），
    // 单个站点的问题在 res.errors 里，启动期提示（如数据目录回退）在 res.startup 里。
    // gate 出现 = 首次运行：后端没去抓作业，先引导用户填账号密码。
    const res = await window.pywebview.api.get_homeworks();
    items = res.items || [];
    gatePending = !!(res.gate && res.gate.skipped);
    renderBanners(res.errors || [], res.need_login || [], res.startup || [], res.gate || null);
    render();
  } catch (e) {
    banners.innerHTML = `<div class="banner err">出错了：${escapeHtml(e)}</div>`;
    // 列表本来是空的（首次加载就失败）才会出现「空白区 + 计数停在 0」这种
    // 看着像"没有任务"的假象，这里给它一个明确的空态。
    // 已经有内容时保持旧列表不动：抓取失败不该把用户正在看的任务擦掉。
    if (!items.length) {
      content.innerHTML = `
        <div class="empty failed">
          <div class="msg">加载失败</div>
          <div class="sub">点右上角的「刷新任务」重试</div>
        </div>`;
      document.getElementById('cnt-todo').textContent = '0';
      document.getElementById('cnt-done').textContent = '0';
    }
  } finally {
    refreshBtn.disabled = false;
    loadingHint.textContent = '';
    // 抓取结束后收起并清空，不保留最后几行
    hideProgress();
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
  await refreshStartupBehavior();
  renderThemeMode();
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
    // 雨课堂靠微信扫码，不需要账号密码；其余站点都支持「填账号密码后后台静默登录」。
    // 优先读后端能力位 supports_silent_login（类型字面量是会变的实现细节），
    // 字段缺失时回退到原来的 type 判断，老后端也不会把按钮藏掉。
    const supportsSilentLogin = (typeof s.supports_silent_login === 'boolean')
      ? s.supports_silent_login
      : s.type !== 'yuketang';
    if (supportsSilentLogin) {
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
  let r;
  try {
    // 必须接返回值：写 sites.yaml 失败时后端是正常返回 {"error": ...}，
    // 不会抛异常，所以下面的 catch 接不到。不判 error 就会在写盘失败后
    // 显示「已应用」，紧接着 refreshSitesPanel() 从磁盘重载，开关/删除
    // 全部静默回滚，用户完全不知道发生了什么。
    r = await window.pywebview.api.apply_sites_changes(siteDraft);
  } catch (err) {
    applyHint.textContent = '应用失败：' + err;
    applyHint.className = 'apply-hint warn';
    return;
  }
  if (r && r.error) {
    applyHint.textContent = '应用失败：' + r.error;
    applyHint.className = 'apply-hint warn';
    return;
  }
  settingsForceRefresh = true;
  await refreshSitesPanel();
  applyHint.textContent = '已应用';
  applyHint.className = 'apply-hint ok';
  setTimeout(() => {
    if (applyHint.textContent === '已应用') {
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

  // 类型判定必须在 await 之后：get_site_credentials 返回的 type 是唯一
  // 不依赖「设置面板是否打开过」的来源。
  // 之前是在 await 之前读 sitesFullInfo —— 那个数组只在 refreshSitesPanel()
  // 之后才有值，于是从顶部提示条直接打开（或首次启动就点）时，
  // 学校字段和 Tyche 的 Basic 凭据字段永远是隐藏的：
  // 数你最灵用户没入口填学校全名，Tyche 的 Basic 凭据被空串覆盖。
  const info = (sitesFullInfo || []).find(x => x.id === siteId) || {};
  const siteType = (r && r.type) || info.type || '';
  if (siteType === 'smartestu' || info.needs_school) schoolWrap.style.display = '';
  if (siteType === 'tyche') bindingWrap.style.display = '';

  // 不同站点的账号叫法不一样
  const labels = {
    thuoj: ['学号', '清华账号密码'],
    learn: ['学号', '清华账号密码'],
    smartestu: ['学号 / 工号', '密码'],
    unipus: ['手机号 / 邮箱', '密码'],
    tyche: ['网站账号', '网站密码'],
  };
  const lb = labels[siteType] || ['账号', '密码'];
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
  // 隐藏的字段说明这个站点不适用（例如雨课堂不要学校名、非 Tyche 没有 Basic
  // 凭据）。这时传 null 而不是空串：后端用 `is not None` 判断「这次要不要改」，
  // 传空串等于把已存好的配置擦掉。
  const schoolVisible = document.getElementById('cred-school-wrap').style.display !== 'none';
  const bindingVisible = document.getElementById('cred-binding-wrap').style.display !== 'none';
  if (!u) { credError.textContent = '请填写账号'; return; }
  if (!p) { credError.textContent = '请填写密码'; return; }
  if (schoolVisible && !school) {
    credError.textContent = '请填写学校全名';
    return;
  }
  let r;
  try {
    r = await window.pywebview.api.save_site_credentials(
      credSiteId, u, p,
      schoolVisible ? school : null,
      bindingVisible ? bu : null,
      bindingVisible ? bp : null
    );
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
  // 凭据存好了，首次运行的引导状态随之作废（下一次 load() 会重新按后端结果判定）
  gatePending = false;
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
const silentModeSwitch = document.getElementById('silent-switch');
const silentStatus = document.getElementById('silent-status');

async function refreshAutostart() {
  let r;
  try {
    r = await window.pywebview.api.get_autostart();
  } catch (e) {
    autostartStatus.textContent = '读取失败';
    autostartStatus.className = 'status';
    return;
  }
  // 注册表读取失败时后端是正常返回 {"enabled": False, "error": ...}，不会抛异常，
  // 上面的 catch 接不到。不判 error 就会把「读不到」显示成确定的「未开启」，
  // 用户据此以为没开，点一下开关反而可能弹「设置失败」。
  if (r && r.error) {
    autostartSwitch.classList.remove('on');
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
// 外观：「深浅主题」三段切换
//
// 三态规则与 theme-boot.js 完全一致（同一套解析，同一个 localStorage 键），
// 所以这里改了之后，两个浮窗会在各自下一次读到时跟着变 —— 它们监听的是
// 系统偏好，而"显式模式"这个键是三份脚本共用的。
//
// 选 auto = 跟随系统（Windows 设置里的"应用模式"）。
// ============================================================
const themeSeg = document.getElementById('theme-seg');
const themeStatus = document.getElementById('theme-status');
const THEME_LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };

function renderThemeMode() {
  if (!themeSeg) return;
  const mode = (window.hwTheme && window.hwTheme.mode) ? window.hwTheme.mode() : 'auto';
  themeSeg.querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', b.dataset.themeMode === mode);
  });
  if (themeStatus) themeStatus.textContent = THEME_LABEL[mode] || '跟随系统';
}

if (themeSeg) {
  themeSeg.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const next = btn.dataset.themeMode;
    // theme-boot.js 一定会加载（head 里同步执行），但万一被拦掉也不该静默失效
    if (window.hwTheme && window.hwTheme.setMode) window.hwTheme.setMode(next);
    else {
      try { localStorage.setItem('hwThemeMode', next); } catch (_) {}
      const dark = next === 'dark' || (next === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
      document.documentElement.toggleAttribute('data-lg-dark', dark);
    }
    renderThemeMode();
  });
}

// ============================================================// 启动行为：「启动时静默」开关
//
// 开关样式和「开机自启动」共用 .switch。
// 开 = silent（每次启动都不出现窗口），关 = visible（启动即显示界面）。
// ============================================================
async function refreshStartupBehavior() {
  let r;
  try {
    r = await window.pywebview.api.get_startup_behavior();
  } catch (e) {
    silentStatus.textContent = '读取失败';
    silentStatus.className = 'status';
    return;
  }
  // 后端只返回 {"mode": ...}，读取失败会走上面的 catch（pywebview 把异常
  // 变成 rejected promise），正常返回里没有 error 字段可判。
  if (r && r.mode === 'silent') {
    silentModeSwitch.classList.add('on');
    silentStatus.textContent = '已开启';
    silentStatus.className = 'status on';
  } else {
    silentModeSwitch.classList.remove('on');
    silentStatus.textContent = '未开启';
    silentStatus.className = 'status';
  }
}

silentModeSwitch.addEventListener('click', async () => {
  // 开关上的 'on' 类就是「静默」这个选项本身
  const wantSilent = !silentModeSwitch.classList.contains('on');
  silentModeSwitch.style.pointerEvents = 'none';
  let r;
  try {
    r = await window.pywebview.api.set_startup_behavior(wantSilent ? 'silent' : 'visible');
  } catch (e) {
    r = { error: String(e) };
  } finally {
    // 无论成功失败都要恢复点击，否则开关会被永久锁死
    silentModeSwitch.style.pointerEvents = '';
  }
  if (r && r.error) {
    // set_startup_behavior 用正常返回值表达失败（非法值 / 写盘失败）。
    // 重新读一次后端状态来回滚开关，并把失败原因留在状态文字里。
    await refreshStartupBehavior();
    silentStatus.textContent = '保存失败：' + r.error;
    silentStatus.className = 'status';
    return;
  }
  await refreshStartupBehavior();
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

window.addEventListener('pywebviewready', async () => {
  // 必须先告诉 Python「前端就绪」：抓取期间它会用 evaluate_js 推进度，
  // 而 evaluate_js 在未就绪时会先白等 20 秒（pywebview 的 @_shown_call）。
  try { await window.pywebview.api.main_ready(); } catch (_) {}
  // 开关没有「默认勾选」这回事，必须主动读一次后端状态，
  // 否则设置面板刚打开时开关是空的。
  await refreshStartupBehavior();
  await load();
});

setInterval(() => {
  if (!items.length) return;
  document.querySelectorAll('.item').forEach(el => {
    const cb = el.querySelector('input[type=checkbox]');
    if (!cb) return;
    const it = items.find(x => x.uid === cb.dataset.uid);
    if (!it || it.done) return;
    const ms = remainingMs(it.end);
    const daysEl = el.querySelector('.days');
    if (daysEl) daysEl.textContent = fmtRemaining(ms);
    // 倒计时跨过「截止 / 24 小时」两条线时，行的状态色也要跟着变，
    // 用的是与 render() 完全相同的阈值。
    // ★ 这里只增删 class，绝不调 render()：整块重建会重置滚动位置、
    //   输入焦点和用户正在操作的复选框。
    const expired = ms !== null && ms <= 0;
    const urgent = ms !== null && ms > 0 && ms < 24 * 3600 * 1000;
    el.classList.toggle('expired', expired);
    el.classList.toggle('urgent', urgent);
  });
}, 60 * 1000);
