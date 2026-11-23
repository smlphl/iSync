"""iPhone'un Photos.sqlite veritabanından albüm/favori/gizli/silinmiş bilgisi okur.

Veritabanı büyük olabilir (~1 GB). Bir kez çekilir, yerel önbellekte tutulur ve
cihazdaki dosya değişmedikçe yeniden indirilmez.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from .device import Device

log = logging.getLogger("isync.db")

APPLE_EPOCH = 978307200  # 2001-01-01 UTC
DB_FILES = ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm")


async def fetch_db(dev: Device, cache_dir: Path, progress: Optional[Callable[[int, int], None]] = None) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    afc = await dev.new_afc()
    try:
        stats = {}
        for f in DB_FILES:
            try:
                stats[f] = await afc.stat(f"/PhotoData/{f}")
            except Exception:
                pass
        sig = "|".join(f"{f}:{s['st_size']}:{s['st_mtime'].timestamp()}" for f, s in stats.items())
        sig_file = cache_dir / "db.sig"
        db_path = cache_dir / "Photos.sqlite"
        if db_path.exists() and sig_file.exists() and sig_file.read_text() == sig:
            return db_path
        total = sum(s["st_size"] for s in stats.values())
        done = 0
        for f in DB_FILES:
            target = cache_dir / f
            if f not in stats:
                target.unlink(missing_ok=True)
                continue
            h = await afc.fopen(f"/PhotoData/{f}", "r")
            try:
                with open(target, "wb") as out:
                    while True:
                        chunk = await afc.fread(h, 4 << 20)
                        await asyncio.to_thread(out.write, chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                        if len(chunk) < 4 << 20:
                            break
            finally:
                await afc.fclose(h)
        sig_file.write_text(sig)
        return db_path
    finally:
        await afc.aclose()


def _album_join(con: sqlite3.Connection) -> Optional[tuple[str, str, str]]:
    """Albüm<->varlık ara tablosunu bulur (adı iOS sürümüne göre değişir, örn. Z_33ASSETS)."""
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Z\\_%ASSETS' ESCAPE '\\'"):
        if not re.fullmatch(r"Z_\d+ASSETS", name):
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
        album_col = next((c for c in cols if re.fullmatch(r"Z_\d+ALBUMS", c)), None)
        asset_col = next((c for c in cols if re.fullmatch(r"Z_\d+ASSETS", c)), None)
        if album_col and asset_col:
            return name, album_col, asset_col
    return None


def apply_db(dev: Device, db_path: Path) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ZASSET)")}
        sub = "ZKINDSUBTYPE" if "ZKINDSUBTYPE" in cols else "0"
        rows = con.execute(
            f"SELECT Z_PK, ZDIRECTORY, ZFILENAME, ZDATECREATED, ZFAVORITE, ZHIDDEN, ZTRASHEDSTATE, {sub} FROM ZASSET"
        ).fetchall()
        pk_to_assets: dict[int, list] = {}
        stem_index: dict[tuple[str, str], list] = {}
        for a in dev.assets:
            stem_index.setdefault((a.dir, a.stem), []).append(a)
            a.albums = []
        for pk, directory, filename, created, fav, hidden, trashed, subtype in rows:
            if not directory or not filename or not directory.startswith("DCIM/"):
                continue
            d = directory.split("/", 1)[1]
            stem = filename.rsplit(".", 1)[0]
            group = stem_index.get((d, stem))
            if not group:
                continue
            pk_to_assets[pk] = group
            for a in group:
                # Asıl dosya ve eşlikçileri (Live MOV, AAE) aynı bilgiyi paylaşır
                if created:
                    a.mtime = created + APPLE_EPOCH
                a.favorite = bool(fav)
                a.hidden = bool(hidden)
                a.trashed = bool(trashed)
                a.screenshot = subtype == 10
        albums = []
        join = _album_join(con)
        if join:
            jt, acol, scol = join
            for pk, title in con.execute(
                "SELECT Z_PK, ZTITLE FROM ZGENERICALBUM WHERE ZKIND=2 AND ZTITLE IS NOT NULL "
                "AND (ZTRASHEDSTATE IS NULL OR ZTRASHEDSTATE=0) ORDER BY ZTITLE COLLATE NOCASE"
            ):
                count = 0
                for (asset_pk,) in con.execute(f"SELECT {scol} FROM {jt} WHERE {acol}=?", (pk,)):
                    group = pk_to_assets.get(asset_pk)
                    if group:
                        count += 1
                        for a in group:
                            a.albums.append(pk)
                if count:
                    albums.append({"id": pk, "title": title, "count": count})
        dev.albums = albums
        dev.db_loaded = True
        log.info("Photos.sqlite uygulandı: %d albüm", len(albums))
    finally:
        con.close()
