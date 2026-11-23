"""Yerel klasördeki fotoğraf/videoların konum (GPS) ve tarih indeksi + küçük resimleri.

- JPG/HEIC/PNG/TIFF/DNG: EXIF GPS + DateTimeOriginal (görüntü çözülmeden, sadece başlık okunur)
- MOV/MP4: QuickTime 'moov' kutusundaki ISO6709 konumu ve creationdate
- İndeks <klasör>/.isync/geo.json içinde tutulur; sonraki taramalar sadece yeni/değişen dosyaları okur.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("isync.geo")

IMG_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".dng", ".webp"}
VID_EXT = {".mov", ".mp4", ".m4v", ".3gp"}
INDEX_VERSION = 2

_heif_ready = False


def _heif():
    global _heif_ready
    if not _heif_ready:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _heif_ready = True


def _ratio(v) -> float:
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return 0.0


def _dms(vals, ref) -> Optional[float]:
    try:
        d, m, s = (_ratio(x) for x in vals)
    except Exception:
        return None
    deg = d + m / 60 + s / 3600
    if ref in ("S", "W", b"S", b"W"):
        deg = -deg
    return deg


def _exif_time(s: Optional[str]) -> Optional[float]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        return None


def read_image_meta(path: Path) -> dict[str, Any]:
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif"):
        _heif()
    out: dict[str, Any] = {}
    with Image.open(path) as im:
        ex = im.getexif()
        if not ex:
            return out
        gps = ex.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            lat, lon = _dms(gps[2], gps.get(1)), _dms(gps[4], gps.get(3))
            if lat is not None and lon is not None and not (lat == 0 and lon == 0) and abs(lat) <= 90 and abs(lon) <= 180:
                out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
        exif = ex.get_ifd(0x8769)
        t = _exif_time(exif.get(36867) or exif.get(36868) or ex.get(306))
        if t:
            out["t"] = t
    return out


_ISO6709 = re.compile(rb"([+-]\d{1,2}\.\d{3,})([+-]\d{1,3}\.\d{3,})")
_QTDATE = re.compile(rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{4}|Z)?")


def _find_moov(fh, size: int) -> Optional[bytes]:
    pos = 0
    while pos + 8 <= size:
        fh.seek(pos)
        hdr = fh.read(16)
        if len(hdr) < 8:
            return None
        box, typ = struct.unpack(">I4s", hdr[:8])
        hl = 8
        if box == 1:
            box = struct.unpack(">Q", hdr[8:16])[0]
            hl = 16
        elif box == 0:
            box = size - pos
        if box < hl:
            return None
        if typ == b"moov":
            if box > 64 << 20:
                return None
            fh.seek(pos + hl)
            return fh.read(box - hl)
        pos += box
    return None


def read_video_meta(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    size = path.stat().st_size
    with open(path, "rb") as fh:
        moov = _find_moov(fh, size)
    if not moov:
        return out
    m = _ISO6709.search(moov)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if abs(lat) <= 90 and abs(lon) <= 180 and not (lat == 0 and lon == 0):
            out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
    d = _QTDATE.search(moov)
    if d:
        s = d.group(1).decode()
        tz = (d.group(2) or b"").decode()
        try:
            if tz and tz != "Z":
                out["t"] = datetime.strptime(s + tz, "%Y-%m-%dT%H:%M:%S%z").timestamp()
            else:
                out["t"] = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            pass
    return out


class GeoIndex:
    """Bir kök klasörün indeksi. Sunucu boyunca bellekte tutulur."""

    def __init__(self, root: Path):
        self.root = root
        self.file = root / ".isync" / "geo.json"
        self.items: list[dict[str, Any]] = []  # {r, s, m, k, lat?, lon?, t}
        self.state: dict[str, Any] = {"status": "idle", "done": 0, "total": 0, "message": ""}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if d.get("v") == INDEX_VERSION:
                self.items = d["items"]
                self.state.update(status="done", indexed=d.get("time"))
        except Exception:
            pass

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": INDEX_VERSION, "time": time.time(), "items": self.items},
                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.file)

    def scan(self) -> None:
        """Bloklayan tarama (ayrı thread'de çağrılır)."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self.state.update(status="running", done=0, total=0, message="Dosyalar listeleniyor…")
            old = {it["r"]: it for it in self.items}
            files: list[tuple[str, int, float, str]] = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    kind = "p" if ext in IMG_EXT else "v" if ext in VID_EXT else None
                    if not kind or fn.startswith("."):
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    rel = os.path.relpath(full, self.root).replace("\\", "/")
                    files.append((rel, st.st_size, st.st_mtime, kind))
            todo, keep = [], []
            for rel, size, mtime, kind in files:
                o = old.get(rel)
                if o and o["s"] == size and abs(o["m"] - mtime) < 2:
                    keep.append(o)
                else:
                    todo.append((rel, size, mtime, kind))
            self.state.update(total=len(todo), message="Konumlar okunuyor…")

            def work(f):
                rel, size, mtime, kind = f
                it: dict[str, Any] = {"r": rel, "s": size, "m": round(mtime, 1), "k": kind}
                try:
                    meta = read_image_meta(self.root / rel) if kind == "p" else read_video_meta(self.root / rel)
                    it.update(meta)
                except Exception as e:
                    log.debug("meta okunamadı %s: %r", rel, e)
                it.setdefault("t", mtime)
                self.state["done"] += 1
                return it

            with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as ex:
                new = list(ex.map(work, todo, chunksize=16))
            items = keep + new
            items.sort(key=lambda x: x.get("t", 0), reverse=True)
            self.items = items
            self._save()
            self.state.update(status="done", message="", indexed=time.time())
        except Exception as e:
            log.exception("indeks hatası")
            self.state.update(status="error", message=str(e))
        finally:
            self._lock.release()

    def points(self) -> dict[str, Any]:
        pts = []
        no_gps = 0
        for i, it in enumerate(self.items):
            if "lat" in it:
                pts.append([i, it["lat"], it["lon"], int(it.get("t", 0)), it["k"]])
            else:
                no_gps += 1
        return {"root": str(self.root), "count": len(self.items), "no_gps": no_gps, "points": pts,
                "names": [it["r"] for it in self.items]}

    def path_of(self, i: int) -> Path:
        if not (0 <= i < len(self.items)):
            raise KeyError(i)
        p = (self.root / self.items[i]["r"]).resolve()
        if self.root.resolve() not in p.parents:
            raise KeyError(i)
        return p

    # ---------- küçük resim ----------
    def _thumb_file(self, i: int) -> Path:
        it = self.items[i]
        key = hashlib.sha1(f"{it['r']}|{it['s']}".encode()).hexdigest()
        return self.root / ".isync" / "thumbs" / key[:2] / f"{key}.jpg"

    def thumb(self, i: int) -> Optional[bytes]:
        cfile = self._thumb_file(i)
        if cfile.exists():
            return cfile.read_bytes()
        it = self.items[i]
        data = make_thumb(self.root / it["r"]) if it["k"] == "p" else None
        if data:
            self.store_thumb(i, data)
        return data

    def store_thumb(self, i: int, data: bytes) -> None:
        cfile = self._thumb_file(i)
        cfile.parent.mkdir(parents=True, exist_ok=True)
        cfile.write_bytes(data)


def make_thumb(path: Path, side: int = 360) -> Optional[bytes]:
    from PIL import Image, ImageOps

    ext = path.suffix.lower()
    try:
        if ext in (".heic", ".heif"):
            import pillow_heif

            _heif()
            hf = pillow_heif.open_heif(path)
            img = None
            try:
                if hf[0].info.get("thumbnails"):
                    img = hf[0].get_thumbnail(0).to_pillow()
            except Exception:
                img = None
            if img is None or min(img.size) < side // 2:
                img = hf[0].to_pillow()
        else:
            img = Image.open(path)
            img.draft("RGB", (side * 2, side * 2))
            img = ImageOps.exif_transpose(img)
        img.thumbnail((side, side))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=78)
        return buf.getvalue()
    except Exception as e:
        log.debug("thumb hatası %s: %r", path, e)
        return None
