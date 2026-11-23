"use strict";
/* iSync — Harita, Cihaz ve Uygulamalar sekmeleri. app.js'teki yardımcıları ($, api, fmtBytes, n, toast, openViewer…) kullanır. */

window.iSyncViews = {};

// =====================================================================
// HARİTA — indirilen klasördeki fotoğrafları konumlarına göre gösterir
// =====================================================================
const M = { root: "", data: null, map: null, cluster: null, base: null, kind: "all", year: "", visible: [], shown: 0, poll: null };
const TILES = {
  dark: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', 19],
  osm: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', 19],
  sat: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "Tiles &copy; Esri", 19],
};

function loadScript(src) {
  return new Promise((ok, fail) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.head.appendChild(s); });
}
function loadCss(href) { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = href; document.head.appendChild(l); }
let leafletReady = null;
function loadLeaflet() {
  if (!leafletReady) {
    // Uygulamanın içine gömülü (çevrimdışı da açılır); sadece harita altlıkları internetten gelir
    loadCss("/static/vendor/leaflet/leaflet.css");
    loadCss("/static/vendor/markercluster/MarkerCluster.css");
    leafletReady = loadScript("/static/vendor/leaflet/leaflet.js")
      .then(() => loadScript("/static/vendor/markercluster/leaflet.markercluster.js"));
  }
  return leafletReady;
}

const q = (o) => Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
const thumbUrl = (i) => `/api/geo/thumb/${i}?${q({ root: M.root })}`;
const fileUrl = (i, extra = {}) => `/api/geo/file/${i}?${q({ root: M.root, ...extra })}`;

function mapMsg(html) { $("mapMsg").innerHTML = html ? `<div>${html}</div>` : ""; $("mapMsg").hidden = !html; }

window.iSyncViews.map = async (first) => {
  if (first) {
    M.root = localStorage.getItem("isync.mapRoot") || $("dest").value.trim() || (await api("/api/info")).settings.dest;
    $("mapRoot").value = M.root;
    mapMsg("<b>Harita yükleniyor…</b>");
    try { await loadLeaflet(); } catch {
      mapMsg("<b>Harita kütüphanesi yüklenemedi</b>iSync dosyaları eksik olabilir; uygulamayı yeniden kurun.");
      return;
    }
    createMap();
    await loadPoints();
  } else if (M.map) {
    setTimeout(() => M.map.invalidateSize(), 50);
  }
};

function createMap() {
  M.map = L.map("map", { zoomControl: true, worldCopyJump: true, preferCanvas: false }).setView([39, 35], 5);
  setBase("dark");
  M.map.on("moveend", updateVisible);
}
function setBase(name) {
  if (M.base) M.map.removeLayer(M.base);
  const [url, attr, maxZoom] = TILES[name];
  M.base = L.tileLayer(url, { attribution: attr, maxZoom, className: name === "dark" ? "tiles-dark" : "" }).addTo(M.map);
  document.querySelectorAll("#mapLayer button").forEach((b) => b.classList.toggle("on", b.dataset.l === name));
}

async function loadPoints() {
  try {
    M.data = await api(`/api/geo/points?${q({ root: M.root })}`);
  } catch (e) {
    M.data = null;
    mapMsg(`<b>Klasör açılamadı</b>${escapeHtml(e.message)}`);
    $("mapStatus").textContent = "";
    return;
  }
  const d = M.data;
  if (d.state.status === "running") { watchScan(); return; }
  if (!d.count) {
    mapMsg(`<b>Bu klasör henüz taranmadı</b>Sağdaki <i>Konumları tara</i> düğmesine basın. İlk tarama her dosyanın GPS bilgisini okur; sonrakiler sadece yenileri okur.`);
    $("mapStatus").textContent = "";
    return;
  }
  mapMsg("");
  const withGps = d.points.length;
  $("mapStatus").textContent = `${n(d.count)} dosya · ${n(withGps)} konumlu · ${n(d.no_gps)} konumsuz`;
  const years = [...new Set(d.points.map((p) => new Date(p[3] * 1000).getFullYear()))].sort((a, b) => b - a);
  $("mapYear").innerHTML = `<option value="">Tüm yıllar</option>` + years.map((y) => `<option>${y}</option>`).join("");
  $("mapYear").value = M.year && years.includes(+M.year) ? M.year : "";
  buildMarkers(true);
}

function firstMarker(c) {
  // Kümenin en yeni (en küçük indeks) temsilcisi: alt kümeleri gezmeden hızlıca
  let best = null;
  const visit = (k, depth) => {
    // fotoğraf varsa onu tercih et (videoların küçük resmi her zaman olmayabilir), sonra en yeni
    for (const m of k._markers) {
      if (!best || (m.options.k === "p" && best.options.k !== "p") || (m.options.k === best.options.k && m.options.i < best.options.i)) best = m;
    }
    if (depth < 3) for (const ch of k._childClusters) visit(ch, depth + 1);
  };
  visit(c, 0);
  return best || c.getAllChildMarkers()[0];
}

function buildMarkers(fit) {
  if (M.cluster) M.map.removeLayer(M.cluster);
  const pts = filteredPoints();
  M.cluster = L.markerClusterGroup({
    chunkedLoading: true, showCoverageOnHover: false, maxClusterRadius: 72, spiderfyOnMaxZoom: true,
    iconCreateFunction(c) {
      const m = firstMarker(c);
      return L.divIcon({
        className: "pin-icon",
        html: `<div class="pin${m.options.k === "v" ? " v" : ""}" style="background-image:url('${thumbUrl(m.options.i)}')"><span>${n(c.getChildCount())}</span></div>`,
        iconSize: [54, 54], iconAnchor: [27, 62],
      });
    },
  });
  const markers = pts.map((p) => L.marker([p[1], p[2]], {
    i: p[0], k: p[4],
    icon: L.divIcon({ className: "pin-icon", html: `<div class="pin${p[4] === "v" ? " v" : ""}" style="background-image:url('${thumbUrl(p[0])}')"></div>`, iconSize: [54, 54], iconAnchor: [27, 62] }),
  }));
  M.cluster.addLayers(markers);
  M.cluster.on("click", (e) => {
    const i = e.layer.options.i;
    const idx = M.visible.findIndex((p) => p[0] === i);
    if (idx >= 0) openViewer(mapSource, idx);
    else { M.visible = [pts.find((p) => p[0] === i)]; openViewer(mapSource, 0); }
  });
  M.map.addLayer(M.cluster);
  if (fit && markers.length) M.map.fitBounds(M.cluster.getBounds(), { padding: [40, 40], maxZoom: 15 });
  updateVisible();
}

function filteredPoints() {
  if (!M.data) return [];
  return M.data.points.filter((p) => (M.kind === "all" || p[4] === M.kind) && (!M.year || new Date(p[3] * 1000).getFullYear() === +M.year));
}

function updateVisible() {
  if (!M.data || !M.map) return;
  const b = M.map.getBounds();
  M.visible = filteredPoints().filter((p) => b.contains([p[1], p[2]])).sort((a, c) => c[3] - a[3]);
  $("mapVisCount").textContent = `${n(M.visible.length)} öğe`;
  M.shown = 0;
  $("mapGrid").innerHTML = "";
  showMoreVisible();
}
function showMoreVisible() {
  const grid = $("mapGrid");
  grid.querySelector(".more")?.remove();
  const next = M.visible.slice(M.shown, M.shown + 120);
  const html = next.map((p, k) =>
    `<div class="mt" data-idx="${M.shown + k}"><img loading="lazy" src="${thumbUrl(p[0])}" alt="" onerror="this.remove()">${p[4] === "v" ? '<span class="badge">▶</span>' : ""}</div>`).join("");
  grid.insertAdjacentHTML("beforeend", html);
  M.shown += next.length;
  if (M.shown < M.visible.length) grid.insertAdjacentHTML("beforeend", `<button class="btn more">Daha fazla göster (${n(M.visible.length - M.shown)})</button>`);
}
$("mapGrid").addEventListener("click", (e) => {
  if (e.target.closest(".more")) { showMoreVisible(); return; }
  const t = e.target.closest(".mt");
  if (t) openViewer(mapSource, +t.dataset.idx);
});

const mapSource = {
  len: () => M.visible.length,
  item(i) {
    const p = M.visible[i]; if (!p) return null;
    const rel = M.data.names[p[0]];
    const parts = rel.split("/");
    const isImg = p[4] === "p";
    return {
      name: parts[parts.length - 1],
      meta: `${new Date(p[3] * 1000).toLocaleString(LOCALE)} · ${parts.slice(0, -1).join("/") || "/"} · ${p[1].toFixed(4)}, ${p[2].toFixed(4)}`,
      img: isImg ? fileUrl(p[0], { preview: 1 }) : null,
      video: isImg ? null : fileUrl(p[0]),
      download: fileUrl(p[0], { download: 1 }),
      reveal: () => api("/api/geo/reveal", { root: M.root, i: p[0] }).catch((e) => toast(e.message, true)),
    };
  },
};

document.querySelectorAll("#mapKind button").forEach((b) => (b.onclick = () => {
  M.kind = b.dataset.k;
  document.querySelectorAll("#mapKind button").forEach((x) => x.classList.toggle("on", x === b));
  buildMarkers(false);
}));
document.querySelectorAll("#mapLayer button").forEach((b) => (b.onclick = () => setBase(b.dataset.l)));
$("mapYear").onchange = () => { M.year = $("mapYear").value; buildMarkers(true); };
$("mapFit").onclick = () => { if (M.cluster && M.cluster.getLayers().length) M.map.fitBounds(M.cluster.getBounds(), { padding: [40, 40] }); };
function setMapRoot(v) { M.root = v; localStorage.setItem("isync.mapRoot", v); if (M.map) loadPoints(); }
$("mapRoot").addEventListener("change", () => setMapRoot($("mapRoot").value.trim()));
$("mapPick").onclick = async () => {
  const r = await api("/api/pick-folder", { initial: $("mapRoot").value });
  if (r.path) { $("mapRoot").value = r.path; setMapRoot(r.path); }
};
$("mapScan").onclick = async () => {
  try {
    await api("/api/geo/scan", { root: M.root });
    watchScan();
  } catch (e) { toast(e.message, true); }
};
function watchScan() {
  clearTimeout(M.poll);
  $("mapScan").disabled = true;
  $("mapProg").hidden = false;
  const tick = async () => {
    let st;
    try { st = await api(`/api/geo/status?${q({ root: M.root })}`); } catch { st = { status: "error", message: "?" }; }
    const pct = st.total ? (st.done / st.total) * 100 : 0;
    $("mapProg").firstElementChild.style.width = pct + "%";
    $("mapStatus").textContent = st.status === "running" ? `${st.message} ${st.total ? `${n(st.done)} / ${n(st.total)}` : ""}` : st.message || "";
    if (st.status === "running") { M.poll = setTimeout(tick, 500); return; }
    $("mapScan").disabled = false;
    $("mapProg").hidden = true;
    if (st.status === "error") toast(st.message, true);
    await loadPoints();
    if (M.data) toast(`${n(M.data.points.length)} konumlu öğe haritada`);
  };
  tick();
}

// =====================================================================
// CİHAZ BİLGİSİ
// =====================================================================
const kv = (rows) => `<dl class="kv">${rows.filter((r) => r[1] != null && r[1] !== "").map(([k, v, copy]) =>
  `<dt>${k}</dt><dd${copy ? ` class="copy" title="Kopyalamak için tıklayın"` : ""}>${escapeHtml(v)}</dd>`).join("")}</dl>`;
const card = (title, body) => `<div class="card"><div class="card-title">${title}</div>${body}</div>`;
const yesno = (b) => (b == null ? null : b ? "Açık" : "Kapalı");

window.iSyncViews.device = async (first) => { if (first) loadDevice(); };
$("devRefresh").onclick = () => loadDevice();
async function loadDevice() {
  const box = $("devCards");
  box.innerHTML = `<div class="muted">Cihazdan okunuyor…</div>`;
  let d;
  try { d = await api("/api/device/details"); } catch (e) { box.innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; return; }
  const h = d.hardware, b = d.battery || {}, s = d.storage, bk = d.backup;
  const health = b.health;
  const hcls = health == null ? "" : health >= 85 ? "" : health >= 75 ? "mid" : "low";
  const used = s.data_total ? s.data_total - s.data_free : 0;
  const html = [
    card("Telefon", `
      <div style="display:flex;gap:14px;align-items:center">
        <div style="flex:1">
          <div style="font-size:20px;font-weight:700">${escapeHtml(h.name || "iPhone")}</div>
          <div class="muted">${escapeHtml(h.model || "")} · ${escapeHtml(h.color || "")}</div>
          <div class="muted">iOS ${escapeHtml(h.ios || "")} (${escapeHtml(h.build || "")})</div>
        </div>
      </div>
      <img class="wall" id="wallImg" alt="Duvar kağıdı" hidden>
      <div style="display:flex;gap:8px"><button class="btn small" id="wallBtn">Duvar kağıdını göster</button><a class="btn small" href="/api/wallpaper" download="iphone-duvar-kagidi.png">İndir</a></div>`),
    card("Pil sağlığı", `
      <div class="big-num">${health != null ? pct(health) : "–"} <small>maksimum kapasite</small></div>
      <div class="gauge"><div class="${hcls}" style="width:${health || 0}%"></div></div>
      ${kv([
        ["Şarj döngüsü", b.cycles != null ? n(b.cycles) : null],
        ["Tam kapasite", b.full_mah ? `${n(b.full_mah)} mAh` : null],
        ["Tasarım kapasitesi", b.design_mah ? `${n(b.design_mah)} mAh` : null],
        ["Ham oran", b.health_raw != null ? pct(b.health_raw) : null],
        ["Şarj seviyesi", b.level != null ? `${pct(b.level)}${b.charging ? " · şarj oluyor" : ""}` : null],
        ["Sıcaklık", b.temp_c != null ? `${b.temp_c} °C` : null],
        ["Ortalama / en yüksek", b.avg_temp_c != null ? `${b.avg_temp_c} °C / ${b.max_temp_c ?? "–"} °C` : null],
        ["Voltaj", b.voltage ? `${b.voltage} V` : null],
        ["Şarj cihazı", b.adapter ? `${b.adapter}${b.adapter_watts ? ` · ${b.adapter_watts} W` : ""}` : null],
      ])}`),
    card("Depolama", `
      <div class="big-num">${fmtBytes(s.data_free)} <small>şu an boş</small></div>
      <div class="gauge"><div class="${s.data_free / (s.data_total || 1) < 0.05 ? "low" : ""}" style="width:${s.data_total ? (used / s.data_total) * 100 : 0}%;background:var(--accent)"></div></div>
      ${kv([
        ["Toplam", s.total ? fmtBytes(s.total) : null],
        ["Kullanıcı alanı", s.data_total ? fmtBytes(s.data_total) : null],
        ["Kullanılan", used ? fmtBytes(used) : null],
        ["Boşaltılabilir dahil", s.data_purgeable_free ? fmtBytes(s.data_purgeable_free) : null],
        ["Sistem", s.system ? fmtBytes(s.system) : null],
      ])}
      <div class="muted small">"Boşaltılabilir" alan iOS'un gerektiğinde kendisi temizlediği alandır (iCloud'daki fotoğrafların orijinalleri, önbellekler). Ayarlar uygulaması bu rakamı gösterebilir. Gerçekten yer açmak için fotoğrafları iSync ile aldıktan sonra Fotoğraflar uygulamasından silin.</div>`),
    card("Kimlik bilgileri", kv([
      ["Seri numarası", h.serial, 1], ["IMEI", h.imei, 1], ["IMEI 2", h.imei2, 1], ["MEID", h.meid, 1],
      ["Model numarası", h.model_number, 1], ["Donanım", `${h.product_type || ""} · ${h.hardware_model || ""}`],
      ["UDID", h.udid, 1], ["Wi-Fi MAC", h.wifi_mac, 1], ["Bluetooth MAC", h.bt_mac, 1], ["Baseband", h.baseband],
    ]) + `<div class="muted small">Garanti sorgusu ve servis için. Değere tıklayınca kopyalanır.</div>`),
    card("Yedekleme ve eşitleme", kv([
      ["iCloud yedekleme", yesno(bk.icloud)],
      ["Son iCloud yedeği", bk.last_icloud ? new Date(bk.last_icloud * 1000).toLocaleString(LOCALE) : null],
      ["Yerel yedek şifreli", bk.encrypted_local == null ? null : bk.encrypted_local ? "Evet" : "Hayır"],
      ...Object.entries(d.sync_sources || {}).map(([k, v]) => [({ Contacts: "Kişiler", Notes: "Notlar", Bookmarks: "Yer imleri", Calendars: "Takvimler" }[k] || k), v.join(", ") || "Bu cihaz"]),
      ["Wi-Fi ile eşitleme", d.wifi_sync == null ? null : d.wifi_sync ? "Destekleniyor" : "Yok"],
    ])),
    card("Bölge ve güvenlik", kv([
      ["Dil", h.language], ["Bölge", h.locale], ["Saat dilimi", h.timezone],
      ["Aktivasyon", h.activation === "Activated" ? "Aktif" : h.activation],
      ["Parola", h.passcode == null ? null : h.passcode ? "Ayarlı" : "Yok"],
      ["Operatör paketi", h.carrier],
    ])),
  ].join("");
  box.innerHTML = html;
  $("wallBtn").onclick = () => { const im = $("wallImg"); im.hidden = false; im.src = "/api/wallpaper"; $("wallBtn").remove(); };
  box.querySelectorAll("dd.copy").forEach((dd) => (dd.onclick = () => { navigator.clipboard.writeText(dd.textContent); toast("Kopyalandı"); }));
}

// =====================================================================
// UYGULAMALAR
// =====================================================================
const A = { apps: [] };
window.iSyncViews.apps = async (first) => { if (first) loadApps(); };
$("appsRefresh").onclick = () => loadApps(true);
async function loadApps(refresh) {
  $("appsList").innerHTML = `<div class="muted">Uygulama listesi okunuyor…</div>`;
  const r = refresh ? "&refresh=1" : "";
  try { A.apps = (await api(`/api/apps?sizes=0${r}`)).apps; } catch (e) { $("appsList").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; return; }
  A.sized = false;
  renderApps();
  // Boyutları iPhone hesaplıyor (~1 dk); hazır olunca listeyi güncelle
  try {
    const full = await api(`/api/apps?sizes=1${r}`);
    A.apps = full.apps; A.sized = true;
    renderApps();
  } catch (e) { toast("Uygulama boyutları alınamadı: " + e.message, true); }
}
function renderApps() {
  const qq = $("appsSearch").value.trim().toLowerCase();
  let list = A.apps.filter((a) => (!qq || a.name.toLowerCase().includes(qq) || a.id.toLowerCase().includes(qq)) && (!$("appsSharing").checked || a.sharing));
  const sort = A.sized ? $("appsSort").value : "name";
  const key = { size: "size", data: "data_size", app: "app_size" }[sort];
  if (key) list.sort((a, b) => b[key] - a[key]); else list.sort((a, b) => a.name.localeCompare(b.name, LOCALE));
  const total = A.apps.reduce((s, a) => s + a.size, 0);
  $("appsStats").textContent = `${n(A.apps.length)} uygulama · ${A.sized ? "toplam " + fmtBytes(total) : "boyutlar iPhone'da hesaplanıyor (~1 dk)…"} · ${n(A.apps.filter((a) => a.sharing).length)} tanesinin belgelerine erişilebilir`;
  const sz = (v) => (A.sized ? fmtBytes(v) : "…");
  const max = Math.max(1, ...list.map((a) => a.size));
  $("appsList").innerHTML = `<div class="app head"><div></div><div>Uygulama</div><div class="sz">Uygulama</div><div class="sz opt">Veri</div><div class="sz">Toplam</div><div class="opt"></div></div>` +
    list.map((a) => `<div class="app">
      <img loading="lazy" src="/api/apps/${encodeURIComponent(a.id)}/icon" alt="" onerror="this.style.visibility='hidden'">
      <div class="nm"><b>${escapeHtml(a.name)}</b><span>${escapeHtml(a.version || "")} · ${escapeHtml(a.id)}</span><div class="bar" style="width:${(a.size / max) * 100}%"></div></div>
      <div class="sz muted">${sz(a.app_size)}</div>
      <div class="sz opt">${sz(a.data_size)}</div>
      <div class="sz"><b>${sz(a.size)}</b></div>
      <div class="opt">${a.sharing ? `<button class="btn small" data-docs="${escapeHtml(a.id)}">Dosyalar</button>` : ""}</div>
    </div>`).join("");
}
$("appsSearch").addEventListener("input", debounce(renderApps, 150));
$("appsSort").onchange = renderApps;
$("appsSharing").onchange = renderApps;
$("appsList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-docs]");
  if (b) openDocs(b.dataset.docs, "");
});

const D = { id: "", path: "" };
async function openDocs(id, path) {
  D.id = id; D.path = path;
  const app = A.apps.find((a) => a.id === id);
  $("docs").hidden = false;
  $("docsTitle").textContent = app ? app.name : id;
  $("docsIcon").src = `/api/apps/${encodeURIComponent(id)}/icon`;
  $("docsPath").textContent = "Belgeler/" + path;
  $("docsList").innerHTML = `<div class="muted" style="padding:10px">Okunuyor…</div>`;
  try {
    const r = await api(`/api/apps/${encodeURIComponent(id)}/docs?${q({ path })}`);
    let h = path ? `<div class="doc" data-up="1"><span class="ic">↩</span><span class="nm">..</span></div>` : "";
    if (!r.items.length) h += `<div class="muted" style="padding:10px">Bu klasör boş.</div>`;
    h += r.items.map((it) => {
      const p = (path ? path + "/" : "") + it.name;
      return it.dir
        ? `<div class="doc" data-dir="${escapeHtml(p)}"><span class="ic">📁</span><span class="nm">${escapeHtml(it.name)}</span></div>`
        : `<a class="doc" href="/api/apps/${encodeURIComponent(id)}/file?${q({ path: p })}" download="${escapeHtml(it.name)}" style="color:inherit;text-decoration:none">
             <span class="ic">📄</span><span class="nm">${escapeHtml(it.name)}</span>
             <span class="muted">${fmtBytes(it.size)}${it.mtime ? " · " + new Date(it.mtime * 1000).toLocaleDateString(LOCALE) : ""}</span></a>`;
    }).join("");
    $("docsList").innerHTML = h;
  } catch (e) { $("docsList").innerHTML = `<div class="muted" style="padding:10px">${escapeHtml(e.message)}</div>`; }
}
$("docsList").addEventListener("click", (e) => {
  const d = e.target.closest("[data-dir]"); if (d) { openDocs(D.id, d.dataset.dir); return; }
  if (e.target.closest("[data-up]")) openDocs(D.id, D.path.split("/").slice(0, -1).join("/"));
});
$("docsClose").onclick = () => ($("docs").hidden = true);
$("docs").addEventListener("click", (e) => { if (e.target.id === "docs") $("docs").hidden = true; });

// URL'deki sekmeyle aç (#map, #device, #apps)
{
  const t = location.hash.slice(1);
  if (t && t !== "photos" && document.getElementById("view-" + t)) showTab(t);
}
