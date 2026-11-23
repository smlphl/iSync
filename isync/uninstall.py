"""Kurulu iSync'i kaldırır (iSync.exe --uninstall). Fotoğraflara, yedeklere ve ayarlara dokunmaz."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\iSync"
CREATE_NO_WINDOW = 0x08000000


def _lang() -> str:
    try:  # önce kurulumda/arayüzde seçilen dil
        import json

        lang = json.loads((Path.home() / ".isync" / "settings.json").read_text(encoding="utf-8")).get("lang")
        if lang in ("tr", "en", "zh"):
            return lang
    except Exception:
        pass
    try:
        import ctypes

        return {0x1F: "tr", 0x04: "zh"}.get(ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF, "en")
    except Exception:
        return "en"


_MSG = {
    "tr": ("iSync'i kaldır", "iSync bu bilgisayardan kaldırılsın mı?\n\n{}\n\nFotoğraflarınız, yedekleriniz ve ayarlarınız (~/.isync) silinmez.",
           "iSync kaldırıldı."),
    "en": ("Uninstall iSync", "Remove iSync from this computer?\n\n{}\n\nYour photos, backups and settings (~/.isync) are not deleted.",
           "iSync has been uninstalled."),
    "zh": ("卸载 iSync", "要从这台电脑上移除 iSync 吗？\n\n{}\n\n你的照片、备份和设置（~/.isync）不会被删除。", "iSync 已卸载。"),
}


def _folder(name: str) -> Path:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"[Environment]::GetFolderPath('{name}')"],
                       creationflags=CREATE_NO_WINDOW, capture_output=True, text=True)
    return Path(r.stdout.strip())


def uninstall(silent: bool = False) -> int:
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        print("Kaldırma yalnızca kurulu iSync.exe için geçerlidir.")
        return 1
    install_dir = Path(sys.executable).resolve().parent
    if not (install_dir / "_internal").is_dir():
        return 1
    if not silent:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        title, question, _ = _MSG[_lang()]
        ok = messagebox.askyesno(title, question.format(install_dir))
        root.destroy()
        if not ok:
            return 0
    # Arka planda çalışan iSync (sunucu) dosyaları kilitler; kendimiz hariç kapat
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name='iSync.exe'\" | "
          f"Where-Object {{ $_.ExecutablePath -eq '{install_dir / 'iSync.exe'}' -and $_.ProcessId -ne {os.getpid()} }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=CREATE_NO_WINDOW, capture_output=True)
    for name in ("Programs", "Desktop"):
        try:
            (_folder(name) / "iSync.lnk").unlink(missing_ok=True)
        except Exception:
            pass
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # Çalışan exe kendini silemez: kısa bir gecikmeyle klasörü ayrı bir süreç silsin
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 > nul & rmdir /s /q "{install_dir}"',
                     creationflags=CREATE_NO_WINDOW | 0x00000008, close_fds=True)  # DETACHED_PROCESS
    if not silent:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("iSync", _MSG[_lang()][2])
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(uninstall("--silent" in sys.argv))
