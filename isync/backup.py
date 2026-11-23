"""iPhone yedeği ve geri yükleme (iTunes/Finder biçimi, mobilebackup2) — artımlı, ilerlemeli.

İlk yedek uzun sürebilir; sonrakiler sadece değişen dosyaları alır.
Fotoğraf kütüphanesi (DCIM + PhotoData) varsayılan olarak diske yazılmaz:
fotoğraflar zaten iSync'in ana özelliğiyle alınıyor, yedek gereksiz yere yüzlerce GB büyümesin.
Bu durumda yedek bitince atlanan dosyalar manifestten de çıkarılır; böylece yedek tutarlıdır
ve geri yüklenebilir (fotoğraflar iCloud'dan veya iSync kopyasından gelir).
"""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import shutil
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from pymobiledevice3.services.mobilebackup2 import BackupFile, Mobilebackup2Service

from .device import Device

log = logging.getLogger("isync.backup")

PHOTO_MARKERS = ("/Media/DCIM/", "/Media/PhotoData/", "/Media/PhotoStreamsData/")
PHOTO_DOMAINS = ("CameraRollDomain",)
PHOTO_REL = ("Media/DCIM", "Media/PhotoData", "Media/PhotoStreamsData")
MARKER = "iSync.json"  # iSync'in yedek hakkındaki notu (tamamlandı mı, geri yüklenebilir mi)


def default_backup_root() -> Path:
    return Path.home() / "iSync Yedekleri"


def is_photo_file(bf: BackupFile) -> bool:
    """Aktarım sırasında (device_name) ya da manifestte (domain/relative_path) fotoğraf kütüphanesi dosyası mı?"""
    if bf.device_name and any(m in bf.device_name for m in PHOTO_MARKERS):
        return True
    if bf.domain in PHOTO_DOMAINS:
        return True
    rel = bf.relative_path or ""
    return bf.domain == "MediaDomain" and rel.startswith(PHOTO_REL)


def read_marker(device_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((device_dir / MARKER).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_marker(device_dir: Path, **kw: Any) -> None:
    m = read_marker(device_dir)
    m.update(kw)
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / MARKER).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def backup_dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def backup_info(root: Path, udid: str) -> Optional[dict[str, Any]]:
    d = root / udid
    if not (d / "Manifest.db").exists():
        return None
    info: dict[str, Any] = {"path": str(d), "udid": udid}
    try:
        with open(d / "Status.plist", "rb") as f:
            st = plistlib.load(f)
        dt = st.get("Date")  # iOS bu tarihi UTC olarak (saat dilimi bilgisi olmadan) yazar
        info["date"] = dt.replace(tzinfo=timezone.utc).timestamp() if dt else None
        info["state"] = st.get("BackupState")
    except Exception:
        info["date"] = (d / "Manifest.db").stat().st_mtime
    try:
        with open(d / "Info.plist", "rb") as f:
            ip = plistlib.load(f)
        info.update(device=ip.get("Device Name"), ios=ip.get("Product Version"))
    except Exception:
        pass
    try:
        with open(d / "Manifest.plist", "rb") as f:
            mp = plistlib.load(f)
        info["encrypted"] = bool(mp.get("IsEncrypted"))
    except Exception:
        info["encrypted"] = False
    m = read_marker(d)
    info["skip_photos"] = m.get("skip_photos")
    info["size"] = m.get("size")
    info["restorable"] = bool(m.get("complete")) and (not m.get("skip_photos") or bool(m.get("manifest_pruned")))
    return info


def finalize_backup(device_dir: Path, skip_photos: bool, encrypted: bool, st: Optional[dict] = None) -> None:
    """Yedek bitince: fotoğraf atlandıysa manifestten de çıkar (yedek tutarlı/geri yüklenebilir olsun), notu yaz."""
    pruned = False
    if skip_photos and not encrypted:
        if st is not None:
            st["status"] = "finalizing"
            st["message"] = "Yedek dizini düzenleniyor…"
        Mobilebackup2Service.prune_backup_manifest(device_dir, lambda bf: not is_photo_file(bf))
        pruned = True
    write_marker(device_dir, complete=True, skip_photos=skip_photos, manifest_pruned=pruned,
                 finished=time.time(), encrypted=encrypted, size=backup_dir_size(device_dir))


class BackupJob:
    """Yedekleme ve geri yükleme işi (aynı anda yalnızca biri)."""

    def __init__(self):
        self.state: dict[str, Any] = {"status": "idle"}
        self._task: Optional[asyncio.Task] = None

    def running(self) -> bool:
        return self.state.get("status") in ("starting", "running", "passcode", "finalizing")

    def _begin(self, mode: str, root: Path, msg: str) -> dict[str, Any]:
        if self.running():
            raise RuntimeError("Zaten bir yedekleme veya geri yükleme sürüyor.")
        self.state = {"mode": mode, "status": "starting", "percent": 0.0, "message": msg,
                      "started": time.time(), "root": str(root), "skipped_files": 0, "received_files": 0}
        return self.state

    def start(self, dev: Device, root: Path, skip_photos: bool = True) -> dict[str, Any]:
        st = self._begin("backup", root, "Yedekleme başlatılıyor…")
        self._task = asyncio.get_running_loop().create_task(self._run_backup(dev, root, skip_photos))
        return st

    def start_restore(self, dev: Device, root: Path, keep_settings: bool = True) -> dict[str, Any]:
        st = self._begin("restore", root, "Geri yükleme başlatılıyor…")
        self._task = asyncio.get_running_loop().create_task(self._run_restore(dev, root, keep_settings))
        return st

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ---------- ortak ----------
    def _progress(self, pct: float) -> None:
        st = self.state
        if pct and pct > st["percent"]:
            st["status"] = "running"  # ilerleme geldiyse parola onaylanmıştır
            st["message"] = ""
        st["percent"] = max(st["percent"], float(pct or 0))

    def _watch(self) -> logging.Handler:
        """Kütüphane cihaz bildirimlerini (parola ekranı açıldı/kapandı) log olarak yazıyor; onu dinliyoruz."""
        st = self.state
        what = "geri yükleme" if st.get("mode") == "restore" else "yedekleme"

        class _Watch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                msg = record.getMessage()
                if "enter the device passcode" in msg:
                    st["status"] = "passcode"
                    st["message"] = f"iPhone'da parolanızı girin — {what} onay bekliyor."
                elif "passcode prompt dismissed" in msg:
                    st["status"] = "running"
                    st["message"] = ""
                elif "cancelled the backup" in msg:
                    st["message"] = "İşlem iPhone'dan iptal edildi."

        return _Watch(level=logging.INFO)

    @staticmethod
    async def _service(dev: Device) -> Mobilebackup2Service:
        if dev.lockdown is None:
            await dev.connect()
        return Mobilebackup2Service(dev.lockdown)

    # ---------- yedek ----------
    async def _run_backup(self, dev: Device, root: Path, skip_photos: bool) -> None:
        st = self.state
        try:
            root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
            if free < 5 << 30:
                raise RuntimeError(f"Yedek klasörünün bulunduğu diskte çok az yer var ({free / 1e9:.1f} GB).")

            def keep(bf: BackupFile) -> bool:
                if is_photo_file(bf):
                    st["skipped_files"] += 1
                    return False
                st["received_files"] += 1
                return True

            device_dir = root / dev.udid
            if (device_dir / "Manifest.db").exists():
                write_marker(device_dir, complete=False)  # yarıda kalırsa geri yüklenemez sayılsın
            watch = self._watch()
            async with await self._service(dev) as mb:
                st["encrypted"] = await mb.get_will_encrypt()
                mb.logger.addHandler(watch)
                if mb.logger.getEffectiveLevel() > logging.INFO:
                    mb.logger.setLevel(logging.INFO)
                try:
                    st["status"] = "running"
                    st["message"] = "iPhone yedeği hazırlıyor… (ekranda parola sorulursa girin)"
                    await mb.backup(full=False, backup_directory=root, progress_callback=self._progress,
                                    filter_callback=keep if skip_photos else None)
                finally:
                    mb.logger.removeHandler(watch)
            await asyncio.to_thread(finalize_backup, device_dir, skip_photos, bool(st.get("encrypted")), st)
            st.update(status="done", percent=100.0, message="", finished=time.time())
        except asyncio.CancelledError:
            st.update(status="cancelled", message="Yedekleme iptal edildi.", finished=time.time())
        except Exception as e:
            log.exception("Yedekleme hatası")
            st.update(status="error", message=f"{type(e).__name__}: {e}", finished=time.time())

    # ---------- geri yükleme ----------
    async def _run_restore(self, dev: Device, root: Path, keep_settings: bool) -> None:
        st = self.state
        try:
            device_dir = root / dev.udid
            size = await asyncio.to_thread(backup_dir_size, device_dir)
            # Yer varsa kütüphane geri yüklemeden önce yedeğin bir kopyasını alır (yedek bozulmasın)
            copy = shutil.disk_usage(root).free > size * 1.1 + (2 << 30)
            st["copied_backup"] = copy
            watch = self._watch()
            async with await self._service(dev) as mb:
                mb.logger.addHandler(watch)
                if mb.logger.getEffectiveLevel() > logging.INFO:
                    mb.logger.setLevel(logging.INFO)
                try:
                    st["status"] = "running"
                    st["message"] = "Geri yükleme hazırlanıyor… (ekranda parola sorulursa girin)"
                    await mb.restore(backup_directory=root, system=False, reboot=True, copy=copy,
                                     settings=keep_settings, remove=False, progress_callback=self._progress)
                finally:
                    mb.logger.removeHandler(watch)
            await dev.reset()
            st.update(status="done", percent=100.0, finished=time.time(),
                      message="Geri yükleme tamamlandı. iPhone yeniden başlıyor; uygulamalar App Store'dan yeniden inecek.")
        except asyncio.CancelledError:
            st.update(status="cancelled", message="Geri yükleme iptal edildi.", finished=time.time())
        except Exception as e:
            log.exception("Geri yükleme hatası")
            st.update(status="error", message=f"{type(e).__name__}: {e}", finished=time.time())
