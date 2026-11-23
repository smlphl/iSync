"""iPhone bağlantısı ve medya kütüphanesi taraması.

Windows Explorer'ın kullandığı WPD/MTP katmanı yerine doğrudan Apple'ın AFC
(Apple File Conduit) protokolünü kullanır. AFC; usbmuxd (Windows'ta "Apple Mobile
Device Service", macOS'ta yerleşik, Linux'ta usbmuxd paketi) üzerinden çalışır.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pymobiledevice3.exceptions import AfcException
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import list_devices

log = logging.getLogger("isync.device")

PHOTO_EXT = {"JPG", "JPEG", "HEIC", "HEIF", "PNG", "GIF", "WEBP", "TIF", "TIFF", "DNG", "BMP", "AVIF"}
VIDEO_EXT = {"MOV", "MP4", "M4V", "3GP", "3GPP", "AVI"}
SIDECAR_EXT = {"AAE", "XMP"}

THUMB_ROOT = "/PhotoData/Thumbnails/V2"
MUTATIONS_ROOT = "/PhotoData/Mutations"

# Bilinen model kodları (ProductType -> pazarlama adı). Bilinmeyen kodda ham kod gösterilir.
MODEL_NAMES = {
    "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro", "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2. nesil)", "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro", "iPhone13,4": "iPhone 12 Pro Max", "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13", "iPhone14,2": "iPhone 13 Pro", "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,6": "iPhone SE (3. nesil)", "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max", "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus", "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro", "iPhone17,2": "iPhone 16 Pro Max", "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus", "iPhone17,5": "iPhone 16e", "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max", "iPhone18,3": "iPhone 17", "iPhone18,4": "iPhone Air",
}


class DeviceError(Exception):
    """Kullanıcıya gösterilecek, anlaşılır hata."""


@dataclass
class Asset:
    id: int
    path: str            # /DCIM/120APPLE/IMG_0001.HEIC
    dir: str             # 120APPLE
    name: str            # IMG_0001.HEIC
    ext: str             # HEIC
    size: int
    mtime: float         # epoch saniye (cihazdaki oluşturma zamanı)
    kind: str            # photo | video | sidecar | other
    live: bool = False   # Live Photo çiftinin parçası mı (HEIC+MOV)
    screenshot: bool = False
    favorite: bool = False
    hidden: bool = False
    trashed: bool = False
    has_edit: bool = False
    albums: list[int] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "dir": self.dir, "name": self.name, "ext": self.ext, "size": self.size,
            "t": int(self.mtime), "k": self.kind[0],
            "l": int(self.live), "s": int(self.screenshot), "f": int(self.favorite),
            "h": int(self.hidden), "x": int(self.trashed), "e": int(self.has_edit), "a": self.albums,
        }


def classify(ext: str) -> str:
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return "other"


class Device:
    """Tek bir bağlı iPhone. AFC bağlantılarını ve kütüphane önbelleğini tutar."""

    def __init__(self, udid: str):
        self.udid = udid
        self.lockdown = None
        self.info: dict[str, Any] = {}
        self._afc: Optional[AfcService] = None       # galeri / önizleme bağlantısı
        self._lock = asyncio.Lock()
        self.assets: list[Asset] = []
        self.by_path: dict[str, Asset] = {}
        self.scan_time: float = 0
        self.albums: list[dict[str, Any]] = []
        self.db_loaded = False

    # ---------- bağlantı ----------
    async def connect(self) -> None:
        try:
            self.lockdown = await create_using_usbmux(serial=self.udid, autopair=True)
        except Exception as e:  # pairing / kilit vb.
            name = type(e).__name__
            if "Password" in name or "Pairing" in name or "Denied" in name:
                raise DeviceError("iPhone kilitli veya bu bilgisayara güvenilmemiş. Kilidi açıp 'Bu Bilgisayara Güven'e dokunun.") from e
            raise DeviceError(f"iPhone'a bağlanılamadı: {name}: {e}") from e
        si = self.lockdown.short_info
        self.info = {
            "udid": self.udid,
            "name": si.get("DeviceName"),
            "product_type": si.get("ProductType"),
            "model": MODEL_NAMES.get(si.get("ProductType", ""), si.get("ProductType")),
            "ios": si.get("ProductVersion"),
            "connection": si.get("ConnectionType"),
        }
        try:
            bat = await self.lockdown.get_value("com.apple.mobile.battery", "BatteryCurrentCapacity")
            self.info["battery"] = bat
        except Exception:
            pass

    async def afc(self) -> AfcService:
        async with self._lock:
            if self._afc is None or self._afc._afc_terminated.is_set():
                if self.lockdown is None:
                    await self.connect()
                svc = AfcService(self.lockdown)
                await svc.__aenter__()
                self._afc = svc
            return self._afc

    async def new_afc(self) -> AfcService:
        """Kopyalama işi için ayrı bir AFC bağlantısı (galeri akıcı kalsın diye)."""
        if self.lockdown is None:
            await self.connect()
        svc = AfcService(self.lockdown)
        await svc.__aenter__()
        return svc

    async def reset(self) -> None:
        if self._afc is not None:
            try:
                await self._afc.aclose()
            except Exception:
                pass
        self._afc = None
        self.lockdown = None

    async def call(self, fn, *args, retries: int = 1):
        """AFC çağrısı; bağlantı koptuysa bir kez yeniden bağlanıp dener."""
        for attempt in range(retries + 1):
            afc = await self.afc()
            try:
                return await fn(afc, *args)
            except AfcException:
                raise  # dosya yok vb. - gerçek hata, yeniden deneme anlamsız
            except Exception as e:
                if attempt >= retries:
                    raise
                log.warning("AFC bağlantısı koptu, yeniden bağlanılıyor: %r", e)
                await self.reset()

    async def storage(self) -> dict[str, int]:
        info = await self.call(lambda a: a.get_device_info())
        return {"total": int(info.get("FSTotalBytes", 0)), "free": int(info.get("FSFreeBytes", 0))}

    # ---------- tarama ----------
    async def scan(self) -> list[Asset]:
        t0 = time.time()
        afc = await self.afc()
        dirs = sorted(d for d in await afc.listdir("/DCIM") if not d.startswith("."))

        listings = await asyncio.gather(*[afc.listdir(f"/DCIM/{d}") for d in dirs], return_exceptions=True)
        paths: list[tuple[str, str]] = []
        for d, files in zip(dirs, listings):
            if isinstance(files, Exception):
                log.warning("Klasör okunamadı %s: %r", d, files)
                continue
            for f in sorted(files):
                if f.startswith("."):
                    continue
                paths.append((d, f))

        # Düzenlenmiş fotoğrafların listesi (Mutations/DCIM/<dir>/<stem>)
        edits: set[tuple[str, str]] = set()
        try:
            mdirs = await afc.listdir(f"{MUTATIONS_ROOT}/DCIM")
            ml = await asyncio.gather(*[afc.listdir(f"{MUTATIONS_ROOT}/DCIM/{d}") for d in mdirs], return_exceptions=True)
            for d, stems in zip(mdirs, ml):
                if isinstance(stems, list):
                    edits.update((d, s) for s in stems if not s.startswith("."))
        except Exception:
            pass

        sem = asyncio.Semaphore(64)

        async def st(d: str, f: str):
            async with sem:
                try:
                    return await afc.stat(f"/DCIM/{d}/{f}")
                except Exception as e:
                    return e

        stats = await asyncio.gather(*[st(d, f) for d, f in paths])
        assets: list[Asset] = []
        for (d, f), s in zip(paths, stats):
            if isinstance(s, Exception) or s.get("st_ifmt") == "S_IFDIR":
                continue
            ext = f.rsplit(".", 1)[-1].upper() if "." in f else ""
            bt: datetime = s.get("st_birthtime") or s.get("st_mtime")
            a = Asset(
                id=len(assets), path=f"/DCIM/{d}/{f}", dir=d, name=f, ext=ext,
                size=int(s.get("st_size", 0)), mtime=bt.timestamp() if bt else 0, kind=classify(ext),
            )
            a.has_edit = (d, a.stem) in edits
            assets.append(a)

        # Live Photo eşleştirme: aynı klasörde aynı isimli foto + video
        stems: dict[tuple[str, str], list[Asset]] = {}
        for a in assets:
            stems.setdefault((a.dir, a.stem), []).append(a)
        for group in stems.values():
            kinds = {a.kind for a in group}
            if "photo" in kinds and "video" in kinds:
                for a in group:
                    if a.kind in ("photo", "video"):
                        a.live = True
            for a in group:
                if a.kind == "photo" and a.ext == "PNG":
                    a.screenshot = True  # DB yüklenirse kesin bilgiyle güncellenir

        self.assets = assets
        self.by_path = {a.path: a for a in assets}
        self.scan_time = time.time()
        self.db_loaded = False
        self.albums = []
        log.info("Tarama: %d dosya, %.1f sn", len(assets), time.time() - t0)
        return assets

    # ---------- okuma ----------
    async def read_file(self, path: str) -> bytes:
        return await self.call(lambda a: a.get_file_contents(path))

    async def thumbnail(self, asset: Asset) -> Optional[bytes]:
        p = f"{THUMB_ROOT}/DCIM/{asset.dir}/{asset.name}/5005.JPG"
        try:
            return await self.call(lambda a: a.get_file_contents(p))
        except Exception:
            return None

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        async def _r(afc: AfcService):
            h = await afc.fopen(path, "r")
            try:
                return await afc.pread(h, length, offset)
            finally:
                await afc.fclose(h)
        return await self.call(_r)

    async def edited_render(self, afc: AfcService, asset: Asset) -> Optional[str]:
        """Düzenlenmiş halin (FullSizeRender.*) cihaz yolunu döndürür."""
        base = f"{MUTATIONS_ROOT}/DCIM/{asset.dir}/{asset.stem}/Adjustments"
        try:
            for f in await afc.listdir(base):
                if f.startswith("FullSizeRender") and "." in f:
                    return f"{base}/{f}"
        except Exception:
            return None
        return None


class DeviceManager:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.current: Optional[Device] = None

    async def list(self) -> list[dict[str, Any]]:
        try:
            muxs = await list_devices()
        except Exception as e:
            raise DeviceError(
                "usbmuxd'ye ulaşılamadı. Windows'ta 'Apple Devices' veya iTunes yüklü ve "
                "'Apple Mobile Device Service' çalışıyor olmalı."
            ) from e
        seen: dict[str, dict[str, Any]] = {}
        for m in muxs:
            prev = seen.get(m.serial)
            if prev is None or m.connection_type == "USB":
                seen[m.serial] = {"udid": m.serial, "connection": m.connection_type}
        for udid, d in self.devices.items():
            if udid in seen and d.info:
                seen[udid].update({k: d.info.get(k) for k in ("name", "model", "ios")})
        return list(seen.values())

    async def get(self, udid: Optional[str] = None) -> Device:
        if udid is None:
            # Galeri saniyede yüzlerce istek atar; her seferinde usbmux'u sorgulamamak için kısa önbellek
            now = time.time()
            if self.current is not None and now - getattr(self, "_checked", 0) < 3:
                return self.current
            devs = await self.list()
            self._checked = now
            present = {d["udid"] for d in devs}
            if self.current is not None:
                if self.current.udid in present:
                    return self.current
                # Telefon çıkarılmış (ya da başka telefon takılmış): eski bağlantıyı bırak
                await self.current.reset()
                self.current = None
            if not devs:
                raise DeviceError("Bağlı iPhone bulunamadı. Kabloyu takıp telefonun kilidini açın.")
            udid = devs[0]["udid"]
        dev = self.devices.get(udid)
        if dev is None:
            dev = Device(udid)
            await dev.connect()
            self.devices[udid] = dev
        self.current = dev
        return dev
