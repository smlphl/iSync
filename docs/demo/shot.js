"use strict";
/* Demo ekran görüntüleri: ?shot=<ad> ile sayfayı ilgili duruma getirir ve hazır olunca <html data-ready> koyar. */
(async () => {
  const shot = new URLSearchParams(location.search).get("shot");
  if (!shot) return;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (fn, ms = 15000) => { const t = Date.now(); while (!fn() && Date.now() - t < ms) await sleep(100); };
  const done = () => document.documentElement.setAttribute("data-ready", "1");

  if (shot === "wizard") { await sleep(3500); return done(); }
  await until(() => S.groups.length > 0);
  await sleep(400);

  if (shot === "photos" || shot === "jobcard") {
    S.list.slice(2, 9).forEach((g) => S.selected.add(g.id));
    S.list.slice(12, 15).forEach((g) => S.selected.add(g.id));
    selChanged();
    await sleep(2500);
  } else if (shot === "lightbox") {
    openLightbox(5);
    await sleep(2500);
  } else if (shot === "map") {
    showTab("map");
    await until(() => M.map && M.data && M.cluster);
    await sleep(800);
    M.map.setView([39.0, 32.0], 6);
    await sleep(6000);
  } else if (shot === "device") {
    showTab("device");
    await sleep(2500);
  } else if (shot === "apps") {
    showTab("apps");
    await sleep(2500);
  } else if (shot === "contacts") {
    showTab("backup"); await sleep(800); showPane("contacts"); await sleep(1500);
  } else if (shot === "whatsapp" || shot === "messages") {
    showTab("backup"); await sleep(800);
    showPane(shot === "whatsapp" ? "wa" : "sms"); await sleep(1200);
    document.querySelectorAll(".chi")[0].click();
    await sleep(2500);
  } else if (shot === "backup") {
    showTab("backup"); await sleep(2500);
  }
  done();
})();
