/* 生图网页版前端。原生 JS，无构建步骤。遵循 Endfield UI 规范。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  cfg: null,
  tab: 'txt2img',
  inputs: {
    txt2img: { prompt: '', model: '', size: '', name: '' },
    img2img: { prompt: '', model: '', size: '', name: '' },
  },
  files: [],          // {id, file, url}
  tasks: new Map(),   // id -> task 对象
  settings: null,     // GET /api/settings 的结果
  keyDirty: false,
  dirDirty: false,
  urlDirty: false,
  histOffset: 0,
  histLimit: 30,
  histTotal: 0,
  theme: 'dark',      // dark（夜班）| light（冷库）| classic-dark（原版暗色）| system
};

/* ------------------------------------------------------------------ Endfield 主题切换 */

function initTheme() {
  const saved = localStorage.getItem('a6_theme_preference') || 'dark';
  applyTheme(saved, false);

  const media = window.matchMedia('(prefers-color-scheme: dark)');
  media.addEventListener('change', () => {
    if (state.theme === 'system') {
      applyTheme('system', false);
    }
  });

  const quickBtn = $('#btn-quick-theme');
  if (quickBtn) {
    quickBtn.onclick = () => {
      const currentEffective = document.documentElement.dataset.theme;
      const nextTheme = currentEffective === 'dark' ? 'light' : 'dark';
      applyTheme(nextTheme, true);
    };
  }

  $$('input[name="app-theme"]').forEach((radio) => {
    radio.onchange = (e) => {
      if (e.target.checked) {
        applyTheme(e.target.value, true);
      }
    };
  });
}

function applyTheme(pref, save = true) {
  if (!['dark', 'light', 'classic-dark', 'system'].includes(pref)) pref = 'dark';
  state.theme = pref;
  let effective = pref;
  if (pref === 'system') {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    effective = isDark ? 'dark' : 'light';
  }

  // 浅色对应 coldstore，深色对应 nightshift
  const variant = effective === 'classic-dark' ? 'classic-dark'
    : effective === 'light' ? 'coldstore' : 'nightshift';

  document.documentElement.dataset.theme = effective === 'classic-dark' ? 'dark' : effective;
  document.documentElement.dataset.efVariant = variant;

  const quickBtn = $('#btn-quick-theme');
  const nextLabel = effective === 'light' ? '切换到夜班暗色' : '切换到冷库亮色';
  quickBtn.title = nextLabel;
  quickBtn.setAttribute('aria-label', nextLabel);
  quickBtn.querySelector('.sun-icon').textContent = nextLabel;
  quickBtn.querySelector('.moon-icon').textContent = nextLabel;

  const targetRadio = $(`input[name="app-theme"][value="${pref}"]`);
  if (targetRadio) targetRadio.checked = true;

  if (save) {
    localStorage.setItem('a6_theme_preference', pref);
  }
}

/* ------------------------------------------------------------------ 模块拖拽调节系统 */

function initResizers() {
  // 1. 左右拖拽分割线（侧边栏宽度 300px - 1300px）
  const resizerCols = $('#resizer-cols');
  const sidebar = $('#col-sidebar');
  if (resizerCols && sidebar) {
    const savedWidth = localStorage.getItem('a6_sidebar_width');
    if (savedWidth && window.innerWidth > 900) {
      sidebar.style.width = savedWidth + 'px';
    }

    let startX = 0;
    let startW = 0;

    const onMouseMoveH = (e) => {
      const delta = e.clientX - startX;
      let newW = startW + delta;
      const maxW = Math.max(300, window.innerWidth - 320);
      newW = Math.min(Math.max(newW, 300), Math.min(maxW, 1300));
      sidebar.style.width = newW + 'px';
      localStorage.setItem('a6_sidebar_width', Math.round(newW));
    };

    const onMouseUpH = () => {
      document.body.classList.remove('resizing-h');
      window.removeEventListener('mousemove', onMouseMoveH);
      window.removeEventListener('mouseup', onMouseUpH);
    };

    resizerCols.addEventListener('mousedown', (e) => {
      if (window.innerWidth <= 900) return;
      startX = e.clientX;
      startW = sidebar.getBoundingClientRect().width;
      document.body.classList.add('resizing-h');
      window.addEventListener('mousemove', onMouseMoveH);
      window.addEventListener('mouseup', onMouseUpH);
    });

    resizerCols.addEventListener('dblclick', () => {
      sidebar.style.width = '440px';
      localStorage.removeItem('a6_sidebar_width');
    });
  }

  // 2. 上下拖拽分割线（任务面板 vs 历史面板）
  const resizerRows = $('#resizer-rows');
  const tasksPanel = $('#tasks-panel');
  const histPanel = $('#hist-panel');
  const colMain = $('#col-main');

  if (resizerRows && tasksPanel && histPanel && colMain) {
    const savedRatio = localStorage.getItem('a6_tasks_ratio');
    if (savedRatio) {
      tasksPanel.style.flex = `${savedRatio} 1 0%`;
      histPanel.style.flex = `${100 - savedRatio} 1 0%`;
    }

    let startY = 0;
    let startTasksH = 0;
    let totalH = 0;

    const onMouseMoveV = (e) => {
      const delta = e.clientY - startY;
      const newTasksH = startTasksH + delta;
      const minH = 100;
      const maxH = totalH - minH;
      const clampedH = Math.min(Math.max(newTasksH, minH), maxH);
      const ratio = (clampedH / totalH) * 100;

      tasksPanel.style.flex = `${ratio.toFixed(2)} 1 0%`;
      histPanel.style.flex = `${(100 - ratio).toFixed(2)} 1 0%`;
      localStorage.setItem('a6_tasks_ratio', ratio.toFixed(2));
    };

    const onMouseUpV = () => {
      document.body.classList.remove('resizing-v');
      window.removeEventListener('mousemove', onMouseMoveV);
      window.removeEventListener('mouseup', onMouseUpV);
    };

    resizerRows.addEventListener('mousedown', (e) => {
      startY = e.clientY;
      startTasksH = tasksPanel.getBoundingClientRect().height;
      totalH = colMain.getBoundingClientRect().height - 14;
      document.body.classList.add('resizing-v');
      window.addEventListener('mousemove', onMouseMoveV);
      window.addEventListener('mouseup', onMouseUpV);
    });

    resizerRows.addEventListener('dblclick', () => {
      tasksPanel.style.flex = '1 1 50%';
      histPanel.style.flex = '1 1 50%';
      localStorage.removeItem('a6_tasks_ratio');
    });
  }
}

/* ------------------------------------------------------------------ 工具函数 */

const fmtDur = (s) => {
  if (s == null) return '';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + '秒';
  return Math.floor(s / 60) + '分' + String(s % 60).padStart(2, '0') + '秒';
};

const fmtSize = (b) => {
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(0) + ' KB';
  return (b / 1024 / 1024).toFixed(1) + ' MB';
};

const fmtTime = (ts) => {
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const STATUS_TEXT = {
  queued: '排队中',
  running: '生成中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
};

async function api(url, opts) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch { /* 非 JSON 响应 */ }
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
  return body;
}

/* ---------------------------------------------------------------- 初始化 */

async function boot() {
  initTheme();
  initResizers();

  try {
    state.cfg = await api('/api/config');
  } catch (e) {
    alert('拿不到配置，后端可能未启动：' + e.message);
    return;
  }

  const c = state.cfg;
  if (c.mock) $('#mock-pill').hidden = false;

  fillSelect($('#model'), c.models, c.default_model);
  fillSelect($('#size'), c.sizes, c.default_size);

  for (const k of ['txt2img', 'img2img']) {
    state.inputs[k] = { prompt: '', model: $('#model').value, size: $('#size').value, name: '' };
  }

  $('#submit').onclick = submit;
  $('#tab-txt2img').onclick = () => setTab('txt2img');
  $('#tab-img2img').onclick = () => setTab('img2img');

  const drop = $('#drop');
  drop.onclick = () => $('#file-input').click();
  $('#file-input').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };

  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    addFiles(e.dataTransfer.files);
  };

  $('#prompt').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
  });

  $('#clear-tasks').onclick = dropFinishedTasks;
  $('#hist-reload').onclick = () => loadHistory(true);
  $('#hist-more').onclick = () => loadHistory(false);

  const closeLightbox = () => $('#lightbox').classList.remove('on');
  $('#lightbox-close').onclick = closeLightbox;
  $('#lightbox').onclick = (e) => {
    if (e.target.id === 'lightbox' || e.target.classList.contains('lightbox-content')) {
      closeLightbox();
    }
  };

  // ---- 设置面板 ----
  $('#btn-settings').onclick = openSettings;
  $('#settings-close').onclick = closeSettings;
  const cancelBtn = $('#settings-cancel');
  if (cancelBtn) cancelBtn.onclick = closeSettings;
  $('#settings-modal').onclick = (e) => { if (e.target.id === 'settings-modal') closeSettings(); };
  $('#settings-save').onclick = saveSettings;
  $('#set-browse').onclick = browseFolder;
  $('#set-key-toggle').onclick = toggleKey;
  $('#set-reset-dir').onclick = () => { $('#set-output-dir').value = ''; state.dirDirty = true; };
  $('#set-output-dir').oninput = () => { state.dirDirty = true; };
  $('#set-api-key').oninput = () => { state.keyDirty = true; };
  $('#set-base-url').oninput = () => { state.urlDirty = true; };
  $('#set-reset-url').onclick = () => { $('#set-base-url').value = ''; state.urlDirty = true; };

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeSettings();
    closeLightbox();
  });

  if (c.can_pick_folder === false) $('#set-browse').hidden = true;

  if (!c.has_api_key) {
    const h = $('#foot-hint');
    h.hidden = false;
    h.textContent = 'API key 尚未配置，请在「设置」中填入';
  }

  setTab('txt2img');
  await loadHistory(true);

  try {
    const r = await api('/api/tasks');
    r.items.forEach(upsertTask);
  } catch { /* 无所谓 */ }

  setInterval(pollTasks, 2000);
  setInterval(tick, 500);
}

function fillSelect(el, values, dflt) {
  el.innerHTML = '';
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v;
    if (v === dflt) o.selected = true;
    el.appendChild(o);
  }
}

/* ------------------------------------------------------------ 参考图管理 */

let fileSeq = 0;

function addFiles(list) {
  const max = (state.cfg && state.cfg.max_images) || 8;
  for (const f of list) {
    if (!f.type.startsWith('image/')) continue;
    if (state.files.length >= max) {
      formErr(`最多添加 ${max} 张参考图`);
      break;
    }
    state.files.push({ id: ++fileSeq, file: f, url: URL.createObjectURL(f) });
  }
  if (state.tab === 'txt2img') setTab('img2img');
  renderThumbs();
}

function renderThumbs() {
  const box = $('#thumbs');
  box.innerHTML = '';
  state.files.forEach((it, i) => {
    const d = document.createElement('div');
    d.className = 'thumb';

    const img = document.createElement('img');
    img.src = it.url;
    img.alt = it.file.name;

    const idx = document.createElement('span');
    idx.className = 'idx';
    idx.textContent = `图 ${i + 1}`;

    const ops = document.createElement('div');
    ops.className = 'ops';
    ops.append(
      mkOp('▲', i === 0, () => move(i, -1)),
      mkOp('▼', i === state.files.length - 1, () => move(i, 1)),
      mkOp('✕', false, () => remove(i)),
    );

    d.append(img, idx, ops);
    box.appendChild(d);
  });
}

function mkOp(label, disabled, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = label;
  b.disabled = disabled;
  b.onclick = fn;
  return b;
}

function move(i, d) {
  const j = i + d;
  if (j < 0 || j >= state.files.length) return;
  [state.files[i], state.files[j]] = [state.files[j], state.files[i]];
  renderThumbs();
}

function remove(i) {
  URL.revokeObjectURL(state.files[i].url);
  state.files.splice(i, 1);
  renderThumbs();
}

function setTab(tab) {
  const cur = state.inputs[state.tab];
  cur.prompt = $('#prompt').value;
  cur.model = $('#model').value;
  cur.size = $('#size').value;
  cur.name = $('#name').value;

  state.tab = tab;
  const next = state.inputs[tab];
  $('#prompt').value = next.prompt;
  $('#model').value = next.model;
  $('#size').value = next.size;
  $('#name').value = next.name;

  $('#tab-txt2img').classList.toggle('on', tab === 'txt2img');
  $('#tab-img2img').classList.toggle('on', tab === 'img2img');
  $('#ref-field').hidden = tab === 'txt2img';
  $('#thumbs').hidden = tab === 'txt2img';
}

/* ---------------------------------------------------------------- 提交 */

function formErr(msg) {
  const el = $('#form-err');
  if (!msg) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = msg;
}

async function submit() {
  formErr('');
  const prompt = $('#prompt').value.trim();
  if (!prompt) return formErr('提示词不能为空');

  const fd = new FormData();
  fd.append('prompt', prompt);
  fd.append('model', $('#model').value);
  fd.append('size', $('#size').value);
  fd.append('name', $('#name').value);

  if (state.tab === 'img2img') {
    if (!state.files.length) return formErr('图生图至少需要选择 1 张参考图');
    for (const it of state.files) fd.append('files', it.file, it.file.name);
  }

  const btn = $('#submit');
  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span> <span class="btn-text">正在生成中…</span>`;
  try {
    const r = await api('/api/generate', { method: 'POST', body: fd });
    upsertTask(r.task);
  } catch (e) {
    formErr(e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
}

/* ------------------------------------------------------------- 任务轮询 */

async function pollTasks() {
  const active = [...state.tasks.values()]
    .filter((t) => t.status === 'queued' || t.status === 'running');
  if (!active.length) return;

  await Promise.all(active.map(async (t) => {
    try {
      const fresh = await api(`/api/tasks/${t.id}`);
      const wasRunning = t.status;
      upsertTask(fresh);
      if (fresh.status === 'done' && wasRunning !== 'done') loadHistory(true);
    } catch (e) {
      t.status = 'error';
      t.error = e.message;
      upsertTask(t);
    }
  }));
}

function upsertTask(task) {
  state.tasks.set(task.id, task);
  if (!$(`#task-${task.id}`)) {
    const el = buildCard(task);
    $('#tasks').prepend(el);
    renderTaskEmpty();
  }
  updateCard(task);
  updateBusy();
}

function renderTaskEmpty() {
  const box = $('#tasks');
  const has = box.querySelector('.task');
  let e = box.querySelector('.empty');
  if (!has) {
    if (!e) {
      e = document.createElement('div');
      e.className = 'empty';
      e.textContent = '还没有运行中的任务';
      box.appendChild(e);
    }
  } else if (e) {
    e.remove();
  }

  const countBadge = $('#tasks-count');
  if (countBadge) {
    const total = state.tasks.size;
    countBadge.textContent = total;
    countBadge.hidden = total === 0;
  }
}

function updateBusy() {
  const n = [...state.tasks.values()]
    .filter((t) => t.status === 'queued' || t.status === 'running').length;
  const p = $('#busy-pill');
  p.hidden = n === 0;
  const txt = $('#busy-text');
  if (txt) {
    txt.textContent = `${n} 进行中`;
  }
}

function openLightbox(url, name, sizeStr) {
  const lb = $('#lightbox');
  const img = $('#lightbox-img');
  const info = $('#lightbox-info');
  const rawBtn = $('#lightbox-open-raw');

  img.src = url;
  if (info) info.textContent = `${name || '生成图片'} ${sizeStr ? '· ' + sizeStr : ''}`;
  if (rawBtn) {
    rawBtn.href = url;
    rawBtn.download = name || 'image.png';
  }
  lb.classList.add('on');
}

function buildCard(task) {
  const el = document.createElement('div');
  el.className = 'task';
  el.id = `task-${task.id}`;
  el.dataset.status = task.status;

  const head = document.createElement('div');
  head.className = 'head';

  const kind = document.createElement('span');
  kind.className = 'kind';

  const status = document.createElement('span');
  status.dataset.role = 'status';

  const timer = document.createElement('span');
  timer.dataset.role = 'timer';

  const model = document.createElement('span');
  model.dataset.role = 'model';
  model.className = 'ef-field';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'ef-btn ef-btn-quiet ghost-sm';
  cancelBtn.innerHTML = '<span class="btn-text">取消任务</span>';
  cancelBtn.style.marginLeft = 'auto';
  cancelBtn.onclick = async () => {
    cancelBtn.disabled = true;
    try {
      const r = await api(`/api/tasks/${task.id}/cancel`, { method: 'POST' });
      if (r.note) alert(r.note);
      upsertTask(r.task);
    } catch (e) {
      alert('取消失败：' + e.message);
    } finally {
      cancelBtn.disabled = false;
    }
  };

  head.append(kind, status, timer, model, cancelBtn);

  const prompt = document.createElement('div');
  prompt.className = 'prompt';
  prompt.textContent = task.prompt;

  const bar = document.createElement('div');
  bar.className = 'bar';
  bar.dataset.role = 'bar';
  bar.appendChild(document.createElement('i'));

  const err = document.createElement('div');
  err.className = 'ef-notice';
  err.dataset.role = 'err';
  err.hidden = true;

  const out = document.createElement('div');
  out.className = 'out';
  out.dataset.role = 'out';

  el.append(head, prompt, bar, err, out);
  return el;
}

function updateCard(task) {
  const el = $(`#task-${task.id}`);
  if (!el) return;

  el.dataset.status = task.status;

  el.querySelector('.kind').textContent =
    task.kind === 'img2img' ? `图生图 · ${task.ref_names.length} 张参考` : '文生图';

  const status = el.querySelector('[data-role=status]');
  status.className = 'status';
  status.dataset.status = task.status;
  status.textContent = '';
  const cls = { queued: 'st-queued', running: 'st-running', done: 'st-done',
                error: 'st-error', cancelled: 'st-cancelled' }[task.status];
  if (cls) status.classList.add(cls);
  if (task.status === 'running') {
    const sp = document.createElement('span');
    sp.className = 'spin';
    status.append(sp, ' ' + STATUS_TEXT.running);
  } else {
    status.textContent = STATUS_TEXT[task.status] || task.status;
  }

  const timer = el.querySelector('[data-role=timer]');
  timer.className = 'ef-field';
  timer.textContent = task.status === 'queued'
    ? `排队 ${fmtDur(task.queued_for)}`
    : task.status === 'running'
      ? `已耗时 ${fmtDur(task.elapsed)}`
      : fmtDur(task.elapsed);

  const model = el.querySelector('[data-role=model]');
  model.textContent = task.model_used && task.model_used !== task.model
    ? `${task.model} → ${task.model_used}`
    : (task.model_used || task.model || '');

  const busy = task.status === 'queued' || task.status === 'running';
  el.querySelector('[data-role=bar]').hidden = !busy;
  const cancelBtn = el.querySelector('button.ef-btn');
  if (cancelBtn) cancelBtn.hidden = !busy;

  const err = el.querySelector('[data-role=err]');
  err.hidden = !task.error;
  if (task.error) err.textContent = task.error;

  const out = el.querySelector('[data-role=out]');
  if (out.childElementCount !== (task.images || []).length) {
    out.innerHTML = '';
    for (const im of task.images || []) {
      const a = document.createElement('a');
      a.href = im.url;
      a.target = '_blank';
      const img = document.createElement('img');
      img.src = im.url;
      img.alt = im.name;
      img.title = `${im.name} · ${fmtSize(im.size)} — 点开看大图`;
      img.onclick = (e) => {
        e.preventDefault();
        openLightbox(im.url, im.name, fmtSize(im.size));
      };
      a.appendChild(img);
      out.appendChild(a);
    }
  }
}

function tick() {
  for (const t of state.tasks.values()) {
    if (t.status !== 'queued' && t.status !== 'running') continue;
    const el = $(`#task-${t.id}`);
    if (!el) continue;
    const timer = el.querySelector('[data-role=timer]');
    const now = Date.now() / 1000;
    timer.textContent = t.status === 'queued'
      ? `排队 ${fmtDur(now - t.created)}`
      : `已耗时 ${fmtDur(t.started ? now - t.started : 0)}`;
  }
}

/* ---------------------------------------------------------------- 设置 */

const SRC_TEXT = { settings: '设置文件', env: '环境变量', default: '默认设置', none: '未配置' };

function settingsErr(msg) {
  const el = $('#settings-err');
  el.hidden = !msg;
  if (msg) el.textContent = msg;
}

async function openSettings() {
  settingsErr('');
  try {
    state.settings = await api('/api/settings');
  } catch (e) {
    alert('读取配置失败：' + e.message);
    return;
  }
  const s = state.settings;

  const targetRadio = $(`input[name="app-theme"][value="${state.theme}"]`);
  if (targetRadio) targetRadio.checked = true;

  $('#set-output-dir').value = s.output_dir || '';
  $('#set-output-hint').textContent =
    `当前生效：${s.output_dir_effective}（来源：${SRC_TEXT[s.output_dir_source] || s.output_dir_source}）` +
    ` · 更改保存目录不会移动原有旧文件`;
  $('#set-path').textContent = s.settings_path;

  const k = $('#set-api-key');
  k.type = 'password';
  k.value = '';
  k.placeholder = s.has_api_key
    ? `已保存 ${s.api_key_masked}（点击「显示明文」可查看完整值）`
    : '还没配置 API key';
  $('#set-key-toggle').querySelector('.btn-text').textContent = '显示明文';

  $('#set-base-url').value = s.base_url || '';
  $('#set-url-hint').textContent =
    `当前生效：${s.base_url_effective}（来源：${SRC_TEXT[s.base_url_source] || s.base_url_source}）` +
    ` · 默认地址：${s.base_url_default}`;

  state.keyDirty = false;
  state.dirDirty = false;
  state.urlDirty = false;
  $('#settings-modal').classList.add('on');
}

function closeSettings() {
  $('#settings-modal').classList.remove('on');
}

async function toggleKey() {
  const k = $('#set-api-key');
  const btn = $('#set-key-toggle').querySelector('.btn-text');

  if (k.type !== 'password') {
    k.type = 'password';
    btn.textContent = '显示明文';
    return;
  }

  if (!state.keyDirty) {
    try {
      const r = await api('/api/settings/api-key');
      k.value = r.api_key;
    } catch (e) {
      settingsErr('获取 API key 失败：' + e.message);
      return;
    }
  }
  k.type = 'text';
  btn.textContent = '隐藏密文';
}

async function browseFolder() {
  const btn = $('#set-browse');
  btn.disabled = true;
  btn.querySelector('.btn-text').textContent = '选择中…';
  try {
    const r = await api('/api/pick-folder?initial=' + encodeURIComponent($('#set-output-dir').value.trim()),
                        { method: 'POST' });
    if (r.ok) { $('#set-output-dir').value = r.path; state.dirDirty = true; }
  } catch (e) {
    settingsErr(e.message);
  } finally {
    btn.disabled = false;
    btn.querySelector('.btn-text').textContent = '浏览…';
  }
}

async function saveSettings() {
  settingsErr('');

  const body = {};
  if (state.dirDirty) body.output_dir = $('#set-output-dir').value.trim();
  if (state.keyDirty) body.api_key = $('#set-api-key').value;
  if (state.urlDirty) body.base_url = $('#set-base-url').value.trim();

  if (!Object.keys(body).length) { closeSettings(); return; }

  const running = [...state.tasks.values()]
    .filter((t) => t.status === 'queued' || t.status === 'running').length;
  if (running && body.output_dir !== undefined &&
      !confirm(`还有 ${running} 个任务正在运行。更改目录后新图会存入新路径，旧图不动。是否继续？`)) {
    return;
  }

  const btn = $('#settings-save');
  btn.disabled = true;
  try {
    const r = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.cfg.output_root = r.output_dir_effective;
    state.cfg.has_api_key = r.has_api_key;
    if (r.has_api_key) $('#foot-hint').hidden = true;
    if (r.warning) alert(r.warning);
    if (r.changed.output_dir) onOutputRootChanged();
    closeSettings();
  } catch (e) {
    settingsErr(e.message);
  } finally {
    btn.disabled = false;
  }
}

function dropFinishedTasks() {
  for (const [id, t] of state.tasks) {
    if (t.status === 'done' || t.status === 'error' || t.status === 'cancelled') {
      state.tasks.delete(id);
      $(`#task-${id}`)?.remove();
    }
  }
  renderTaskEmpty();
}

function onOutputRootChanged() {
  dropFinishedTasks();
  loadHistory(true);
}

/* ---------------------------------------------------------------- 历史 */

async function loadHistory(reset) {
  if (reset) { state.histOffset = 0; $('#hist').innerHTML = ''; }
  const r = await api(`/api/history?limit=${state.histLimit}&offset=${state.histOffset}`);
  state.histTotal = r.total;
  state.histOffset += r.items.length;

  const statEl = $('#hist-stat');
  if (statEl) {
    statEl.textContent = state.histTotal > 0 ? `共 ${state.histTotal} 张` : '';
  }

  for (const it of r.items) {
    const a = document.createElement('a');
    a.href = it.url;
    a.target = '_blank';
    a.className = 'hist-item';
    a.title = `${it.rel}\n${fmtTime(it.mtime)} · ${fmtSize(it.size)}`;

    const img = document.createElement('img');
    img.src = it.url;
    img.loading = 'lazy';
    img.alt = it.name;

    const overlay = document.createElement('div');
    overlay.className = 'hist-overlay';
    const overlayText = document.createElement('span');
    overlayText.className = 'hist-overlay-text';
    overlayText.textContent = it.name;
    overlay.appendChild(overlayText);

    a.onclick = (e) => {
      e.preventDefault();
      openLightbox(it.url, it.name, `${fmtTime(it.mtime)} · ${fmtSize(it.size)}`);
    };

    a.append(img, overlay);
    $('#hist').appendChild(a);
  }

  $('#hist-empty').hidden = state.histTotal > 0;
  $('#hist-more').hidden = state.histOffset >= state.histTotal;
}

boot();
