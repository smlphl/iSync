"""Şifresiz iPhone yedeğinden veri okuma: kişiler, iMessage/SMS, WhatsApp, arama geçmişi, Safari.

Yedek biçimi: <yedek>/Manifest.db (Files: fileID, domain, relativePath, flags) ve dosyalar
<yedek>/<fileID[:2]>/<fileID>. SQLite veritabanları (-wal/-shm ile birlikte) okunmadan önce
bir çalışma klasörüne doğru adlarla kopyalanır; yedeğin kendisine asla yazılmaz.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("isync.backupdata")

APPLE_EPOCH = 978307200
WA_DOMAINS = ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared", "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared")


def _apple_ts(v) -> Optional[float]:
    """2001 tabanlı zaman (saniye veya nanosaniye) -> unix epoch."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e14:  # nanosaniye (iOS 11+ sms.db)
        v /= 1e9
    return v + APPLE_EPOCH if v else None


def phone_key(s: Optional[str]) -> Optional[str]:
    """Telefon numarası eşleştirme anahtarı: son 10 hane. E-postalar küçük harfe çevrilir."""
    if not s:
        return None
    s = s.strip()
    if "@" in s:
        return s.lower()
    d = re.sub(r"\D", "", s)
    return d[-10:] if len(d) >= 7 else (d or None)


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """iOS 16+ sms.db 'attributedBody' (NSAttributedString typedstream) içinden düz metni çıkarır."""
    if not blob:
        return None
    try:
        i = blob.find(b"NSString")
        if i < 0:
            return None
        j = blob.find(b"\x2b", i + 8)  # '+' : C-string türü işareti
        if j < 0 or j > i + 20:
            return None
        n = blob[j + 1]
        k = j + 2
        if n == 0x81:
            n = int.from_bytes(blob[k:k + 2], "little")
            k += 2
        elif n == 0x82:
            n = int.from_bytes(blob[k:k + 4], "little")
            k += 4
        return blob[k:k + n].decode("utf-8", errors="replace")
    except Exception:
        return None


class Backup:
    def __init__(self, path: Path, work: Path):
        self.path = path
        self.work = work
        self._manifest: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._dbs: dict[str, sqlite3.Connection] = {}
        self._contact_index: Optional[dict[str, str]] = None

    # ---------- manifest ----------
    def manifest(self) -> sqlite3.Connection:
        if self._manifest is None:
            self._manifest = self._open_copy("Manifest", [self.path / "Manifest.db"], "Manifest.db")
        return self._manifest

    def lookup(self, domain: str, rel: str) -> Optional[Path]:
        with self._lock:
            row = self.manifest().execute(
                "SELECT fileID FROM Files WHERE domain=? AND relativePath=? AND flags=1", (domain, rel)
            ).fetchone()
        if not row:
            return None
        p = self.path / row[0][:2] / row[0]
        return p if p.exists() else None

    def find_domains(self, like: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.manifest().execute("SELECT DISTINCT domain FROM Files WHERE domain LIKE ?", (like,))]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            m = self.manifest()
            n = m.execute("SELECT COUNT(*) FROM Files WHERE flags=1").fetchone()[0]
            apps = m.execute("SELECT COUNT(DISTINCT domain) FROM Files WHERE domain LIKE 'AppDomain-%'").fetchone()[0]
        return {"files": n, "app_domains": apps}

    def _open_copy(self, key: str, sources: list[Path], name: str) -> sqlite3.Connection:
        """Veritabanını (ve -wal/-shm'yi) çalışma klasörüne kopyalayıp salt-okunur açar."""
        d = self.work / key
        d.mkdir(parents=True, exist_ok=True)
        main = sources[0]
        target = d / name
        sig = d / ".sig"
        stamp = "|".join(f"{p}:{p.stat().st_size}:{p.stat().st_mtime}" for p in sources if p and p.exists())
        if not target.exists() or not sig.exists() or sig.read_text() != stamp:
            for suffix in ("", "-wal", "-shm"):
                (d / (name + suffix)).unlink(missing_ok=True)
            shutil.copyfile(main, target)
            for p, suffix in zip(sources[1:], ("-wal", "-shm")):
                if p and p.exists():
                    shutil.copyfile(p, d / (name + suffix))
            # WAL'i ana dosyaya işle ki sonraki açılışlar sabit olsun
            con = sqlite3.connect(target)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.DatabaseError:
                pass
            con.close()
            sig.write_text(stamp)
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def db(self, key: str, domain: str, rel: str) -> Optional[sqlite3.Connection]:
        with self._lock:
            if key in self._dbs:
                return self._dbs[key]
            main = self.lookup(domain, rel)
            if not main:
                return None
            srcs = [main, self.lookup(domain, rel + "-wal"), self.lookup(domain, rel + "-shm")]
            con = self._open_copy(key, srcs, Path(rel).name)
            self._dbs[key] = con
            return con

    def close(self) -> None:
        for c in self._dbs.values():
            c.close()
        self._dbs.clear()
        if self._manifest:
            self._manifest.close()
            self._manifest = None

    # ---------- özet ----------
    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        ab = self.db("contacts", "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
        out["contacts"] = ab.execute("SELECT COUNT(*) FROM ABPerson").fetchone()[0] if ab else None
        sms = self.db("sms", "HomeDomain", "Library/SMS/sms.db")
        out["chats"] = sms.execute("SELECT COUNT(*) FROM chat").fetchone()[0] if sms else None
        out["messages"] = sms.execute("SELECT COUNT(*) FROM message").fetchone()[0] if sms else None
        wa = self.whatsapp_db()
        out["wa_chats"] = wa.execute("SELECT COUNT(*) FROM ZWACHATSESSION").fetchone()[0] if wa else None
        out["wa_messages"] = wa.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0] if wa else None
        ch = self.db("calls", "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
        out["calls"] = ch.execute("SELECT COUNT(*) FROM ZCALLRECORD").fetchone()[0] if ch else None
        sf = self.db("safari", "HomeDomain", "Library/Safari/History.db")
        out["safari"] = sf.execute("SELECT COUNT(*) FROM history_visits").fetchone()[0] if sf else None
        return out

    # ---------- kişiler ----------
    def contacts(self) -> list[dict[str, Any]]:
        ab = self.db("contacts", "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
        if not ab:
            return []
        labels = {r[0]: r[1] for r in ab.execute("SELECT ROWID, value FROM ABMultiValueLabel")}
        people: dict[int, dict[str, Any]] = {}
        cols = {r[1] for r in ab.execute("PRAGMA table_info(ABPerson)")}
        opt = lambda c: c if c in cols else "NULL"
        for r in ab.execute(f"SELECT ROWID, First, Middle, Last, Organization, {opt('JobTitle')}, {opt('Nickname')}, "
                            f"{opt('Note')}, {opt('Birthday')} FROM ABPerson"):
            first, mid, last = r[1] or "", r[2] or "", r[3] or ""
            name = " ".join(x for x in (first, mid, last) if x).strip() or (r[4] or r[6] or "")
            people[r[0]] = {"id": r[0], "name": name, "first": first, "last": last, "org": r[4], "job": r[5],
                            "nick": r[6], "note": r[7], "birthday": r[8], "phones": [], "emails": [],
                            "addresses": [], "urls": []}
        for rid, prop, label, value in ab.execute("SELECT record_id, property, label, value FROM ABMultiValue"):
            p = people.get(rid)
            if not p or value is None:
                continue
            lab = _clean_label(labels.get(label))
            if prop == 3:
                p["phones"].append({"label": lab, "value": str(value)})
            elif prop == 4:
                p["emails"].append({"label": lab, "value": str(value)})
            elif prop == 22:
                p["urls"].append({"label": lab, "value": str(value)})
        try:  # adresler ABMultiValueEntry'de parça parça
            keys = {r[0]: r[1] for r in ab.execute("SELECT ROWID, value FROM ABMultiValueEntryKey")}
            addr: dict[int, dict[str, str]] = {}
            owner: dict[int, int] = {}
            for uid, rid in ab.execute("SELECT UID, record_id FROM ABMultiValue WHERE property=5"):
                owner[uid] = rid
            for parent, key, value in ab.execute("SELECT parent_id, key, value FROM ABMultiValueEntry"):
                if parent in owner:
                    addr.setdefault(parent, {})[keys.get(key, str(key))] = value
            for uid, parts in addr.items():
                text = ", ".join(v for k in ("Street", "SubLocality", "City", "State", "ZIP", "Country") if (v := parts.get(k)))
                if text and owner[uid] in people:
                    people[owner[uid]]["addresses"].append({"label": "", "value": text})
        except sqlite3.DatabaseError:
            pass
        out = [p for p in people.values() if p["name"] or p["phones"] or p["emails"]]
        # Harfle başlayan adlar önce; sadece numarası olan kayıtlar sonda
        out.sort(key=lambda p: (not (p["name"][:1].isalpha()), (p["name"] or "~").casefold()))
        return out

    def contact_photo(self, rid: int) -> Optional[bytes]:
        img = self.db("contact_images", "HomeDomain", "Library/AddressBook/AddressBookImages.sqlitedb")
        if not img:
            return None
        for table in ("ABThumbnailImage", "ABFullSizeImage"):
            try:
                row = img.execute(f"SELECT data FROM {table} WHERE record_id=? ORDER BY format DESC LIMIT 1", (rid,)).fetchone()
            except sqlite3.DatabaseError:
                row = None
            if row and row[0]:
                return bytes(row[0])
        return None

    def contact_index(self) -> dict[str, str]:
        if self._contact_index is None:
            idx: dict[str, str] = {}
            for c in self.contacts():
                for v in c["phones"] + c["emails"]:
                    k = phone_key(v["value"])
                    if k and c["name"]:
                        idx.setdefault(k, c["name"])
            self._contact_index = idx
        return self._contact_index

    def name_for(self, handle: Optional[str]) -> Optional[str]:
        k = phone_key(handle)
        return self.contact_index().get(k) if k else None

    # ---------- iMessage / SMS ----------
    def sms_chats(self) -> list[dict[str, Any]]:
        db = self.db("sms", "HomeDomain", "Library/SMS/sms.db")
        if not db:
            return []
        parts: dict[int, list[str]] = {}
        for cid, hid in db.execute("SELECT chat_id, h.id FROM chat_handle_join j JOIN handle h ON h.ROWID=j.handle_id"):
            parts.setdefault(cid, []).append(hid)
        out = []
        for r in db.execute("""
            SELECT c.ROWID, c.chat_identifier, c.display_name, c.service_name, MAX(m.date) AS last, COUNT(*) AS cnt
            FROM chat c JOIN chat_message_join cmj ON cmj.chat_id=c.ROWID JOIN message m ON m.ROWID=cmj.message_id
            GROUP BY c.ROWID ORDER BY last DESC"""):
            handles = parts.get(r["ROWID"], [r["chat_identifier"]])
            names = [self.name_for(h) or h for h in handles]
            title = r["display_name"] or ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
            out.append({"id": r["ROWID"], "title": title, "handles": handles, "service": r["service_name"],
                        "group": len(handles) > 1, "last": _apple_ts(r["last"]), "count": r["cnt"]})
        return out

    def sms_messages(self, chat_id: int, before: Optional[float] = None, limit: int = 300) -> list[dict[str, Any]]:
        db = self.db("sms", "HomeDomain", "Library/SMS/sms.db")
        if not db:
            return []
        cols = {r[1] for r in db.execute("PRAGMA table_info(message)")}
        ab = "m.attributedBody" if "attributedBody" in cols else "NULL"
        amt = "m.associated_message_type" if "associated_message_type" in cols else "0"
        params: list[Any] = [chat_id]
        cond = ""
        if before:
            cond = " AND m.date < ?"
            raw = before - APPLE_EPOCH
            params.append(raw * 1e9 if self._sms_ns(db) else raw)
        params.append(limit)
        rows = db.execute(f"""
            SELECT m.ROWID, m.text, {ab} AS ab, m.is_from_me, m.date, h.id AS handle, m.cache_has_attachments AS att,
                   {amt} AS amt, m.service
            FROM chat_message_join cmj JOIN message m ON m.ROWID=cmj.message_id
            LEFT JOIN handle h ON h.ROWID=m.handle_id
            WHERE cmj.chat_id=?{cond} ORDER BY m.date DESC LIMIT ?""", params).fetchall()
        ids = [r["ROWID"] for r in rows if r["att"]]
        atts: dict[int, list[dict[str, Any]]] = {}
        if ids:
            q = ",".join("?" * len(ids))
            for a in db.execute(f"""SELECT j.message_id, a.filename, a.mime_type, a.transfer_name, a.total_bytes
                                    FROM message_attachment_join j JOIN attachment a ON a.ROWID=j.attachment_id
                                    WHERE j.message_id IN ({q})""", ids):
                fn = a["filename"] or ""
                if fn.endswith(".pluginPayloadAttachment"):
                    continue  # iMessage bağlantı önizlemesi; çoğu yedekte yok, ek gibi gösterme
                rel = re.sub(r"^(~|/var/mobile|/private/var/mobile)/", "", fn)
                atts.setdefault(a["message_id"], []).append({
                    "domain": "MediaDomain", "path": rel, "mime": a["mime_type"] or "",
                    "name": a["transfer_name"] or Path(fn).name, "size": a["total_bytes"] or 0})
        out = []
        for r in rows:
            if r["amt"] and int(r["amt"]) != 0:
                continue  # tepkiler (tapback) ayrı mesaj gibi gösterilmesin
            text = r["text"] or decode_attributed_body(r["ab"])
            if text:
                text = text.replace("\ufffc", "").strip()
            a = atts.get(r["ROWID"], [])
            if not text and not a:
                continue
            out.append({"id": r["ROWID"], "me": bool(r["is_from_me"]), "t": _apple_ts(r["date"]),
                        "from": None if r["is_from_me"] else (self.name_for(r["handle"]) or r["handle"]),
                        "text": text, "att": a})
        out.reverse()
        return out

    @staticmethod
    def _sms_ns(db: sqlite3.Connection) -> bool:
        r = db.execute("SELECT MAX(date) FROM message").fetchone()
        return bool(r and r[0] and r[0] > 1e14)

    # ---------- WhatsApp ----------
    def whatsapp_domain(self) -> Optional[str]:
        for d in WA_DOMAINS:
            if self.lookup(d, "ChatStorage.sqlite"):
                return d
        return None

    def whatsapp_db(self) -> Optional[sqlite3.Connection]:
        d = self.whatsapp_domain()
        return self.db("whatsapp", d, "ChatStorage.sqlite") if d else None

    def wa_chats(self) -> list[dict[str, Any]]:
        db = self.whatsapp_db()
        if not db:
            return []
        out = []
        for r in db.execute("""
            SELECT s.Z_PK, s.ZPARTNERNAME, s.ZCONTACTJID, s.ZLASTMESSAGEDATE, s.ZSESSIONTYPE, COUNT(m.Z_PK) AS cnt
            FROM ZWACHATSESSION s LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION=s.Z_PK
            GROUP BY s.Z_PK HAVING cnt > 0 ORDER BY s.ZLASTMESSAGEDATE DESC"""):
            jid = r["ZCONTACTJID"] or ""
            if jid.endswith("@status") or jid == "status@broadcast":
                continue
            num = jid.split("@")[0]
            title = r["ZPARTNERNAME"] or self.name_for(num) or num
            out.append({"id": r["Z_PK"], "title": title, "jid": jid, "group": jid.endswith("@g.us"),
                        "last": _apple_ts(r["ZLASTMESSAGEDATE"]), "count": r["cnt"]})
        return out

    def wa_messages(self, chat_id: int, before: Optional[float] = None, limit: int = 300) -> list[dict[str, Any]]:
        db = self.whatsapp_db()
        dom = self.whatsapp_domain()
        if not db:
            return []
        push = {}
        try:
            push = {r[0]: r[1] for r in db.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME")}
        except sqlite3.DatabaseError:
            pass
        params: list[Any] = [chat_id]
        cond = ""
        if before:
            cond = " AND m.ZMESSAGEDATE < ?"
            params.append(before - APPLE_EPOCH)
        params.append(limit)
        rows = db.execute(f"""
            SELECT m.Z_PK, m.ZISFROMME, m.ZTEXT, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZFROMJID,
                   g.ZCONTACTNAME AS gname, g.ZMEMBERJID AS gjid,
                   mi.ZMEDIALOCALPATH AS mpath, mi.ZTITLE AS mtitle, mi.ZFILESIZE AS msize,
                   mi.ZLATITUDE AS lat, mi.ZLONGITUDE AS lon, mi.ZVCARDNAME AS vname
            FROM ZWAMESSAGE m
            LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK=m.ZGROUPMEMBER
            LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK=m.ZMEDIAITEM
            WHERE m.ZCHATSESSION=?{cond} ORDER BY m.ZMESSAGEDATE DESC LIMIT ?""", params).fetchall()
        out = []
        for r in rows:
            typ = r["ZMESSAGETYPE"] or 0
            if typ in (6, 10):  # grup sistem olayları
                continue
            sender = None
            if not r["ZISFROMME"]:
                jid = r["gjid"] or r["ZFROMJID"] or ""
                sender = r["gname"] or push.get(jid) or self.name_for(jid.split("@")[0]) or jid.split("@")[0] or None
            att = []
            if r["mpath"]:
                p = r["mpath"].lstrip("/")
                rel = p if p.startswith("Message/") else f"Message/{p}"
                att.append({"domain": dom, "path": rel, "mime": _wa_mime(typ, rel), "name": Path(rel).name,
                            "size": r["msize"] or 0})
            text = r["ZTEXT"]
            if typ == 5 and r["lat"]:
                text = f"📍 Konum: {r['lat']:.5f}, {r['lon']:.5f}"
            elif typ == 4 and r["vname"]:
                text = f"👤 Kişi kartı: {r['vname']}"
            elif r["mtitle"] and not text:
                text = r["mtitle"]
            if not text and not att:
                if typ == 0:
                    continue
                text = {1: "📷 Fotoğraf", 2: "🎬 Video", 3: "🎤 Sesli mesaj", 8: "📄 Belge", 15: "Çıkartma"}.get(typ, "[ortam]")
            out.append({"id": r["Z_PK"], "me": bool(r["ZISFROMME"]), "t": _apple_ts(r["ZMESSAGEDATE"]),
                        "from": sender, "text": text, "att": att})
        out.reverse()
        return out

    # ---------- arama geçmişi ----------
    def calls(self) -> list[dict[str, Any]]:
        db = self.db("calls", "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
        if not db:
            return []
        cols = {r[1] for r in db.execute("PRAGMA table_info(ZCALLRECORD)")}
        name = "ZNAME" if "ZNAME" in cols else "NULL"
        out = []
        for r in db.execute(f"SELECT ZADDRESS, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, ZCALLTYPE, {name} AS nm "
                            "FROM ZCALLRECORD ORDER BY ZDATE DESC"):
            addr = r["ZADDRESS"]
            if isinstance(addr, bytes):
                addr = addr.decode("utf-8", errors="ignore")
            kind = {1: "Telefon", 8: "FaceTime Video", 16: "FaceTime Sesli"}.get(r["ZCALLTYPE"], "Diğer")
            out.append({"who": r["nm"] or self.name_for(addr) or addr, "number": addr, "t": _apple_ts(r["ZDATE"]),
                        "dur": round(r["ZDURATION"] or 0), "out": bool(r["ZORIGINATED"]),
                        "answered": bool(r["ZANSWERED"]), "kind": kind})
        return out

    # ---------- Safari ----------
    def safari(self, limit: int = 5000) -> list[dict[str, Any]]:
        db = self.db("safari", "HomeDomain", "Library/Safari/History.db")
        if not db:
            return []
        return [{"url": r["url"], "title": r["title"], "t": _apple_ts(r["visit_time"]), "visits": r["visit_count"]}
                for r in db.execute("""SELECT i.url, v.title, v.visit_time, i.visit_count FROM history_visits v
                                       JOIN history_items i ON i.id=v.history_item ORDER BY v.visit_time DESC LIMIT ?""",
                                    (limit,))]


def _clean_label(s: Optional[str]) -> str:
    if not s:
        return ""
    m = re.match(r"_\$!<(.+)>!\$_", s)
    s = m.group(1) if m else s
    return {"Mobile": "Cep", "Home": "Ev", "Work": "İş", "Main": "Ana", "Other": "Diğer", "iPhone": "iPhone",
            "HomeFAX": "Ev faks", "WorkFAX": "İş faks", "Pager": "Çağrı"}.get(s, s)


def _wa_mime(typ: int, path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if typ == 1 or ext in ("jpg", "jpeg", "png", "webp", "heic"):
        return "image/" + ("jpeg" if ext in ("jpg", "jpeg", "") else ext)
    if typ == 2 or ext in ("mp4", "mov"):
        return "video/mp4"
    if typ == 3 or ext in ("opus", "m4a", "aac", "mp3"):
        return "audio/" + (ext or "ogg")
    if typ == 15:
        return "image/webp"
    return "application/octet-stream"


# ---------- dışa aktarma ----------
def to_vcard(contacts: Iterable[dict[str, Any]]) -> str:
    def esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    out = []
    for c in contacts:
        out += ["BEGIN:VCARD", "VERSION:3.0", f"N:{esc(c['last'])};{esc(c['first'])};;;", f"FN:{esc(c['name'])}"]
        if c.get("org"):
            out.append(f"ORG:{esc(c['org'])}")
        if c.get("job"):
            out.append(f"TITLE:{esc(c['job'])}")
        for p in c["phones"]:
            out.append(f"TEL;TYPE={esc(p['label'] or 'CELL')}:{p['value']}")
        for e in c["emails"]:
            out.append(f"EMAIL;TYPE=INTERNET:{e['value']}")
        for a in c["addresses"]:
            out.append(f"ADR:;;{esc(a['value'])};;;;")
        for u in c["urls"]:
            out.append(f"URL:{u['value']}")
        if c.get("note"):
            out.append(f"NOTE:{esc(c['note'])}")
        out.append("END:VCARD")
    return "\r\n".join(out) + "\r\n"
