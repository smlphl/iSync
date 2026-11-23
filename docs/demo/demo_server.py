"""iSync demo sunucusu — README ekran görüntüleri için SAHTE verilerle çalışır.

Gerçek iPhone, fotoğraf, kişi ya da mesaj kullanılmaz: tüm görseller programla çizilir,
isimler ve mesajlar uydurmadır. Aynı arayüz (isync/static) sunulur.

    python docs/demo/demo_server.py [--port 8790] [--wizard]

Ekran görüntüsü için sayfaya ?shot=<ad> eklenir (docs/demo/shot.js eylemleri yapar).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import random
import sys
import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "isync" / "static"
DEMO = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from isync import __version__  # noqa: E402

app = FastAPI()
ARGS = argparse.Namespace(wizard=False)
NOW = time.time()
DAY = 86400

# ------------------------------------------------------------------ görsel üretici
PALETTES = [
    ((255, 170, 110), (120, 80, 160), (40, 30, 70)),     # gün batımı
    ((140, 200, 255), (210, 235, 255), (60, 110, 80)),   # açık gökyüzü
    ((255, 210, 150), (255, 140, 90), (90, 60, 50)),     # altın saat
    ((30, 40, 90), (90, 70, 140), (20, 20, 40)),         # gece
    ((170, 220, 240), (240, 245, 250), (70, 100, 130)),  # kış
    ((120, 190, 230), (190, 230, 245), (30, 90, 120)),   # deniz
]


def _grad(d: ImageDraw.ImageDraw, w: int, h: int, top, bot, y0=0, y1=None):
    y1 = y1 or h
    for y in range(y0, y1):
        t = (y - y0) / max(1, y1 - y0)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))


@lru_cache(maxsize=4096)
def picture(seed: str, w: int = 360, h: int = 360) -> bytes:
    rnd = random.Random(seed)
    sky1, sky2, land = rnd.choice(PALETTES)
    kind = rnd.choice(["mountain", "mountain", "sea", "city", "hills"])
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    horizon = int(h * rnd.uniform(0.5, 0.7))
    _grad(d, w, h, sky1, sky2, 0, horizon)
    # güneş / ay
    r = rnd.randint(w // 14, w // 8)
    cx, cy = rnd.randint(r, w - r), rnd.randint(r, int(horizon * 0.7))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 245, 220) if sky1 != (30, 40, 90) else (235, 235, 255))
    if kind in ("mountain", "hills"):
        for layer in range(3):
            base = horizon + layer * h // 12
            amp = (h // 3 if kind == "mountain" else h // 10) // (layer + 1)
            col = tuple(max(0, min(255, int(land[i] * (0.6 + 0.25 * layer) + sky2[i] * 0.15))) for i in range(3))
            pts = [(0, h)]
            phase = rnd.random() * 6
            for x in range(0, w + 8, 8):
                y = base - amp * (0.5 + 0.5 * math.sin(x / (w / (2 + layer)) + phase)) - rnd.randint(0, amp // 6 + 1)
                pts.append((x, y))
            pts.append((w, h))
            d.polygon(pts, fill=col)
    elif kind == "sea":
        _grad(d, w, h, tuple(int(c * 0.7) for c in sky2), land, horizon, h)
        for _ in range(40):
            y = rnd.randint(horizon + 4, h - 2)
            x = rnd.randint(0, w)
            d.line([(x, y), (x + rnd.randint(8, 30), y)], fill=(255, 255, 255), width=1)
        d.rectangle([cx - r // 3, horizon, cx + r // 3, h], fill=tuple(min(255, c + 40) for c in land))
    else:  # şehir silüeti
        _grad(d, w, h, land, tuple(int(c * 0.5) for c in land), horizon, h)
        x = 0
        while x < w:
            bw = rnd.randint(w // 18, w // 8)
            bh = rnd.randint(h // 8, int(horizon * 0.85))
            col = tuple(int(c * rnd.uniform(0.35, 0.6)) for c in sky2)
            d.rectangle([x, horizon - bh, x + bw, h], fill=col)
            for wy in range(horizon - bh + 6, horizon, 10):
                for wx in range(x + 4, x + bw - 4, 8):
                    if rnd.random() < 0.35:
                        d.rectangle([wx, wy, wx + 3, wy + 4], fill=(255, 220, 140))
            x += bw + rnd.randint(1, 4)
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


@lru_cache(maxsize=512)
def icon(name: str) -> bytes:
    h = int(hashlib.md5(name.encode()).hexdigest()[:6], 16)
    col = ((h >> 16) & 255, (h >> 8) & 255, h & 255)
    col = tuple(int(60 + c * 0.7) for c in col)
    im = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 119, 119], radius=27, fill=col)
    d.ellipse([34, 34, 86, 86], fill=(255, 255, 255, 220))
    d.ellipse([48, 48, 72, 72], fill=col)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ sahte kütüphane
rnd = random.Random(7)
ASSETS = []
for i in range(1200):
    t = NOW - i * DAY * 0.85 - rnd.randint(0, 40000)
    k = "v" if rnd.random() < 0.1 else "p"
    ext = "MOV" if k == "v" else rnd.choice(["HEIC", "HEIC", "HEIC", "JPG", "PNG"])
    ASSETS.append({"id": i, "dir": f"{100 + i // 999}APPLE", "name": f"IMG_{9999 - i:04d}.{ext}", "ext": ext,
                   "size": rnd.randint(1_500_000, 4_500_000) if k == "p" else rnd.randint(20_000_000, 180_000_000),
                   "t": int(t), "k": k, "l": int(k == "p" and rnd.random() < 0.18), "s": int(ext == "PNG"),
                   "f": int(rnd.random() < 0.06), "h": 0, "x": 0, "e": int(rnd.random() < 0.08),
                   "a": [1] if i % 7 == 0 else ([2] if i % 11 == 0 else [])})
ALBUMS = [{"id": 1, "title": "Tatil 2026", "count": sum(1 for a in ASSETS if 1 in a["a"])},
          {"id": 2, "title": "Aile", "count": sum(1 for a in ASSETS if 2 in a["a"])},
          {"id": 3, "title": "Kamp", "count": 0}]

PLACES = [("İstanbul", 41.01, 28.97), ("İzmir", 38.42, 27.14), ("Antalya", 36.89, 30.71), ("Kapadokya", 38.64, 34.83),
          ("Bodrum", 37.03, 27.43), ("Trabzon", 41.0, 39.72), ("Paris", 48.85, 2.35), ("Roma", 41.9, 12.49),
          ("Tokyo", 35.68, 139.69), ("Ankara", 39.93, 32.85)]
GEO = []
for i in range(900):
    name, la, lo = PLACES[min(int(rnd.expovariate(0.45)), len(PLACES) - 1)]
    GEO.append([i, round(la + rnd.gauss(0, 0.05), 5), round(lo + rnd.gauss(0, 0.07), 5),
                int(NOW - i * DAY * 1.1), "v" if rnd.random() < 0.08 else "p"])
GEO_NAMES = [f"{time.strftime('%Y', time.localtime(p[3]))}/{time.strftime('%Y-%m', time.localtime(p[3]))}/IMG_{5000 + p[0]}.HEIC"
             for p in GEO]

FIRST = ["Ayşe", "Mehmet", "Zeynep", "Can", "Elif", "Burak", "Selin", "Emre", "Deniz", "Mert", "Ece", "Kerem", "Defne",
         "Ali", "Naz", "Oğuz", "Ceren", "Tolga", "İrem", "Kaan"]
LAST = ["Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Arslan", "Doğan", "Koç", "Aydın", "Öztürk"]
CONTACTS = []
for i in range(160):
    f, l = rnd.choice(FIRST), rnd.choice(LAST)
    CONTACTS.append({"id": i, "name": f"{f} {l}", "first": f, "last": l, "org": rnd.choice([None, None, "Örnek A.Ş.", "Demo Ltd."]),
                     "job": None, "nick": None, "note": None, "birthday": None,
                     "phones": [{"label": "Cep", "value": f"+90 5{rnd.randint(30, 59)} 000 {rnd.randint(10, 99)} {rnd.randint(10, 99)}"}],
                     "emails": [{"label": "", "value": f"{f.lower()}.{l.lower()}@example.com"
                                 .replace("ş", "s").replace("ç", "c").replace("ğ", "g").replace("ı", "i").replace("ö", "o").replace("ü", "u").replace("i̇", "i")}]
                     if rnd.random() < 0.4 else [], "addresses": [], "urls": []})
CONTACTS.sort(key=lambda c: c["name"])

WA_LINES = [("Hafta sonu kampa gidiyor muyuz? 🏕️", False), ("Evet! Cumartesi sabah 8'de çıkalım", True),
            ("Çadırı ben getiririm", False), ("Süper, ben de ocağı alırım 🔥", True), ("Fotoğraflar harika olmuş", False),
            ("Teşekkürler! Hepsini iSync ile bilgisayara aldım 😄", True), ("Bana da atar mısın?", False),
            ("Tabii, şimdi gönderiyorum", True), ("Manzaraya bak!", False), ("Bir dahaki sefere göle gidelim", True)]


def chat_messages(src: str, chat: int):
    r = random.Random(f"{src}{chat}")
    out = []
    t = NOW - 3 * DAY
    for j in range(40):
        line, me = WA_LINES[j % len(WA_LINES)]
        t += r.randint(60, 4000)
        att = []
        if j % 9 == 4:
            att = [{"domain": "demo", "path": f"chat/{src}/{chat}/{j}.jpg", "mime": "image/jpeg", "name": f"IMG_{j}.jpg", "size": 240000}]
        out.append({"id": j, "me": me, "t": t, "from": None if me else (r.choice(FIRST) if chat % 3 == 0 else None),
                    "text": line if not att else "", "att": att})
    return out


def chats(src: str):
    r = random.Random(src)
    titles = ["Kamp Ekibi", "Aile 👨‍👩‍👧", "Ayşe Yılmaz", "Mehmet Kaya", "İş Grubu", "Zeynep Demir", "Can Şahin",
              "Elif Çelik", "Komşular", "Burak Arslan", "Selin Doğan", "Futbol"]
    return [{"id": i, "title": tt, "group": tt in ("Kamp Ekibi", "Aile 👨‍👩‍👧", "İş Grubu", "Komşular", "Futbol"),
             "last": NOW - i * DAY * r.uniform(0.2, 2), "count": r.randint(40, 9000),
             "service": "iMessage" if src == "sms" else None, "handles": [f"+90 5xx 000 00 {i:02d}"] if src == "sms" else None,
             "jid": f"9050000000{i:02d}@s.whatsapp.net" if src == "wa" else None} for i, tt in enumerate(titles)]


APPS = [("WhatsApp", 9.1e9), ("YouTube", 2.4e9), ("Instagram", 1.9e9), ("Spotify", 1.6e9), ("Google Maps", 1.2e9),
        ("Netflix", 1.1e9), ("Telegram", 0.9e9), ("Chrome", 0.6e9), ("Pages", 0.5e9), ("Numbers", 0.45e9),
        ("Duolingo", 0.4e9), ("Notion", 0.35e9), ("VLC", 0.3e9), ("Strava", 0.25e9), ("Shazam", 0.12e9)]

JOB = {"started": NOW}


# ------------------------------------------------------------------ API
def jpg(b: bytes) -> Response:
    return Response(b, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/info")
def info():
    return {"app": "iSync", "version": __version__, "settings": {
        "dest": r"C:\Users\demo\Pictures\iPhone", "layout": "month", "skip_existing": True, "include_sidecar": False,
        "include_live_video": True, "convert_heic": "jpg", "include_edited": False}}


DEV = {"udid": "DEMO-0000", "name": "Demo iPhone", "model": "iPhone 15 Pro Max", "ios": "26.5", "connection": "USB", "battery": 86}


@app.get("/api/devices")
def devices():
    return {"devices": [] if ARGS.wizard else [DEV]}


@app.post("/api/connect")
def connect():
    if ARGS.wizard:
        return JSONResponse({"error": "iPhone kilitli"}, status_code=503)
    return {**DEV, "storage": {"total": 256e9, "free": 61e9}}


@app.get("/api/diagnose")
def diagnose():
    steps = [{"id": "windows", "title": "Windows sürümü", "status": "ok", "detail": ""},
             {"id": "driver", "title": "Apple iPhone sürücüsü", "status": "ok", "detail": "Apple Mobile Device Service kurulu."},
             {"id": "service", "title": "Apple bağlantı servisi", "status": "ok", "detail": "Çalışıyor."},
             {"id": "usb", "title": "iPhone kabloyla bağlı", "status": "ok", "detail": "iPhone (USB)"},
             {"id": "trust", "title": "iPhone'da 'Güven'e dokunun", "status": "wait",
              "detail": "iPhone ekranında 'Bu Bilgisayara Güvenilsin mi?' sorusu bekliyor. 'Güven'e dokunup parolanızı girin."}]
    return {"ok": not ARGS.wizard, "steps": steps, "driver": {"status": "idle"}}


@app.get("/api/library")
@app.post("/api/scan")
def library():
    return {"udid": "DEMO-0000", "scan_time": NOW, "db_loaded": True, "albums": ALBUMS, "assets": ASSETS, "scan_seconds": 3.9}


@app.get("/api/thumb/{i}")
def thumb(i: int):
    return jpg(picture(f"a{i}"))


@app.get("/api/preview/{i}")
def preview(i: int):
    return jpg(picture(f"a{i}", 1600, 1200))


@app.post("/api/new-count")
def new_count():
    ids = [a["id"] for a in ASSETS[:86]]
    return {"ids": ids, "count": len(ids), "bytes": sum(ASSETS[i]["size"] for i in ids)}


def job_json():
    total = 4_120_000_000
    done = int(total * 0.62)  # ekran görüntüsü için sabit, gerçekçi bir an
    el = done / 36.2e6
    return {"id": "demo", "status": "running", "message": "", "total_files": 1240, "total_bytes": total,
            "done_files": int(1240 * done / total), "done_bytes": done, "skipped": 312, "converted": 140,
            "errors": [], "error_count": 0, "current": ["2026/2026-11/IMG_9871.HEIC", "2026/2026-11/IMG_9872.MOV"],
            "speed": 36.2e6, "eta": (total - done) / 36.2e6, "elapsed": el, "dest": r"C:\Users\demo\Pictures\iPhone"}


@app.get("/api/jobs")
def jobs(request: Request):
    return {"jobs": [job_json()] if "jobcard" in request.headers.get("referer", "") else []}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    return job_json()


@app.post("/api/ping")
@app.post("/api/settings")
def ok():
    return {"ok": True}


@app.get("/api/db/status")
def db_status():
    return {"status": "idle"}


@app.get("/api/device/details")
def details():
    return {"hardware": {"name": "Demo iPhone", "model": "iPhone 15 Pro Max", "product_type": "iPhone16,2",
                         "model_number": "XXXX0TU/A", "hardware_model": "D84AP", "color": "Doğal Titanyum", "ios": "26.5",
                         "build": "23F77", "serial": "DEMO000000", "imei": "35000000000000 (örnek)", "imei2": None,
                         "udid": "00000000-DEMO", "wifi_mac": "00:00:5e:00:53:01", "bt_mac": "00:00:5e:00:53:02",
                         "baseband": "3.10.01", "activation": "Activated", "passcode": True, "timezone": "Europe/Istanbul",
                         "language": "tr-TR", "locale": "tr_TR"},
            "battery": {"level": 86, "charging": True, "cycles": 214, "design_mah": 4422, "full_mah": 4113, "health": 93.0,
                        "health_raw": 93.0, "temp_c": 29.4, "voltage": 4.31, "avg_temp_c": 24, "max_temp_c": 41.2,
                        "adapter": "pd charger", "adapter_watts": 20},
            "storage": {"total": 256e9, "data_total": 247e9, "data_free": 61e9, "data_purgeable_free": 92e9, "system": 8.3e9},
            "backup": {"icloud": True, "last_icloud": NOW - 6 * 3600, "encrypted_local": False},
            "sync_sources": {"Contacts": ["iCloud"], "Notes": ["iCloud"]}, "wifi_sync": True}


@app.get("/api/wallpaper")
def wallpaper():
    return jpg(picture("wall", 600, 1200))


@app.get("/api/apps")
def apps():
    out = []
    for i, (n, sz) in enumerate(APPS):
        data = sz * 0.7
        out.append({"id": f"com.demo.{n.lower().replace(' ', '')}", "name": n, "version": f"{1 + i % 9}.{i}.0",
                    "app_size": int(sz - data), "data_size": int(data), "size": int(sz),
                    "sharing": n in ("VLC", "Pages", "Numbers", "Chrome"), "sized": True})
    return {"apps": out, "sized": True}


@app.get("/api/apps/{bid}/icon")
def app_icon(bid: str):
    return Response(icon(bid), media_type="image/png")


@app.get("/api/geo/points")
def geo_points():
    return {"root": r"C:\Users\demo\Pictures\iPhone", "count": 1240, "no_gps": 340, "points": GEO, "names": GEO_NAMES,
            "state": {"status": "done"}}


@app.get("/api/geo/status")
def geo_status():
    return {"status": "done"}


@app.get("/api/geo/thumb/{i}")
def geo_thumb(i: int):
    return jpg(picture(f"g{i}"))


@app.get("/api/geo/file/{i}")
def geo_file(i: int):
    return jpg(picture(f"g{i}", 1600, 1200))


BINFO = {"path": r"C:\Users\demo\iSync Yedekleri\DEMO-0000", "udid": "DEMO-0000", "date": NOW - 2 * 3600,
         "device": "Demo iPhone", "ios": "26.5", "encrypted": False, "skip_photos": True, "size": 21.4e9, "restorable": True}


@app.get("/api/backup/info")
def backup_info():
    return {"root": r"C:\Users\demo\iSync Yedekleri", "info": BINFO, "job": {"status": "idle"}}


@app.get("/api/backup/summary")
def backup_summary():
    return {"contacts": len(CONTACTS), "chats": 12, "messages": 15323, "wa_chats": 12, "wa_messages": 206908,
            "calls": None, "safari": None, "files": 100038, "app_domains": 363}


@app.get("/api/backup/restore-check")
def restore_check():
    return {"ok": False, "device_name": "Demo iPhone", "info": BINFO, "device_ios": "26.5", "checks": [
        {"ok": True, "text": "Bu iPhone'a ait bir yedek var"}, {"ok": True, "text": "Yedek şifresiz"},
        {"ok": True, "text": "Yedek eksiksiz ve tutarlı"},
        {"ok": False, "text": "iPhone'umu Bul AÇIK — Ayarlar › [adınız] › Bul › iPhone'umu Bul'u kapatın"},
        {"ok": True, "text": "Başka yedek/geri yükleme işlemi yok"}, {"ok": True, "text": "Fotoğraf kopyalama sürmüyor"}]}


@app.get("/api/backup/contacts")
def backup_contacts():
    return {"contacts": CONTACTS}


@app.get("/api/backup/contacts/{rid}/photo")
def contact_photo(rid: int):
    return Response(status_code=404)


@app.get("/api/backup/chats")
def backup_chats(src: str = "sms"):
    return {"chats": chats(src)}


@app.get("/api/backup/messages")
def backup_messages(chat: int, src: str = "sms", before: float = 0):
    return {"messages": [] if before else chat_messages(src, chat)}


@app.get("/api/backup/file")
def backup_file(path: str, preview: int = 0):
    return jpg(picture(path, 640, 480))


# ------------------------------------------------------------------ sayfa
@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('<script src="/static/backup.js"></script>',
                        '<script src="/static/backup.js"></script>\n<script src="/demo/shot.js"></script>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/demo", StaticFiles(directory=DEMO), name="demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--wizard", action="store_true", help="Telefon bağlı değilmiş gibi davran (bağlantı yardımcısı)")
    ARGS = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port, log_level="warning")
