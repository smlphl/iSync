"""Kurulu iSync'i kaldırır (iSync.exe --uninstall). Fotoğraflara, yedeklere ve ayarlara dokunmaz."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\iSync"
CREATE_NO_WINDOW = 0x08000000


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
        ok = messagebox.askyesno(
            "iSync'i kaldır",
            f"iSync bu bilgisayardan kaldırılsın mı?\n\n{install_dir}\n\n"
            "Fotoğraflarınız, yedekleriniz ve ayarlarınız (~/.isync) silinmez.")
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
        messagebox.showinfo("iSync", "iSync kaldırıldı.")
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(uninstall("--silent" in sys.argv))
