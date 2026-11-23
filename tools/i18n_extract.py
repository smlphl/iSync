"""Arayüzde görünen Türkçe metinleri çıkarır (çeviri anahtarları) ve sözlükte eksik olanları raporlar.

    python tools/i18n_extract.py            # eksik çevirileri listele
    python tools/i18n_extract.py --keys     # tüm anahtarları yaz

Anahtar = arayüzdeki Türkçe metin. Değişken kısımlar {} ile gösterilir; çeviri motoru bunları
düzenli ifadeyle yakalar (ör. "{} öğe seçili · {}").
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "isync" / "static"
DICT_DIR = STATIC / "i18n"
PY_FILES = ["server.py", "diag.py", "backup.py", "sync.py", "device.py", "devinfo.py", "photosdb.py", "geo.py"]
TR_CHARS = set("çğıöşüÇĞİÖŞÜ")
TR_WORDS = re.compile(r"\b(ve|bir|bu|için|yok|var|ile|da|de|mı|mi|seç|öğe|dosya|klasör|yedek|telefon|iPhone|kopyala|"
                      r"aç|kapat|göster|indir|yükle|tara|sil|hata|başarısız|tamam|evet|hayır|Tümü|Fotoğraf|Video)\b", re.I)


def looks_turkish(s: str) -> bool:
    if not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]{2}", s):
        return False
    if re.fullmatch(r"[\w\-./:#%=&?{}]+", s) and not (set(s) & TR_CHARS):  # kod/sınıf adı/yol
        return False
    return bool(set(s) & TR_CHARS) or bool(TR_WORDS.search(s)) or (" " in s and s[:1].isupper())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class TextGrab(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("placeholder", "title", "alt", "aria-label") and v:
                self.out.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def pieces_from_markup(s: str) -> list[str]:
    """Şablon/HTML parçasını metin düğümlerine böl (etiketler arası)."""
    s = re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "\x00", s)  # ${...} -> yer tutucu
    out = []
    for part in re.split(r"<[^>]*>", s):
        part = part.replace("\x00", "{}")
        for chunk in re.split(r"(?:&nbsp;|\n)", part):
            chunk = norm(chunk)
            if chunk and chunk not in ("{}",) and looks_turkish(chunk.replace("{}", "")):
                out.append(chunk)
    # etiket öznitelikleri (title="..." placeholder="...")
    for m in re.finditer(r'(?:title|placeholder|alt)="([^"$]*)"', s):
        if looks_turkish(m.group(1)):
            out.append(norm(m.group(1)))
    return out


PY_STR = re.compile(r'(f?)"((?:\\.|[^"\\\n])*)"|(f?)\'((?:\\.|[^\'\\\n])*)\'')


def js_strings(src: str) -> list[str]:
    """JS kaynağındaki dize/şablon değişmezlerini döndürür. Şablonlardaki ${...} ifadeleri {} olur;
    ifadelerin içindeki dizeler de ayrıca (özyinelemeli) çıkarılır. Yorumlar ve regex değişmezleri atlanır."""
    out: list[str] = []
    i, n = 0, len(src)
    prev = ""  # son anlamlı karakter (regex mi bölme mi kararı için)

    def read_quoted(j: int, q: str) -> tuple[str, int]:
        buf = []
        j += 1
        while j < n and src[j] != q:
            if src[j] == "\\" and j + 1 < n:
                buf.append(src[j + 1]); j += 2; continue
            if src[j] == "\n":
                break
            buf.append(src[j]); j += 1
        return "".join(buf), j + 1

    def read_template(j: int) -> tuple[str, int]:
        buf = []
        j += 1
        while j < n and src[j] != "`":
            if src[j] == "\\" and j + 1 < n:
                buf.append(src[j + 1]); j += 2; continue
            if src[j] == "$" and j + 1 < n and src[j + 1] == "{":
                depth, k = 1, j + 2
                while k < n and depth:
                    c = src[k]
                    if c in "\"'":
                        _, k = read_quoted(k, c); continue
                    if c == "`":
                        _, k = read_template(k); continue
                    depth += c == "{"
                    depth -= c == "}"
                    k += 1
                out.extend(js_strings(src[j + 2:k - 1]))  # ifadenin içindeki dizeler
                buf.append("{}")
                j = k
                continue
            buf.append(src[j]); j += 1
        return "".join(buf), j + 1

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i = src.find("\n", i); i = n if i < 0 else i; continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i = src.find("*/", i + 2); i = n if i < 0 else i + 2; continue
        if c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^"):
            j = i + 1
            in_cls = False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2; continue
                if src[j] == "[": in_cls = True
                elif src[j] == "]": in_cls = False
                elif src[j] == "/" and not in_cls: break
                j += 1
            i = j + 1; prev = "x"; continue
        if c in "\"'":
            text, i = read_quoted(i, c); out.append(text); prev = "x"; continue
        if c == "`":
            text, i = read_template(i); out.append(text); prev = "x"; continue
        if not c.isspace():
            prev = c
        i += 1
    return out


def extract_js(text: str) -> list[str]:
    keys = []
    for s in js_strings(text):
        keys += pieces_from_markup(s.replace("{}", "${x}"))
    return keys


# Arayüzde görünmeyen metinler (SQL, PowerShell, dosya adları, model adları, plist anahtarları…)
IGNORE = re.compile(r"^(SELECT|PRAGMA|AND |Get-|Add-Type|\$d=|Select-Object|inline;|attachment;|iPhone \d|iPhone (SE|Air)|"
                    r"Apple Mobile Device Service$|App Store$|Device Name$|Product Version$|Apple iPhone OS|"
                    r"ne yapmalıyım$|B\.src|.*navItem\(|S\.view|S\.wizard|//|class=\"|iSync Dışa Aktarım$|Tiles &copy;|HTTP \{\}$|.*\$\(\"|if \()")


def extract_py(text: str) -> list[str]:
    """Python: ast ile dize sabitleri ve f-dizeleri (bitişik dizeler birleşik gelir); docstring ve log çağrıları hariç."""
    import ast

    tree = ast.parse(text)
    skip: set[int] = set()
    for node in ast.walk(tree):
        # docstring'ler
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                skip.add(id(first.value))
        # log.info(...), logger.warning(...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "debug", "info", "warning", "error", "exception") :
            for sub in ast.walk(node):
                skip.add(id(sub))
    keys = []

    def render(node) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                parts.append(v.value if isinstance(v, ast.Constant) else "{}")
            return "".join(parts)
        return None

    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                inner.add(id(v))
    for node in ast.walk(tree):
        if id(node) in skip or id(node) in inner:
            continue
        s = render(node)
        if s is None:
            continue
        s = norm(s)
        if s and looks_turkish(s.replace("{}", "")) and not IGNORE.match(s):
            keys.append(s)
    return keys


# Kodda birleştirilerek oluşan (çıkarıcının göremediği) metinler
EXTRA = ["Dışa aktarıldı: {}", "Uygulama boyutları alınamadı: {}", "Geri yükleme koşulları sağlanmıyor: {}",
         "Ocak", "Mart", "Nisan", "Haziran", "Temmuz", "Ekim", "{} sn", "{} sa {} dk", "{} dk {} sn",
         "yedekleme", "Yedekleme", "Tümü", "Fotoğraflar", "Videolar", "Live Photos", "Ekran görüntüleri",
         "Düzenlenmiş", "Favoriler", "Gizli", "Son silinenler", "Yedekleniyor: %{}", "Geri yükleniyor: %{}",
         "{} dosya alındı", "{} geçti", "tahmini {} kaldı", "{} fotoğraf kütüphanesi dosyası atlandı", "{} zaten vardı",
         "{} JPG'ye çevrildi", "şarj oluyor", "{} eşleşme", "Yedek klasörü", "Albüm"]


def all_keys() -> list[str]:
    keys: list[str] = []
    g = TextGrab()
    g.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    keys += [norm(x) for x in g.out if norm(x) and looks_turkish(norm(x)) and not IGNORE.match(norm(x))]
    keys += EXTRA
    for js in ("app.js", "views.js", "backup.js"):
        keys += [k for k in extract_js((STATIC / js).read_text(encoding="utf-8")) if not IGNORE.match(k)]
    for py in PY_FILES:
        keys += extract_py((ROOT / "isync" / py).read_text(encoding="utf-8"))
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main():
    keys = all_keys()
    if "--keys" in sys.argv:
        print(json.dumps(keys, ensure_ascii=False, indent=1))
        return
    dicts = {}
    for lang in ("en", "zh"):
        p = DICT_DIR / f"{lang}.json"
        dicts[lang] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    missing = {lang: [k for k in keys if k not in d] for lang, d in dicts.items()}
    for lang, ks in missing.items():
        print(f"[{lang}] eksik: {len(ks)} / {len(keys)}")
        for k in ks[:400]:
            print("   ", k)
    sys.exit(1 if any(missing.values()) else 0)


if __name__ == "__main__":
    main()
