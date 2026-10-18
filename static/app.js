/* 会议号池前端 —— 无构建依赖，直接跑 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const STORE_KEY = 'pool.myBookings';

  // 支持挂在子路径下（如 https://your-domain.com/tenxunhuiyiyuding/）。
  // 根路径时为 ''，子路径时为 '/tenxunhuiyiyuding'，所以所有接口都用 API_BASE + '/api/...'。
  const API_BASE = new URL('.', document.baseURI).pathname.replace(/\/$/, '');

  const state = {
    cfg: {},
    admin: null,
    tlDate: null,
    accounts: [],
    editing: null,
    driver: 'mcp',
  };

  /* ------------------------------------------------------------ 工具 */

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  function pad(n) { return String(n).padStart(2, '0'); }

  function clock(iso) {
    const d = new Date(iso);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function isoDate(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

  function toast(msg, ms = 2200) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove('show'), ms);
  }

  async function api(path, options = {}) {
    const res = await fetch(API_BASE + path, {
      credentials: 'same-origin',
      headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      ...options,
    });
    let data = null;
    const type = res.headers.get('content-type') || '';
    if (type.includes('json')) data = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error((data && data.detail) || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function copy(text, label = '已复制') {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      toast(label);
    } catch (e) {
      toast('复制失败，请手动选中');
    }
  }

  function readStore() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || '[]'); } catch (e) { return []; }
  }

  function writeStore(list) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(list.slice(0, 20))); } catch (e) { /* 忽略 */ }
  }

  function remember(booking) {
    const list = readStore().filter((x) => x.id !== booking.id && x.token !== booking.manage_token);
    list.unshift({ id: booking.id, token: booking.manage_token });
    writeStore(list);
  }

  function fmtRange(start, end) {
    const s = new Date(start), e = new Date(end);
    return `${s.getMonth() + 1}-${pad(s.getDate())} ${pad(s.getHours())}:${pad(s.getMinutes())}–${pad(e.getHours())}:${pad(e.getMinutes())}`;
  }

  const STATUS_TEXT = {
    pending: '待开始', active: '进行中', done: '已结束', cancelled: '已取消', failed: '失败',
  };

  function statusPill(status) {
    const map = { pending: 'blue', active: 'green', done: 'gray', cancelled: 'gray', failed: 'red' };
    return `<span class="pill ${map[status] || 'gray'}">${STATUS_TEXT[status] || status}</span>`;
  }

  /* ------------------------------------------------------------ 视图切换 */

  function switchView(view) {
    $$('#main-tabs .tab').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $('#view-borrow').classList.toggle('hidden', view !== 'borrow');
    $('#view-admin').classList.toggle('hidden', view !== 'admin');
    if (view === 'admin') ensureAdmin();
    if (location.hash !== `#/${view}`) history.replaceState(null, '', `#/${view}`);
  }

  /* ------------------------------------------------------------ 员工端 */

  function defaultSlot() {
    const now = new Date();
    const startHour = Number(state.cfg.work_hour_start || 8);
    const endHour = Number(state.cfg.work_hour_end || 23);
    const d = new Date(now.getTime());
    d.setSeconds(0, 0);
    d.setMinutes(Math.ceil((d.getMinutes() + 10) / 15) * 15);
    // 凌晨/深夜都要落到可借时段内，否则一进页面点提交就被拒
    if (d.getHours() < startHour) d.setHours(startHour, 0, 0, 0);
    if (d.getHours() >= endHour - 1) {
      d.setDate(d.getDate() + 1);
      d.setHours(startHour, 0, 0, 0);
    }
    return d;
  }

  function renderPublicStats(s) {
    $('#public-stats').innerHTML = `
      <div class="stat accent"><div class="k">当前空闲号</div><div class="v">${s.accounts_idle_now}<small>/ ${s.accounts_active} 个号</small></div></div>
      <div class="stat"><div class="k">今日已借出</div><div class="v">${s.today_bookings}<small>场</small></div></div>
      <div class="stat"><div class="k">单场最长</div><div class="v">${Math.round(s.max_minutes / 60 * 10) / 10}<small>小时</small></div></div>
      <div class="stat"><div class="k">每人每周</div><div class="v">${s.max_per_week || '不限'}<small>次</small></div></div>`;
    $('#form-note').textContent = s.notice || '';
  }

  async function loadState() {
    const s = await api('/api/state');
    state.cfg = s;
    renderPublicStats(s);
    const d = defaultSlot();
    const form = $('#book-form');
    if (!form.dataset.touched) {
      form.date.value = isoDate(d);
      form.time.value = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    if (!state.tlDate) state.tlDate = isoDate(new Date());
    return s;
  }

  function renderResult(booking) {
    const card = $('#result-card');
    card.innerHTML = `
      <div class="result-live">
        <div class="result-top">
          <div>
            <div class="code-label">会议号</div>
            <div class="code">${esc(booking.meeting_code || '—')}</div>
            <div class="code-sub">${esc(fmtRange(booking.start_at, booking.end_at))} · ${esc(booking.subject)}</div>
          </div>
          ${statusPill(booking.status)}
        </div>
        <dl class="kv">
          ${booking.password ? `<dt>入会密码</dt><dd><b>${esc(booking.password)}</b></dd>` : '<dt>入会密码</dt><dd class="muted">未设置（任何人凭会议号可入会）</dd>'}
          <dt>主持人选项</dt><dd>${booking.record ? '<span class="pill green">自动云录制</span>' : '<span class="pill gray">未开录制</span>'} ${booking.waiting_room ? '<span class="pill amber">等候室</span>' : ''}</dd>
          ${booking.host_key ? `<dt>主持人密钥</dt><dd><b>${esc(booking.host_key)}</b> <span class="muted small">仅自己用，别发群里</span></dd>` : ''}
          ${booking.join_url ? `<dt>入会链接</dt><dd><a href="${esc(booking.join_url)}" target="_blank" rel="noopener">${esc(booking.join_url)}</a></dd>` : ''}
          <dt>出号账号</dt><dd>${esc(booking.account_label || '—')} <span class="muted small">（用完请点下方结束）</span></dd>
        </dl>
        <div class="result-actions">
          <button class="btn primary tiny" data-act="copy-code">复制会议号</button>
          <button class="btn tiny" data-act="copy-all">复制邀请文案</button>
          <button class="btn tiny" data-act="copy-link">复制入会链接</button>
          <button class="btn tiny danger" data-act="cancel">结束这场会</button>
        </div>
        <div class="share-box" id="share-box">${esc(booking.share_text || '')}</div>
      </div>`;

    const share = booking.share_text || '';
    card.querySelector('[data-act="copy-code"]').onclick = () => copy(booking.meeting_code, '会议号已复制');
    card.querySelector('[data-act="copy-all"]').onclick = () => copy(share, '邀请文案已复制，可直接发群里');
    card.querySelector('[data-act="copy-link"]').onclick = () => copy(booking.join_url || share, '入会链接已复制');
    card.querySelector('[data-act="cancel"]').onclick = () => cancelMyBooking(booking.id, booking.manage_token, true);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function cancelMyBooking(id, token, fromResult) {
    const ok = await confirmDialog('结束这场会？', '会同时在腾讯会议里取消这场会，取消成功后号码立即回池。');
    if (!ok) return;
    try {
      const res = await api(`/api/bookings/${id}/cancel`, {
        method: 'POST',
        body: JSON.stringify({ token }),
      });
      toast(res.note || '已取消，号已回池', 3000);
      if (fromResult) {
        $('#result-card').innerHTML = `
          <div class="result-empty"><div class="empty-icon">✓</div>
          <h3>这场会已取消</h3><p>腾讯会议里也一并取消了，号码回到了号池。</p></div>`;
      }
      await Promise.all([loadState(), loadTimeline(), loadMy()]);
    } catch (e) {
      toast(e.message, 7000);
      if (fromResult) {
        const live = $('#result-card').querySelector('.result-live');
        if (live) {
          let note = live.querySelector('.cancel-error');
          if (!note) {
            note = document.createElement('div');
            note.className = 'cancel-error';
            live.appendChild(note);
          }
          note.textContent = e.message;
        }
      }
    }
  }

  async function loadMy() {
    const list = readStore();
    const box = $('#my-list');
    if (!list.length) {
      box.innerHTML = '<p class="muted small">还没有借过号。借完会自动记在这里（只在本机保存）。</p>';
      return;
    }
    try {
      const data = await api('/api/my', { method: 'POST', body: JSON.stringify({ tokens: list.map((x) => x.token) }) });
      const items = data.items || [];
      const alive = items.filter((b) => ['pending', 'active'].includes(b.status));
      if (!items.length) {
        box.innerHTML = '<p class="muted small">还没有借过号。借完会自动记在这里（只在本机保存）。</p>';
        return;
      }
      box.innerHTML = items.map((b) => `
        <div class="my-item">
          <div>
            <div class="t">${esc(b.subject)} <span class="code-inline">${esc(b.meeting_code)}</span></div>
            <div class="m">${esc(fmtRange(b.start_at, b.end_at))} · ${esc(b.account_label || '')}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            ${statusPill(b.status)}
            ${['pending', 'active'].includes(b.status)
              ? `<button class="btn ghost tiny" data-cancel="${b.id}" data-token="${esc(b.manage_token)}">结束</button>` : ''}
          </div>
        </div>`).join('');
      $$('[data-cancel]', box).forEach((btn) => {
        btn.onclick = () => cancelMyBooking(Number(btn.dataset.cancel), btn.dataset.token, false);
      });
      if (alive.length) remember(alive[0]);
    } catch (e) {
      box.innerHTML = `<p class="muted small">读取失败：${esc(e.message)}</p>`;
    }
  }

  async function loadTimeline(date) {
    if (date) state.tlDate = date;
    const data = await api(`/api/timeline?date=${encodeURIComponent(state.tlDate)}`);
    $('#tl-date').textContent = data.date;
    const startH = Number(state.cfg.work_hour_start || 8);
    const endH = Number(state.cfg.work_hour_end || 23);
    const span = Math.max(1, endH - startH);
    state.tlStartH = startH;
    state.tlSpan = span;
    state.tlBlocks = [];
    const track = (blocks, accLabel) => blocks.map((b) => {
      const s = new Date(b.start_at), e = new Date(b.end_at);
      const from = ((s.getHours() + s.getMinutes() / 60) - startH) / span;
      const to = ((e.getHours() + e.getMinutes() / 60) - startH) / span;
      const left = Math.max(0, Math.min(1, from)) * 100;
      const width = Math.max(1.6, Math.min(1, Math.max(0, to)) * 100 - left);
      let cls = '';
      if (b.source === 'external') cls = 'external';
      else if (b.status === 'active') cls = 'active';
      else if (b.status === 'done') cls = 'done';
      const tip = `${b.subject} · ${b.source === 'external' ? '上游已约' : b.requester} · ${fmtRange(b.start_at, b.end_at)}`;
      const label = b.source === 'external' ? `上游 · ${b.subject}` : b.subject;
      const key = state.tlBlocks.push({ ...b, account: accLabel }) - 1;
      return `<button type="button" class="tl-block ${cls}" data-block="${key}" style="left:${left}%;width:${width}%" title="${esc(tip)}" aria-label="${esc(tip)}">${esc(label)}</button>`;
    }).join('');

    if (!data.accounts.length) {
      $('#timeline').innerHTML = '<p class="muted small">号池里还没有可用账号，去「号池管理」里加一个。</p>';
      return;
    }
    $('#timeline').innerHTML = data.accounts.map((a) => `
      <div class="tl-row">
        <div class="tl-name" title="${esc(a.label)}">
          <span class="dot ${esc(a.health || 'unknown')}"></span>${esc(a.label)}
        </div>
        <div class="tl-track" data-acc="${esc(a.label)}">${track(a.blocks, a.label)}</div>
      </div>`).join('') + `
      <div class="tl-axis"><div></div><div class="tl-ticks">
        ${Array.from({ length: 6 }, (_, i) => {
          const h = Math.round(startH + (span * i) / 5);
          return `<span>${pad(h)}:00</span>`;
        }).join('')}
      </div></div>
      <p class="tl-hint">左右滑动看全天，点色块看这个时段</p>`;
  }


  /* 时间轴：点色块 / 点空白看时段 */
  function slotSheet(block) {
    const isExternal = block.source === 'external';
    const statusMap = { pending: '待开始', active: '进行中', done: '已结束', external: block.requester || '上游已约' };
    const statusText = statusMap[block.status] || block.status;
    const m = openModal(`
      <div class="modal-head">
        <h3>${esc(block.subject)}</h3>
        <p>${esc(fmtRange(block.start_at, block.end_at))}</p>
      </div>
      <div class="modal-body">
        <dl class="kv">
          <dt>时段</dt><dd>${esc(clock(block.start_at))} – ${esc(clock(block.end_at))}</dd>
          <dt>占用方</dt><dd>${isExternal ? '号主自己约的会（腾讯会议侧）' : `号池借出 · ${esc(block.requester || '—')}`}</dd>
          <dt>出号账号</dt><dd>${esc(block.account || '—')}</dd>
          <dt>状态</dt><dd>${esc(statusText)}</dd>
        </dl>
      </div>
      <div class="modal-foot"><button class="btn" data-close>知道了</button></div>`);
    m.querySelector('[data-close]').onclick = () => closeModal(false);
  }

  function freeSlotSheet(accLabel, at) {
    const m = openModal(`
      <div class="modal-head">
        <h3>这个时段空着</h3>
        <p>${esc(accLabel)} 在 ${pad(at.hour)}:${pad(at.minute)} 起没有会</p>
      </div>
      <div class="modal-body">
        <p class="muted small">想用这个时段，回到左边表单把日期和开始时间填成
          <b>${pad(at.hour)}:${pad(at.minute)}</b> 就能借到号。</p>
      </div>
      <div class="modal-foot">
        <button class="btn" data-close>知道了</button>
        <button class="btn primary" data-fill>填进表单</button>
      </div>`);
    m.querySelector('[data-close]').onclick = () => closeModal(false);
    m.querySelector('[data-fill]').onclick = () => {
      const form = $('#book-form');
      form.date.value = state.tlDate;
      form.time.value = `${pad(at.hour)}:${pad(at.minute)}`;
      form.dataset.touched = '1';
      closeModal(false);
      form.scrollIntoView({ behavior: 'smooth', block: 'center' });
      form.requester.focus();
    };
  }

  /* ------------------------------------------------------------ 借用提交 */

  async function submitBooking(ev) {
    ev.preventDefault();
    const form = ev.target;
    const btn = $('#book-submit');
    const note = $('#form-note');
    note.classList.remove('err');
    form.dataset.touched = '1';

    const startAt = `${form.date.value}T${form.time.value}:00+08:00`;
    const payload = {
      requester: form.requester.value.trim(),
      dept: form.dept.value.trim(),
      contact: form.contact.value.trim(),
      subject: form.subject.value.trim(),
      note: form.note.value.trim(),
      record: form.record.checked,
      waiting_room: form.waiting_room.checked,
      password_enabled: form.password_enabled.checked,
      password: form.password_enabled.checked ? form.password.value.trim() : '',
      start_at: startAt,
      duration_minutes: Number(form.duration.value),
    };
    if (!payload.requester || !payload.subject) {
      note.textContent = '名字和会议主题要填一下';
      note.classList.add('err');
      return;
    }
    btn.disabled = true;
    btn.textContent = '正在分配号码…';
    try {
      const booking = await api('/api/bookings', { method: 'POST', body: JSON.stringify(payload) });
      remember(booking);
      renderResult(booking);
      toast('号码到手，可以直接发群里了');
      await Promise.all([loadState(), loadTimeline(), loadMy()]);
    } catch (e) {
      note.textContent = e.message;
      note.classList.add('err');
      toast(e.message, 3200);
    } finally {
      btn.disabled = false;
      btn.textContent = '借一个会议号';
    }
  }

  /* ------------------------------------------------------------ 弹窗 */

  let modalResolve = null;

  function openModal(html, opts = {}) {
    const root = $('#modal-root');
    root.innerHTML = `<div class="modal ${opts.wide ? 'wide' : ''}">${html}</div>`;
    root.classList.remove('hidden');
    root.onclick = (e) => { if (e.target === root) closeModal(false); };
    return root.firstElementChild;
  }

  function closeModal(result) {
    $('#modal-root').classList.add('hidden');
    $('#modal-root').innerHTML = '';
    if (modalResolve) { modalResolve(result); modalResolve = null; }
  }

  function confirmDialog(title, text, okText = '确定') {
    return new Promise((resolve) => {
      modalResolve = resolve;
      const m = openModal(`
        <div class="modal-head"><h3>${esc(title)}</h3><p>${esc(text)}</p></div>
        <div class="modal-foot">
          <button class="btn" data-no>取消</button>
          <button class="btn primary" data-yes>${esc(okText)}</button>
        </div>`);
      m.querySelector('[data-no]').onclick = () => closeModal(false);
      m.querySelector('[data-yes]').onclick = () => closeModal(true);
    });
  }

  /* ------------------------------------------------------------ 管理端 */

  async function ensureAdmin() {
    if (!state.admin) {
      try {
        const me = await api('/api/admin/me');
        if (!me.authed) throw new Error('未登录');
        state.admin = me;
        if (me.default_password) {
          toast('管理员仍在使用默认口令，请尽快在 .env 里改掉', 5000);
        }
      } catch (e) {
        state.admin = null;
        $('#admin-login').classList.remove('hidden');
        $('#admin-panel').classList.add('hidden');
        return;
      }
    }
    $('#admin-login').classList.add('hidden');
    $('#admin-panel').classList.remove('hidden');
    await loadAdmin();
  }

  async function loadAdmin() {
    const [overview, accounts, bookings, audit, cfg] = await Promise.all([
      api('/api/admin/overview'),
      api('/api/admin/accounts'),
      api(`/api/admin/bookings?${bookingQuery()}`),
      api('/api/admin/audit'),
      api('/api/admin/config'),
    ]);
    state.accounts = accounts.items;
    renderAdminStats(overview);
    renderProblems(overview.problems || []);
    renderAccounts(accounts.items);
    renderBookings(bookings.items);
    renderCharts(overview);
    renderConfig(cfg);
    renderAudit(audit.items);
    const syncAt = overview.sync && overview.sync.last_sync_at
      ? overview.sync.last_sync_at.slice(5, 16).replace('T', ' ')
      : '还没对账';
    const syncErr = overview.sync && overview.sync.errors.length
      ? ` · ${overview.sync.errors.length} 个号对账失败` : '';
    $('#admin-sub').textContent =
      `${overview.accounts_total} 个账号 · 今日 ${overview.today_bookings} 场 / `
      + `${Math.round(overview.today_minutes / 60 * 10) / 10} 小时 · 近 7 天失败率 `
      + `${overview.failure_rate}% · 上游另有 ${overview.external_total} 场 · 上次对账 ${syncAt}${syncErr}`;
  }

  function bookingQuery() {
    const p = new URLSearchParams();
    const d = $('#filter-date').value;
    const s = $('#filter-status').value;
    const k = $('#filter-keyword').value.trim();
    if (d) p.set('date', d);
    if (s) p.set('status', s);
    if (k) p.set('keyword', k);
    return p.toString();
  }

  function renderAdminStats(o) {
    $('#admin-stats').innerHTML = `
      <div class="stat"><div class="k">号池规模</div><div class="v">${o.accounts_active}<small>/ ${o.accounts_total} 在线</small></div></div>
      <div class="stat accent"><div class="k">此刻空闲</div><div class="v">${o.accounts_idle_now}<small>个号</small></div></div>
      <div class="stat"><div class="k">今日借出</div><div class="v">${o.today_bookings}<small>场</small></div></div>
      <div class="stat"><div class="k">需要处理</div><div class="v">${o.accounts_attention}<small>个号</small></div></div>`;
  }

  const DRIVER_TEXT = { mcp: 'MCP 接口', manual: '固定会议号', mock: '演示' };
  const TIER_TEXT = { free: '免费版', pro: '专业版', biz: '商业版' };
  const HEALTH_TEXT = { ok: '正常', warn: '异常', bad: '不可用', unknown: '未检测' };

  function renderProblems(items) {
    const box = $('#admin-problems');
    if (!items.length) { box.innerHTML = ''; return; }
    const rows = items.map((p) => {
      const isCancelFail = p.upstream_state === 'cancel_failed';
      const note = isCancelFail
        ? (p.cancel_error || '本地已取消，但腾讯会议里这场会仍然存在')
        : '腾讯会议里已经找不到这场会了，号码还占着';
      return `<div class="problem-row">
        <div>
          <div class="cell-main">${esc(p.subject)} <span class="muted small">#${p.id}</span>
            <span class="pill ${isCancelFail ? 'red' : 'amber'}">${isCancelFail ? '取消失败' : '会议已消失'}</span></div>
          <div class="cell-sub">${esc(fmtRange(p.start_at, p.end_at))} · ${esc(p.account_label || '')} · ${esc(note)}</div>
        </div>
        <div class="row-actions">
          ${isCancelFail
            ? `<button class="btn tiny" data-retry="${p.id}">重新取消</button>`
            : `<button class="btn tiny" data-release="${p.id}">结束并释放</button>`}
        </div>
      </div>`;
    }).join('');
    box.innerHTML = `<div class="problem-card">
      <div class="problem-head"><b>${items.length} 条跟腾讯会议对不上</b>
        <span class="muted small">本地记录和上游状态不一致，处理完会消失</span></div>
      ${rows}</div>`;

    $$('[data-retry]', box).forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = '取消中…';
        try {
          await api(`/api/admin/bookings/${btn.dataset.retry}/retry-cancel`, { method: 'POST' });
          toast('这次真的取消了');
          await loadAdmin();
        } catch (e) {
          toast(e.message, 6000);
          btn.disabled = false;
          btn.textContent = '重新取消';
        }
      };
    });
    $$('[data-release]', box).forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/admin/bookings/${btn.dataset.release}/cancel`, {
            method: 'POST', body: JSON.stringify({ force: true }),
          });
          toast('号码已释放');
          await loadAdmin();
          await loadTimeline();
        } catch (e) { toast(e.message, 5000); }
      };
    });
  }

  function renderAccounts(items) {
    const tbody = $('#accounts-table tbody');
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="muted small" style="padding:22px">号池是空的。点右上角「新增账号」，把同事的会员号加进来。</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((a) => {
      const credential = a.driver === 'mcp'
        ? (a.has_token ? `<span class="token-chip">${esc(a.token_hint || '已绑定')}</span>` : '<span class="pill red">缺 Token</span>')
        : (a.driver === 'manual'
          ? `<span class="token-chip">${esc(a.fixed_code)}</span>`
          : '<span class="pill gray">演示</span>');
      const statusPillHtml = a.status === 'active'
        ? '<span class="pill green">启用</span>'
        : (a.status === 'paused' ? '<span class="pill amber">暂停</span>' : '<span class="pill gray">停用</span>');
      return `<tr>
        <td><div class="cell-main">${esc(a.label)}</div><div class="cell-sub">${esc(a.note || '')}</div></td>
        <td><div>${esc(a.owner_name || '—')}</div><div class="cell-sub">${esc(a.owner_contact || '')}</div></td>
        <td>${esc(TIER_TEXT[a.tier] || a.tier)}</td>
        <td>${esc(DRIVER_TEXT[a.driver] || a.driver)}</td>
        <td>${credential}</td>
        <td>${a.today_count} / ${a.week_count} 次<div class="cell-sub">${Math.round(a.week_minutes / 60 * 10) / 10} 小时</div></td>
        <td>${statusPillHtml}${a.busy_now ? '<div class="cell-sub">使用中</div>' : ''}
          ${a.external_count ? `<div class="cell-sub">上游 ${a.external_count} 场</div>` : ''}</td>
        <td><span class="dot ${esc(a.health)}"></span>${esc(HEALTH_TEXT[a.health] || a.health)}
          ${a.health_note ? `<div class="cell-sub" title="${esc(a.health_note)}">${esc(a.health_note.slice(0, 22))}</div>` : ''}
          ${a.sync_error ? `<div class="cell-sub" title="${esc(a.sync_error)}" style="color:var(--amber)">对账失败</div>` : ''}
          ${a.last_sync_at ? `<div class="cell-sub">同步 ${esc(a.last_sync_at.slice(5, 16).replace('T', ' '))}</div>` : ''}</td>
        <td><div class="row-actions">
          <button class="btn ghost tiny" data-probe="${a.id}" title="检测 Token 是否可用">检测</button>
          <button class="btn ghost tiny" data-edit="${a.id}">编辑</button>
          <button class="btn ghost tiny" data-toggle="${a.id}">${a.status === 'active' ? '暂停' : '启用'}</button>
          <button class="btn ghost tiny danger" data-del="${a.id}">删除</button>
        </div></td>
      </tr>`;
    }).join('');

    $$('[data-probe]', tbody).forEach((b) => b.onclick = () => probeAccount(Number(b.dataset.probe), b));
    $$('[data-edit]', tbody).forEach((b) => b.onclick = () => openAccountModal(items.find((x) => x.id === Number(b.dataset.edit))));
    $$('[data-toggle]', tbody).forEach((b) => b.onclick = () => toggleAccount(Number(b.dataset.toggle)));
    $$('[data-del]', tbody).forEach((b) => b.onclick = () => deleteAccount(Number(b.dataset.del)));
  }

  function renderBookings(items) {
    const tbody = $('#bookings-table tbody');
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted small" style="padding:22px">没有符合条件的记录。</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((b) => `
      <tr>
        <td><div>${esc(fmtRange(b.start_at, b.end_at))}</div><div class="cell-sub">${esc(b.created_at.slice(5, 16).replace('T', ' '))} 建</div></td>
        <td><div class="cell-main">${esc(b.subject)}</div>${b.fail_reason ? `<div class="cell-sub">${esc(b.fail_reason)}</div>` : ''}</td>
        <td><div>${esc(b.requester)}</div><div class="cell-sub">${esc(b.dept || b.contact || '')}</div></td>
        <td>${esc(b.account_label || '—')}</td>
        <td><span class="token-chip">${esc(b.meeting_code || '—')}</span></td>
        <td>${statusPill(b.status)}</td>
        <td><div class="row-actions">
          ${['pending', 'active'].includes(b.status)
            ? `<button class="btn ghost tiny danger" data-bcancel="${b.id}">强制结束</button>` : ''}
          ${b.join_url ? `<a class="btn ghost tiny" href="${esc(b.join_url)}" target="_blank" rel="noopener">入会</a>` : ''}
        </div></td>
      </tr>`).join('');

    $$('[data-bcancel]', tbody).forEach((b) => {
      b.onclick = async () => {
        if (!await confirmDialog('结束这场会？', '会同时在腾讯会议侧取消（MCP 账号），并立即释放号码。', '结束')) return;
        try {
          await api(`/api/admin/bookings/${b.dataset.bcancel}/cancel`, { method: 'POST' });
          toast('已在腾讯会议侧取消并释放');
          await loadAdmin();
          await loadTimeline();
        } catch (e) {
          if (e.status === 409 && /取消|上游/.test(e.message)) {
            const force = await confirmDialog(
              '腾讯会议侧取消失败',
              `${e.message}\n\n要强制在号池里释放这个号码吗？会议仍会留在腾讯会议上。`,
              '强制释放',
            );
            if (force) {
              try {
                await api(`/api/admin/bookings/${b.dataset.bcancel}/cancel`, {
                  method: 'POST', body: JSON.stringify({ force: true }),
                });
                toast('已强制释放，会议仍在腾讯会议侧', 4000);
                await loadAdmin();
                await loadTimeline();
              } catch (err) { toast(err.message, 5000); }
            }
          } else {
            toast(e.message, 6000);
          }
        }
      };
    });
  }

  function renderCharts(o) {
    const hasUsage = o.account_usage.some((x) => x.week_count > 0);
    if (!hasUsage) {
      $('#usage-chart').innerHTML = '<p class="muted small">近 7 天还没有借用记录。员工借号后，这里会显示每个号被用了多少次。</p>';
    } else {
      const maxWeek = Math.max(1, ...o.account_usage.map((x) => x.week_count));
      $('#usage-chart').innerHTML = o.account_usage.map((x) => `
        <div class="bar-row">
          <div class="tl-name" title="${esc(x.label)}">${esc(x.label)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.round(x.week_count / maxWeek * 100)}%"></div></div>
          <div class="num">${x.week_count} 场</div>
        </div>`).join('');
    }

    const totalHours = o.hour_heat.reduce((sum, x) => sum + x.count, 0);
    if (!totalHours) {
      $('#hour-chart').innerHTML = '<p class="muted small">还没有数据。等积累几场会后，这里能看出大家都爱在几点开会。</p>';
      return;
    }
    const maxHour = Math.max(1, ...o.hour_heat.map((x) => x.count));
    $('#hour-chart').innerHTML = o.hour_heat.map((x) => `
      <div class="heat-col" title="${pad(x.hour)}:00 · ${x.count} 场">
        <div class="heat-bar" style="height:${Math.max(3, Math.round(x.count / maxHour * 96))}px;background:${x.count ? 'linear-gradient(180deg,#8fb4ff,#3b74ff)' : '#eef1f6'}"></div>
        <div class="heat-lbl">${pad(x.hour)}</div>
      </div>`).join('');
  }

  function renderConfig(cfg) {
    const form = $('#config-form');
    Object.entries(cfg).forEach(([k, v]) => {
      const field = form.elements[k];
      if (field) field.value = v;
    });
  }

  function renderAudit(items) {
    const box = $('#audit-list');
    if (!items.length) { box.innerHTML = '<p class="muted small">暂无记录。</p>'; return; }
    box.innerHTML = items.map((a) => `
      <div class="audit-item">
        <div class="audit-time">${esc(a.created_at.slice(5, 16).replace('T', ' '))}</div>
        <div class="audit-body"><b>${esc(a.action)}</b> <span>${esc(a.actor)} ${esc(a.detail)}</span></div>
      </div>`).join('');
  }

  async function probeAccount(id, btn) {
    btn.disabled = true;
    btn.textContent = '检测中';
    try {
      const acc = await api(`/api/admin/accounts/${id}/probe`, { method: 'POST' });
      toast(`${acc.label}：${HEALTH_TEXT[acc.health] || acc.health} · ${acc.health_note || ''}`, 3600);
      await loadAdmin();
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '检测';
    }
  }

  async function toggleAccount(id) {
    const acc = state.accounts.find((x) => x.id === id);
    if (!acc) return;
    const next = acc.status === 'active' ? 'paused' : 'active';
    try {
      await api(`/api/admin/accounts/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          label: acc.label, owner_name: acc.owner_name, owner_contact: acc.owner_contact,
          tier: acc.tier, driver: acc.driver, fixed_code: acc.fixed_code,
          capacity: acc.capacity, max_minutes: acc.max_minutes, daily_limit: acc.daily_limit,
          status: next, note: acc.note,
        }),
      });
      toast(next === 'active' ? '已启用' : '已暂停，不再分号');
      await loadAdmin();
    } catch (e) { toast(e.message); }
  }

  async function deleteAccount(id) {
    const acc = state.accounts.find((x) => x.id === id);
    if (!acc) return;
    if (!await confirmDialog(`删除「${acc.label}」？`, '删除后该账号不再参与分配，历史借用记录会保留。', '删除')) return;
    try {
      await api(`/api/admin/accounts/${id}`, { method: 'DELETE' });
      toast('已删除');
      await loadAdmin();
    } catch (e) { toast(e.message, 3200); }
  }

  function openAccountModal(acc) {
    state.editing = acc || null;
    state.driver = acc ? acc.driver : 'mcp';
    const m = openModal(`
      <div class="modal-head">
        <h3>${acc ? '编辑账号' : '新增账号'}</h3>
        <p>MCP 模式请到 meeting.tencent.com/ai-skill.html 用该账号登录后取 Token</p>
      </div>
      <div class="modal-body">
        <label class="field"><span>账号名称 <i>*</i></span>
          <input name="label" maxlength="60" placeholder="市场部 · 张伟的会员号" value="${esc(acc ? acc.label : '')}" /></label>
        <div class="row-2">
          <label class="field"><span>提供者</span><input name="owner_name" value="${esc(acc ? acc.owner_name : '')}" placeholder="张伟" /></label>
          <label class="field"><span>联系方式</span><input name="owner_contact" value="${esc(acc ? acc.owner_contact : '')}" placeholder="微信 / 手机" /></label>
        </div>
        <div class="row-2">
          <label class="field"><span>会员类型</span>
            <select name="tier">
              ${Object.entries(TIER_TEXT).map(([k, v]) => `<option value="${k}" ${acc && acc.tier === k ? 'selected' : ''}>${v}</option>`).join('')}
            </select></label>
          <label class="field"><span>接入方式</span>
            <select name="driver">
              <option value="mcp">MCP 接口（推荐）</option>
              <option value="manual">固定会议号</option>
              <option value="mock">演示模式</option>
            </select></label>
        </div>
        <label class="field" data-token-row><span>账号 Token ${acc ? '（留空则不修改）' : '<i>*</i>'}</span>
          <input name="token" autocomplete="off" placeholder="${acc && acc.has_token ? '已绑定 ' + esc(acc.token_hint) + '，留空保持不变' : '粘贴该账号的 Token'}" />
        </label>
        <label class="field hidden" data-code-row><span>固定会议号 <i>*</i></span>
          <input name="fixed_code" maxlength="20" placeholder="9 位会议号" value="${esc(acc ? acc.fixed_code : '')}" /></label>
        <div class="row-3">
          <label class="field"><span>单场最长（分钟）</span><input type="number" name="max_minutes" min="15" max="720" value="${acc ? acc.max_minutes : 240}" /></label>
          <label class="field"><span>每日上限（场）</span><input type="number" name="daily_limit" min="1" max="50" value="${acc ? acc.daily_limit : 6}" /></label>
          <label class="field"><span>人数上限</span><input type="number" name="capacity" min="1" max="2000" value="${acc ? acc.capacity : 100}" /></label>
        </div>
        <label class="field"><span>备注</span><input name="note" maxlength="200" value="${esc(acc ? acc.note : '')}" placeholder="使用须知，会显示在台账里" /></label>
        <label class="field"><span>状态</span>
          <select name="status">
            <option value="active" ${acc && acc.status === 'active' ? 'selected' : ''}>启用</option>
            <option value="paused" ${acc && acc.status === 'paused' ? 'selected' : ''}>暂停</option>
            <option value="disabled" ${acc && acc.status === 'disabled' ? 'selected' : ''}>停用</option>
          </select></label>
      </div>
      <div class="modal-foot">
        <button class="btn" data-close>取消</button>
        <button class="btn primary" data-save>保存</button>
      </div>`);

    const driverSel = m.querySelector('[name=driver]');
    driverSel.value = state.driver;
    const syncDriver = () => {
      const d = driverSel.value;
      m.querySelector('[data-token-row]').classList.toggle('hidden', d !== 'mcp');
      m.querySelector('[data-code-row]').classList.toggle('hidden', d !== 'manual');
    };
    driverSel.onchange = syncDriver;
    syncDriver();
    m.querySelector('[data-close]').onclick = () => closeModal(false);
    m.querySelector('[data-save]').onclick = async () => {
      const get = (n) => m.querySelector(`[name=${n}]`).value.trim();
      const payload = {
        label: get('label'), owner_name: get('owner_name'), owner_contact: get('owner_contact'),
        tier: get('tier'), driver: get('driver'),
        fixed_code: get('fixed_code'),
        max_minutes: Number(get('max_minutes')),
        daily_limit: Number(get('daily_limit')),
        capacity: Number(get('capacity')),
        note: get('note'), status: get('status'),
      };
      const token = get('token');
      if (token) payload.token = token;
      try {
        if (acc) await api(`/api/admin/accounts/${acc.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
        else await api('/api/admin/accounts', { method: 'POST', body: JSON.stringify(payload) });
        closeModal(true);
        toast(acc ? '已保存' : '账号已加入号池');
        await loadAdmin();
        await loadState();
        await loadTimeline();
      } catch (e) { toast(e.message, 3600); }
    };
  }

  /* ------------------------------------------------------------ MCP 自检 */

  function openDiagnoseModal() {
    const mcpAccounts = state.accounts.filter((a) => a.driver === 'mcp');
    const options = mcpAccounts.map((a) => (
      `<option value="${a.id}">${esc(a.label)}${a.has_token ? '' : '（未绑 Token）'}</option>`
    )).join('');

    const m = openModal(`
      <div class="modal-head">
        <h3>MCP 连接自检</h3>
        <p>依次试官方候选端点，报告 HTTP 状态、工具数量，以及 Token 是否真的能调通。</p>
      </div>
      <div class="modal-body">
        ${mcpAccounts.length
          ? `<label class="field"><span>用哪个账号的 Token 检测</span>
               <select name="account">${options}</select></label>`
          : '<p class="muted small" style="margin-bottom:12px">号池里还没有 MCP 账号。先新增一个，把 Token 填进去再自检。</p>'}
        <div id="diag-result"><p class="muted small">点「开始检测」，大约 5–15 秒。</p></div>
      </div>
      <div class="modal-foot">
        <button class="btn" data-close>关闭</button>
        <button class="btn primary" data-run ${mcpAccounts.length ? '' : 'disabled'}>开始检测</button>
      </div>`, { wide: true });

    m.querySelector('[data-close]').onclick = () => closeModal(false);
    const runBtn = m.querySelector('[data-run]');
    runBtn.onclick = async () => {
      const accId = m.querySelector('[name=account]')?.value;
      const box = m.querySelector('#diag-result');
      box.innerHTML = '<p class="muted small">正在逐个端点握手…</p>';
      runBtn.disabled = true;
      try {
        const data = await api('/api/admin/mcp/diagnose', {
          method: 'POST',
          body: JSON.stringify({ account_id: accId ? Number(accId) : null }),
        });
        const rows = data.rows.map((r) => {
          const cls = r.verdict === '可用' ? 'green' : (r.path_ok ? 'amber' : 'red');
          return `<tr>
            <td><code>${esc(r.url)}</code></td>
            <td>${r.http ?? '—'}</td>
            <td>${esc(r.initialize || '—')}</td>
            <td>${r.tools || 0}</td>
            <td>${esc(r.token || '—')}</td>
            <td><span class="pill ${cls}">${esc(r.verdict || '—')}</span></td>
          </tr>`;
        }).join('');
        const tip = data.working_url
          ? (data.working_url === data.current_url
            ? '<p class="diag-tip ok">当前配置的地址可用。若 Token 列不是「可用」，问题在 Token 而不是地址——去 ai-skill 页面重新取一次。</p>'
            : `<p class="diag-tip warn">可用地址是 <code>${esc(data.working_url)}</code>，请把 .env 里的 <code>POOL_MCP_URL</code> 改成它并重启服务。</p>`)
          : '<p class="diag-tip warn">所有候选地址都没跑通。确认服务器能出网；若都显示「地址对（带 Token 才能验证）」，说明地址没错，把真实 Token 填进账号再点一次检测。</p>';
        box.innerHTML = `
          <div class="diag">
            <p class="muted small">当前配置：<code>${esc(data.current_url)}</code> · Token 来源：${esc(data.token_from)}</p>
            <div class="table-wrap"><table class="table diag-table">
              <thead><tr><th>端点</th><th>HTTP</th><th>握手</th><th>工具数</th><th>Token</th><th>结论</th></tr></thead>
              <tbody>${rows}</tbody>
            </table></div>
            ${tip}
          </div>`;
      } catch (e) {
        box.innerHTML = `<p class="form-note err">${esc(e.message)}</p>`;
      } finally {
        runBtn.disabled = false;
      }
    };
  }

  /* ------------------------------------------------------------ 事件绑定 */

  function bindEvents() {
    $('#main-tabs').addEventListener('click', (e) => {
      const btn = e.target.closest('.tab');
      if (btn) switchView(btn.dataset.view);
    });

    $('#book-form').addEventListener('submit', submitBooking);
    $('#book-form').querySelector('[name=password_enabled]').addEventListener('change', (e) => {
      const field = $('#password-field');
      field.classList.toggle('hidden', !e.target.checked);
      if (e.target.checked) field.querySelector('input').focus();
    });
    $('#book-form').addEventListener('input', (e) => {
      e.currentTarget.dataset.touched = '1';
    });
    $('#my-refresh').onclick = () => loadMy().then(() => toast('已刷新'));

    $('#timeline').addEventListener('click', (e) => {
      const block = e.target.closest('.tl-block');
      if (block) {
        const data = state.tlBlocks[Number(block.dataset.block)];
        if (data) slotSheet(data);
        return;
      }
      const track = e.target.closest('.tl-track');
      if (!track) return;
      const rect = track.getBoundingClientRect();
      if (!rect.width) return;
      const ratio = (e.clientX - rect.left) / rect.width;
      const hourFloat = state.tlStartH + ratio * state.tlSpan;
      const total = Math.max(0, Math.min(23 * 60 + 55, Math.round(hourFloat * 12) * 5));
      freeSlotSheet(track.dataset.acc || '这个号', { hour: Math.floor(total / 60), minute: total % 60 });
    });

    $('#tl-prev').onclick = () => {
      const d = new Date(state.tlDate + 'T12:00:00');
      d.setDate(d.getDate() - 1);
      loadTimeline(isoDate(d));
    };
    $('#tl-next').onclick = () => {
      const d = new Date(state.tlDate + 'T12:00:00');
      d.setDate(d.getDate() + 1);
      loadTimeline(isoDate(d));
    };

    $('#login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.target;
      const note = $('#login-note');
      note.classList.remove('err');
      try {
        state.admin = await api('/api/admin/login', {
          method: 'POST',
          body: JSON.stringify({ username: form.username.value.trim(), password: form.password.value }),
        });
        form.password.value = '';
        await ensureAdmin();
        toast('已登录');
      } catch (err) {
        note.textContent = err.message;
        note.classList.add('err');
      }
    });

    $('#btn-logout').onclick = async () => {
      await api('/api/admin/logout', { method: 'POST' });
      state.admin = null;
      switchView('borrow');
      toast('已退出');
    };
    $('#btn-add-account').onclick = () => openAccountModal(null);
    $('#btn-diag').onclick = openDiagnoseModal;
    $('#btn-sync').onclick = async (e) => {
      const btn = e.currentTarget;
      const old = btn.textContent;
      btn.disabled = true;
      btn.textContent = '对账中…';
      try {
        const r = await api('/api/admin/sync', { method: 'POST' });
        const parts = [`对账 ${r.accounts} 个号`, `上游新增 ${r.imported} 场`];
        if (r.cancel_failed) parts.push(`${r.cancel_failed} 场取消失败`);
        if (r.missing) parts.push(`${r.missing} 场上游已消失`);
        toast(parts.join(' · '), 4000);
        if (r.errors && r.errors.length) {
          toast(`有 ${r.errors.length} 个号对账失败：${r.errors[0].label} ${r.errors[0].error}`, 6000);
        }
        await Promise.all([loadAdmin(), loadState(), loadTimeline()]);
      } catch (err) {
        toast(err.message, 6000);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    };
    $('#btn-refresh').onclick = () => loadAdmin().then(() => toast('已刷新'));
    $('#btn-export').onclick = () => {
      const d = $('#filter-date').value;
      const q = d ? `?date_from=${d}&date_to=${d}` : '';
      window.open(`${API_BASE}/api/admin/export.csv${q}`, '_blank');
    };

    ['#filter-date', '#filter-status'].forEach((sel) => {
      $(sel).onchange = () => loadAdmin().catch((e) => toast(e.message));
    });
    let kwTimer = null;
    $('#filter-keyword').oninput = () => {
      clearTimeout(kwTimer);
      kwTimer = setTimeout(() => loadAdmin().catch((e) => toast(e.message)), 350);
    };
    $('#filter-clear').onclick = () => {
      $('#filter-date').value = '';
      $('#filter-status').value = '';
      $('#filter-keyword').value = '';
      loadAdmin().catch((e) => toast(e.message));
    };

    $('#config-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.target;
      const payload = {};
      ['buffer_minutes', 'max_per_week', 'window_days', 'min_minutes', 'max_minutes',
        'work_hour_start', 'work_hour_end'].forEach((k) => { payload[k] = Number(form.elements[k].value); });
      payload.notice = form.elements.notice.value;
      try {
        await api('/api/admin/config', { method: 'PATCH', body: JSON.stringify(payload) });
        toast('规则已保存');
        await loadState();
        await loadAdmin();
      } catch (err) { toast(err.message); }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('#modal-root').classList.contains('hidden')) closeModal(false);
    });
  }

  /* ------------------------------------------------------------ 启动 */

  async function boot() {
    bindEvents();
    $('#foot-time').textContent = new Date().toLocaleString('zh-CN', { hour12: false });
    try {
      await loadState();
      await loadTimeline();
      await loadMy();
    } catch (e) {
      toast(`加载失败：${e.message}`, 3600);
    }
    const hash = location.hash.replace('#/', '');
    switchView(hash === 'admin' ? 'admin' : 'borrow');
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
