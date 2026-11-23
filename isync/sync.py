"""Kopyalama motoru: seçilen dosyaları iPhone'dan bilgisayara aktarır.

- Ayrı bir AFC bağlantısı + boru hattı (pipelining) ile USB hattını doyurur
- Yarım dosya bırakmaz (.part -> rename), tarihleri korur
- Artımlı: hedef klasördeki manifest sayesinde daha önce alınanları atlar
- İsteğe bağlı HEIC -> JPG dönüşümü (ayrı iş parçacıklarında, aktarımı yavaşlatmadan)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .device import Asset, Device

log = logging.getLogger("isync.sync")

CHUNK = 4 << 20
WORKERS = 3  # aynı bağlantı üzerinde eşzamanlı dosya (küçük dosyalarda gecikmeyi gizler)
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str) -> str:
    s = INVALID.sub("_", s).strip().rstrip(".")
    return s or "_"


def set_file_times(path: Path, ts: float) -> None:
    """Değiştirme + erişim tarihini, Windows'ta ayrıca oluşturma tarihini ayarlar."""
    try:
        os.utime(path, (ts, ts))
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            k32.CreateFileW.restype = wintypes.HANDLE
            h = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES
            if h and h != wintypes.HANDLE(-1).value:
                ft = int(ts * 10_000_000) + 116444736000000000
                ftime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
                k32.SetFileTime(h, ctypes.byref(ftime), None, None)
                k32.CloseHandle(h)
        except Exception:
            pass


_heif_ready = False


def convert_heic(src: Path, dst: Path, quality: int = 92) -> None:
    global _heif_ready
    from PIL import Image

    if not _heif_ready:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _heif_ready = True
    with Image.open(src) as im:
        kw: dict[str, Any] = {"quality": quality, "subsampling": 0 if quality >= 95 else 2}
        exif = im.getexif()
        if exif:
            exif[0x0112] = 1  # yön bilgisi pillow-heif tarafından zaten uygulandı
            kw["exif"] = exif.tobytes()
        if im.info.get("icc_profile"):
            kw["icc_profile"] = im.info["icc_profile"]
        im.convert("RGB").save(dst, "JPEG", **kw)


@dataclass
class Options:
    dest: str
    layout: str = "month"          # month | year | folder | flat
    subfolder: str = ""            # ör. albüm adı
    skip_existing: bool = True
    include_sidecar: bool = False  # .AAE düzenleme dosyaları
    include_live_video: bool = True
    convert_heic: str = "none"     # none | jpg | jpg_keep
    include_edited: bool = False   # düzenlenmiş hali "_edited" olarak da al

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Options":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Item:
    asset: Asset
    src: str
    rel: str            # hedef köke göre yol
    size: int
    convert: bool = False
    edited: bool = False


@dataclass
class Job:
    id: str
    device: Device
    opts: Options
    asset_ids: list[int]
    status: str = "preparing"   # preparing | running | done | cancelled | error
    message: str = ""
    total_files: int = 0
    total_bytes: int = 0
    done_files: int = 0
    done_bytes: int = 0
    skipped: int = 0
    converted: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0
    _cancel: bool = False
    _task: Any = None
    _samples: list[tuple[float, int]] = field(default_factory=list)

    def speed(self) -> float:
        now = time.time()
        self._samples = [s for s in self._samples if now - s[0] <= 6]
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def to_json(self) -> dict[str, Any]:
        sp = self.speed() if self.status == "running" else 0
        remaining = max(self.total_bytes - self.done_bytes, 0)
        return {
            "id": self.id, "status": self.status, "message": self.message,
            "total_files": self.total_files, "total_bytes": self.total_bytes,
            "done_files": self.done_files, "done_bytes": self.done_bytes,
            "skipped": self.skipped, "converted": self.converted,
            "errors": self.errors[-50:], "error_count": len(self.errors),
            "current": self.current, "speed": sp, "eta": remaining / sp if sp > 0 else None,
            "elapsed": (self.finished or time.time()) - self.started, "dest": self.opts.dest,
        }


class Manifest:
    """Hedefte .isync/<udid>.jsonl: cihaz yolu+boyut -> göreli hedef yolu."""

    def __init__(self, root: Path, udid: str):
        self.dir = root / ".isync"
        self.file = self.dir / f"{udid}.jsonl"
        self.map: dict[str, str] = {}
        if self.file.exists():
            for line in self.file.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    self.map[f"{r['p']}|{r['s']}"] = r["r"]
                except Exception:
                    continue
        self._fh = None

    def get(self, path: str, size: int) -> Optional[str]:
        return self.map.get(f"{path}|{size}")

    def add(self, path: str, size: int, rel: str) -> None:
        if self._fh is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetFileAttributesW(str(self.dir), 0x2)  # gizli
                except Exception:
                    pass
            self._fh = open(self.file, "a", encoding="utf-8")
        self.map[f"{path}|{size}"] = rel
        self._fh.write(json.dumps({"p": path, "s": size, "r": rel}, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class SyncEngine:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2), thread_name_prefix="heic")

    def start(self, device: Device, asset_ids: list[int], opts: Options) -> Job:
        for j in self.jobs.values():
            if j.status in ("preparing", "running"):
                raise RuntimeError("Zaten devam eden bir kopyalama var.")
        job = Job(id=uuid.uuid4().hex[:8], device=device, opts=opts, asset_ids=asset_ids)
        self.jobs[job.id] = job
        # Görev referansı saklanmalı; aksi halde çöp toplayıcı işi yarıda öldürebilir.
        job._task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    def cancel(self, job_id: str) -> None:
        if job_id in self.jobs:
            self.jobs[job_id]._cancel = True

    # ---------- hedef yolu ----------
    def _subdir(self, a: Asset, o: Options) -> str:
        parts = []
        if o.subfolder:
            parts.append(safe_name(o.subfolder))
        dt = datetime.fromtimestamp(a.mtime) if a.mtime else None
        if o.layout == "month" and dt:
            parts += [f"{dt:%Y}", f"{dt:%Y-%m}"]
        elif o.layout == "year" and dt:
            parts.append(f"{dt:%Y}")
        elif o.layout == "folder":
            parts.append(a.dir)
        return "/".join(parts)

    async def _plan(self, job: Job, manifest: Manifest, root: Path) -> list[Item]:
        o = job.opts
        dev = job.device
        sel = set(job.asset_ids)
        assets = [a for a in dev.assets if a.id in sel]
        if not o.include_sidecar:
            assets = [a for a in assets if a.kind != "sidecar"]
        if not o.include_live_video:
            assets = [a for a in assets if not (a.live and a.kind == "video")]

        reserved: set[str] = set()
        items: list[Item] = []

        def place(sub: str, name: str, size: Optional[int]) -> Optional[str]:
            """Çakışmasız göreli yol; aynı dosya zaten varsa None (atla)."""
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            n = 0
            while True:
                cand_name = name if n == 0 else f"{stem}_{n}{dot}{ext}"
                rel = f"{sub}/{cand_name}" if sub else cand_name
                key = rel.lower()
                if key not in reserved:
                    p = root / rel
                    if not p.exists():
                        reserved.add(key)
                        return rel
                    if o.skip_existing and (size is None or p.stat().st_size == size):
                        return None
                n += 1

        afc = None
        for a in assets:
            sub = self._subdir(a, o)
            convert = o.convert_heic != "none" and a.ext in ("HEIC", "HEIF")
            if o.skip_existing:
                prev = manifest.get(a.path, a.size)
                if prev and (root / prev).exists():
                    job.skipped += 1
                    continue
            if convert:
                jpg_rel = place(sub, f"{a.stem}.JPG", None)
                if jpg_rel is None:
                    job.skipped += 1
                    continue
                items.append(Item(a, a.path, jpg_rel, a.size, convert=True))
                if o.convert_heic == "jpg_keep":
                    rel = place(sub, a.name, a.size)
                    if rel:
                        items.append(Item(a, a.path, rel, a.size))
            else:
                rel = place(sub, a.name, a.size)
                if rel is None:
                    manifest.add(a.path, a.size, f"{sub}/{a.name}" if sub else a.name)
                    job.skipped += 1
                    continue
                items.append(Item(a, a.path, rel, a.size))

            if o.include_edited and a.has_edit and a.kind in ("photo", "video"):
                if afc is None:
                    afc = await dev.afc()
                src = await dev.edited_render(afc, a)
                if src:
                    ext = src.rsplit(".", 1)[-1]
                    try:
                        size = (await afc.stat(src))["st_size"]
                    except Exception:
                        continue
                    conv = o.convert_heic != "none" and ext.lower() in ("heic", "heif")
                    name = f"{a.stem}_edited.{'JPG' if conv else ext.upper()}"
                    rel = place(sub, name, None if conv else size)
                    if rel:
                        items.append(Item(a, src, rel, size, convert=conv, edited=True))
        return items

    # ---------- çalıştır ----------
    async def _run(self, job: Job) -> None:
        o = job.opts
        root = Path(o.dest).expanduser()
        manifest: Optional[Manifest] = None
        afc = None
        pending_conv: list[asyncio.Future] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
            manifest = Manifest(root, job.device.udid)
            job.message = "Hazırlanıyor…"
            items = await self._plan(job, manifest, root)
            job.total_files = len(items)
            job.total_bytes = sum(i.size for i in items)
            free = shutil.disk_usage(root).free
            if job.total_bytes > free:
                raise RuntimeError(
                    f"Hedef diskte yer yetersiz: {job.total_bytes / 1e9:.1f} GB gerekli, {free / 1e9:.1f} GB boş."
                )
            job.status = "running"
            job.message = ""
            job.started = time.time()
            job._samples = [(time.time(), 0)]
            if not items:
                job.status = "done"
                job.message = "Kopyalanacak yeni dosya yok."
                return

            afc = await job.device.new_afc()
            queue: asyncio.Queue[Item] = asyncio.Queue()
            for it in items:
                queue.put_nowait(it)
            loop = asyncio.get_running_loop()

            async def copy_one(it: Item) -> None:
                nonlocal afc
                final = root / it.rel
                final.parent.mkdir(parents=True, exist_ok=True)
                tmp_ext = ".heic.part" if it.convert else ".part"
                part = final.with_name(final.name + tmp_ext)
                got = 0
                try:
                    h = await afc.fopen(it.src, "r")
                    try:
                        with open(part, "wb", buffering=0) as out:
                            while True:
                                if job._cancel:
                                    raise asyncio.CancelledError
                                chunk = await afc.fread(h, CHUNK)
                                if chunk:
                                    await asyncio.to_thread(out.write, chunk)
                                    got += len(chunk)
                                    job.done_bytes += len(chunk)
                                    job._samples.append((time.time(), job.done_bytes))
                                if len(chunk) < CHUNK:
                                    break
                    finally:
                        try:
                            await afc.fclose(h)
                        except Exception:
                            pass
                    if got != it.size:
                        raise IOError(f"Eksik okuma ({got}/{it.size} bayt)")
                except BaseException:
                    job.done_bytes -= got
                    part.unlink(missing_ok=True)
                    raise

                ts = it.asset.mtime
                if it.convert:
                    def _conv() -> None:
                        try:
                            convert_heic(part, final)
                            set_file_times(final, ts)
                        finally:
                            part.unlink(missing_ok=True)

                    fut = loop.run_in_executor(self.pool, _conv)

                    def _done(f: asyncio.Future, it=it) -> None:
                        if f.exception():
                            job.errors.append({"file": it.rel, "error": f"Dönüştürme hatası: {f.exception()}"})
                        else:
                            job.converted += 1
                            if not it.edited:
                                manifest.add(it.src, it.size, it.rel)

                    fut.add_done_callback(_done)
                    pending_conv.append(fut)
                else:
                    os.replace(part, final)
                    set_file_times(final, ts)
                    if not it.edited:
                        manifest.add(it.src, it.size, it.rel)

            async def worker() -> None:
                nonlocal afc
                while not queue.empty() and not job._cancel:
                    it = queue.get_nowait()
                    label = it.rel
                    job.current.append(label)
                    try:
                        for attempt in range(3):
                            try:
                                await copy_one(it)
                                break
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                from pymobiledevice3.exceptions import AfcException

                                if isinstance(e, AfcException) or attempt == 2:
                                    raise
                                log.warning("Yeniden deneniyor (%s): %r", it.rel, e)
                                await asyncio.sleep(1 + attempt)
                                try:
                                    await job.device.reset()
                                    afc = await job.device.new_afc()
                                except Exception:
                                    pass
                        job.done_files += 1
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        job.errors.append({"file": it.rel, "error": f"{type(e).__name__}: {e}"})
                        job.total_bytes -= it.size  # ilerleme çubuğu %100'e ulaşabilsin
                    finally:
                        if label in job.current:
                            job.current.remove(label)

            await asyncio.gather(*[worker() for _ in range(WORKERS)])
            if pending_conv:
                job.message = "HEIC dönüştürmeleri tamamlanıyor…"
                await asyncio.gather(*pending_conv, return_exceptions=True)
                job.message = ""
            job.status = "cancelled" if job._cancel else "done"
        except Exception as e:
            log.exception("Kopyalama hatası")
            job.status = "error"
            job.message = str(e)
        finally:
            job.finished = time.time()
            job.current = []
            if manifest:
                manifest.close()
            if afc is not None:
                try:
                    await afc.aclose()
                except Exception:
                    pass
