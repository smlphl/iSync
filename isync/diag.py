"""Sistem ve iPhone bağlantı tanılaması + onarım eylemleri.

Hiçbir şey bilmeyen bir kullanıcıyı telefon bağlanana kadar adım adım yönlendirmek için:
her adım {id, title, status: ok|fail|wait|skip, detail, action?} döner. Arayüz bunu
düzenli aralıkla sorgular; ilk başarısız adım "ne yapmalıyım" sorusunun cevabıdır.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Optional

log = logging.getLogger("isync.diag")

SERVICE = "Apple Mobile Device Service"
STORE_ID = "9NP83LWLPZ9K"  # Microsoft Store: Apple Devices
STORE_URL = f"ms-windows-store://pdp/?ProductId={STORE_ID}"
STORE_WEB = f"https://apps.microsoft.com/detail/{STORE_ID}"
CREATE_NO_WINDOW = 0x08000000
IS_WIN = sys.platform == "win32"


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW if IS_WIN else 0,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- Windows kontrolleri
def windows_build() -> int:
    try:
        return int(platform.version().split(".")[2])
    except Exception:
        return 0


def driver_installed() -> bool:
    if not IS_WIN:
        return True
    import winreg

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE}"))
        return True
    except OSError:
        return False


def service_running() -> Optional[bool]:
    """sc çıktısındaki durum kodu dilden bağımsızdır: 'STATE : 4 RUNNING'."""
    if not IS_WIN:
        return None
    try:
        r = _run(["sc", "query", SERVICE], timeout=10)
    except Exception:
        return None
    for line in r.stdout.splitlines():
        if ":" in line and ("STATE" in line.upper() or "DURUM" in line.upper()):
            code = line.split(":", 1)[1].strip().split(" ")[0]
            return code == "4"
    return False if r.returncode else None


def usbmux_reachable() -> bool:
    if IS_WIN:
        try:
            with socket.create_connection(("127.0.0.1", 27015), timeout=1.5):
                return True
        except OSError:
            return False
    return os.path.exists("/var/run/usbmuxd")


def usb_apple_devices() -> Optional[list[dict[str, str]]]:
    """USB'de görünen Apple aygıtları (sürücü sorunlarını kablo sorunlarından ayırmak için)."""
    if not IS_WIN:
        return None
    ps = ("Get-CimInstance Win32_PnPEntity -Filter \"Name LIKE '%Apple%' OR Name LIKE '%iPhone%' OR Name LIKE '%iPad%'\" | "
          "Select-Object Name,Status,ConfigManagerErrorCode | ConvertTo-Json -Compress")
    try:
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=15)
        import json

        data = json.loads(r.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return [{"name": d.get("Name") or "", "status": d.get("Status") or "",
                 "error": d.get("ConfigManagerErrorCode")} for d in data]
    except Exception:
        return None


# ---------------------------------------------------------------- sürücü kurulumu (winget / Store)
class DriverInstaller:
    def __init__(self):
        self.state: dict[str, Any] = {"status": "idle", "log": ""}
        self._lock = threading.Lock()

    @staticmethod
    def winget_path() -> Optional[str]:
        p = shutil.which("winget")
        if p:
            return p
        local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "winget.exe")
        return local if os.path.exists(local) else None

    def start(self) -> dict[str, Any]:
        """Apple Devices'ı Microsoft Store'dan (winget ile) kurar. Kullanıcı düğmeye bastığında çağrılır."""
        if not self._lock.acquire(blocking=False):
            return self.state
        wg = self.winget_path()
        if not wg:
            self._lock.release()
            open_store()
            self.state = {"status": "store", "log": "winget bulunamadı; Microsoft Store sayfası açıldı."}
            return self.state
        self.state = {"status": "running", "log": "Apple Devices indiriliyor ve kuruluyor… (birkaç dakika sürebilir)",
                      "started": time.time()}

        def work():
            try:
                r = _run([wg, "install", "--id", STORE_ID, "--source", "msstore", "--exact",
                          "--accept-package-agreements", "--accept-source-agreements",
                          "--disable-interactivity", "--silent"], timeout=1800)
                out = (r.stdout + "\n" + r.stderr).strip()
                tail = "\n".join(l for l in out.splitlines() if l.strip() and not set(l.strip()) <= set("-\\|/█▒ "))[-600:]
                if r.returncode == 0 or driver_installed():
                    self.state = {"status": "done", "log": "Apple Devices kuruldu."}
                else:
                    # Kurulu ise winget hata kodu dönebilir; sürücüye bakarak karar ver
                    self.state = {"status": "error", "log": f"Otomatik kurulum başarısız (kod {r.returncode}). "
                                  f"Microsoft Store sayfası açıldı; oradan 'Yükle'ye basın.\n{tail}"}
                    open_store()
            except Exception as e:
                self.state = {"status": "error", "log": f"Otomatik kurulum başarısız: {e}. Microsoft Store sayfası açıldı."}
                open_store()
            finally:
                self._lock.release()

        threading.Thread(target=work, daemon=True).start()
        return self.state


def open_store() -> None:
    if not IS_WIN:
        return
    try:
        os.startfile(STORE_URL)  # noqa
    except Exception:
        try:
            os.startfile(STORE_WEB)  # noqa
        except Exception:
            pass


def start_service_elevated() -> bool:
    """Servisi yönetici olarak başlat (UAC onayı çıkar)."""
    if not IS_WIN:
        return False
    import ctypes

    cmd = f'/c sc config "{SERVICE}" start= auto & sc start "{SERVICE}"'
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0)
    return r > 32


# ---------------------------------------------------------------- tam tanılama
async def diagnose(devices) -> dict[str, Any]:
    """devices: DeviceManager. Telefonla gerçek bağlantıyı da dener (eşleştirme / Güven)."""
    steps: list[dict[str, Any]] = []

    def add(id_, title, status, detail="", action=None):
        steps.append({"id": id_, "title": title, "status": status, "detail": detail, "action": action})

    if IS_WIN:
        build = windows_build()
        add("windows", "Windows sürümü", "ok" if build >= 17763 or build == 0 else "fail",
            "" if build >= 17763 or build == 0 else "Windows 10 (1809) veya daha yenisi gerekli. Windows Update ile güncelleyin.")

        inst = await asyncio.to_thread(driver_installed)
        if not inst:
            add("driver", "Apple iPhone sürücüsü", "fail",
                "iPhone'a bağlanmak için Apple'ın ücretsiz 'Apple Devices' uygulaması gerekiyor. "
                "Aşağıdaki düğme onu Microsoft Store'dan kurar.", "install_driver")
            return _finish(steps)
        add("driver", "Apple iPhone sürücüsü", "ok", "Apple Mobile Device Service kurulu.")

        running = await asyncio.to_thread(service_running)
        mux = await asyncio.to_thread(usbmux_reachable)
        if not mux:
            if running is False:
                add("service", "Apple bağlantı servisi", "fail",
                    "Servis kurulu ama çalışmıyor. 'Servisi başlat' düğmesine basın ve çıkan Windows onayına 'Evet' deyin.",
                    "start_service")
            else:
                add("service", "Apple bağlantı servisi", "wait",
                    "Servis başlıyor… Birkaç saniye bekleyin. Düzelmezse bilgisayarı yeniden başlatın.", "start_service")
            return _finish(steps)
        add("service", "Apple bağlantı servisi", "ok", "Çalışıyor.")

    # ---- telefon listede mi?
    try:
        devs = await devices.list()
    except Exception as e:
        add("usb", "iPhone bağlantısı", "fail", str(e))
        return _finish(steps)

    if not devs:
        usb = await asyncio.to_thread(usb_apple_devices) if IS_WIN else None
        seen = [d for d in (usb or []) if "iphone" in d["name"].lower() or "apple mobile device" in d["name"].lower()]
        if seen and any(d.get("error") not in (0, None) for d in seen):
            add("usb", "iPhone kabloyla bağlı", "fail",
                "Telefon USB'de görünüyor ama Windows sürücüsü düzgün yüklenmemiş. Telefonu çıkarıp tekrar takın; "
                "düzelmezse bilgisayarı yeniden başlatın.")
        elif seen:
            add("usb", "iPhone kabloyla bağlı", "wait",
                "Telefon USB'de görünüyor. Telefonun kilidini açın; ekranda 'Bu Bilgisayara Güvenilsin mi?' çıkarsa "
                "'Güven'e dokunup parolanızı girin.")
        else:
            add("usb", "iPhone kabloyla bağlı", "fail",
                "Telefon algılanmadı. iPhone'u USB kablosuyla bilgisayara takın ve kilidini açın. Algılanmazsa başka bir "
                "USB girişi veya orijinal kablo deneyin (sadece şarj eden kablolar veri taşımaz).")
        return _finish(steps)
    names = ", ".join(d.get("name") or "iPhone" for d in devs) + (" (USB)" if any(d.get("connection") == "USB" for d in devs) else "")
    add("usb", "iPhone kabloyla bağlı", "ok", names)

    # ---- eşleştirme / güven
    try:
        dev = await asyncio.wait_for(devices.get(), timeout=20)
        add("trust", "Bu bilgisayara güven", "ok", f"{dev.info.get('name')} · {dev.info.get('model')} · iOS {dev.info.get('ios')}")
    except Exception as e:
        cause = e.__cause__ or e
        kind = type(cause).__name__
        devices.current = None
        if "PasswordRequired" in kind:
            add("trust", "Telefonun kilidini açın", "wait",
                "iPhone kilitli. Kilidi açın ve ekranda 'Bu Bilgisayara Güvenilsin mi?' çıkarsa 'Güven'e dokunun.")
        elif "PairingDialogResponsePending" in kind:
            add("trust", "iPhone'da 'Güven'e dokunun", "wait",
                "iPhone ekranında 'Bu Bilgisayara Güvenilsin mi?' sorusu bekliyor. 'Güven'e dokunup parolanızı girin.")
        elif "UserDeniedPairing" in kind:
            add("trust", "Güven reddedildi", "fail",
                "Telefonda 'Güvenme' seçilmiş. Telefonu çıkarıp tekrar takın; soru yeniden çıkınca 'Güven'e dokunun. "
                "Soru çıkmazsa: Ayarlar › Genel › iPhone'u Aktar veya Sıfırla › Sıfırla › Konumu ve Gizliliği Sıfırla.")
        elif isinstance(e, asyncio.TimeoutError):
            add("trust", "iPhone'da 'Güven'e dokunun", "wait",
                "Telefon yanıt vermiyor. Kilidini açın ve ekrandaki soruyu yanıtlayın.")
        else:
            add("trust", "Telefonla bağlantı", "fail",
                f"Bağlantı kurulamadı ({kind}). Telefonu çıkarıp tekrar takın ve kilidini açın.")
        return _finish(steps)
    return _finish(steps)


def _finish(steps: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(steps) and all(s["status"] == "ok" for s in steps) and steps[-1]["id"] == "trust"
    return {"ok": ok, "steps": steps, "platform": sys.platform}
