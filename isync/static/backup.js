"use strict";
/* iSync — Yedek sekmesi: yedek alma + kişiler, iMessage/SMS, WhatsApp, arama ve Safari geçmişi. */

const B = { pane: "status", summary: null, contacts: null, chats: {}, src: "sms", chat: null, msgs: [], calls: null, safari: null, poll: null };
const fileUrl2 = (a, extra = {}) => `/api/backup/file?${q({ domain: a.domain, path: a.path, ...extra })}`;
const initials = (s) => (s || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "");
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString(LOCALE, { day: "numeric", month: "long", year: "numeric" }) : "");
const fmtDT = (t) => (t ? new Date(t * 1000).toLocaleString(LOCALE) : "");

window.iSyncViews.backup = async (first) => {
  await refreshBackup(); // her girişte güncel durum
};

document.getElementById("bkNav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-pane]");
  if (a) showPane(a.dataset.pane);
});

function showPane(p) {
  B.pane = p;
  document.querySelectorAll("#bkNav a").forEach((a) => a.classList.toggle("active", a.dataset.pane === p));
  const chat = p === "sms" || p === "wa";
  for (const id of ["status", "contacts", "calls", "safari"]) $("pane-" + id).hidden = id !== p;
  $("pane-chat").hidden = !chat;
  $("pane-chat").classList.toggle("wa", p === "wa");
  if (p === "contacts") loadContacts();
  if (chat) loadChats(p);
  if (p === "calls") loadCalls();
  if (p === "safari") loadSafari();
}

// ---------------- durum + yedek alma ----------------
async function refreshBackup() {
  let r;
  try { r = await api("/api/backup/info"); } catch (e) { $("bkInfo").textContent = e.message; return; }
  if (!$("bkRoot").value) $("bkRoot").value = r.root;
  const i = r.info;
  $("bkInfo").innerHTML = i
    ? kv([["Tarih", fmtDT(i.date)], ["Cihaz", i.device], ["iOS", i.ios], ["Konum", i.path, 1], ["Boyut", i.size ? fmtBytes(i.size) : null], ["Şifreli", i.encrypted ? "Evet" : "Hayır"], ["Geri yüklenebilir", i.restorable ? "Evet" : "Hayır"]])
    : RUNNING.includes(r.job.status)
      ? `İlk yedek alınıyor… Bitince kişiler, mesajlar ve WhatsApp soldaki menüden görüntülenebilecek.`
      : `Bu bilgisayarda henüz yedek yok. Sağdaki düğmeyle ilk yedeği alın.`;
  $("bkStart").textContent = i ? "Yedeği güncelle (artımlı)" : "İlk yedeği başlat";
  showJobState(r.job);
  if (i && !RUNNING.includes(r.job.status)) loadSummary();
  if (!RUNNING.includes(r.job.status)) loadRestoreCheck();
  else { $("rsChecks").innerHTML = `<li class="unk">Bir işlem sürüyor…</li>`; $("rsOpen").disabled = true; }
}

const RUNNING = ["starting", "running", "passcode", "finalizing"];
function showJobState(j) {
  const running = RUNNING.includes(j.status);
  const restore = j.mode === "restore";
  B.jobRunning = running;
  $("bkStart").disabled = running;
  $("bkCancel").hidden = !running || restore;  // geri yükleme yarıda kesilmez
  $("bkProg").hidden = !running && j.status !== "done";
  $("bkProg").firstElementChild.style.width = (j.percent || 0) + "%";
  $("bkAlert").hidden = j.status !== "passcode";
  $("bkAlert").textContent = j.message || "";
  const el = j.started ? ((j.finished || Date.now() / 1000) - j.started) : 0;
  let txt = "";
  if (running) {
    const pct = j.percent || 0;
    const eta = pct > 1 && j.status === "running" ? (el / pct) * (100 - pct) : null;
    txt = `${restore ? "Geri yükleniyor" : "Yedekleniyor"}: %${pct.toFixed(1)}`; // i18n: "Yedekleniyor: %{}" kalıbı
    if (!restore) txt += ` · ${n(j.received_files || 0)} dosya alındı`;
    txt += ` · ${fmtDur(el)} geçti${eta ? ` · tahmini ${fmtDur(eta)} kaldı` : ""}`;
    if (j.skipped_files) txt += ` · ${n(j.skipped_files)} fotoğraf kütüphanesi dosyası atlandı`;
    if (j.message && j.status !== "passcode") txt += ` — ${j.message}`;
  } else if (j.status === "done") txt = restore ? j.message : `Yedek tamamlandı (${fmtDur(el)}).`;
  else if (j.status === "error" || j.status === "cancelled") txt = j.message;
  $("bkJob").textContent = txt;
  if (running) { clearTimeout(B.poll); B.poll = setTimeout(pollJobState, 1500); }
}
async function pollJobState() {
  const r = await api("/api/backup/info").catch(() => null);
  if (!r) { B.poll = setTimeout(pollJobState, 3000); return; }
  const wasRunning = B.jobRunning;
  showJobState(r.job);
  if (wasRunning && !RUNNING.includes(r.job.status)) {
    if (r.job.status === "done") toast(r.job.mode === "restore" ? "Geri yükleme tamamlandı" : "iPhone yedeği tamamlandı");
    B.contacts = null; B.chats = {}; B.calls = null; B.safari = null;
    refreshBackup();
  }
}

// ---------------- geri yükleme ----------------
let RS = null;
async function loadRestoreCheck() {
  try { RS = await api("/api/backup/restore-check"); } catch (e) {
    $("rsChecks").innerHTML = `<li class="unk">${escapeHtml(e.message)}</li>`; $("rsOpen").disabled = true; return;
  }
  $("rsChecks").innerHTML = RS.checks.map((c) => `<li class="${c.ok === true ? "ok" : c.ok === false ? "bad" : "unk"}">${escapeHtml(c.text)}</li>`).join("");
  $("rsOpen").disabled = !RS.ok;
}
$("rsOpen").onclick = () => {
  if (!RS) return;
  $("rsName").textContent = RS.device_name || "";
  $("rsSub").textContent = RS.info ? `Yedek tarihi: ${fmtDT(RS.info.date)} · iOS ${RS.info.ios || "?"} → telefondaki iOS ${RS.device_ios || "?"}` : "";
  $("rsConfirm").value = ""; $("rsUnderstood").checked = false; $("rsGo").disabled = true;
  $("rsModal").hidden = false;
  setTimeout(() => $("rsConfirm").focus(), 50);
};
const loose = (s) => (s || "").trim().normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/ı/g, "i").toLowerCase();
const rsValid = () => $("rsUnderstood").checked && RS && loose($("rsConfirm").value) === loose(RS.device_name);
["rsConfirm", "rsUnderstood"].forEach((id) => $(id).addEventListener("input", () => ($("rsGo").disabled = !rsValid())));
$("rsUnderstood").addEventListener("change", () => ($("rsGo").disabled = !rsValid()));
const rsClose = () => ($("rsModal").hidden = true);
$("rsClose").onclick = rsClose; $("rsCancel").onclick = rsClose;
$("rsGo").onclick = async () => {
  if (!rsValid()) return;
  $("rsGo").disabled = true;
  try {
    const st = await api("/api/backup/restore", { confirm_name: $("rsConfirm").value, understood: true, keep_settings: $("rsKeep").checked });
    rsClose();
    showPane("status");
    showJobState(st);
    toast("Geri yükleme başladı — kabloyu çıkarmayın, telefonda parola sorulursa girin");
  } catch (e) { toast(e.message, true); $("rsGo").disabled = !rsValid(); }
};

$("bkStart").onclick = async () => {
  try {
    const st = await api("/api/backup/start", { root: $("bkRoot").value.trim(), skip_photos: $("bkSkipPhotos").checked });
    showJobState(st);
    toast("Yedekleme başladı — iPhone'da parola sorulursa girin");
  } catch (e) { toast(e.message, true); }
};
$("bkCancel").onclick = () => api("/api/backup/cancel", {}).then(showJobState);
$("bkPick").onclick = async () => {
  const r = await api("/api/pick-folder", { initial: $("bkRoot").value });
  if (r.path) $("bkRoot").value = r.path;
};

async function loadSummary() {
  try {
    const s = await api("/api/backup/summary");
    B.summary = s;
    const c = (v) => (v == null ? "yok" : n(v));
    $("bkSummary").innerHTML = kv([
      ["Kişiler", c(s.contacts)], ["iMessage/SMS", s.chats == null ? "yok" : `${n(s.chats)} sohbet · ${n(s.messages)} mesaj`],
      ["WhatsApp", s.wa_chats == null ? "yok" : `${n(s.wa_chats)} sohbet · ${n(s.wa_messages)} mesaj`],
      ["Aramalar", c(s.calls)], ["Safari ziyaretleri", c(s.safari)], ["Yedekteki dosyalar", n(s.files)], ["Uygulama verisi", `${n(s.app_domains)} uygulama`],
    ]);
    // Yedekte olmayan veri türlerini menüde gizle (ör. arama geçmişi sadece şifreli yedekte olur)
    const set = (id, v) => { $(id).textContent = v == null ? "" : n(v); $(id).closest("a").hidden = v == null; };
    set("cntContacts", s.contacts); set("cntSms", s.chats); set("cntWa", s.wa_chats); set("cntCalls", s.calls); set("cntSafari", s.safari);
  } catch (e) { $("bkSummary").textContent = e.message; }
}

// ---------------- kişiler ----------------
async function loadContacts() {
  if (!B.contacts) {
    $("ctList").innerHTML = `<div class="muted">Okunuyor…</div>`;
    try { B.contacts = (await api("/api/backup/contacts")).contacts; } catch (e) { $("ctList").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; return; }
  }
  renderContacts();
}
function renderContacts() {
  const qq = $("ctSearch").value.trim().toLowerCase();
  const digits = qq.replace(/\D/g, "");
  const list = B.contacts.filter((c) => !qq || c.name.toLowerCase().includes(qq) || (c.org || "").toLowerCase().includes(qq) ||
    c.emails.some((e) => e.value.toLowerCase().includes(qq)) || (digits.length > 2 && c.phones.some((p) => p.value.replace(/\D/g, "").includes(digits))));
  $("ctStats").textContent = `${n(B.contacts.length)} kişi${qq ? ` · ${n(list.length)} eşleşme` : ""}`;
  $("ctList").innerHTML = list.slice(0, 1500).map((c) => `
    <div class="ct">
      <div class="av"><img loading="lazy" src="/api/backup/contacts/${c.id}/photo" alt="" onerror="this.replaceWith(document.createTextNode('${escapeHtml(initials(c.name)).replace(/'/g, "")}'))"></div>
      <div class="info">
        <b>${escapeHtml(c.name || "(adsız)")}</b>
        ${c.org ? `<div>${escapeHtml(c.org)}${c.job ? " · " + escapeHtml(c.job) : ""}</div>` : ""}
        ${c.phones.map((p) => `<div><span class="l">${escapeHtml(p.label)}</span>${escapeHtml(p.value)}</div>`).join("")}
        ${c.emails.map((p) => `<div><span class="l">${escapeHtml(p.label)}</span>${escapeHtml(p.value)}</div>`).join("")}
        ${c.addresses.map((p) => `<div>${escapeHtml(p.value)}</div>`).join("")}
        ${c.birthday ? `<div>🎂 ${escapeHtml(String(c.birthday).slice(0, 10))}</div>` : ""}
      </div>
    </div>`).join("") + (list.length > 1500 ? `<div class="muted">… ilk 1500 gösteriliyor, aramayı daraltın.</div>` : "");
}
$("ctSearch").addEventListener("input", debounce(renderContacts, 150));

// ---------------- sohbetler ----------------
async function loadChats(src) {
  B.src = src;
  $("thTitle").textContent = "Bir sohbet seçin"; $("thSub").textContent = ""; $("thBody").innerHTML = ""; $("thExport").hidden = true;
  if (!B.chats[src]) {
    $("chList").innerHTML = `<div class="muted" style="padding:12px">Okunuyor…</div>`;
    try { B.chats[src] = (await api(`/api/backup/chats?src=${src}`)).chats; } catch (e) { $("chList").innerHTML = `<div class="muted" style="padding:12px">${escapeHtml(e.message)}</div>`; return; }
  }
  renderChats();
}
function renderChats() {
  const qq = $("chSearch").value.trim().toLowerCase();
  const list = B.chats[B.src].filter((c) => !qq || c.title.toLowerCase().includes(qq) || (c.handles || [c.jid]).join(" ").includes(qq));
  if (!list.length) { $("chList").innerHTML = `<div class="muted" style="padding:12px">${B.chats[B.src].length ? "Eşleşme yok" : "Bu yedekte sohbet bulunamadı."}</div>`; return; }
  $("chList").innerHTML = list.map((c) => `
    <div class="chi${B.chat && B.chat.id === c.id ? " active" : ""}" data-id="${c.id}">
      <div class="av">${c.group ? "👥" : escapeHtml(initials(c.title))}</div>
      <div class="nm"><b>${escapeHtml(c.title)}</b><span>${fmtDate(c.last)} · ${n(c.count)} mesaj${c.service ? " · " + escapeHtml(c.service) : ""}</span></div>
    </div>`).join("");
}
$("chSearch").addEventListener("input", debounce(renderChats, 150));
$("chList").addEventListener("click", (e) => {
  const el = e.target.closest(".chi");
  if (!el) return;
  const c = B.chats[B.src].find((x) => x.id === +el.dataset.id);
  openChat(c);
});

async function openChat(c) {
  B.chat = c; B.msgs = [];
  renderChats();
  $("thTitle").textContent = c.title;
  $("thSub").textContent = `${n(c.count)} mesaj${c.handles ? " · " + c.handles.slice(0, 5).join(", ") : c.jid ? " · " + c.jid.split("@")[0] : ""}`;
  $("thExport").hidden = false;
  $("thBody").innerHTML = `<div class="muted">Okunuyor…</div>`;
  await loadOlder(true);
}
async function loadOlder(initial) {
  const c = B.chat;
  const before = B.msgs.length ? B.msgs[0].t : 0;
  const r = await api(`/api/backup/messages?${q({ src: B.src, chat: c.id, before, limit: 300 })}`);
  if (B.chat !== c) return;
  const body = $("thBody");
  const prevH = body.scrollHeight;
  B.msgs = r.messages.concat(B.msgs);
  renderThread(r.messages.length === 300);
  B.stick = !!initial;  // ilk açılışta en altta kal (resimler yüklendikçe yükseklik artar)
  if (initial) body.scrollTop = body.scrollHeight;
  else body.scrollTop = body.scrollHeight - prevH;
}
function attHtml(a) {
  const m = a.mime || "";
  if (m.startsWith("image/")) {
    const heic = /heic|heif/i.test(m) || /\.hei[cf]$/i.test(a.path);
    return `<img class="att" loading="lazy" src="${fileUrl2(a, heic ? { preview: 2 } : {})}" data-full="${fileUrl2(a, heic ? { preview: 1 } : {})}" data-dl="${fileUrl2(a, { download: 1 })}" data-name="${escapeHtml(a.name)}" alt="" onerror="this.outerHTML='<a class=&quot;file&quot;>🖼 ${escapeHtml(a.name).replace(/'/g, "")} (yedekte yok)</a>'">`;
  }
  if (m.startsWith("video/")) return `<video controls preload="none" src="${fileUrl2(a)}"></video>`;
  if (m.startsWith("audio/")) return `<audio controls preload="none" src="${fileUrl2(a)}"></audio>`;
  return `<a class="file" href="${fileUrl2(a, { download: 1 })}" download>📎 ${escapeHtml(a.name)} ${a.size ? "· " + fmtBytes(a.size) : ""}</a>`;
}
function renderThread(more) {
  let h = more ? `<button class="btn small older" id="thOlder">Daha eski mesajlar</button>` : "";
  let day = "";
  for (const m of B.msgs) {
    const d = fmtDate(m.t);
    if (d !== day) { day = d; h += `<div class="day">${d}</div>`; }
    h += `<div class="msg${m.me ? " me" : ""}">${!m.me && B.chat.group && m.from ? `<div class="from">${escapeHtml(m.from)}</div>` : ""}${m.att.map(attHtml).join("")}${m.text ? escapeHtml(m.text) : ""}<span class="tm">${fmtTime(m.t)}</span></div>`;
  }
  $("thBody").innerHTML = h || `<div class="muted">Mesaj yok.</div>`;
  const o = $("thOlder"); if (o) o.onclick = () => loadOlder(false);
}
// Resimler yüklendikçe, kullanıcı yukarı kaydırmadıysa en altta tut
$("thBody").addEventListener("load", () => { if (B.stick) $("thBody").scrollTop = $("thBody").scrollHeight; }, true);
$("thBody").addEventListener("wheel", () => { B.stick = false; }, { passive: true });
$("thBody").addEventListener("mousedown", () => { B.stick = false; });
// sohbetteki fotoğraflara tıklayınca ortak önizleyiciyi aç
$("thBody").addEventListener("click", (e) => {
  const img = e.target.closest("img.att");
  if (!img) return;
  const imgs = [...$("thBody").querySelectorAll("img.att")];
  const src = {
    len: () => imgs.length,
    item: (i) => imgs[i] && { name: imgs[i].dataset.name, meta: B.chat.title, img: imgs[i].dataset.full, download: imgs[i].dataset.dl },
  };
  openViewer(src, imgs.indexOf(img));
});
$("thExport").onclick = async () => {
  $("thExport").disabled = true;
  try {
    const r = await api("/api/backup/export-chat", { src: B.src, chat: B.chat.id, title: B.chat.title });
    toast("Dışa aktarıldı: " + r.path);
    api("/api/open-folder", { path: r.path }).catch(() => {});
  } catch (e) { toast(e.message, true); } finally { $("thExport").disabled = false; }
};

// ---------------- aramalar ----------------
async function loadCalls() {
  if (!B.calls) {
    $("clList").innerHTML = `<div class="muted">Okunuyor…</div>`;
    try { B.calls = (await api("/api/backup/calls")).calls; } catch (e) { $("clList").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; return; }
  }
  renderCalls();
}
function renderCalls() {
  const qq = $("clSearch").value.trim().toLowerCase();
  const list = B.calls.filter((c) => !qq || (c.who || "").toLowerCase().includes(qq) || (c.number || "").includes(qq));
  $("clStats").textContent = B.calls.length ? `${n(B.calls.length)} arama` : "Bu yedekte arama geçmişi yok (iOS bunu sadece şifreli yedeklere koyuyor olabilir).";
  $("clList").innerHTML = list.slice(0, 3000).map((c) => {
    const icon = c.out ? "↗" : c.answered ? "↙" : "✕";
    return `<div class="tr"><div class="${!c.out && !c.answered ? "miss" : ""}">${icon}</div>
      <div><div>${escapeHtml(c.who || "Bilinmeyen")}</div><div class="sub">${escapeHtml(c.number || "")}</div></div>
      <div>${fmtDT(c.t)}</div><div>${c.dur ? fmtDur(c.dur) : "–"}</div><div class="sub">${escapeHtml(c.kind)}</div></div>`;
  }).join("");
}
$("clSearch").addEventListener("input", debounce(renderCalls, 150));

// ---------------- safari ----------------
async function loadSafari() {
  if (!B.safari) {
    $("sfList").innerHTML = `<div class="muted">Okunuyor…</div>`;
    try { B.safari = (await api("/api/backup/safari")).visits; } catch (e) { $("sfList").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`; return; }
  }
  renderSafari();
}
function renderSafari() {
  const qq = $("sfSearch").value.trim().toLowerCase();
  const list = B.safari.filter((v) => !qq || (v.title || "").toLowerCase().includes(qq) || v.url.toLowerCase().includes(qq));
  $("sfStats").textContent = B.safari.length ? `son ${n(B.safari.length)} ziyaret` : "Bu yedekte Safari geçmişi yok.";
  $("sfList").innerHTML = list.slice(0, 2000).map((v) => `<div class="tr sf">
    <div style="min-width:0"><div class="sub" style="color:var(--text)">${escapeHtml(v.title || v.url)}</div><div class="sub"><a href="${escapeHtml(v.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(v.url)}</a></div></div>
    <div class="sub">${fmtDT(v.t)}</div></div>`).join("");
}
$("sfSearch").addEventListener("input", debounce(renderSafari, 150));

// #backup ile açıldıysa
if (location.hash.slice(1) === "backup") showTab("backup");
