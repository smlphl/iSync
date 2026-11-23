"use strict";
/* iSync çeviri motoru.
 * Kaynak dil Türkçe; anahtarlar arayüzdeki Türkçe metinlerin kendisidir (static/i18n/<dil>.json).
 * "{}" içeren anahtarlar kalıptır: değişken kısımlar yakalanır ve çevirinin içine yerleştirilir.
 * Ekrandaki metinler (sonradan eklenenler ve sunucu mesajları dahil) otomatik çevrilir. */
(function () {
  const LANGS = { tr: "Türkçe", en: "English", zh: "中文" };
  const LOCALES = { tr: "tr-TR", en: "en-US", zh: "zh-CN" };
  const nav = (navigator.language || "").toLowerCase();
  const qp = new URLSearchParams(location.search).get("lang");
  if (qp && LANGS[qp]) localStorage.setItem("isync.lang", qp);
  let lang = localStorage.getItem("isync.lang") || (nav.startsWith("tr") ? "tr" : nav.startsWith("zh") ? "zh" : "en");
  if (!LANGS[lang]) lang = "en";

  window.LANG = lang;
  /** Yüzde: Türkçe "%93", diğerleri "93%" */
  window.pct = (v) => (lang === "tr" ? `%${v}` : `${v}%`);
  window.LOCALE = LOCALES[lang];
  document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;

  const exact = new Map();
  const tmpl = new Map(); // {} içeren anahtarlar: anahtar -> çeviri şablonu
  const patterns = [];
  const cache = new Map();
  if (lang !== "tr") {
    try {
      const x = new XMLHttpRequest();
      x.open("GET", `/static/i18n/${lang}.json`, false); // küçük yerel dosya; titremesiz ilk çizim için eşzamanlı
      x.send();
      const dict = JSON.parse(x.responseText);
      const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      for (const [k, v] of Object.entries(dict)) {
        if (k.startsWith("_")) continue;
        if (k.includes("{}")) {
          const parts = k.split("{}");
          const anchor = parts.reduce((a, b) => (b.length > a.length ? b : a), "");
          patterns.push({ re: new RegExp("^" + parts.map(esc).join("([\\s\\S]+?)") + "$"), anchor, out: v, n: parts.length - 1 });
          tmpl.set(k, v);
        } else exact.set(k, v);
      }
      patterns.sort((a, b) => b.anchor.length - a.anchor.length);
    } catch (e) { console.warn("i18n yüklenemedi", e); }
  }

  function fill(tpl, vals) {
    let i = 0;
    return tpl.replace(/\{(\d*)\}/g, (_, d) => {
      const v = vals[d === "" ? i++ : +d];
      return v == null ? "" : v;
    });
  }
  function lookup(core) {
    if (exact.has(core)) return exact.get(core);
    for (const p of patterns) {
      if (p.anchor && !core.includes(p.anchor)) continue;
      const m = p.re.exec(core);
      if (m) return fill(p.out, m.slice(1).map((v) => {
        const c = v.trim();
        const r = exact.get(c) ?? (c && HAS_LETTER.test(c) ? lookup(c) : null);
        return r == null ? v : v.replace(c, r); // boşlukları koru
      }));
    }
    return null;
  }
  const HAS_LETTER = /[A-Za-zÇĞİÖŞÜçğıöşü]/;
  function tr(s) {
    if (lang === "tr" || !s || !HAS_LETTER.test(s)) return s;
    if (cache.has(s)) return cache.get(s);
    const core = s.trim();
    const key = core.replace(/\s+/g, " "); // HTML'deki çoklu boşluk/satır sonları anahtarla eşleşsin
    let r = lookup(key);
    if (r == null) {
      // " · " ve " — " ile birleştirilmiş metinleri parça parça çevir
      for (const sep of [" · ", " — "]) {
        if (key.includes(sep)) {
          const parts = key.split(sep);
          const out = parts.map((p) => lookup(p.trim()) ?? p);
          if (out.some((o, i) => o !== parts[i])) { r = out.join(sep); break; }
        }
      }
    }
    const res = r == null ? s : s.replace(core, r);
    if (cache.size > 5000) cache.clear();
    cache.set(s, res);
    return res;
  }
  /** Kodda açıkça kullanmak için: t("{} öğe", [5]) */
  window.t = (key, vals) => {
    const tpl = lang === "tr" ? key : (exact.get(key) ?? tmpl.get(key) ?? key);
    return vals ? fill(tpl, vals) : tpl;
  };
  window.trText = tr;

  if (lang === "tr") return setupPicker();

  const ATTRS = ["placeholder", "title", "alt", "aria-label"];
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA"]);
  function doText(node) {
    const p = node.parentNode;
    if (!p || SKIP.has(p.nodeName)) return;
    const v = node.nodeValue, nv = tr(v);
    if (nv !== v) node.nodeValue = nv;
  }
  function doEl(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) { const nv = tr(v); if (nv !== v) el.setAttribute(a, nv); }
    }
  }
  function walk(root) {
    if (root.nodeType === 3) return doText(root);
    if (root.nodeType !== 1 || SKIP.has(root.nodeName)) return;
    doEl(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) n.nodeType === 3 ? doText(n) : doEl(n);
  }
  walk(document.body);
  document.title = tr(document.title);
  new MutationObserver((recs) => {
    for (const r of recs) {
      if (r.type === "childList") r.addedNodes.forEach(walk);
      else if (r.type === "characterData") doText(r.target);
      else if (r.type === "attributes") doEl(r.target);
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  setupPicker();

  function setupPicker() {
    const host = document.querySelector(".top-actions");
    if (!host || document.getElementById("langSel")) return;
    const sel = document.createElement("select");
    sel.id = "langSel";
    sel.className = "lang-sel";
    sel.title = "Language / Dil / 语言";
    for (const [k, v] of Object.entries(LANGS)) sel.add(new Option(v, k, false, k === lang));
    sel.onchange = () => {
      localStorage.setItem("isync.lang", sel.value);
      fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lang: sel.value }) })
        .finally(() => location.reload());
    };
    host.prepend(sel);
  }
})();
