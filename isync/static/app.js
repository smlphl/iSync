"use strict";
/* iSync arayüzü — bağımlılıksız, sanal (virtualized) ızgara ile on binlerce fotoğrafı akıcı gösterir. */

const $ = (id) => document.getElementById(id);
// Ay adları seçili dile göre (Intl): Türkçe "Kasım", İngilizce "November", Çince "11月"
const MONTHS = Array.from({ length: 12 }, (_, i) => new Intl.DateTimeFormat(window.LOCALE || "tr-TR", { month: "long" }).format(new Date(2000, i, 1)));
const GAP = 6, HEAD_H = 46;

const S = {
  device: null,
  assets: [],          // sunucudan gelen ham dosyalar
  groups: [],          // ekranda gösterilen öğeler (Live Photo = foto + mov + aae tek öğe)
  byId: new Map(),     // grup birincil id -> grup
  albums: [],
  dbLoaded: false,
  view: { type: "all", value: null, title: "Tümü" },
  list: [],            // filtrelenmiş + sıralanmış gruplar
  rows: [],
  selected: new Set(),
  lastClicked: null,
  tile: 150,
  job: null,
  newIds: null,
  openYears: new Set(),
};

// ---------------- yardımcılar ----------------
async function api(path, body, method) {
  const opt = { method: method || (body !== undefined ? "POST" : "GET"), headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* boş */ }
  if (!r.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${r.status}`);
  return data;
}
function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.round(s);
  if (s < 60) return t("{} sn", [s]);
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? t("{} sa {} dk", [h, m % 60]) : t("{} dk {} sn", [m, s % 60]);
}
const n = (x) => x.toLocaleString(window.LOCALE || "tr-TR");
function ym(t) { const d = new Date(t * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; }
function ymLabel(k) { const [y, m] = k.split("-"); return new Intl.DateTimeFormat(window.LOCALE || "tr-TR", { year: "numeric", month: "long" }).format(new Date(+y, +m - 1, 1)); }
let toastTimer;
function toast(msg, err) {
  const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------------- cihaz ----------------
async function connect() {
  const dev = $("device");
  try {
    const list = await api("/api/devices");
    const sel = $("devSelect");
    if (list.devices.length > 1) {
      sel.hidden = false;
      sel.innerHTML = list.devices.map((d) => `<option value="${d.udid}">${d.name || d.udid.slice(0, 12)}</option>`).join("");
      if (S.device) sel.value = S.device.udid;
    } else sel.hidden = true;
    if (!list.devices.length) throw new Error("Bağlı iPhone bulunamadı. Kabloyu takıp telefonun kilidini açın.");
    const info = await api("/api/connect", { udid: S.device ? S.device.udid : null });
    S.device = info;
    dev.className = "device online";
    $("devName").textContent = info.name || "iPhone";
    $("devSub").textContent = info.busy ? `${info.model || ""} · ⏳ ${info.busy}` : [info.model, info.ios && `iOS ${info.ios}`, info.battery != null && `🔋 ${info.battery}%`, info.connection === "USB" ? "USB" : info.connection].filter(Boolean).join(" · ");
    const st = info.storage;
    if (st && st.total) {
      const used = st.total - st.free, pct = (used / st.total) * 100;
      $("storage").hidden = false;
      $("storageBar").style.width = pct + "%";
      $("storageBar").className = pct > 92 ? "full" : "";
      $("storageText").textContent = `${fmtBytes(st.free)} boş / ${fmtBytes(st.total)}`;
    }
    return true;
  } catch (e) {
    dev.className = "device offline";
    $("devName").textContent = "iPhone bağlı değil";
    $("devSub").textContent = e.message;
    $("storage").hidden = true;
    return false;
  }
}

async function scan() {
  showEmpty("<b>Telefon taranıyor…</b>Tüm DCIM klasörleri doğrudan okunuyor.");
  $("btnRescan").disabled = true;
  try {
    const lib = await api("/api/scan", {});
    loadLibrary(lib);
    toast(`${n(lib.assets.length)} dosya ${lib.scan_seconds} saniyede tarandı`);
  } catch (e) {
    showEmpty(`<b>Tarama başarısız</b>${e.message}`);
    toast(e.message, true);
  } finally { $("btnRescan").disabled = false; }
}

function loadLibrary(lib) {
  S.assets = lib.assets;
  S.albums = lib.albums || [];
  S.dbLoaded = lib.db_loaded;
  buildGroups();
  // Mevcut seçimleri koru (id'ler yeniden taramada değişebilir; bu yüzden temizle)
  S.selected.clear();
  renderNav();
  applyView();
  refreshNewCount();
  $("btnDb").textContent = S.dbLoaded ? "yenile" : "yükle";
}

const RANK = { p: 0, v: 1, o: 2, s: 3 };
function buildGroups() {
  const map = new Map();
  for (const a of S.assets) {
    const stem = a.name.replace(/\.[^.]+$/, "");
    const key = a.dir + "/" + stem;
    let g = map.get(key);
    if (!g) { g = { ids: [], members: [] }; map.set(key, g); }
    g.members.push(a); g.ids.push(a.id);
  }
  const groups = [];
  for (const g of map.values()) {
    g.members.sort((x, y) => RANK[x.k] - RANK[y.k]);
    const p = g.members[0];
    if (p.k === "s") continue; // yetim .AAE dosyaları gösterilmez
    g.id = p.id; g.p = p; g.t = p.t; g.ym = ym(p.t);
    g.size = g.members.reduce((s, m) => s + m.size, 0);
    g.live = p.k === "p" && g.members.some((m) => m.k === "v");
    g.liveVideo = g.live ? g.members.find((m) => m.k === "v") : null;
    groups.push(g);
  }
  S.groups = groups;
  S.byId = new Map(groups.map((g) => [g.id, g]));
}

// ---------------- kenar menü ----------------
function navItem(ic, label, count, view, extra = "") {
  const active = S.view.type === view.type && S.view.value === view.value ? " active" : "";
  return `<a class="${extra}${active}" data-view='${JSON.stringify(view).replace(/'/g, "&#39;")}'>
    <span class="ic">${ic}</span><span class="lbl">${label}</span><span class="cnt">${count != null ? n(count) : ""}</span></a>`;
}
function renderNav() {
  const G = S.groups.filter((g) => !g.p.x);
  const c = (f) => G.filter(f).length;
  let h = navItem("▦", "Tümü", G.length, { type: "all", title: "Tümü" });
  h += navItem("◫", "Fotoğraflar", c((g) => g.p.k === "p"), { type: "photos", title: "Fotoğraflar" });
  h += navItem("▶", "Videolar", c((g) => g.p.k === "v"), { type: "videos", title: "Videolar" });
  h += navItem("◉", "Live Photos", c((g) => g.live), { type: "live", title: "Live Photos" });
  h += navItem("▭", "Ekran görüntüleri", c((g) => g.p.s), { type: "screens", title: "Ekran görüntüleri" });
  h += navItem("✎", "Düzenlenmiş", c((g) => g.p.e), { type: "edited", title: "Düzenlenmiş" });
  if (S.dbLoaded) {
    h += navItem("♥", "Favoriler", c((g) => g.p.f), { type: "fav", title: "Favoriler" });
    h += navItem("◌", "Gizli", c((g) => g.p.h), { type: "hidden", title: "Gizli" });
    h += navItem("🗑", "Son silinenler", S.groups.filter((g) => g.p.x).length, { type: "trash", title: "Son silinenler" });
  }
  $("navLibrary").innerHTML = h;

  if (S.dbLoaded) {
    $("navAlbums").innerHTML = S.albums.length
      ? S.albums.map((a) => navItem("▣", escapeHtml(a.title), a.count, { type: "album", value: a.id, title: a.title })).join("")
      : `<div class="hint">Albüm yok.</div>`;
  }

  // Tarih ağacı
  const months = new Map();
  for (const g of G) months.set(g.ym, (months.get(g.ym) || 0) + 1);
  const years = new Map();
  for (const [k, v] of months) { const y = k.slice(0, 4); years.set(y, (years.get(y) || 0) + v); }
  let d = "";
  for (const y of [...years.keys()].sort().reverse()) {
    const open = S.openYears.has(y);
    d += navItem(`<span class="arrow">▶</span>`, y, years.get(y), { type: "year", value: y, title: y }, open ? "open " : "");
    if (open) {
      for (const k of [...months.keys()].filter((k) => k.startsWith(y)).sort().reverse())
        d += navItem("", MONTHS[+k.slice(5) - 1], months.get(k), { type: "month", value: k, title: ymLabel(k) }, "sub ");
    }
  }
  $("navDates").innerHTML = d;

  const dirs = new Map();
  for (const g of G) dirs.set(g.p.dir, (dirs.get(g.p.dir) || 0) + 1);
  $("navFolders").innerHTML = [...dirs.keys()].sort().map((k) => navItem("▤", k, dirs.get(k), { type: "folder", value: k, title: k })).join("");
}
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

$("side").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-view]");
  if (!a) return;
  const v = JSON.parse(a.dataset.view);
  if (v.type === "year") { S.openYears.has(v.value) ? (S.view.type === "year" && S.view.value === v.value ? S.openYears.delete(v.value) : 0) : S.openYears.add(v.value); }
  S.view = v;
  renderNav();
  applyView(true);
});

// ---------------- görünüm / filtre ----------------
function matches(g) {
  const p = g.p, v = S.view;
  if (v.type === "trash") return p.x;
  if (p.x) return false;
  switch (v.type) {
    case "photos": return p.k === "p";
    case "videos": return p.k === "v";
    case "live": return g.live;
    case "screens": return p.s;
    case "edited": return p.e;
    case "fav": return p.f;
    case "hidden": return p.h;
    case "album": return p.a.includes(v.value);
    case "year": return g.ym.startsWith(v.value);
    case "month": return g.ym === v.value;
    case "folder": return p.dir === v.value;
    default: return true;
  }
}
function applyView(scrollTop) {
  const q = $("search").value.trim().toLowerCase();
  let list = S.groups.filter((g) => matches(g) && (!q || g.p.name.toLowerCase().includes(q)));
  const sort = $("sort").value;
  if (sort === "asc") list.sort((a, b) => a.t - b.t || a.id - b.id);
  else if (sort === "size") list.sort((a, b) => b.size - a.size);
  else list.sort((a, b) => b.t - a.t || b.id - a.id);
  S.list = list;
  $("viewTitle").textContent = S.view.title;
  const total = list.reduce((s, g) => s + g.size, 0);
  const photos = list.filter((g) => g.p.k === "p").length;
  $("viewStats").textContent = `${n(photos)} fotoğraf · ${n(list.length - photos)} video/diğer · ${fmtBytes(total)}`;
  $("albumFolderWrap").hidden = S.view.type !== "album";
  if (scrollTop) $("gridWrap").scrollTop = 0;
  layout();
  if (S.wizard) return; // bağlantı yardımcısı açıkken ekranını bozma
  if (!list.length) showEmpty(S.assets.length ? "<b>Bu görünümde öğe yok</b>" : "<b>Henüz tarama yapılmadı</b>");
  else $("empty").hidden = true;
}
function showEmpty(html) { $("empty").innerHTML = `<div>${html}</div>`; $("empty").hidden = false; }

// ---------------- sanal ızgara ----------------
const tiles = new Map(); // group id -> element
const heads = new Map();
let cols = 1, tileW = 150;

function layout() {
  const wrap = $("gridWrap");
  const w = wrap.clientWidth - 36;
  cols = Math.max(1, Math.floor((w + GAP) / (S.tile + GAP)));
  tileW = (w - GAP * (cols - 1)) / cols;
  const rows = [];
  let y = 0;
  const byDate = $("sort").value !== "size";
  let i = 0;
  while (i < S.list.length) {
    if (byDate) {
      const key = S.list[i].ym;
      let j = i; while (j < S.list.length && S.list[j].ym === key) j++;
      rows.push({ h: true, y, key, from: i, to: j });
      y += HEAD_H;
      for (let k = i; k < j; k += cols) { rows.push({ y, from: k, to: Math.min(k + cols, j) }); y += tileW + GAP; }
      i = j;
    } else {
      if (i === 0) y = 12;
      rows.push({ y, from: i, to: Math.min(i + cols, S.list.length) }); y += tileW + GAP; i += cols;
    }
  }
  S.rows = rows;
  $("gridSpacer").style.height = y + 20 + "px";
  for (const el of tiles.values()) el.remove();
  tiles.clear();
  for (const el of heads.values()) el.remove();
  heads.clear();
  render();
}

function render() {
  const wrap = $("gridWrap");
  const top = wrap.scrollTop - 400, bottom = wrap.scrollTop + wrap.clientHeight + 600;
  const rows = S.rows;
  // ikili arama ile ilk görünen satır
  let lo = 0, hi = rows.length - 1, start = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (rows[m].y + tileW < top) { lo = m + 1; start = lo; } else hi = m - 1; }
  const wantTiles = new Set(), wantHeads = new Set();
  const spacer = $("gridSpacer");
  const selecting = S.selected.size > 0;
  spacer.classList.toggle("selecting", selecting);
  for (let r = start; r < rows.length && rows[r].y < bottom; r++) {
    const row = rows[r];
    if (row.h) {
      wantHeads.add(row.key);
      let el = heads.get(row.key);
      if (!el) {
        el = document.createElement("div");
        el.className = "ghead";
        el.dataset.from = row.from; el.dataset.to = row.to;
        heads.set(row.key, el);
        spacer.appendChild(el);
      }
      el.style.top = row.y + "px"; el.style.height = HEAD_H + "px";
      const cnt = row.to - row.from;
      let allSel = true;
      for (let k = row.from; k < row.to; k++) if (!S.selected.has(S.list[k].id)) { allSel = false; break; }
      el.innerHTML = `<div>${ymLabel(row.key)}<span class="muted">${n(cnt)} öğe</span></div><button class="link" data-act="selgroup">${allSel ? "Seçimi kaldır" : "Tümünü seç"}</button>`;
      continue;
    }
    for (let k = row.from; k < row.to; k++) {
      const g = S.list[k];
      wantTiles.add(g.id);
      let el = tiles.get(g.id);
      if (!el) { el = makeTile(g); tiles.set(g.id, el); spacer.appendChild(el); }
      const c = k - row.from;
      el.style.cssText = `top:${row.y}px;left:${c * (tileW + GAP)}px;width:${tileW}px;height:${tileW}px`;
      el.classList.toggle("sel", S.selected.has(g.id));
      el.dataset.idx = k;
    }
  }
  for (const [id, el] of tiles) if (!wantTiles.has(id)) { el.remove(); tiles.delete(id); }
  for (const [k, el] of heads) if (!wantHeads.has(k)) { el.remove(); heads.delete(k); }
}

function makeTile(g) {
  const el = document.createElement("div");
  el.className = "tile";
  el.dataset.id = g.id;
  const p = g.p;
  const b = [];
  if (p.f) b.push(`<span class="badge fav">♥</span>`);
  if (p.e) b.push(`<span class="badge">✎</span>`);
  if (g.live) b.push(`<span class="badge">LIVE</span>`);
  if (p.k === "v") b.push(`<span class="badge">▶ ${fmtBytes(p.size)}</span>`);
  else if (p.ext !== "HEIC" && p.ext !== "JPG" && p.ext !== "JPEG") b.push(`<span class="badge">${p.ext}</span>`);
  el.innerHTML = `<div class="ph">${p.ext}</div><img alt="" decoding="async">
    <div class="chk"></div>
    <div class="zoom-btn" title="Önizle"><svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></div>
    <div class="badges">${b.join("")}</div>`;
  const img = el.querySelector("img");
  img.onload = () => img.classList.add("ok");
  img.onerror = () => img.remove();
  img.src = `/api/thumb/${g.id}`;
  el.title = `${p.name} · ${fmtBytes(g.size)} · ${new Date(p.t * 1000).toLocaleString(LOCALE)}`;
  return el;
}

const wrapEl = $("gridWrap");
let raf = 0;
wrapEl.addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); });
new ResizeObserver(debounce(() => layout(), 80)).observe(wrapEl);

wrapEl.addEventListener("click", (e) => {
  const hb = e.target.closest("[data-act=selgroup]");
  if (hb) {
    const h = hb.closest(".ghead");
    const from = +h.dataset.from, to = +h.dataset.to;
    const ids = S.list.slice(from, to).map((g) => g.id);
    const all = ids.every((id) => S.selected.has(id));
    ids.forEach((id) => (all ? S.selected.delete(id) : S.selected.add(id)));
    selChanged(); return;
  }
  const t = e.target.closest(".tile");
  if (!t) return;
  const idx = +t.dataset.idx;
  if (e.target.closest(".zoom-btn")) { openLightbox(idx); return; }
  if (e.detail > 1) return; // çift tıklamanın ikinci tıkı
  const id = +t.dataset.id;
  if (e.shiftKey && S.lastClicked != null) {
    const [a, b] = [Math.min(S.lastClicked, idx), Math.max(S.lastClicked, idx)];
    for (let k = a; k <= b; k++) S.selected.add(S.list[k].id);
  } else {
    S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
  }
  S.lastClicked = idx;
  selChanged();
});
wrapEl.addEventListener("dblclick", (e) => {
  const t = e.target.closest(".tile");
  // ilk tık seçimi değiştirdi; onu geri al ve önizlemeyi aç
  if (t && !e.target.closest(".chk") && !e.target.closest(".zoom-btn")) {
    const id = +t.dataset.id;
    S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
    selChanged();
    openLightbox(+t.dataset.idx);
  }
});

function selChanged() {
  render();
  const groups = [...S.selected].map((id) => S.byId.get(id)).filter(Boolean);
  const size = groups.reduce((s, g) => s + g.size, 0);
  $("selInfo").textContent = groups.length ? `${n(groups.length)} öğe seçili · ${fmtBytes(size)}` : "Seçim yok — fotoğraflara tıklayarak seçin (Shift ile aralık)";
  $("copySelText").textContent = groups.length ? `Seçilenleri kopyala (${n(groups.length)})` : "Seçilenleri kopyala";
  $("btnCopySel").disabled = !groups.length || isJobRunning();
  $("btnSelNone").hidden = !groups.length;
}

$("btnSelAll").onclick = () => { S.list.forEach((g) => S.selected.add(g.id)); selChanged(); };
$("btnSelNone").onclick = () => { S.selected.clear(); selChanged(); };
$("search").addEventListener("input", debounce(() => applyView(true), 200));
$("sort").onchange = () => applyView(true);
$("zoom").addEventListener("input", () => { S.tile = +$("zoom").value; layout(); });
$("btnRescan").onclick = async () => {
  if (await connect()) { scan(); return; }
  showTab("photos");
  await connectionWizard();
  scan();
};
$("devSelect").onchange = async (e) => { S.device = { udid: e.target.value }; if (await connect()) scan(); };

// ---------------- önizleme (tüm sekmelerde ortak) ----------------
// Kaynak arayüzü: { len(), item(i) -> {name, meta, img?, video?, live?, download, selected?, toggle?(), reveal?()} }
const LB = { src: null, idx: -1 };
function openViewer(src, idx) { LB.src = src; LB.idx = idx; $("lb").hidden = false; showLb(); }
function closeLightbox() { $("lb").hidden = true; $("lbStage").innerHTML = ""; LB.idx = -1; }
const photoSource = {
  len: () => S.list.length,
  item(i) {
    const g = S.list[i]; if (!g) return null;
    const p = g.p;
    return {
      name: p.name,
      meta: `${new Date(p.t * 1000).toLocaleString(LOCALE)} · ${fmtBytes(g.size)} · ${p.dir}`,
      img: p.k !== "v" ? `/api/preview/${p.id}` : null,
      video: p.k === "v" ? `/api/preview/${p.id}` : null,
      live: g.liveVideo ? `/api/preview/${g.liveVideo.id}` : null,
      download: `/api/file/${p.id}`,
      selected: S.selected.has(g.id),
      toggle() { S.selected.has(g.id) ? S.selected.delete(g.id) : S.selected.add(g.id); selChanged(); },
    };
  },
};
function openLightbox(idx) { openViewer(photoSource, idx); }

function showLb(playLive) {
  const it = LB.src && LB.src.item(LB.idx);
  if (!it) return closeLightbox();
  const stage = $("lbStage");
  stage.innerHTML = `<div class="spinner"></div>`;
  const vsrc = playLive ? it.live : it.video;
  if (vsrc) {
    const v = document.createElement("video");
    v.controls = true; v.autoplay = true; v.loop = !!playLive; v.src = vsrc;
    v.onloadeddata = () => stage.querySelector(".spinner")?.remove();
    v.onerror = () => { stage.innerHTML = `<div class="muted" style="text-align:center">Bu video tarayıcıda oynatılamıyor (muhtemelen HEVC).<br>İndirip bilgisayarınızda açabilirsiniz.</div>`; };
    stage.appendChild(v);
  } else {
    const img = new Image();
    img.onload = () => { stage.innerHTML = ""; stage.appendChild(img); };
    img.onerror = () => { stage.innerHTML = `<div class="muted">Önizleme yüklenemedi.</div>`; };
    img.src = it.img;
  }
  $("lbName").textContent = it.name;
  const extra = it.live ? ` · <a class="link" id="lbLive">Live videoyu oynat</a>` : "";
  $("lbMeta").innerHTML = `${escapeHtml(it.meta)} · ${LB.idx + 1}/${n(LB.src.len())}${extra}`;
  if (it.live) $("lbLive").onclick = () => showLb(true);
  $("lbDownload").href = it.download;
  $("lbSelect").hidden = !it.toggle;
  $("lbSelect").textContent = it.selected ? "✓ Seçili" : "Seç";
  $("lbReveal").hidden = !it.reveal;
  // komşuları önceden yükle
  for (const k of [LB.idx + 1, LB.idx - 1]) { const q = LB.src.item(k); if (q && q.img) new Image().src = q.img; }
}
$("lbClose").onclick = closeLightbox;
$("lbPrev").onclick = () => { if (LB.idx > 0) { LB.idx--; showLb(); } };
$("lbNext").onclick = () => { if (LB.idx < LB.src.len() - 1) { LB.idx++; showLb(); } };
$("lbSelect").onclick = () => { const it = LB.src.item(LB.idx); if (it && it.toggle) { it.toggle(); $("lbSelect").textContent = LB.src.item(LB.idx).selected ? "✓ Seçili" : "Seç"; } };
$("lbReveal").onclick = () => { const it = LB.src.item(LB.idx); if (it && it.reveal) it.reveal(); };
$("lb").addEventListener("click", (e) => { if (e.target.id === "lb" || e.target.id === "lbStage") closeLightbox(); });
document.addEventListener("keydown", (e) => {
  if ($("lb").hidden) {
    if (currentTab !== "photos") return;
    if ((e.ctrlKey || e.metaKey) && e.key === "a" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("btnSelAll").click(); }
    if (e.key === "Escape") $("btnSelNone").click();
    return;
  }
  if (e.key === "Escape") closeLightbox();
  else if (e.key === "ArrowLeft") $("lbPrev").click();
  else if (e.key === "ArrowRight") $("lbNext").click();
  else if (e.key === " " && !$("lbSelect").hidden) { e.preventDefault(); $("lbSelect").click(); }
});

// ---------------- sekmeler ----------------
let currentTab = "photos";
const tabInit = {};
function showTab(name) {
  currentTab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + name));
  if (name === "photos") layout();
  if (window.iSyncViews && window.iSyncViews[name]) { window.iSyncViews[name](!tabInit[name]); tabInit[name] = true; }
  history.replaceState(null, "", "#" + name);
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));

// ---------------- ayarlar ----------------
function options() {
  return {
    dest: $("dest").value.trim(),
    layout: $("layout").value,
    convert_heic: $("convert").value,
    skip_existing: $("optSkip").checked,
    include_live_video: $("optLive").checked,
    include_edited: $("optEdited").checked,
    include_sidecar: $("optAae").checked,
  };
}
const saveSettings = debounce(() => api("/api/settings", options()).catch(() => {}), 400);
["layout", "convert", "optSkip", "optLive", "optEdited", "optAae"].forEach((id) => $(id).addEventListener("change", saveSettings));
$("dest").addEventListener("change", () => { saveSettings(); refreshNewCount(); });
$("btnPick").onclick = async () => {
  $("btnPick").disabled = true;
  try {
    const r = await api("/api/pick-folder", { initial: $("dest").value });
    if (r.path) { $("dest").value = r.path; saveSettings(); refreshNewCount(); }
  } finally { $("btnPick").disabled = false; }
};
$("btnOpen").onclick = () => api("/api/open-folder", { path: $("dest").value }).catch((e) => toast(e.message, true));
$("btnJobOpen").onclick = () => $("btnOpen").click();

async function refreshNewCount() {
  if (!S.assets.length) return;
  try {
    const r = await api("/api/new-count", { dest: $("dest").value.trim() });
    S.newIds = r.ids;
    $("syncNewText").textContent = r.count ? `Yenileri senkronize et (${n(r.count)} · ${fmtBytes(r.bytes)})` : "Her şey güncel ✓";
    $("btnSyncNew").disabled = !r.count || isJobRunning();
  } catch { /* yoksay */ }
}

// ---------------- kopyalama ----------------
function isJobRunning() { return S.job && (S.job.status === "running" || S.job.status === "preparing"); }

async function startJob(ids) {
  const o = options();
  if (!o.dest) { toast("Önce hedef klasörü seçin", true); return; }
  if (S.view.type === "album" && $("optAlbumFolder").checked && !$("albumFolderWrap").hidden) o.subfolder = S.view.title;
  try {
    S.job = await api("/api/sync", { asset_ids: ids, options: o });
    showJob();
    pollJob();
  } catch (e) { toast(e.message, true); }
}
$("btnCopySel").onclick = () => {
  const ids = [];
  for (const id of S.selected) { const g = S.byId.get(id); if (g) ids.push(...g.ids); }
  startJob(ids);
};
$("btnSyncNew").onclick = () => { if (S.newIds) startJob(S.newIds); };
$("btnCancel").onclick = () => { if (S.job) api(`/api/jobs/${S.job.id}/cancel`, {}); };

async function pollJob() {
  if (!S.job) return;
  try { S.job = await api(`/api/jobs/${S.job.id}`); } catch { /* sunucu meşgul */ }
  showJob();
  if (isJobRunning()) setTimeout(pollJob, 500);
  else { selChanged(); refreshNewCount(); }
}
function showJob() {
  const j = S.job, box = $("job");
  box.hidden = false;
  const running = isJobRunning();
  box.className = "card job " + (j.status === "done" ? "done" : j.status === "error" ? "error" : "");
  const pct = j.total_bytes ? Math.min(100, (j.done_bytes / j.total_bytes) * 100) : (j.status === "done" ? 100 : 0);
  $("jobBar").style.width = pct + "%";
  $("jobPct").textContent = Math.floor(pct) + "%";
  $("jobSpeed").textContent = running && j.speed ? fmtBytes(j.speed) + "/s" : "–";
  $("jobEta").textContent = running ? fmtDur(j.eta) : fmtDur(j.elapsed);
  $("jobEta").nextElementSibling.textContent = running ? "kalan süre" : "geçen süre";
  const titles = { preparing: "Hazırlanıyor…", running: "Kopyalanıyor", done: "Tamamlandı ✓", cancelled: "İptal edildi", error: "Hata" };
  $("jobTitle").textContent = titles[j.status] || j.status;
  let counts = `${n(j.done_files)} / ${n(j.total_files)} dosya · ${fmtBytes(j.done_bytes)} / ${fmtBytes(j.total_bytes)}`;
  if (j.skipped) counts += ` · ${n(j.skipped)} zaten vardı`;
  if (j.converted) counts += ` · ${n(j.converted)} JPG'ye çevrildi`;
  $("jobCounts").textContent = counts;
  $("jobCurrent").innerHTML = j.message ? escapeHtml(j.message) : (j.current || []).map((c) => `<div>${escapeHtml(c)}</div>`).join("");
  $("btnCancel").hidden = !running;
  $("btnJobOpen").hidden = running;
  $("jobErrWrap").hidden = !j.error_count;
  if (j.error_count) {
    $("jobErrSum").textContent = `${n(j.error_count)} dosya kopyalanamadı`;
    $("jobErrors").innerHTML = j.errors.map((e) => `<div>${escapeHtml(e.file)} — ${escapeHtml(e.error)}</div>`).join("");
  }
  $("btnCopySel").disabled = running || !S.selected.size;
  $("btnSyncNew").disabled = running || !(S.newIds && S.newIds.length);
  if (!running && j.status === "done" && !box.dataset.toasted) { box.dataset.toasted = j.id; toast(j.message || `Aktarım tamamlandı: ${n(j.done_files)} dosya`); }
}

// ---------------- albüm veritabanı ----------------
$("btnDb").onclick = async () => {
  try { await api("/api/db/load", {}); pollDb(); } catch (e) { toast(e.message, true); }
};
async function pollDb() {
  const st = await api("/api/db/status");
  const box = $("dbStatus");
  if (st.status === "running") {
    box.hidden = false;
    const pct = st.total ? (st.done / st.total) * 100 : 0;
    box.innerHTML = `${escapeHtml(st.message)} ${st.total ? `${fmtBytes(st.done)} / ${fmtBytes(st.total)}` : ""}<div class="progress"><div style="width:${pct}%"></div></div>`;
    setTimeout(pollDb, 600);
  } else if (st.status === "error") {
    box.hidden = false; box.textContent = st.message;
  } else {
    box.hidden = true;
    const lib = await api("/api/library");
    const view = S.view;
    loadLibrary(lib);
    S.view = view; renderNav(); applyView();
    toast(`${S.albums.length} albüm yüklendi`);
  }
}


// ---------------- bağlantı yardımcısı ----------------
// Telefon bağlanana kadar sistemi adım adım denetler ve kullanıcıya ne yapacağını söyler.
const STEP_ICON = { ok: "✓", fail: "!", wait: "…", skip: "–" };
async function connectionWizard() {
  S.wizard = true;
  try { await runWizard(); } finally { S.wizard = false; }
}
async function runWizard() {
  let busyAction = false;
  for (;;) {
    let d;
    try { d = await api("/api/diagnose"); } catch (e) { d = { ok: false, steps: [{ id: "app", title: "iSync", status: "fail", detail: e.message }] }; }
    if (d.ok && (await connect())) { S.wizard = false; $("empty").hidden = true; toast("iPhone bağlandı"); return; }
    const drv = d.driver || {};
    const first = d.steps.find((st) => st.status !== "ok");
    const html = `<div class="wizard">
      <div class="wz-title">iPhone bağlantı yardımcısı</div>
      <div class="muted small">Adımlar otomatik denetleniyor; her şey yeşil olunca fotoğraflar açılır.</div>
      <ol class="wz-steps">${d.steps.map((st) => `
        <li class="wz-${st.status}${st === first ? " current" : ""}">
          <span class="wz-ic">${STEP_ICON[st.status] || "?"}</span>
          <div><b>${escapeHtml(st.title)}</b>${st.detail ? `<div class="wz-detail">${escapeHtml(st.detail)}</div>` : ""}
          ${st === first ? wizardAction(st.action, drv) : ""}</div>
        </li>`).join("")}
      </ol>
      ${first && first.id === "usb" ? `<div class="wz-phone">📱 ➜ 🔌 ➜ 💻</div>` : ""}
    </div>`;
    showEmpty(html);
    const btn = document.querySelector("#empty [data-wz]");
    if (btn) btn.onclick = async () => {
      if (busyAction) return;
      busyAction = true; btn.disabled = true;
      try {
        const act = btn.dataset.wz;
        if (act === "install_driver") await api("/api/driver/install", {});
        else if (act === "start_service") { await api("/api/driver/start-service", {}); toast("Windows onay penceresinde 'Evet'e basın"); }
        else if (act === "open_store") await api("/api/driver/open-store", {});
      } catch (e) { toast(e.message, true); } finally { busyAction = false; }
    };
    const alt = document.querySelector("#empty [data-wz-store]");
    if (alt) alt.onclick = () => api("/api/driver/open-store", {}).catch(() => {});
    await new Promise((r) => setTimeout(r, drv.status === "running" ? 3000 : 2500));
  }
}
function wizardAction(action, drv) {
  if (action === "install_driver") {
    if (drv.status === "running") return `<div class="wz-act"><span class="spin-sm"></span> ${escapeHtml(drv.log || "Kuruluyor…")}</div>`;
    const err = drv.status === "error" || drv.status === "store" ? `<div class="wz-detail err">${escapeHtml(drv.log)}</div>` : "";
    return `${err}<div class="wz-act"><button class="btn primary" data-wz="install_driver">Apple sürücüsünü otomatik kur</button>
      <button class="link" data-wz-store>Microsoft Store'da aç</button></div>
      <div class="muted small">Ücretsiz "Apple Devices" uygulaması Microsoft Store'dan kurulur; Store'un kullanım koşulları geçerlidir.</div>`;
  }
  if (action === "start_service") return `<div class="wz-act"><button class="btn primary" data-wz="start_service">Servisi başlat</button></div>`;
  return "";
}

// ---------------- başlangıç ----------------
async function init() {
  const info = await api("/api/info");
  document.title = `${info.app} ${info.version}`;
  const s = info.settings;
  $("dest").value = s.dest;
  $("layout").value = s.layout;
  $("convert").value = s.convert_heic;
  $("optSkip").checked = s.skip_existing;
  $("optLive").checked = s.include_live_video;
  $("optEdited").checked = s.include_edited;
  $("optAae").checked = s.include_sidecar;
  selChanged();
  // önceki/çalışan iş varsa göster
  const jobs = (await api("/api/jobs")).jobs;
  if (jobs.length) { S.job = jobs[jobs.length - 1]; showJob(); if (isJobRunning()) pollJob(); }

  if (!(await connect())) await connectionWizard();
  const lib = await api("/api/library").catch(() => null);
  if (lib && lib.assets.length) loadLibrary(lib); else await scan();
  // bağlantı takibi
  setInterval(async () => {
    if (isJobRunning()) return;
    try {
      const d = await api("/api/devices");
      const here = S.device && d.devices.some((x) => x.udid === S.device.udid);
      const wasOnline = $("device").classList.contains("online");
      if (!here && wasOnline) {
        $("device").className = "device offline"; $("devSub").textContent = "Bağlantı kesildi — tekrar takın";
        // Fotoğraflar sekmesinde kullanıcıyı yeniden bağlanmaya yönlendir; bağlanınca kütüphaneyi tazele
        if (!S.wizard) connectionWizard().then(() => scan());
      }
      if (here && !wasOnline) { if (await connect()) toast("iPhone yeniden bağlandı"); }
    } catch { /* yoksay */ }
  }, 4000);
}
init().catch((e) => toast(e.message, true));
// Uygulama penceresi açık olduğu sürece sunucuya "buradayım" de (kapanınca sunucu kendiliğinden kapanır)
const ping = () => fetch("/api/ping", { method: "POST" }).catch(() => {});
ping(); setInterval(ping, 15000);
