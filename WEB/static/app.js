/* 生图网页版前端。原生 JS，无构建步骤。 */

const $ = (s) => document.querySelector(s);

const state = {
  cfg: null,
  tab: 'txt2img',
  // 两个标签页各存一份输入（提示词/模型/尺寸/文件名），互相不影响；模型和尺寸的初值在 boot 里按配置填
  inputs: {
    txt2img: { prompt: '', model: '', size: '', name: '' },
    img2img: { prompt: '', model: '', size: '', name: '' },
  },
  files: [],          // {id, file, url}
  tasks: new Map(),   // id -> task 对象
  settings: null,     // GET /api/settings 的结果
  keyDirty: false,    // 用户动过 key 输入框没有；没动过就不提交 api_key
  dirDirty: false,    // 同上，针对目录框
  histOffset: 0,
  histLimit: 30,
  histTotal: 0,
};

/* ------------------------------------------------------------------ 工具 */

const fmtDur = (s) => {
  if (s == null) return '';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
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
  done: '完成',
  error: '失败',
  cancelled: '已作废',
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
  try {
    state.cfg = await api('/api/config');
  } catch (e) {
    alert('拿不到配置，后端可能没起来：' + e.message);
    return;
  }

  const c = state.cfg;
  if (c.mock) $('#mock-pill').hidden = false;

  fillSelect($('#model'), c.models, c.default_model);
  fillSelect($('#size'), c.sizes, c.default_size);

  // 两个标签页的模型/尺寸初值都取配置里的默认项；读回来是为了拿到真正被选中的那项
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

  $('#lightbox').onclick = () => $('#lightbox').classList.remove('on');

  // ---- 设置面板 ----
  $('#btn-settings').onclick = openSettings;
  $('#settings-close').onclick = closeSettings;
  $('#settings-modal').onclick = (e) => { if (e.target.id === 'settings-modal') closeSettings(); };
  $('#settings-save').onclick = saveSettings;
  $('#set-browse').onclick = browseFolder;
  $('#set-key-toggle').onclick = toggleKey;
  $('#set-reset-dir').onclick = () => { $('#set-output-dir').value = ''; state.dirDirty = true; };
  $('#set-output-dir').oninput = () => { state.dirDirty = true; };
  $('#set-api-key').oninput = () => { state.keyDirty = true; };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeSettings();
    $('#lightbox').classList.remove('on');
  });

  // 这台机器弹不出原生对话框时，把「浏览…」藏掉，只留手输路径
  if (c.can_pick_folder === false) $('#set-browse').hidden = true;

  // 没配 key 首页就给个提示，否则第一次用的人点了生成才知道要配
  if (!c.has_api_key) {
    const h = $('#foot-hint');
    h.hidden = false;
    h.textContent = '还没配置 API key，点「设置」填一个';
  }

  setTab('txt2img');
  await loadHistory(true);

  // 服务可能跑着还在进行的任务，恢复一下
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
      formErr(`最多 ${max} 张参考图`);
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
    // 顺序有意义：模型按顺序理解「图 1 / 图 2」，所以要有上移/下移
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
  // 先把当前标签页的输入存回去，再换上目标标签页自己的，两边互不影响
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
    if (!state.files.length) return formErr('图生图至少要有一张参考图');
    // 同名 key 重复 append，FastAPI 才会映射成 list[UploadFile]
    for (const it of state.files) fd.append('files', it.file, it.file.name);
  }

  const btn = $('#submit');
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const r = await api('/api/generate', { method: 'POST', body: fd });
    upsertTask(r.task);
  } catch (e) {
    formErr(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成';
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
      // 404 一般是服务重启过，任务表在内存里所以丢了
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
      e.textContent = '还没有任务';
      box.appendChild(e);
    }
  } else if (e) {
    e.remove();
  }
}

function updateBusy() {
  const n = [...state.tasks.values()]
    .filter((t) => t.status === 'queued' || t.status === 'running').length;
  const p = $('#busy-pill');
  p.hidden = n === 0;
  if (n) p.textContent = `${n} 个进行中`;
}

function buildCard(task) {
  const el = document.createElement('div');
  el.className = 'task';
  el.id = `task-${task.id}`;

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
  model.style.cssText = 'color:var(--fg-dim);font-size:11.5px';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'ghost';
  cancelBtn.textContent = '取消';
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
  prompt.textContent = task.prompt;      // 用 textContent，不拼 HTML

  const bar = document.createElement('div');
  bar.className = 'bar';
  bar.dataset.role = 'bar';
  bar.appendChild(document.createElement('i'));

  const err = document.createElement('div');
  err.className = 'err';
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
  timer.style.cssText = 'color:var(--fg-dim);font-size:11.5px';
  timer.textContent = task.status === 'queued'
    ? `已排队 ${fmtDur(task.queued_for)}`
    : task.status === 'running'
      ? `已生成 ${fmtDur(task.elapsed)}`
      : fmtDur(task.elapsed);

  const model = el.querySelector('[data-role=model]');
  model.textContent = task.model_used && task.model_used !== task.model
    ? `${task.model} → ${task.model_used}`
    : (task.model_used || task.model || '');

  const busy = task.status === 'queued' || task.status === 'running';
  el.querySelector('[data-role=bar]').hidden = !busy;
  el.querySelector('button.ghost').hidden = !busy;

  const err = el.querySelector('[data-role=err]');
  err.hidden = !task.error;
  if (task.error) err.textContent = task.error;

  const out = el.querySelector('[data-role=out]');
  // 只在数量变化时重建，避免每次轮询都重新加载图片造成闪烁
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
        $('#lightbox-img').src = im.url;
        $('#lightbox').classList.add('on');
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
      ? `已排队 ${fmtDur(now - t.created)}`
      : `已生成 ${fmtDur(t.started ? now - t.started : 0)}`;
  }
}

/* ---------------------------------------------------------------- 设置 */

const SRC_TEXT = { settings: '设置', env: '环境变量', default: '默认', none: '未设置' };

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
    alert('读不到设置：' + e.message);
    return;
  }
  const s = state.settings;

  $('#set-output-dir').value = s.output_dir || '';      // 空 = 用默认目录
  $('#set-output-hint').textContent =
    `当前生效：${s.output_dir_effective}（来源：${SRC_TEXT[s.output_dir_source] || s.output_dir_source}）` +
    `· 换目录不会动旧目录里的文件`;
  $('#set-path').textContent = s.settings_path;

  // key 一律回到「密码态 + 占位符」，【绝不把完整值塞进 DOM】。
  // 想看完整值要显式点「显示」，那时才单独去后端取一次。
  const k = $('#set-api-key');
  k.type = 'password';
  k.value = '';
  k.placeholder = s.has_api_key
    ? `已保存 ${s.api_key_masked}（点「显示」看完整值）`
    : '还没设置';
  $('#set-key-toggle').textContent = '显示';

  state.keyDirty = false;
  state.dirDirty = false;
  $('#settings-modal').classList.add('on');
}

function closeSettings() {
  $('#settings-modal').classList.remove('on');
}

async function toggleKey() {
  const k = $('#set-api-key');
  const btn = $('#set-key-toggle');

  if (k.type !== 'password') {          // 已经在明文态 → 藏回去
    k.type = 'password';
    btn.textContent = '显示';
    return;
  }

  // 用户已经自己敲过内容了就别去覆盖他，只把 type 切过来
  if (!state.keyDirty) {
    try {
      const r = await api('/api/settings/api-key');
      k.value = r.api_key;
    } catch (e) {
      settingsErr('取不到 key：' + e.message);
      return;
    }
  }
  k.type = 'text';
  btn.textContent = '隐藏';
}

async function browseFolder() {
  const btn = $('#set-browse');
  btn.disabled = true;
  btn.textContent = '选择中…';
  try {
    const r = await api('/api/pick-folder?initial=' + encodeURIComponent($('#set-output-dir').value.trim()),
                        { method: 'POST' });
    if (r.ok) { $('#set-output-dir').value = r.path; state.dirDirty = true; }
    // r.cancelled 就是用户点了取消，什么都不做，保留他已经输入的值
  } catch (e) {
    settingsErr(e.message);             // 501 时后端给的就是「请手动输入路径」
  } finally {
    btn.disabled = false;
    btn.textContent = '浏览…';
  }
}

async function saveSettings() {
  settingsErr('');

  const body = {};
  if (state.dirDirty) body.output_dir = $('#set-output-dir').value.trim();   // "" = 恢复默认
  if (state.keyDirty) body.api_key = $('#set-api-key').value;                // "" = 清空

  if (!Object.keys(body).length) { closeSettings(); return; }   // 什么都没改

  const running = [...state.tasks.values()]
    .filter((t) => t.status === 'queued' || t.status === 'running').length;
  if (running && body.output_dir !== undefined &&
      !confirm(`还有 ${running} 个任务在跑。换目录后它们会存到新目录，旧目录里的文件不动。继续？`)) {
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
    // 后端保证这时候当前生效的目录一个字都没动
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

// 换了输出目录之后：
// 已结束的任务卡片上那些图指向旧目录，URL 是 "/images/日期/名字"，换根之后必然 404，
// 所以连同卡片一起清掉；【正在跑的任务要留着】—— 它们此刻还没有图，等 done 的时候
// save_image 读的是那一刻的 OUTPUT_ROOT（已经是新目录了），URL 天然指向新目录。
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

  for (const it of r.items) {
    const a = document.createElement('a');
    a.href = it.url;
    a.target = '_blank';
    a.title = `${it.rel}\n${fmtTime(it.mtime)} · ${fmtSize(it.size)}`;
    const img = document.createElement('img');
    img.src = it.url;
    img.loading = 'lazy';      // 历史可能很多，别一次全拉
    img.alt = it.name;
    img.onclick = (e) => {
      e.preventDefault();
      $('#lightbox-img').src = it.url;
      $('#lightbox').classList.add('on');
    };
    a.appendChild(img);
    $('#hist').appendChild(a);
  }

  $('#hist-empty').hidden = state.histTotal > 0;
  $('#hist-more').hidden = state.histOffset >= state.histTotal;
}

boot();
