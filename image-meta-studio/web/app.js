/* ImageMeta Studio 前端逻辑 */
'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const RATIOS = {'16:9': [16, 9], '9:16': [9, 16], '4:3': [4, 3], '3:2': [3, 2], '1:1': [1, 1]};
// 比例锁定：改宽自动算高、改高自动算宽；与后端 normalize_dimensions 用同一套四舍五入
function syncRatio(changed) {
  const r = RATIOS[$('#opt-ratio').value];
  if (!r) return;
  if (changed === 'w') {
    const w = Number($('#opt-w').value) || 0;
    $('#opt-h').value = w ? Math.round(w * r[1] / r[0]) : 0;
  } else {
    const h = Number($('#opt-h').value) || 0;
    $('#opt-w').value = h ? Math.round(h * r[0] / r[1]) : 0;
  }
}

const state = {
  root: '', items: [], selected: new Set(), config: null,
  editIndex: -1, jobId: '', jobKind: '', aiSeen: 0, timer: null,
  dirPath: '', dirParent: '', lastMissing: [],
};

async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET'};
  if (opts.body) {
    init.body = JSON.stringify(opts.body);
    init.headers = {'Content-Type': 'application/json'};
  }
  const res = await fetch(path, init);
  let data;
  try { data = await res.json(); } catch (e) { throw new Error('服务返回了非 JSON 内容'); }
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

let toastTimer;
function toast(msg, type = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3600);
}
function showModal(id) { $(id).classList.remove('hidden'); }
function closeModals() { $$('.modal').forEach((m) => m.classList.add('hidden')); }
function sizeText(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function splitKw(s) {
  return String(s || '').split(/[;,，；、|\n]+/).map((x) => x.trim()).filter(Boolean);
}
function ratioLabel(w, h) {
  if (!w || !h) return '';
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  const d = gcd(w, h) || 1;
  const rw = Math.round(w / d), rh = Math.round(h / d);
  return (rw === 16 && rh === 9) ? '16:9' : `${rw}:${rh}`;
}

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    const h = await api('/api/health');
    $('#health').textContent = '本地服务正常 · 127.0.0.1:' + h.port;
    $('#health').classList.add('ok');
    state.config = await api('/api/config');
    applyDefaults(state.config.defaults || {});
    fillAi(state.config.ai || {});
  } catch (e) {
    $('#health').textContent = '服务连接失败';
    toast('无法连接本地服务：' + e.message, 'bad');
  }
}

function applyDefaults(d) {
  $('#opt-w').value = d.width || 0;
  $('#opt-h').value = d.height || 0;
  $('#opt-ratio').value = d.ratio || 'free';
  $('#opt-fit').value = d.fit || 'contain';
  $('#opt-bg').value = /^#[0-9a-f]{6}$/i.test(d.bg || '') ? d.bg : '#ffffff';
  $('#opt-bg-text').value = d.bg || '#FFFFFF';
  $('#opt-format').value = d.format || 'keep';
  $('#opt-quality').value = d.quality || 92;
  $('#q-label').textContent = $('#opt-quality').value;
  $('#opt-suffix').value = d.suffix || '';
  $('#opt-tree').checked = d.keep_tree !== false;
  $('#opt-sidecar').checked = d.sidecar !== false;
}
function fillAi(ai) {
  $('#ai-base').value = ai.base_url || '';
  $('#ai-model').value = ai.model || '';
  $('#ai-conc').value = ai.concurrency || 3;
  $('#ai-timeout').value = ai.timeout || 60;
  $('#ai-proxy').value = ai.proxy_mode || 'auto';
  $('#ai-prompt').value = ai.prompt || '';
  $('#ai-key').value = '';
  $('#ai-key').placeholder = ai.api_key_set ? '已保存密钥（留空表示不修改）' : '留空 = 使用离线兜底补全';
}
function aiPayload() {
  return {
    base_url: $('#ai-base').value.trim(),
    api_key: $('#ai-key').value.trim(),
    model: $('#ai-model').value.trim(),
    concurrency: Number($('#ai-conc').value) || 3,
    timeout: Number($('#ai-timeout').value) || 60,
    proxy_mode: $('#ai-proxy').value,
    prompt: $('#ai-prompt').value,
  };
}
async function saveAi(silent = false) {
  const r = await api('/api/config', {method: 'POST', body: {ai: aiPayload()}});
  state.config = r.config;
  fillAi(r.config.ai || {});
  if (!silent) toast('AI 设置已保存', 'ok');
  return r;
}

/* ---------------- 目录浏览 ---------------- */
async function openBrowse(path) {
  showModal('#modal-dir');
  try {
    const d = await api('/api/browse?path=' + encodeURIComponent(path || ''));
    state.dirPath = d.path || '';
    state.dirParent = d.parent || '';
    $('#dir-current').textContent = d.path || '本机磁盘';
    let html = '';
    if (d.drives && d.drives.length) {
      html += '<div class="section-label">磁盘</div>';
      d.drives.forEach((p) => html += `<div class="dir-item" data-dir="${esc(p)}"><span>💽 ${esc(p)}</span><small>打开</small></div>`);
    }
    if (d.quick && d.quick.length) {
      html += '<div class="section-label">常用位置</div>';
      d.quick.forEach((q) => html += `<div class="dir-item" data-dir="${esc(q.path)}"><span>📁 ${esc(q.name)}</span><small>${esc(q.path)}</small></div>`);
    }
    if (d.path) {
      html += `<div class="section-label">当前目录 · ${d.image_count || 0} 张图片</div>`;
      if (!d.dirs.length) html += '<div class="dir-item" style="cursor:default;color:var(--dim)">没有子文件夹</div>';
      d.dirs.forEach((x) => html += `<div class="dir-item" data-dir="${esc(x.path)}"><span>📂 ${esc(x.name)}</span><small>进入</small></div>`);
    }
    $('#dir-list').innerHTML = html;
  } catch (e) { toast(e.message, 'bad'); }
}

/* ---------------- 扫描 ---------------- */
async function scan(root) {
  const target = (root || $('#root-input').value).trim().replace(/^"|"$/g, '');
  if (!target) { toast('请先选择或填写一个文件夹', 'bad'); openBrowse(''); return; }
  $('#scan-stat').textContent = '正在扫描…';
  try {
    const d = await api('/api/scan', {method: 'POST', body: {root: target, recursive: $('#recursive').checked}});
    state.root = d.root;
    state.items = d.items;
    $('#root-input').value = d.root;
    state.selected = new Set(d.items.map((it, i) => i).filter((i) => !d.items[i].error));
    $('#select-all').checked = true;
    renderTable();
    toast(`扫描完成，找到 ${d.items.length} 张图片`, 'ok');
  } catch (e) {
    $('#scan-stat').textContent = '扫描失败';
    toast(e.message, 'bad');
  }
}

function renderStats() {
  const items = state.items;
  const ok = items.filter((x) => !x.error);
  const total = ok.reduce((a, x) => a + (x.bytes || 0), 0);
  const ai = ok.filter((x) => x.meta && x.meta.source === 'ai').length;
  const bad = items.length - ok.length;
  $('#meta-count').textContent = `${items.length} 张 · 已选 ${state.selected.size}`;
  if (!items.length) { $('#scan-stat').className = 'stat-box empty'; $('#scan-stat').textContent = '尚未扫描'; return; }
  $('#scan-stat').className = 'stat-box';
  $('#scan-stat').innerHTML = `共 <b>${items.length}</b> 张 · 有效 <b>${ok.length}</b> 张 · 合计 ${sizeText(total)}<br>`
    + `AI 已打标 ${ai} 张${bad ? ` · <span style="color:var(--red)">${bad} 张读取失败</span>` : ''}<br>`
    + `<span class="mono">${esc(state.root)}</span>`;
}

function sourceBadge(it) {
  if (it.error) return '<span class="badge err">读取失败</span>';
  const s = (it.meta || {}).source || 'manual';
  const map = {ai: ['AI', 'ai'], offline: ['离线', 'offline'], existing: ['已有', 'existing'], manual: ['手动', 'manual']};
  const [label, cls] = map[s] || map.manual;
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderTable() {
  renderStats();
  const wrap = $('#table-wrap');
  if (!state.items.length) {
    wrap.innerHTML = '<div class="empty-state">先在左侧选择一个文件夹并扫描 <span class="kbd">↓</span></div>';
    return;
  }
  let rows = '';
  state.items.forEach((it, i) => {
    const m = it.meta || {};
    const kws = (m.keywords || []).join('、');
    rows += `<tr class="${state.selected.has(i) ? 'selected' : ''}" data-i="${i}">
      <td><input type="checkbox" class="row-check" data-i="${i}" ${state.selected.has(i) ? 'checked' : ''} ${it.error ? 'disabled' : ''}></td>
      <td class="thumb"><img loading="lazy" src="/api/thumb?path=${encodeURIComponent(it.path)}&w=92" alt=""></td>
      <td><div class="t-name" title="${esc(it.name)}">${esc(it.name)}</div>
          <div class="t-sub">${it.width}×${it.height} · ${esc(it.format || it.ext)} · ${sizeText(it.bytes)}</div></td>
      <td><div class="cell-clip" title="${esc(m.title || '')}">${esc(m.title || '—')}</div>
          <div class="cell-clip" title="${esc(m.description || '')}">${esc(m.description || '—')}</div></td>
      <td><div class="cell-clip" title="${esc(kws)}">${esc(kws || '—')}</div></td>
      <td>${sourceBadge(it)}</td>
      <td><button class="btn ghost sm" data-act="edit" data-i="${i}">编辑</button></td>
    </tr>`;
  });
  wrap.innerHTML = `<table><thead><tr>
    <th style="width:34px"></th><th style="width:58px"></th><th style="width:210px">文件</th>
    <th>标题 / 描述</th><th style="width:170px">关键词</th><th style="width:78px">来源</th><th style="width:64px"></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

/* ---------------- 逐张编辑 ---------------- */
function renderCustomRows(obj) {
  const rows = Object.entries(obj || {});
  $('#custom-rows').innerHTML = rows.length ? rows.map(([k, v]) => customRow(k, v)).join('') : customRow('', '');
}
function customRow(k, v) {
  return `<div class="custom-row"><input class="input" data-ck placeholder="字段名" value="${esc(k)}">
    <input class="input" data-cv placeholder="字段值" value="${esc(v)}">
    <button class="btn ghost sm" data-act="del-custom">×</button></div>`;
}
function collectCustom() {
  const out = {};
  $$('#custom-rows .custom-row').forEach((r) => {
    const k = r.querySelector('[data-ck]').value.trim();
    const v = r.querySelector('[data-cv]').value.trim();
    if (k) out[k] = v;
  });
  return out;
}
function openEdit(i) {
  if (i < 0 || i >= state.items.length) return;
  state.editIndex = i;
  const it = state.items[i], m = it.meta || {};
  $('#edit-title').textContent = `编辑元数据 · ${i + 1} / ${state.items.length}`;
  $('#edit-img').src = '/api/thumb?path=' + encodeURIComponent(it.path) + '&w=440';
  $('#edit-file').textContent = it.path;
  $('#f-title').value = m.title || '';
  $('#f-desc').value = m.description || '';
  $('#f-kw').value = (m.keywords || []).join('; ');
  $('#f-cat').value = m.category || '';
  $('#f-author').value = m.author || '';
  $('#f-copyright').value = m.copyright || '';
  $('#f-alt').value = m.alt || '';
  renderCustomRows(m.custom || {});
  showModal('#modal-edit');
}
function saveCurrentEdit() {
  const i = state.editIndex;
  if (i < 0) return;
  const m = state.items[i].meta || {};
  Object.assign(m, {
    title: $('#f-title').value.trim(),
    description: $('#f-desc').value.trim(),
    keywords: splitKw($('#f-kw').value),
    category: $('#f-cat').value.trim(),
    author: $('#f-author').value.trim(),
    copyright: $('#f-copyright').value.trim(),
    alt: $('#f-alt').value.trim(),
    custom: collectCustom(),
    source: 'manual',
  });
  state.items[i].meta = m;
  renderTable();
}

/* ---------------- 批量套用 ---------------- */
function applyBatch() {
  const author = $('#b-author').value.trim(), copyright = $('#b-copyright').value.trim();
  const cat = $('#b-cat').value.trim(), prefix = $('#b-prefix').value.trim();
  const kws = splitKw($('#b-kw').value);
  const ck = $('#b-ckey').value.trim(), cv = $('#b-cval').value.trim();
  if (![...state.selected].length) { toast('请先勾选图片', 'bad'); return; }
  let n = 0;
  state.selected.forEach((i) => {
    const m = state.items[i].meta || {};
    if (author) m.author = author;
    if (copyright) m.copyright = copyright;
    if (cat) m.category = cat;
    if (prefix && !(m.title || '').startsWith(prefix)) m.title = prefix + (m.title || '');
    if (kws.length) m.keywords = Array.from(new Set([...(m.keywords || []), ...kws]));
    if (ck) { m.custom = m.custom || {}; m.custom[ck] = cv; }
    state.items[i].meta = m; n++;
  });
  renderTable(); closeModals();
  toast(`已套用到 ${n} 张图片`, 'ok');
}

async function autofill() {
  if (!state.items.length) { toast('请先扫描图片', 'bad'); return; }
  try {
    const r = await api('/api/autofill', {
      method: 'POST',
      body: {root: state.root, paths: state.items.map((i) => i.path)}});
    const map = new Map(r.items.map((x) => [x.path, x.meta]));
    state.items.forEach((it) => {
      const m = map.get(it.path);
      if (m) it.meta = {...(it.meta || {}), ...m, source: 'manual'};
    });
    renderTable();
    toast(`已填充 ${r.items.length} 张：类别 / 姿态 / 关键词 / 编号`
      + (r.manifest_hit ? `，并匹配来源清单 ${r.manifest_hit} 条（作者·授权·来源页）` : '（按目录名解析，未找到来源清单）'), 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}

/* ---------------- 任务调度 ---------------- */
function showProgress(text) {
  $('#progress-wrap').classList.remove('hidden');
  $('#cancel-btn').classList.remove('hidden');
  $('#progress-bar').style.width = '0%';
  $('#progress-pct').textContent = '0%';
  $('#progress-text').textContent = text;
}
function updateProgress(j, label) {
  const pct = j.total ? Math.round(j.done / j.total * 100) : 0;
  $('#progress-bar').style.width = pct + '%';
  $('#progress-pct').textContent = pct + '%';
  $('#progress-text').textContent = `${label}：${j.done} / ${j.total}` + (j.errors && j.errors.length ? ` · ${j.errors.length} 项异常` : '');
}
function pollJob() {
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    let j;
    try { j = await api('/api/job?id=' + state.jobId); } catch (e) { clearInterval(state.timer); toast(e.message, 'bad'); return; }
    if (state.jobKind === 'ai') {
      const fresh = j.results.slice(state.aiSeen);
      if (fresh.length) {
        const byPath = new Map(state.items.map((it, i) => [it.path, i]));
        fresh.forEach((r) => {
          const i = byPath.get(r.path);
          if (i !== undefined) { state.items[i].meta = r.meta; state.items[i].aiError = r.error || ''; }
        });
        state.aiSeen = j.results.length;
        renderTable();
      }
    }
    updateProgress(j, state.jobKind === 'ai' ? 'AI 生成中' : '图片转换中');
    if (j.status !== 'running') {
      clearInterval(state.timer);
      $('#cancel-btn').classList.add('hidden');
      if (state.jobKind === 'ai') finishAi(j); else renderReport(j);
    }
  }, 350);
}
async function startAi(scope, offline = false) {
  const idxs = scope === 'all'
    ? state.items.map((_, i) => i).filter((i) => !state.items[i].error)
    : [...state.selected].filter((i) => !state.items[i].error);
  if (!idxs.length) { toast('没有可处理的图片，请先扫描并勾选', 'bad'); return; }
  try {
    const r = await api('/api/ai/job', {
      method: 'POST',
      body: {paths: idxs.map((i) => state.items[i].path), offline},
    });
    state.jobId = r.job_id; state.jobKind = 'ai'; state.aiSeen = 0;
    showProgress(offline ? '离线补全中' : 'AI 生成中');
    pollJob();
  } catch (e) { toast(e.message, 'bad'); }
}
function finishAi(j) {
  const s = j.summary || {}, errs = j.errors || [];
  let html = `<h4>AI 打标完成</h4><div class="kpis">
    <div class="kpi good"><b>${s.ai || 0}</b><span>AI 成功</span></div>
    <div class="kpi"><b>${s.offline || 0}</b><span>离线兜底</span></div>
    <div class="kpi bad"><b>${errs.length}</b><span>异常</span></div></div>`;
  if (errs.length) {
    html += `<div class="notice bad">部分图片走了离线兜底，常见原因：未配置密钥、接口不通、模型不支持视觉、返回非 JSON。前 5 条：<br>`
      + errs.slice(0, 5).map((e) => `<span class="mono">${esc(e.path.split(/[\\/]/).pop())}</span> — ${esc(String(e.error).slice(0, 120))}`).join('<br>') + '</div>';
  } else if (s.ai) {
    html += '<div class="notice ok">所有图片均已由模型生成标题 / 描述 / 关键词，可在表格里核对或逐张修改。</div>';
  }
  html += '<div class="notice">下一步：设置输出规格（如 1080 × 720），然后点「转换为带元数据的图片」，即可得到另存的新图片文件。</div>';
  $('#report').className = 'report'; $('#report').innerHTML = html;
  toast(offline ? '离线补全完成' : 'AI 打标完成', 'ok');
}

/* ---------------- 输出与处理 ---------------- */
function collectOptions() {
  const w = Number($('#opt-w').value) || 0, h = Number($('#opt-h').value) || 0;
  return {
    width: w, height: h, ratio: $('#opt-ratio').value,
    fit: (!w || !h) ? 'none' : $('#opt-fit').value,
    bg: $('#opt-bg-text').value.trim() || '#FFFFFF',
    format: $('#opt-format').value,
    quality: Number($('#opt-quality').value) || 92,
    suffix: $('#opt-suffix').value.trim(),
    out_dir: $('#opt-outdir').value.trim(),
    keep_tree: $('#opt-tree').checked,
    sidecar: $('#opt-sidecar').checked,
  };
}
function renderReport(j) {
  const s = j.summary || {};
  const ok = s.success || 0, bad = s.failed || 0;
  let html = `<h4>${j.status === 'cancelled' ? '任务已取消' : '转换完成'}</h4>`;
  html += `<div class="kpis">
    <div class="kpi good"><b>${ok}</b><span>成功写入</span></div>
    <div class="kpi bad"><b>${bad}</b><span>失败</span></div>
    <div class="kpi"><b>${sizeText(s.total_bytes || 0)}</b><span>输出体积</span></div>
    <div class="kpi"><b>${(j.results || []).length}</b><span>已处理</span></div></div>`;
  if (s.out_dir) {
    html += `<div class="row" style="flex-wrap:wrap"><span class="mono">${esc(s.out_dir)}</span>
      <button class="btn ghost sm" data-act="reveal" data-path="${esc(s.out_dir)}">打开输出目录</button></div>`;
  }
  const firstOk = (j.results || []).find((r) => r.ok);
  const dims = firstOk && firstOk.new_size ? firstOk.new_size : [];
  const dimText = dims.length === 2 ? `，输出 <b>${ratioLabel(dims[0], dims[1])}</b>（${dims.join('×')}）` : '';
  if (ok) html += `<div class="notice ok">已生成 <b>${ok}</b> 份内嵌元数据的新图片${dimText}，原图保持不动。点「打开输出目录」即可拿到成品；鼠标悬停文件行可查看完整路径。</div>`;
  if (state.lastMissing && state.lastMissing.length) html += `<div class="notice bad">另有 ${state.lastMissing.length} 张原图已失效被跳过（被移动 / 重命名 / 删除），请重新扫描目录后再单独转换。</div>`;
  const sc = s.sidecar || {};
  if (sc.csv) html += `<div class="notice ok">台账已生成：<span class="mono">${esc(sc.csv)}</span><br><span class="mono">${esc(sc.json)}</span></div>`;
  if (sc.error) html += `<div class="notice bad">${esc(sc.error)}</div>`;
  const fails = (j.results || []).filter((r) => !r.ok);
  if (fails.length) html += `<div class="notice bad"><b>失败清单</b><br>` + fails.slice(0, 10).map((r) =>
    `<span class="mono">${esc(String(r.input).split(/[\\/]/).pop())}</span> — ${esc(r.error)}`).join('<br>') + '</div>';
  const list = (j.results || []).slice(0, 40);
  if (list.length) {
    html += '<div class="rlist">' + list.map((r) => `<div title="${esc(r.output || r.input || '')}">
      <span class="${r.ok ? 'ok' : 'fail'}">${r.ok ? '✓' : '✕'} ${esc(String(r.output || r.input).split(/[\\/]/).pop())}</span>
      <small>${r.ok ? `${(r.new_size || []).join('×')} · ${esc((r.written || []).join('+'))}` : esc(r.error || '')}</small></div>`).join('') + '</div>';
    if (j.results.length > 40) html += `<div class="hint">仅显示前 40 条，完整清单见 metadata.json</div>`;
  }
  $('#report').className = 'report'; $('#report').innerHTML = html;
  toast(`转换完成：生成 ${ok} 份新图片，失败 ${bad} 张`, bad ? 'bad' : 'ok');
}
async function runProcess() {
  let idxs = [...state.selected].filter((i) => !state.items[i].error);
  if (!idxs.length) { toast('请先扫描并勾选要转换的图片', 'bad'); return; }
  const options = collectOptions();
  try {
    // 转换前先确认原图还在：扫描后文件被移动/删除是最常见的失败原因
    const v = await api('/api/verify', {
      method: 'POST', body: {paths: idxs.map((i) => state.items[i].path)}});
    state.lastMissing = v.missing || [];
    if (state.lastMissing.length) {
      const miss = new Set(state.lastMissing);
      idxs = idxs.filter((i) => !miss.has(state.items[i].path));
      toast(`${state.lastMissing.length} 张原图已不在原路径，已跳过；建议重新扫描`, 'bad');
      if (!idxs.length) {
        $('#report').className = 'report';
        $('#report').innerHTML = '<div class="notice bad"><b>所选图片全部已失效</b>：原图可能被移动、重命名或删除，目录里已找不到它们。请重新选择文件夹并点「扫描图片」。<br>'
          + state.lastMissing.slice(0, 5).map((p) => `<span class="mono">${esc(p)}</span>`).join('<br>') + '</div>';
        return;
      }
    }
    const {out_dir, ...defaults} = options;
    await api('/api/config', {method: 'POST', body: {defaults}});
    const r = await api('/api/process/job', {
      method: 'POST',
      body: {root: state.root, items: idxs.map((i) => ({path: state.items[i].path, meta: state.items[i].meta || {}})), options},
    });
    state.jobId = r.job_id; state.jobKind = 'process';
    showProgress('图片转换中');
    $('#report').className = 'report empty'; $('#report').textContent = '正在转换并另存为新图片…';
    pollJob();
  } catch (e) { toast(e.message, 'bad'); }
}

/* ---------------- 拖拽上传 ---------------- */
function fileToData(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve({name: file.name, data: r.result});
    r.onerror = () => reject(new Error('读取文件失败：' + file.name));
    r.readAsDataURL(file);
  });
}
async function handleFiles(files) {
  const list = Array.from(files).filter((f) => /\.(jpe?g|png|webp|tiff?|bmp|gif|jfif)$/i.test(f.name));
  if (!list.length) { toast('没有识别到支持的图片格式', 'bad'); return; }
  toast(`正在导入 ${list.length} 张图片…`);
  try {
    let inboxRoot = '';
    for (let i = 0; i < list.length; i += 5) {
      const batch = await Promise.all(list.slice(i, i + 5).map(fileToData));
      const r = await api('/api/upload', {method: 'POST', body: {files: batch}});
      inboxRoot = r.root;
    }
    if (inboxRoot) { $('#root-input').value = inboxRoot; await scan(inboxRoot); }
    toast(`已导入 ${list.length} 张到收件箱`, 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener('click', async (e) => {
  if (e.target.classList && e.target.classList.contains('modal')) { closeModals(); return; }
  const el = e.target.closest('[data-act], [data-dir], [data-preset]');
  if (!el) return;
  const act = el.dataset.act;
  const preset = el.dataset.preset;
  const dir = el.dataset.dir;
  if (dir) { await openBrowse(dir); return; }
  if (preset) {
    const [wRaw, hRaw, ratio] = preset.split(',');
    const w = Number(wRaw), h = Number(hRaw);
    $('#opt-w').value = w; $('#opt-h').value = h;
    if (ratio) $('#opt-ratio').value = ratio;
    if (!w || !h) $('#opt-fit').value = 'none'; else if ($('#opt-fit').value === 'none') $('#opt-fit').value = 'contain';
    $$('.chip').forEach((c) => c.classList.toggle('active', c === el));
    return;
  }
  switch (act) {
    case 'autofill': await autofill(); break;
    case 'onekey-169':
      $('#opt-ratio').value = '16:9';
      $('#opt-w').value = 1920; $('#opt-h').value = 1080;
      $('#opt-fit').value = 'cover';
      $$('.chip').forEach((c) => c.classList.toggle('active', c.dataset.preset === '1920,1080,16:9'));
      toast('已设为 1920×1080（16:9）+ 居中裁剪铺满，成品满幅无留白', 'ok');
      break;
    case 'help': showModal('#modal-help'); break;
    case 'close-modal': closeModals(); break;
    case 'browse': await openBrowse(state.root || ''); break;
    case 'dir-up': await openBrowse(state.dirParent || ''); break;
    case 'dir-confirm':
      if (!state.dirPath) { toast('请先进入一个磁盘或文件夹', 'bad'); break; }
      $('#root-input').value = state.dirPath; closeModals(); await scan(state.dirPath); break;
    case 'scan': await scan(); break;
    case 'pick-files': $('#file-input').click(); break;
    case 'ai-settings': fillAi(state.config.ai || {}); $('#ai-test-result').classList.add('hidden'); showModal('#modal-ai'); break;
    case 'ai-save': try { await saveAi(true); closeModals(); toast('AI 设置已保存', 'ok'); } catch (err) { toast(err.message, 'bad'); } break;
    case 'ai-test': {
      const box = $('#ai-test-result'); box.classList.remove('hidden', 'ok', 'bad');
      box.textContent = '正在测试…';
      try {
        await saveAi(true);
        const r = await api('/api/ai/ping', {method: 'POST', body: {}});
        box.className = 'notice ' + (r.ok ? 'ok' : 'bad'); box.textContent = r.message;
      } catch (err) { box.className = 'notice bad'; box.textContent = err.message; }
      break;
    }
    case 'ai-selected': await startAi('selected'); break;
    case 'ai-all': await startAi('all'); break;
    case 'offline': await startAi('selected', true); break;
    case 'batch': showModal('#modal-batch'); break;
    case 'batch-apply': applyBatch(); break;
    case 'clear-meta':
      if (!state.selected.size) { toast('请先勾选图片', 'bad'); break; }
      state.selected.forEach((i) => { state.items[i].meta = {title:'',description:'',keywords:[],author:'',copyright:'',category:'',alt:'',custom:{},source:'manual'}; });
      renderTable(); toast('已清空所选元数据', 'ok'); break;
    case 'edit': openEdit(Number(el.dataset.i)); break;
    case 'add-custom': $('#custom-rows').insertAdjacentHTML('beforeend', customRow('', '')); break;
    case 'del-custom': {
      const row = el.closest('.custom-row');
      if ($('#custom-rows').children.length > 1) row.remove(); else { row.querySelector('[data-ck]').value = ''; row.querySelector('[data-cv]').value = ''; }
      break;
    }
    case 'edit-prev': saveCurrentEdit(); openEdit(Math.max(0, state.editIndex - 1)); break;
    case 'edit-next': saveCurrentEdit(); openEdit(Math.min(state.items.length - 1, state.editIndex + 1)); break;
    case 'edit-save': saveCurrentEdit(); closeModals(); toast('已保存', 'ok'); break;
    case 'run': await runProcess(); break;
    case 'cancel-job':
      try { await api('/api/job/cancel', {method: 'POST', body: {id: state.jobId}}); toast('已请求取消，正在收尾…'); } catch (err) { toast(err.message, 'bad'); }
      break;
    case 'reveal':
      try { await api('/api/reveal', {method: 'POST', body: {path: el.dataset.path}}); } catch (err) { toast(err.message, 'bad'); }
      break;
  }
});

document.addEventListener('change', (e) => {
  const t = e.target;
  if (t.id === 'select-all') {
    state.selected = new Set(t.checked ? state.items.map((_, i) => i).filter((i) => !state.items[i].error) : []);
    renderTable();
  } else if (t.classList.contains('row-check')) {
    const i = Number(t.dataset.i);
    if (t.checked) state.selected.add(i); else state.selected.delete(i);
    renderTable();
  } else if (t.id === 'opt-bg') {
    $('#opt-bg-text').value = t.value.toUpperCase();
  } else if (t.id === 'opt-bg-text') {
    const v = t.value.trim();
    if (/^#[0-9a-f]{6}$/i.test(v)) $('#opt-bg').value = v;
  } else if (t.id === 'opt-ratio') {
    syncRatio('w');
  } else if (t.id === 'file-input') {
    if (t.files && t.files.length) handleFiles(t.files);
    t.value = '';
  }
});
document.addEventListener('input', (e) => {
  const id = e.target.id;
  if (id === 'opt-quality') $('#q-label').textContent = e.target.value;
  else if (id === 'opt-w') syncRatio('w');
  else if (id === 'opt-h') syncRatio('h');
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModals(); });

const dz = $('#dropzone');
dz.addEventListener('click', () => $('#file-input').click());
['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('over'); }));
dz.addEventListener('drop', (e) => { if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files); });

init();
