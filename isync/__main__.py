"""iSync başlatıcı: yerel sunucuyu açar, arayüzü uygulama penceresi olarak gösterir.

Kullanım:  python -m isync [--port 8765] [--no-browser] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_ui(url: str) -> None:
    """Edge/Chrome varsa adres çubuksuz 'uygulama' penceresi; yoksa varsayılan tarayıcı."""
    candidates = []
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
        for base in pf:
            candidates += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        candidates += [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1400,900"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                continue
    webbrowser.open(url)


def running_instance(port: int) -> bool:
    """Bu portta zaten çalışan bir iSync var mı?"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_NAME
    except Exception:
        return False


def selftest() -> int:
    """Paketin tüm parçalarını doğrular; sonucu ~/.isync/selftest.json'a yazar (destek/teşhis için)."""
    import json
    import traceback

    res: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False))}

    def check(name, fn):
        try:
            res[name] = fn() or "ok"
        except Exception as e:
            res[name] = f"HATA: {type(e).__name__}: {e}"
            res.setdefault("_errors", []).append(traceback.format_exc()[-400:])

    def _tk():
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        v = r.tk.call("info", "patchlevel")
        r.destroy()
        return f"ok ({v})"

    def _heif():
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        return f"ok (libheif {pillow_heif.libheif_version()})"

    def _crypto():
        from cryptography.hazmat.bindings import _rust  # noqa: F401  (python3t.dll uyumluluğu)

        return "ok"

    def _pmd3():
        from pymobiledevice3.lockdown import create_using_usbmux  # noqa: F401
        from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service  # noqa: F401

        return "ok"

    def _server():
        from isync import server

        static = server.STATIC
        need = ["index.html", "app.js", "views.js", "backup.js", "style.css",
                "vendor/leaflet/leaflet.js", "vendor/markercluster/leaflet.markercluster.js",
                "i18n.js", "i18n/en.json", "i18n/zh.json"]
        missing = [n for n in need if not (static / n).exists()]
        return "ok" if not missing else f"HATA: eksik dosyalar {missing}"

    def _sqlite():
        import sqlite3

        return f"ok ({sqlite3.sqlite_version})"

    def _driver():
        from isync import diag

        return {"driver_installed": diag.driver_installed(), "service_running": diag.service_running(),
                "usbmux": diag.usbmux_reachable(), "winget": bool(diag.DriverInstaller.winget_path())}

    for n, f in [("tkinter", _tk), ("heif", _heif), ("cryptography", _crypto), ("pymobiledevice3", _pmd3),
                 ("sqlite", _sqlite), ("server_static", _server), ("driver", _driver)]:
        check(n, f)
    out = os.path.join(os.path.expanduser("~"), ".isync")
    os.makedirs(out, exist_ok=True)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "selftest.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    if sys.stdout:
        try:
            print(text)
        except Exception:
            pass
    return 1 if res.get("_errors") or any(isinstance(v, str) and v.startswith("HATA") for v in res.values()) else 0


def main() -> None:
    frozen = getattr(sys, "frozen", False)  # kurulum paketinden (iSync.exe) mi çalışıyor
    if "--uninstall" in sys.argv:
        from isync.uninstall import uninstall

        sys.exit(uninstall(silent="--silent" in sys.argv))
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    ap = argparse.ArgumentParser(prog="isync", description=f"{APP_NAME} {__version__} - iPhone fotoğraf aktarımı")
    ap.add_argument("--host", default="127.0.0.1", help="Ağdan erişim için 0.0.0.0 verin")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Arayüzü otomatik açma")
    ap.add_argument("--auto-exit", action="store_true", default=frozen,
                    help="Pencere kapanınca ve süren iş yoksa sunucuyu kapat (iSync.exe'de varsayılan)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    log_kw: dict = {}
    if frozen or sys.stdout is None:  # pencere uygulamasında konsol yok; günlüğü dosyaya yaz
        log_dir = os.path.join(os.path.expanduser("~"), ".isync")
        os.makedirs(log_dir, exist_ok=True)
        log_kw["filename"] = os.path.join(log_dir, "isync.log")
        log_kw["encoding"] = "utf-8"
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(log_kw["filename"], "a", encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", **log_kw)
    for noisy in ("pymobiledevice3", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Tek örnek: iSync zaten çalışıyorsa yenisini açma, mevcut pencereyi göster
    if args.host == "127.0.0.1" and running_instance(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"{APP_NAME} zaten çalışıyor → {url}")
        if not args.no_browser:
            open_ui(url)
        return

    import uvicorn

    port = free_port(args.port) if args.host == "127.0.0.1" else args.port
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  {APP_NAME} {__version__} çalışıyor →  {url}\n  Kapatmak için bu pencereyi kapatın veya Ctrl+C.\n")

    if args.auto_exit:
        os.environ["ISYNC_AUTO_EXIT"] = "1"

    if not args.no_browser:
        def _later():
            time.sleep(1.2)
            open_ui(url)
        threading.Thread(target=_later, daemon=True).start()

    from isync.server import app  # PyInstaller içinde metinle içe aktarma yerine doğrudan nesne

    uvicorn.run(app, host=args.host, port=port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
