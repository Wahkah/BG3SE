/* BG3 Save Editor - UI logic. Talks to the Python side via pywebview.api. */
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const state = {
  saves: [], overview: null, dirty: false, feats: [],
  comp: { file: '', name: '', start: 0, limit: 50, selected: null, total: 0 },
  tree: { file: '', path: '' },
};

/* ---------------------------------------------------------------- plumbing */
/* Two transports drive the same UI: the pywebview bridge injected by the
   desktop window, and a plain HTTP bridge when served by `bgse web`. */
function installHttpBridge() {
  window.pywebview = {
    api: new Proxy({}, {
      get: (_, method) => (...args) => fetch('/api/' + String(method), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args),
      }).then((r) => r.json()),
    }),
  };
}

function noBridgeNotice() {
  document.querySelectorAll('.screen, #topbar').forEach((n) => n.classList.add('hidden'));
  const box = el('div', 'no-bridge');
  box.appendChild(el('h1', null, 'No backend connected'));
  box.appendChild(el('p', null,
    'This page is the editor’s interface only — it needs the bgse Python backend '
    + 'to do anything. Opening index.html directly in a browser will not work.'));
  box.appendChild(el('p', null, 'Start it one of these ways:'));
  const pre = el('pre', null, 'bgse gui     # native desktop window\nbgse web     # serve this UI at http://127.0.0.1');
  box.appendChild(pre);
  document.body.appendChild(box);
}

function nativeReady() {
  return !!(window.pywebview && window.pywebview.api);
}

async function probeHttpBridge() {
  try {
    const r = await fetch('/api/field_kinds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '[]',
    });
    if (!r.ok) return false;
    const j = await r.json();
    return !!(j && j.ok);
  } catch (e) {
    return false;                       // not the bgse server
  }
}

/* The desktop window also serves its files over http://, so the URL scheme
   cannot tell the two transports apart. Wait for pywebview's injected API and
   probe the HTTP bridge alongside it, always preferring the native one. */
async function apiReady() {
  if (nativeReady()) return;
  for (let attempt = 0; attempt < 14; attempt++) {
    if (nativeReady()) return;
    if (attempt === 2 || attempt === 6) {
      if (await probeHttpBridge()) {
        if (nativeReady()) return;
        installHttpBridge();
        return;
      }
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  if (nativeReady()) return;
  // Nothing behind the page: say so rather than leaving it blank.
  noBridgeNotice();
  throw new Error('no backend bridge');
}

async function call(method, ...args) {
  const res = await window.pywebview.api[method](...args);
  if (res && res.ok === false) throw new Error(res.error || 'unknown error');
  return res ? res.data : null;
}

let toastTimer;
function toast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), kind === 'err' ? 9000 : 3800);
}

async function busy(text, fn) {
  $('busyText').textContent = text;
  $('busy').classList.remove('hidden');
  try { return await fn(); }
  finally { $('busy').classList.add('hidden'); }
}

function markDirty(on) {
  state.dirty = on;
  $('dirtyDot').classList.toggle('hidden', !on);
  $('btnSave').disabled = !on;
}

/* ------------------------------------------------------------ save picker */
async function loadSaves() {
  const saves = await busy('Scanning for savegames…', () => call('list_saves'));
  state.saves = saves;
  renderSaves();
}

function renderSaves() {
  const q = $('saveFilter').value.trim().toLowerCase();
  const grid = $('saveGrid');
  grid.textContent = '';
  const list = state.saves.filter((s) => !q || s.name.toLowerCase().includes(q));

  $('pickerEmpty').classList.toggle('hidden', list.length > 0);
  if (!list.length) {
    $('pickerEmpty').textContent = state.saves.length
      ? 'No savegame matches that filter.'
      : 'No savegames found. Use "Where is it looking?" to see the paths searched.';
    return;
  }

  for (const s of list) {
    const card = el('div', 'save-card');
    const img = el('img', 'shot');
    img.alt = '';
    card.appendChild(img);
    const body = el('div', 'body');
    body.appendChild(el('div', 'title', s.name));
    body.appendChild(el('div', 'sub',
      `${s.modified_display}  ·  ${(s.size / 1e6).toFixed(1)} MB`));
    if (s.profile) body.appendChild(el('div', 'sub', `${s.profile} / ${s.mode}`));
    card.appendChild(body);
    card.onclick = () => openSave(s.path);
    grid.appendChild(card);

    if (s.screenshot) {
      call('screenshot', s.screenshot)
        .then((r) => { if (r && r.data) img.src = r.data; })
        .catch(() => {});
    }
  }
}

/* ----------------------------------------------------------------- editor */
async function openSave(path) {
  try {
    const ov = await busy('Opening savegame…', () => call('open_save', path));
    state.overview = ov;
    try { state.feats = (await call('available_feats')) || []; }
    catch (e) { state.feats = []; }
    $('picker').classList.add('hidden');
    $('editor').classList.remove('hidden');
    $('openInfo').classList.remove('hidden');
    $('btnBack').classList.remove('hidden');
    $('btnSave').classList.remove('hidden');
    $('openName').textContent = ov.path;
    markDirty(false);
    renderOverview(ov);
    await initComponents();
    await initTree();
    showTab('party');
  } catch (e) { toast(String(e.message || e), 'err'); }
}

function renderOverview(ov) {
  $('fSaveName').value = ov.save_info.save_name || ov.name;
  const dl = $('saveMeta');
  dl.textContent = '';
  const rows = [
    ['Difficulty', ov.save_info.difficulty],
    ['Game version', ov.save_info.game_version],
    ['Current level', ov.save_info.current_level],
    ['Platform', ov.save_info.platform],
    ['File', ov.path],
  ];
  for (const [k, v] of rows) {
    if (!v) continue;
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, String(v)));
  }

  const list = $('partyList');
  list.textContent = '';
  for (const m of ov.party) {
    const card = el('div', 'member');
    card.appendChild(el('h3', null, m.name || `Character ${m.index + 1}`));
    card.appendChild(el('div', 'cls', `${m.class_label} · level ${m.level} · ${m.race}`));

    const meta = el('dl', 'meta');
    meta.appendChild(el('dt', null, 'XP (total)'));
    meta.appendChild(el('dd', null, String(m.xp_total)));
    meta.appendChild(el('dt', null, 'XP (this level)'));
    meta.appendChild(el('dd', null, String(m.xp_current_level)));
    if (m.subregion) {
      meta.appendChild(el('dt', null, 'Region'));
      meta.appendChild(el('dd', null, m.subregion));
    }
    card.appendChild(meta);

    if (m.abilities && m.abilities.length) {
      const box = el('div', 'classes');
      box.appendChild(el('div', 'sublabel', 'Ability scores'));
      const grid = el('div', 'abilities');
      for (const a of m.abilities) {
        const cell = el('div', 'ability');
        cell.appendChild(el('span', 'abbr', a.short));
        const input = el('input');
        input.type = 'number';
        input.min = 1;
        input.max = 30;
        input.value = a.value;
        input.title = a.name;
        input.onchange = async () => {
          try {
            await call('set_ability', m.class_row, a.index, Number(input.value));
            markDirty(true);
            toast(`${a.name} set to ${input.value}. Write the save to apply.`, 'ok');
          } catch (e) { toast(String(e.message || e), 'err'); }
        };
        cell.appendChild(input);
        grid.appendChild(cell);
      }
      box.appendChild(grid);
      card.appendChild(box);
    }

    if (m.class_levels && m.class_levels.length) {
      const box = el('div', 'classes');
      box.appendChild(el('div', 'sublabel', 'Class levels (from entity data)'));
      m.class_levels.forEach((c, i) => {
        const row = el('div', 'class-row');
        row.appendChild(el('span', 'cname', c.label));
        const input = el('input');
        input.type = 'number';
        input.min = 0;
        input.max = 20;
        input.value = c.level;
        const btn = el('button', 'ghost small', 'Set');
        btn.onclick = async () => {
          try {
            await call('set_class_level', m.class_row, i, Number(input.value));
            markDirty(true);
            toast(`${c.label} set to level ${input.value}. Write the save to apply.`, 'ok');
          } catch (e) { toast(String(e.message || e), 'err'); }
        };
        row.appendChild(input);
        row.appendChild(btn);
        box.appendChild(row);
      });
      card.appendChild(box);
    }

    if (m.feats && m.feats.length) {
      const box = el('div', 'classes');
      box.appendChild(el('div', 'sublabel', 'Feats'));
      for (const f of m.feats) {
        const row = el('div', 'class-row');
        row.appendChild(el('span', 'cname', `Level ${f.level}`));
        const sel = el('select');
        sel.appendChild(new Option('(none)', ''));
        for (const opt of state.feats) {
          const o = new Option(opt.name, opt.uuid);
          if (opt.uuid === f.feat_uuid) o.selected = true;
          sel.appendChild(o);
        }
        if (f.feat_uuid && !state.feats.some((x) => x.uuid === f.feat_uuid)) {
          const o = new Option(f.feat || f.feat_uuid, f.feat_uuid);
          o.selected = true;
          sel.appendChild(o);
        }
        sel.onchange = async () => {
          try {
            const r = await call('set_feat', f.data_row, sel.value);
            markDirty(true);
            toast(`Level ${f.level} feat set to ${r.feat || 'none'}. `
              + 'Write the save to apply.', 'ok');
          } catch (e) { toast(String(e.message || e), 'err'); }
        };
        row.appendChild(sel);
        box.appendChild(row);
      }
      card.appendChild(box);
    }

    if (m.editable_xp) {
      const row = el('div', 'xp');
      const input = el('input');
      input.type = 'number';
      input.value = m.xp_total;
      const btn = el('button', 'ghost small', 'Set XP');
      btn.onclick = async () => {
        try {
          await call('set_experience', m.xp_slot, Number(input.value));
          markDirty(true);
          toast(`XP set to ${input.value} for ${m.name}. Write the save to apply.`, 'ok');
        } catch (e) { toast(String(e.message || e), 'err'); }
      };
      row.appendChild(input);
      row.appendChild(btn);
      card.appendChild(row);
    } else {
      card.appendChild(el('div', 'warn',
        'No experience row in the ECS could be matched to this character.'));
    }
    list.appendChild(card);
  }

  $('partyNote').textContent = ov.party.length
    ? 'Names, classes and levels come from the save summary. XP is written directly into the '
      + 'game’s entity data. Level and class are derived by the game from progression data '
      + 'that this build does not yet decode.'
    : 'This savegame lists no active party.';

  const tb = $('fileTable').querySelector('tbody');
  tb.textContent = '';
  for (const f of ov.files) {
    const tr = el('tr');
    tr.appendChild(el('td', 'wrap', f.name));
    tr.appendChild(el('td', null, f.kind));
    tr.appendChild(el('td', null, f.size.toLocaleString()));
    tb.appendChild(tr);
  }
}

/* ------------------------------------------------------------- components */
async function initComponents() {
  const files = await call('ecs_files');
  const sel = $('ecsFile');
  sel.textContent = '';
  for (const f of files) sel.appendChild(new Option(f, f));
  state.comp.file = files[0] || '';
  if (state.comp.file) await loadComponentList();
}

async function loadComponentList() {
  const types = await busy('Reading entity data…', () => call(
    'component_types', state.comp.file, $('compFilter').value.trim(),
    $('chkPopulated').checked));
  const ul = $('compList');
  ul.textContent = '';
  for (const t of types) {
    const li = el('li');
    li.appendChild(el('span', null, t.short_name));
    li.appendChild(el('span', 'count', `${t.count}×${t.element_size}`));
    li.title = t.name;
    li.onclick = () => {
      [...ul.children].forEach((c) => c.classList.remove('sel'));
      li.classList.add('sel');
      state.comp.name = t.name;
      state.comp.start = 0;
      loadComponentRows();
    };
    ul.appendChild(li);
  }
  if (!types.length) ul.appendChild(el('li', 'dim', 'nothing matches'));
}

async function loadComponentRows() {
  const c = state.comp;
  const res = await call('component_rows', c.file, c.name, c.start, c.limit);
  c.total = res.total;
  c.selected = null;
  $('compEmpty').classList.add('hidden');
  $('compView').classList.remove('hidden');
  $('compName').textContent = res.type.short_name;
  $('compMeta').textContent =
    `${res.type.name}  ·  ${res.type.count} elements × ${res.type.element_size} bytes`
    + `  ·  arena offset ${res.type.data_offset}`;

  const tb = $('compTable').querySelector('tbody');
  tb.textContent = '';
  for (const r of res.rows) {
    const tr = el('tr');
    tr.appendChild(el('td', null, String(r.index)));
    tr.appendChild(el('td', null, r.hex));
    tr.appendChild(el('td', null, r.int32 == null ? '' : String(r.int32)));
    tr.appendChild(el('td', null, r.uint32 == null ? '' : String(r.uint32)));
    tr.appendChild(el('td', null, r.int64 == null ? '' : String(r.int64)));
    tr.appendChild(el('td', null, r.float == null ? '' : String(r.float)));
    tr.onclick = () => {
      [...tb.children].forEach((x) => x.classList.remove('sel'));
      tr.classList.add('sel');
      c.selected = r.index;
    };
    tb.appendChild(tr);
  }
  const shown = Math.min(c.start + c.limit, c.total);
  $('pageInfo').textContent = `${c.start + 1}–${shown} of ${c.total}`;
  $('btnPrev').disabled = c.start === 0;
  $('btnNext').disabled = shown >= c.total;
}

/* -------------------------------------------------------------- raw tree */
async function initTree() {
  const sel = $('treeFile');
  sel.textContent = '';
  for (const f of state.overview.lsf_files) sel.appendChild(new Option(f, f));
  state.tree.file = state.overview.lsf_files[0] || '';
  await loadTreeRoot();
}

async function loadTreeRoot() {
  const roots = await busy('Reading resource tree…',
    () => call('tree_children', state.tree.file, ''));
  const ul = $('treeRoot');
  ul.textContent = '';
  for (const n of roots) ul.appendChild(treeItem(n));
}

function treeItem(node) {
  const li = el('li');
  const row = el('div', 'node');
  const tw = el('span', 'twisty', node.children ? '▶' : '');
  row.appendChild(tw);
  row.appendChild(el('span', null, node.name));
  if (node.children) row.appendChild(el('span', 'badge', String(node.children)));
  li.appendChild(row);

  let kids = null;
  row.onclick = async (ev) => {
    ev.stopPropagation();
    document.querySelectorAll('.tree .node.sel').forEach((n) => n.classList.remove('sel'));
    row.classList.add('sel');
    showNode(node.path);
    if (!node.children) return;
    if (kids) {
      kids.classList.toggle('hidden');
      tw.textContent = kids.classList.contains('hidden') ? '▶' : '▼';
      return;
    }
    try {
      const children = await call('tree_children', state.tree.file, node.path);
      kids = el('ul');
      for (const c of children) kids.appendChild(treeItem(c));
      li.appendChild(kids);
      tw.textContent = '▼';
    } catch (e) { toast(String(e.message || e), 'err'); }
  };
  return li;
}

async function showNode(path) {
  try {
    const d = await call('node_detail', state.tree.file, path);
    $('nodeEmpty').classList.add('hidden');
    $('nodeView').classList.remove('hidden');
    $('nodeName').textContent = d.name;
    $('nodePath').textContent = d.path;
    const tb = $('attrTable').querySelector('tbody');
    tb.textContent = '';
    for (const a of d.attributes) {
      const tr = el('tr');
      tr.appendChild(el('td', null, a.name));
      tr.appendChild(el('td', null, a.type));
      const valCell = el('td', 'wrap');
      if (a.editable) {
        const input = el('input');
        input.value = a.value;
        input.onkeydown = (e) => { if (e.key === 'Enter') applyAttr(path, a.name, input); };
        valCell.appendChild(input);
        tr.appendChild(valCell);
        const btnCell = el('td');
        const b = el('button', 'ghost small', 'Set');
        b.onclick = () => applyAttr(path, a.name, input);
        btnCell.appendChild(b);
        tr.appendChild(btnCell);
      } else {
        valCell.textContent = a.value;
        tr.appendChild(valCell);
        tr.appendChild(el('td', 'dim', 'read-only'));
      }
      tb.appendChild(tr);
    }
  } catch (e) { toast(String(e.message || e), 'err'); }
}

async function applyAttr(path, name, input) {
  try {
    await call('set_attribute', state.tree.file, path, name, input.value);
    markDirty(true);
    toast(`${name} updated. Write the save to apply.`, 'ok');
  } catch (e) { toast(String(e.message || e), 'err'); }
}

/* ------------------------------------------------------------------ tabs */
function showTab(name) {
  document.querySelectorAll('.tab').forEach(
    (t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.pane').forEach(
    (p) => p.classList.toggle('active', p.id === 'pane-' + name));
}

/* ------------------------------------------------------------------ boot */
apiReady().then(async () => {
  for (const k of await call('field_kinds')) $('fKind').appendChild(new Option(k, k));
  $('fKind').value = 'int32';
  await loadSaves();

  $('saveFilter').oninput = renderSaves;
  $('btnRescan').onclick = loadSaves;
  $('btnEnv').onclick = async () => {
    const box = $('envBox');
    if (!box.classList.contains('hidden')) return box.classList.add('hidden');
    const env = await call('environment');
    box.textContent = Object.entries(env)
      .map(([k, v]) => `${k}: ${Array.isArray(v) ? '\n  ' + (v.join('\n  ') || '(none)') : v}`)
      .join('\n');
    box.classList.remove('hidden');
  };

  document.querySelectorAll('.tab').forEach(
    (t) => { t.onclick = () => showTab(t.dataset.tab); });

  $('btnBack').onclick = async () => {
    if (state.dirty && !confirm('Discard unsaved changes?')) return;
    await call('close_save');
    $('editor').classList.add('hidden');
    $('picker').classList.remove('hidden');
    $('openInfo').classList.add('hidden');
    $('btnBack').classList.add('hidden');
    $('btnSave').classList.add('hidden');
    markDirty(false);
  };

  $('btnSave').onclick = async () => {
    try {
      const r = await busy('Writing savegame…',
        () => call('save_changes', $('chkBackup').checked, ''));
      markDirty(false);
      toast(`Wrote ${r.bytes.toLocaleString()} bytes.`
        + (r.backup ? `\nBackup: ${r.backup}` : ''), 'ok');
    } catch (e) { toast(String(e.message || e), 'err'); }
  };

  $('btnSaveName').onclick = async () => {
    try {
      await call('set_save_name', $('fSaveName').value);
      markDirty(true);
      toast('Save name updated.', 'ok');
    } catch (e) { toast(String(e.message || e), 'err'); }
  };

  $('ecsFile').onchange = () => { state.comp.file = $('ecsFile').value; loadComponentList(); };
  $('compFilter').oninput = () => loadComponentList();
  $('chkPopulated').onchange = () => loadComponentList();
  $('btnPrev').onclick = () => {
    state.comp.start = Math.max(0, state.comp.start - state.comp.limit);
    loadComponentRows();
  };
  $('btnNext').onclick = () => {
    state.comp.start += state.comp.limit;
    loadComponentRows();
  };
  $('btnPatch').onclick = async () => {
    const c = state.comp;
    if (c.selected == null) return toast('Select a row in the table first.', 'err');
    try {
      await call('set_component_field', c.file, c.name, c.selected,
        Number($('fOffset').value), $('fKind').value, $('fValue').value);
      markDirty(true);
      await loadComponentRows();
      toast('Element patched. Write the save to apply.', 'ok');
    } catch (e) { toast(String(e.message || e), 'err'); }
  };

  $('treeFile').onchange = () => {
    state.tree.file = $('treeFile').value;
    $('nodeView').classList.add('hidden');
    $('nodeEmpty').classList.remove('hidden');
    loadTreeRoot();
  };
  $('treeSearch').onkeydown = async (e) => {
    if (e.key !== 'Enter') return;
    const q = e.target.value.trim();
    if (!q) return;
    try {
      const hits = await busy('Searching…', () => call('search', state.tree.file, q, 200));
      const ul = $('treeRoot');
      ul.textContent = '';
      if (!hits.length) { ul.appendChild(el('li', 'dim', 'no matches')); return; }
      for (const h of hits) {
        const li = el('li');
        const row = el('div', 'node');
        row.appendChild(el('span', 'twisty', ''));
        row.appendChild(el('span', null, `${h.name} — ${h.match}`));
        row.onclick = () => showNode(h.path);
        li.appendChild(row);
        ul.appendChild(li);
      }
      toast(`${hits.length} match(es). Clear the box and press Enter to restore the tree.`);
    } catch (err) { toast(String(err.message || err), 'err'); }
    if (!e.target.value.trim()) loadTreeRoot();
  };
}).catch(() => { /* noBridgeNotice() already explained the problem */ });
