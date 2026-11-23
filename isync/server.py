"""iSync web sunucusu (FastAPI). Arayüz tarayıcıda / uygulama penceresinde çalışır."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from urllib.parse import quote

from . import APP_NAME, __version__
from . import devinfo, diag
from .i18n import tr as i18n_tr
from .device import DeviceError, DeviceManager
from .backup import BackupJob, backup_info, default_backup_root
from .backupdata import Backup, to_vcard
from .geo import GeoIndex
from .photosdb import apply_db, fetch_db
from .sync import Manifest, Options, SyncEngine, safe_name

log = logging.getLogger("isync.server")

HOME = Path.home() / ".isync"


def shell_folder(csidl: int, fallback: str) -> Path:
    """Windows bilinen klasörü (Resimler/Belgeler OneDrive'a taşınmış olabilir); diğer sistemlerde ev klasörü altı."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / fallback


STATIC = Path(__file__).parent / "static"
SETTINGS_FILE = HOME / "settings.json"
DEFAULT_SETTINGS = {
    "dest": str(shell_folder(39, "Pictures") / "iPhone"),  # CSIDL_MYPICTURES
    "layout": "month",
    "skip_existing": True,
    "include_sidecar": False,
    "include_live_video": True,
    "convert_heic": "none",
    "include_edited": False,
}

app = FastAPI(title=APP_NAME, version=__version__)
devices = DeviceManager()
engine = SyncEngine()
_bg_tasks: set = set()  # arka plan görevlerine güçlü referans (GC öldürmesin)
db_state: dict[str, Any] = {"status": "idle", "done": 0, "total": 0, "message": ""}

MIME = {
    "JPG": "image/jpeg", "JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp",
    "BMP": "image/bmp", "AVIF": "image/avif", "MOV": "video/quicktime", "MP4": "video/mp4", "M4V": "video/mp4",
    "3GP": "video/3gpp", "3GPP": "video/3gpp",
}


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"  # uygulama güncellenince eski JS/CSS kalmasın
    return resp


_last_ping = {"t": 0.0}


@app.post("/api/ping")
async def ping():
    """Arayüz açık olduğu sürece düzenli çağrılır (otomatik kapanma için)."""
    _last_ping["t"] = time.time()
    return {"ok": True}


def _busy() -> bool:
    return (backup_job.running() or db_state.get("status") == "running"
            or any(j.status in ("preparing", "running") for j in engine.jobs.values())
            or any(g.state.get("status") == "running" for g in _geo.values()))


@app.on_event("startup")
async def _auto_exit_watchdog():
    if os.environ.get("ISYNC_AUTO_EXIT") != "1":
        return

    async def watch():
        started = time.time()
        while True:
            await asyncio.sleep(20)
            last = _last_ping["t"] or started
            # Arka plandaki sekmelerde tarayıcı zamanlayıcıları dakikada bire düşebilir; bu yüzden geniş pay
            if time.time() - last > 240 and not _busy():
                log.info("Arayüz kapalı ve süren iş yok; iSync kapanıyor.")
                os._exit(0)

    _bg_tasks.add(asyncio.create_task(watch()))


@app.exception_handler(DeviceError)
async def _device_error(_: Request, exc: DeviceError):
    return JSONResponse({"error": str(exc)}, status_code=503)


def load_settings() -> dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict[str, Any]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_dir(udid: str) -> Path:
    return HOME / "cache" / udid


def documents_dir() -> Path:
    """Kullanıcının Belgeler klasörü (OneDrive'a taşınmış olabilir)."""
    return shell_folder(5, "Documents")  # CSIDL_PERSONAL


async def current():
    return await devices.get()


def asset_or_404(dev, asset_id: int):
    if not (0 <= asset_id < len(dev.assets)):
        raise HTTPException(404, "Dosya bulunamadı")
    return dev.assets[asset_id]


# ---------------- cihaz ----------------
@app.get("/api/info")
async def info():
    return {"app": APP_NAME, "version": __version__, "settings": load_settings()}


@app.get("/api/devices")
async def list_devices():
    return {"devices": await devices.list()}


# ---------------- bağlantı yardımcısı (tanılama + onarım) ----------------
driver_installer = diag.DriverInstaller()


@app.get("/api/diagnose")
async def diagnose_api():
    if backup_job.running() and devices.current is not None:
        d = devices.current.info
        return {"ok": True, "steps": [{"id": "trust", "title": "iPhone bağlı", "status": "ok",
                                        "detail": f"{d.get('name')} — yedekleme/geri yükleme sürüyor"}],
                "driver": driver_installer.state}
    res = await diag.diagnose(devices)
    res["driver"] = driver_installer.state
    return res


@app.post("/api/driver/install")
async def driver_install():
    return driver_installer.start()


@app.post("/api/driver/start-service")
async def driver_start_service():
    ok = await asyncio.to_thread(diag.start_service_elevated)
    return {"ok": ok}


@app.post("/api/driver/open-store")
async def driver_open_store():
    await asyncio.to_thread(diag.open_store)
    return {"ok": True}


@app.post("/api/connect")
async def connect(body: dict[str, Any] | None = None):
    udid = (body or {}).get("udid")
    if udid and devices.current and devices.current.udid != udid:
        devices.current = None
    if backup_job.running() and devices.current is not None:
        # Yedek/geri yükleme sırasında iOS telefonu meşgul tutar; bilinen bilgiyi döndür
        mode = "Geri yükleme" if backup_job.state.get("mode") == "restore" else "Yedekleme"
        return {**devices.current.info, "storage": None, "busy": f"{mode} sürüyor"}
    dev = await devices.get(udid)
    try:
        storage = await dev.storage()
    except Exception:
        await dev.reset()
        storage = await dev.storage()
    return {**dev.info, "storage": storage}


def library_json(dev) -> dict[str, Any]:
    return {
        "udid": dev.udid,
        "scan_time": dev.scan_time,
        "db_loaded": dev.db_loaded,
        "albums": dev.albums,
        "assets": [a.to_json() for a in dev.assets],
    }


@app.post("/api/scan")
async def scan():
    dev = await current()
    t = time.time()
    try:
        await dev.scan()
    except DeviceError:
        raise
    except Exception:
        await dev.reset()
        await dev.scan()
    # Önbellekte veritabanı varsa (yeniden indirmeden) hemen uygula; tazelemek kullanıcının elinde
    db = cache_dir(dev.udid) / "db" / "Photos.sqlite"
    if db.exists():
        try:
            await asyncio.to_thread(apply_db, dev, db)
        except Exception as e:
            log.warning("Önbellekteki veritabanı uygulanamadı: %r", e)
    out = library_json(dev)
    out["scan_seconds"] = round(time.time() - t, 2)
    return out


@app.get("/api/library")
async def library():
    dev = await current()
    if not dev.assets:
        return await scan()
    return library_json(dev)


async def _load_db(dev) -> None:
    if db_state["status"] == "running":
        return
    db_state.update(status="running", done=0, total=0, message="Fotoğraf veritabanı okunuyor…")

    def prog(done: int, total: int) -> None:
        db_state["done"], db_state["total"] = done, total

    try:
        path = await fetch_db(dev, cache_dir(dev.udid) / "db", prog)
        db_state["message"] = "Albümler eşleştiriliyor…"
        await asyncio.to_thread(apply_db, dev, path)
        db_state.update(status="done", message="", stamp=time.time())
    except Exception as e:
        log.exception("DB yüklenemedi")
        db_state.update(status="error", message=f"Albümler okunamadı: {e}")


@app.post("/api/db/load")
async def db_load():
    dev = await current()
    if not dev.assets:
        await dev.scan()
    task = asyncio.create_task(_load_db(dev))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return db_state


@app.get("/api/db/status")
async def db_status():
    return db_state


# ---------------- medya ----------------
@app.get("/api/thumb/{asset_id}")
async def thumb(asset_id: int):
    dev = await current()
    a = asset_or_404(dev, asset_id)
    cdir = cache_dir(dev.udid) / "thumbs" / a.dir
    cfile = cdir / f"{a.name}.jpg"
    headers = {"Cache-Control": "public, max-age=604800"}
    if cfile.exists():
        return FileResponse(cfile, media_type="image/jpeg", headers=headers)
    data = await dev.thumbnail(a)
    if data is None and a.kind == "photo" and a.size < 25 << 20:
        try:
            raw = await dev.read_file(a.path)
            data = await asyncio.to_thread(_resize, raw, 400, 80)
        except Exception:
            data = None
    if data is None:
        raise HTTPException(404)
    cdir.mkdir(parents=True, exist_ok=True)
    cfile.write_bytes(data)
    return Response(data, media_type="image/jpeg", headers=headers)


def _resize(raw: bytes, max_side: int, quality: int) -> bytes:
    from PIL import Image, ImageOps

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=quality)
        return buf.getvalue()


@app.get("/api/preview/{asset_id}")
async def preview(asset_id: int, request: Request):
    dev = await current()
    a = asset_or_404(dev, asset_id)
    if a.kind == "video":
        return await _stream(dev, a, request)
    if a.ext in ("HEIC", "HEIF", "TIF", "TIFF", "DNG"):
        raw = await dev.read_file(a.path)
        data = await asyncio.to_thread(_resize, raw, 2560, 88)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
    raw = await dev.read_file(a.path)
    return Response(raw, media_type=MIME.get(a.ext, "application/octet-stream"),
                    headers={"Cache-Control": "max-age=3600"})


async def _stream(dev, a, request: Request):
    size = a.size
    rng = request.headers.get("range")
    start, end = 0, size - 1
    status = 200
    if rng and rng.startswith("bytes="):
        s, _, e = rng[6:].split(",")[0].partition("-")
        if s:
            start = int(s)
            end = int(e) if e else size - 1
        else:  # bytes=-N
            start = max(0, size - int(e))
        end = min(end, size - 1)
        status = 206
    length = end - start + 1
    afc = await dev.afc()
    handle = await afc.fopen(a.path, "r")

    async def gen():
        pos, left = start, length
        try:
            while left > 0:
                n = min(left, 1 << 20)
                chunk = await afc.pread(handle, n, pos)
                if not chunk:
                    break
                yield chunk
                pos += len(chunk)
                left -= len(chunk)
        finally:
            try:
                await afc.fclose(handle)
            except Exception:
                pass

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, headers=headers,
                             media_type=MIME.get(a.ext, "application/octet-stream"))


@app.get("/api/file/{asset_id}")
async def download(asset_id: int, request: Request):
    dev = await current()
    a = asset_or_404(dev, asset_id)
    resp = await _stream(dev, a, request)
    resp.headers["Content-Disposition"] = f'attachment; filename="{a.name}"'
    return resp


# ---------------- kopyalama ----------------
@app.post("/api/sync")
async def start_sync(body: dict[str, Any]):
    dev = await current()
    opts_d = body.get("options") or {}
    opts = Options.from_json(opts_d)
    if not opts.dest:
        raise HTTPException(400, "Hedef klasör seçilmedi")
    ids = body.get("asset_ids")
    if ids is None:
        ids = [a.id for a in dev.assets if not a.trashed]
    s = load_settings()
    s.update({k: v for k, v in opts_d.items() if k in DEFAULT_SETTINGS})
    save_settings(s)
    try:
        job = engine.start(dev, list(ids), opts)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return job.to_json()


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = engine.jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return job.to_json()


@app.get("/api/jobs")
async def jobs():
    return {"jobs": [j.to_json() for j in engine.jobs.values()]}


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str):
    engine.cancel(job_id)
    return {"ok": True}


@app.post("/api/new-count")
async def new_count(body: dict[str, Any]):
    """Hedef klasöre göre henüz alınmamış dosyaları döndürür (manifest + dosya var mı)."""
    dev = await current()
    dest = Path(body.get("dest") or "").expanduser()
    if not dest.exists():
        ids = [a.id for a in dev.assets if a.kind != "sidecar" and not a.trashed]
    else:
        m = Manifest(dest, dev.udid)
        ids = [a.id for a in dev.assets
               if a.kind != "sidecar" and not a.trashed and not m.get(a.path, a.size)]
    return {"ids": ids, "count": len(ids), "bytes": sum(dev.assets[i].size for i in ids)}


# ---------------- yardımcılar ----------------
@app.post("/api/pick-folder")
async def pick_folder(body: dict[str, Any] | None = None):
    initial = (body or {}).get("initial") or str(Path.home())

    title = i18n_tr("iSync - Hedef klasörü seçin", load_settings().get("lang"))

    def _pick() -> Optional[str]:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(initialdir=initial, title=title, mustexist=False)
            root.destroy()
            return str(Path(path)) if path else None
        except Exception as e:
            log.warning("tkinter klasör seçici açılamadı, Windows penceresi deneniyor: %r", e)
        if sys.platform != "win32":
            return None
        # Yedek yol: Windows'un kendi klasör seçme penceresi
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description=$env:ISYNC_TITLE;"
              "$d.SelectedPath=$env:ISYNC_INITIAL;$d.ShowNewFolderButton=$true;"
              "$f=New-Object System.Windows.Forms.Form;$f.TopMost=$true;"
              "if($d.ShowDialog($f) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.SelectedPath}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=dict(os.environ, ISYNC_INITIAL=initial, ISYNC_TITLE=title),
                               creationflags=0x08000000, timeout=600)
            return r.stdout.strip() or None
        except Exception as e2:
            log.warning("Klasör seçici açılamadı: %r", e2)
            return None

    return {"path": await asyncio.to_thread(_pick)}


@app.post("/api/open-folder")
async def open_folder(body: dict[str, Any]):
    p = Path(body.get("path", "")).expanduser()
    if not p.exists():
        raise HTTPException(404, "Klasör yok")
    if sys.platform == "win32":
        os.startfile(p)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return {"ok": True}


@app.post("/api/settings")
async def settings(body: dict[str, Any]):
    s = load_settings()
    s.update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS or k in ("auto_db", "backup_root", "lang")})
    save_settings(s)
    return s


# ---------------- cihaz bilgisi / uygulamalar ----------------
@app.get("/api/device/details")
async def device_details():
    dev = await current()
    return await devinfo.details(dev)


_apps_cache: dict[str, list] = {}


@app.get("/api/apps")
async def apps_list(refresh: int = 0, sizes: int = 1):
    dev = await current()
    key = f"{dev.udid}|{int(bool(sizes))}"
    if refresh or key not in _apps_cache:
        _apps_cache[key] = await devinfo.apps(dev, sizes=bool(sizes))
    return {"apps": _apps_cache[key], "sized": bool(sizes)}


@app.get("/api/apps/{bundle_id}/icon")
async def app_icon(bundle_id: str):
    dev = await current()
    cfile = cache_dir(dev.udid) / "icons" / f"{bundle_id}.png"
    headers = {"Cache-Control": "public, max-age=604800"}
    if cfile.exists():
        return FileResponse(cfile, media_type="image/png", headers=headers)
    try:
        data = await devinfo.icon(dev, bundle_id)
    except Exception:
        data = None
    if not data:
        raise HTTPException(404)
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cfile.write_bytes(data)
    return Response(data, media_type="image/png", headers=headers)


@app.get("/api/apps/{bundle_id}/docs")
async def app_docs(bundle_id: str, path: str = ""):
    dev = await current()
    try:
        return {"path": path, "items": await devinfo.app_docs_list(dev, bundle_id, path)}
    except Exception as e:
        raise HTTPException(400, f"Uygulama belgeleri okunamadı: {e}")


@app.get("/api/apps/{bundle_id}/file")
async def app_doc_file(bundle_id: str, path: str):
    dev = await current()
    data = await devinfo.app_docs_read(dev, bundle_id, path)
    name = path.rsplit("/", 1)[-1] or "dosya"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/wallpaper")
async def wallpaper():
    dev = await current()
    data = await devinfo.wallpaper(dev)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/png",
                    headers={"Content-Disposition": 'inline; filename="iphone-duvar-kagidi.png"'})


# ---------------- harita (yerel klasör) ----------------
_geo: dict[str, GeoIndex] = {}
_reverse_manifest: dict[str, dict[str, tuple[str, str]]] = {}


def geo_index(root: str) -> GeoIndex:
    p = Path(root or load_settings()["dest"]).expanduser()
    if not p.is_dir():
        raise HTTPException(404, f"Klasör bulunamadı: {p}")
    key = str(p.resolve()).lower()
    if key not in _geo:
        _geo[key] = GeoIndex(p)
    return _geo[key]


@app.post("/api/geo/scan")
async def geo_scan(body: dict[str, Any]):
    gi = geo_index(body.get("root", ""))
    _reverse_manifest.pop(str(gi.root), None)
    task = asyncio.create_task(asyncio.to_thread(gi.scan))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return gi.state


@app.get("/api/geo/status")
async def geo_status(root: str = ""):
    return geo_index(root).state


@app.get("/api/geo/points")
async def geo_points(root: str = ""):
    gi = geo_index(root)
    return {**gi.points(), "state": gi.state}


def _manifest_reverse(gi: GeoIndex) -> dict[str, tuple[str, str]]:
    """Hedef klasörün manifestlerinden: göreli yol -> (udid, cihaz yolu)."""
    key = str(gi.root)
    if key not in _reverse_manifest:
        m: dict[str, tuple[str, str]] = {}
        for f in (gi.root / ".isync").glob("*.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    m[r["r"]] = (f.stem, r["p"])
                except Exception:
                    continue
        _reverse_manifest[key] = m
    return _reverse_manifest[key]


@app.get("/api/geo/thumb/{i}")
async def geo_thumb(i: int, root: str = ""):
    gi = geo_index(root)
    if not (0 <= i < len(gi.items)):
        raise HTTPException(404)
    headers = {"Cache-Control": "public, max-age=86400"}
    data = await asyncio.to_thread(gi.thumb, i)
    if data is None:
        # Video vb.: iPhone'dan iSync ile alındıysa telefonun küçük resmini kullan
        src = _manifest_reverse(gi).get(gi.items[i]["r"])
        if src:
            udid, dpath = src
            d, name = dpath.split("/")[-2:]
            cfile = cache_dir(udid) / "thumbs" / d / f"{name}.jpg"
            if cfile.exists():
                data = cfile.read_bytes()
            else:
                dev = devices.devices.get(udid)
                a = dev.by_path.get(dpath) if dev else None
                if a:
                    data = await dev.thumbnail(a)
                    if data:
                        cfile.parent.mkdir(parents=True, exist_ok=True)
                        cfile.write_bytes(data)
        if data:
            await asyncio.to_thread(gi.store_thumb, i, data)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers=headers)


@app.get("/api/geo/file/{i}")
async def geo_file(i: int, root: str = "", preview: int = 0, download: int = 0):
    gi = geo_index(root)
    try:
        p = gi.path_of(i)
    except KeyError:
        raise HTTPException(404)
    ext = p.suffix.upper().lstrip(".")
    if preview and ext in ("HEIC", "HEIF", "TIF", "TIFF", "DNG"):
        raw = await asyncio.to_thread(p.read_bytes)
        data = await asyncio.to_thread(_resize, raw, 2560, 88)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
    headers = {}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(p.name)}"
    return FileResponse(p, media_type=MIME.get(ext, "application/octet-stream"), headers=headers)


@app.post("/api/geo/reveal")
async def geo_reveal(body: dict[str, Any]):
    gi = geo_index(body.get("root", ""))
    p = gi.path_of(int(body["i"]))
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent)])
    return {"ok": True}


# ---------------- yedek ----------------
backup_job = BackupJob()


def backup_root() -> Path:
    return Path(load_settings().get("backup_root") or default_backup_root()).expanduser()


@app.get("/api/backup/info")
async def backup_info_api():
    root = backup_root()
    udid = devices.current.udid if devices.current else None
    info = backup_info(root, udid) if udid else None
    if info is None and root.exists():  # cihaz bağlı değilse bulunan ilk yedek
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            info = backup_info(root, d.name)
            if info:
                break
    return {"root": str(root), "info": info, "job": backup_job.state}


@app.post("/api/backup/start")
async def backup_start(body: dict[str, Any] | None = None):
    body = body or {}
    if any(j.status in ("preparing", "running") for j in engine.jobs.values()):
        raise HTTPException(409, "Fotoğraf kopyalama sürerken yedek alınamaz.")
    if body.get("root"):
        s = load_settings()
        s["backup_root"] = body["root"]
        save_settings(s)
    dev = await current()
    try:
        return backup_job.start(dev, backup_root(), skip_photos=body.get("skip_photos", True))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/backup/cancel")
async def backup_cancel():
    if backup_job.state.get("mode") == "restore":
        raise HTTPException(409, "Geri yükleme yarıda kesilemez; telefon tutarsız kalabilir.")
    backup_job.cancel()
    return backup_job.state


async def _findmy_on(dev) -> Optional[bool]:
    for domain, key in (("com.apple.fmip", "IsAssociated"), ("com.apple.mobile.findmy", "IsAssociated")):
        try:
            v = await dev.lockdown.get_value(domain, key)
            if v is not None:
                return bool(v)
        except Exception:
            continue
    return None


@app.get("/api/backup/restore-check")
async def restore_check():
    """Geri yüklemeden önce koşulları denetler; arayüz her maddeyi kullanıcıya gösterir."""
    dev = await current()
    if dev.lockdown is None:
        await dev.connect()
    info = backup_info(backup_root(), dev.udid)
    checks = []

    def add(ok: Optional[bool], text: str):
        checks.append({"ok": ok, "text": text})

    add(info is not None, "Bu iPhone'a ait bir yedek var" if info else "Bu iPhone'a ait yedek bulunamadı")
    if info:
        add(not info.get("encrypted"), "Yedek şifresiz" if not info.get("encrypted") else "Şifreli yedekler desteklenmiyor")
        add(info.get("restorable"), "Yedek eksiksiz ve tutarlı" if info.get("restorable")
            else "Yedek eksik veya iSync'in eski sürümüyle alınmış — önce yedeği güncelleyin")
    fm = await _findmy_on(dev)
    add(None if fm is None else not fm, "iPhone'umu Bul kapalı" if fm is False else
        "iPhone'umu Bul AÇIK — Ayarlar › [adınız] › Bul › iPhone'umu Bul'u kapatın" if fm else
        "iPhone'umu Bul durumu okunamadı (açıksa iOS geri yüklemeyi reddeder)")
    add(not backup_job.running(), "Başka yedek/geri yükleme işlemi yok")
    add(not any(j.status in ("preparing", "running") for j in engine.jobs.values()), "Fotoğraf kopyalama sürmüyor")
    ok = all(c["ok"] is not False for c in checks)
    return {"ok": ok, "checks": checks, "device_name": dev.info.get("name"), "info": info,
            "device_ios": dev.info.get("ios")}


def _loose(s: Optional[str]) -> str:
    """Onay adı karşılaştırması: aksan ve büyük/küçük harf duyarsız (İ/i, ş/s vb. klavye farkları sorun olmasın)."""
    import unicodedata

    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("ı", "i").casefold()


@app.post("/api/backup/restore")
async def restore_start(body: dict[str, Any]):
    chk = await restore_check()
    if not chk["ok"]:
        raise HTTPException(409, "Geri yükleme koşulları sağlanmıyor: " +
                            "; ".join(c["text"] for c in chk["checks"] if c["ok"] is False))
    if _loose(body.get("confirm_name")) != _loose(chk["device_name"]):
        raise HTTPException(400, "Onay için yazılan cihaz adı eşleşmiyor.")
    if body.get("understood") is not True:
        raise HTTPException(400, "Uyarının onaylanması gerekiyor.")
    dev = await current()
    try:
        return backup_job.start_restore(dev, backup_root(), keep_settings=bool(body.get("keep_settings", True)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


_bk: dict[str, Any] = {"key": None, "obj": None}


def open_backup() -> Backup:
    """Mevcut yedeği açar; yedek yenilendiyse (tarih değiştiyse) yeniden açar."""
    if backup_job.running():
        raise HTTPException(409, "Yedekleme sürüyor; bitince veriler görüntülenebilir.")
    root = backup_root()
    udid = devices.current.udid if devices.current else None
    info = backup_info(root, udid) if udid else None
    if info is None and root.is_dir():
        for d in sorted(root.iterdir()):
            info = backup_info(root, d.name)
            if info:
                break
    if not info:
        raise HTTPException(404, "Henüz yedek yok.")
    if info.get("encrypted"):
        raise HTTPException(400, "Yedek şifreli; şifreli yedekler henüz desteklenmiyor.")
    key = f"{info['path']}|{info.get('date')}"
    if _bk["key"] != key:
        if _bk["obj"]:
            _bk["obj"].close()
        path = Path(info["path"])
        _bk.update(key=key, obj=Backup(path, HOME / "backup-work" / path.name))
    return _bk["obj"]


async def _bkcall(fn, *a):
    bk = open_backup()
    return await asyncio.to_thread(fn, bk, *a)


@app.get("/api/backup/summary")
async def backup_summary():
    return await _bkcall(lambda b: {**b.summary(), **b.stats()})


@app.get("/api/backup/contacts")
async def backup_contacts():
    return {"contacts": await _bkcall(lambda b: b.contacts())}


@app.get("/api/backup/contacts.vcf")
async def backup_vcf():
    text = await _bkcall(lambda b: to_vcard(b.contacts()))
    return Response(text.encode("utf-8"), media_type="text/vcard",
                    headers={"Content-Disposition": 'attachment; filename="iphone-kisiler.vcf"'})


@app.get("/api/backup/contacts/{rid}/photo")
async def backup_contact_photo(rid: int):
    data = await _bkcall(lambda b: b.contact_photo(rid))
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/backup/chats")
async def backup_chats(src: str = "sms"):
    return {"chats": await _bkcall(lambda b: b.wa_chats() if src == "wa" else b.sms_chats())}


@app.get("/api/backup/messages")
async def backup_messages(chat: int, src: str = "sms", before: float = 0, limit: int = 300):
    fn = (lambda b: b.wa_messages(chat, before or None, limit)) if src == "wa" else \
         (lambda b: b.sms_messages(chat, before or None, limit))
    return {"messages": await _bkcall(fn)}


@app.get("/api/backup/calls")
async def backup_calls():
    return {"calls": await _bkcall(lambda b: b.calls())}


@app.get("/api/backup/safari")
async def backup_safari():
    return {"visits": await _bkcall(lambda b: b.safari())}


@app.get("/api/backup/file")
async def backup_file(domain: str, path: str, preview: int = 0, download: int = 0):
    p = await _bkcall(lambda b: b.lookup(domain, path))
    if not p:
        raise HTTPException(404, "Dosya yedekte yok")
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].upper() if "." in name else ""
    if preview and ext in ("HEIC", "HEIF", "TIF", "TIFF"):
        raw = await asyncio.to_thread(p.read_bytes)
        data = await asyncio.to_thread(_resize, raw, 1600 if preview == 1 else 400, 85)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    headers = {"Cache-Control": "max-age=86400"}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    mime = MIME.get(ext) or {"OPUS": "audio/ogg", "M4A": "audio/mp4", "AAC": "audio/aac", "MP3": "audio/mpeg",
                             "CAF": "audio/x-caf", "PDF": "application/pdf"}.get(ext, "application/octet-stream")
    return FileResponse(p, media_type=mime, headers=headers)


@app.post("/api/backup/export-chat")
async def backup_export_chat(body: dict[str, Any]):
    src, chat, title = body.get("src", "sms"), int(body["chat"]), body.get("title") or "sohbet"
    # Fotoğraf klasörüne değil Belgeler altına (fotoğraf kütüphanesine/haritaya karışmasın)
    out_root = documents_dir() / "iSync Dışa Aktarım" / ("WhatsApp" if src == "wa" else "Mesajlar")

    def _export(b: Backup) -> str:
        msgs = b.wa_messages(chat, None, 10**7) if src == "wa" else b.sms_messages(chat, None, 10**7)
        d = out_root / safe_name(title)[:80]
        (d / "ekler").mkdir(parents=True, exist_ok=True)
        lines = []
        for m in msgs:
            ts = datetime.fromtimestamp(m["t"]).strftime("%d.%m.%Y %H:%M") if m["t"] else ""
            who = "Ben" if m["me"] else (m["from"] or title)
            line = f"[{ts}] {who}: {m['text'] or ''}"
            for a in m["att"]:
                p = b.lookup(a["domain"], a["path"])
                if p:
                    target = d / "ekler" / safe_name(a["name"] or p.name)
                    if not target.exists():
                        shutil.copyfile(p, target)
                    line += f"  <ek: ekler/{target.name}>"
                else:
                    line += f"  <ek yedekte yok: {a['name']}>"
            lines.append(line)
        (d / "sohbet.txt").write_text("\n".join(lines), encoding="utf-8")
        return str(d)

    path = await _bkcall(_export)
    return {"path": path}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
