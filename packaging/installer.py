"""iSync kurulum programı (PyInstaller ile tek dosya iSync-Setup.exe olur).

- Yönetici izni gerektirmez: %LOCALAPPDATA%\\Programs\\iSync altına kurar
- Başlat menüsü + (isteğe bağlı) masaüstü kısayolu, Programlar ve Özellikler'de kaldırma kaydı
- iPhone bağlantısı için gereken Apple Mobile Device Service yoksa kullanıcıyı
  Microsoft Store'daki "Apple Devices" uygulamasına yönlendirir (Apple'ın sürücüsünü biz dağıtmayız)
- Sessiz kurulum: iSync-Setup.exe --silent [--dir YOL] [--no-desktop] [--no-launch] [--driver] [--lang tr|en|zh]
- Dil: Windows diline göre başlar, sağ üstten değiştirilebilir; seçilen dil uygulamaya da aktarılır
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

APP = "iSync"
EXE = "iSync.exe"
STORE_URL = "ms-windows-store://pdp/?ProductId=9NP83LWLPZ9K"  # Apple Devices (Microsoft Store)
STORE_WEB = "https://apps.microsoft.com/detail/9NP83LWLPZ9K"
ITUNES_WEB = "https://www.apple.com/itunes/"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\iSync"
CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- dil (Windows arayüz diline göre)
def _ui_lang() -> str:
    try:
        import ctypes

        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return {0x1F: "tr", 0x04: "zh"}.get(primary, "en")
    except Exception:
        return "en"


LANG = os.environ.get("ISYNC_LANG") or _ui_lang()
_L = {
    "en": {
        "{} {} Kurulumu": "{} {} Setup",
        "iPhone fotoğraf aktarımı, harita, yedek ve geri yükleme": "iPhone photo transfer, map, backup and restore",
        "Kurulum klasörü:": "Install folder:",
        "Gözat…": "Browse…",
        "Masaüstüne kısayol koy": "Create a desktop shortcut",
        "Kurulumdan sonra iSync'i başlat": "Launch iSync after setup",
        "iPhone bağlantı sürücüsü": "iPhone connection driver",
        "Apple sürücüsünü de otomatik kur (önerilir · Microsoft Store, ücretsiz)": "Also install the Apple driver automatically (recommended · Microsoft Store, free)",
        "Microsoft Store'da aç": "Open in Microsoft Store",
        "Tekrar denetle": "Check again",
        "✓ Apple Mobile Device Service bulundu. iPhone bağlantısı hazır.": "✓ Apple Mobile Device Service found. iPhone connection is ready.",
        "APPLE_MISSING": "Apple's iPhone driver is not on this computer. iSync needs the free \"Apple Devices\" app to connect to the phone. If the option below is checked, setup installs it for you too (Store terms apply).",
        "Kur": "Install",
        "Kapat": "Close",
        "Bitir": "Finish",
        "Kuruldu: {}": "Installed: {}",
        "Kurulum başarısız:\n{}": "Setup failed:\n{}",
        "DRIVER_FAIL_TAIL": "\n\nThe Microsoft Store page will open; click 'Get' there. When iSync opens, the connection assistant will guide you step by step.",
        "iSync kuruldu. Başlat menüsünden açabilirsiniz.": "iSync is installed. You can open it from the Start menu.",
        "Apple sürücüsü zaten kurulu.": "The Apple driver is already installed.",
        "Apple Devices Microsoft Store'dan indiriliyor ve kuruluyor… (birkaç dakika sürebilir)": "Downloading and installing Apple Devices from the Microsoft Store… (may take a few minutes)",
        "Bu Windows'ta winget yok.": "winget isn't available on this Windows.",
        "winget çalıştırılamadı: {}": "Couldn't run winget: {}",
        "Apple sürücüsü kuruldu.": "Apple driver installed.",
        "Otomatik kurulum tamamlanamadı (kod {}).": "Automatic install didn't complete (code {}).",
        "Çalışan iSync kapatılıyor…": "Closing running iSync…",
        "Eski sürüm kaldırılıyor…": "Removing old version…",
        "Dosyalar kopyalanıyor… {}": "Copying files… {}",
        "Kısayollar oluşturuluyor…": "Creating shortcuts…",
        "Kaldırma kaydı yazılıyor…": "Registering uninstaller…",
        "Kurulum tamamlandı.": "Setup complete.",
        "iPhone fotoğraf ve yedek aracı": "iPhone photo and backup tool",
        "Kısayol oluşturulamadı: {}": "Couldn't create shortcut: {}",
    },
    "zh": {
        "{} {} Kurulumu": "{} {} 安装程序",
        "iPhone fotoğraf aktarımı, harita, yedek ve geri yükleme": "iPhone 照片传输、地图、备份与恢复",
        "Kurulum klasörü:": "安装文件夹：",
        "Gözat…": "浏览…",
        "Masaüstüne kısayol koy": "创建桌面快捷方式",
        "Kurulumdan sonra iSync'i başlat": "安装后启动 iSync",
        "iPhone bağlantı sürücüsü": "iPhone 连接驱动",
        "Apple sürücüsünü de otomatik kur (önerilir · Microsoft Store, ücretsiz)": "同时自动安装 Apple 驱动（推荐 · Microsoft Store，免费）",
        "Microsoft Store'da aç": "在 Microsoft Store 中打开",
        "Tekrar denetle": "重新检查",
        "✓ Apple Mobile Device Service bulundu. iPhone bağlantısı hazır.": "✓ 已找到 Apple Mobile Device Service，iPhone 连接已就绪。",
        "APPLE_MISSING": "这台电脑上没有 Apple 的 iPhone 驱动。iSync 需要免费的“Apple Devices”应用才能连接手机。如果勾选下面的选项，安装程序也会为你安装它（适用 Store 使用条款）。",
        "Kur": "安装",
        "Kapat": "关闭",
        "Bitir": "完成",
        "Kuruldu: {}": "已安装：{}",
        "Kurulum başarısız:\n{}": "安装失败：\n{}",
        "DRIVER_FAIL_TAIL": "\n\n将打开 Microsoft Store 页面，请在其中点击“获取”。iSync 打开后，连接助手会一步步指导你。",
        "iSync kuruldu. Başlat menüsünden açabilirsiniz.": "iSync 已安装，可从开始菜单打开。",
        "Apple sürücüsü zaten kurulu.": "Apple 驱动已安装。",
        "Apple Devices Microsoft Store'dan indiriliyor ve kuruluyor… (birkaç dakika sürebilir)": "正在从 Microsoft Store 下载并安装 Apple Devices…（可能需要几分钟）",
        "Bu Windows'ta winget yok.": "此 Windows 上没有 winget。",
        "winget çalıştırılamadı: {}": "无法运行 winget：{}",
        "Apple sürücüsü kuruldu.": "Apple 驱动已安装。",
        "Otomatik kurulum tamamlanamadı (kod {}).": "自动安装未完成（代码 {}）。",
        "Çalışan iSync kapatılıyor…": "正在关闭运行中的 iSync…",
        "Eski sürüm kaldırılıyor…": "正在移除旧版本…",
        "Dosyalar kopyalanıyor… {}": "正在复制文件… {}",
        "Kısayollar oluşturuluyor…": "正在创建快捷方式…",
        "Kaldırma kaydı yazılıyor…": "正在注册卸载程序…",
        "Kurulum tamamlandı.": "安装完成。",
        "iPhone fotoğraf ve yedek aracı": "iPhone 照片与备份工具",
        "Kısayol oluşturulamadı: {}": "无法创建快捷方式：{}",
    },
}
_TR = {
    "APPLE_MISSING": "Bu bilgisayarda Apple'ın iPhone sürücüsü yok. iSync telefona bağlanabilmek için ücretsiz \"Apple Devices\" "
                     "uygulamasına ihtiyaç duyar. Aşağıdaki seçenek işaretliyse kurulum onu da sizin için kurar "
                     "(Store kullanım koşulları geçerlidir).",
    "DRIVER_FAIL_TAIL": "\n\nMicrosoft Store sayfası açılacak; oradan 'Yükle'ye basın. "
                        "iSync açıldığında bağlantı yardımcısı sizi adım adım yönlendirecek.",
}


LANG_NAMES = {"tr": "Türkçe", "en": "English", "zh": "中文"}


def save_lang(lang: str) -> None:
    """Kurulumda seçilen dili uygulamaya aktar (~/.isync/settings.json içindeki 'lang')."""
    import json

    home = Path.home() / ".isync"
    f = home / "settings.json"
    try:
        s = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        s = {}
    s["lang"] = lang
    home.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def T(text: str, *args) -> str:
    s = _L.get(LANG, {}).get(text) or _TR.get(text) or text
    return s.format(*args) if args else s


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def payload_version() -> str:
    try:
        with zipfile.ZipFile(resource("payload.zip")) as z:
            return z.read("version.txt").decode().strip()
    except Exception:
        return "?"


def default_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / APP


def apple_service_installed() -> bool:
    import winreg

    for name in ("Apple Mobile Device Service",):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}"))
            return True
        except OSError:
            pass
    return False


def winget_path() -> str | None:
    import shutil

    p = shutil.which("winget")
    if p:
        return p
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(local) if local.exists() else None


def install_apple_driver(progress=lambda pct, msg: None) -> tuple[bool, str]:
    """Apple Devices'ı Microsoft Store'dan winget ile kurar. (başarılı mı, mesaj)"""
    if apple_service_installed():
        return True, T("Apple sürücüsü zaten kurulu.")
    wg = winget_path()
    if not wg:
        return False, T("Bu Windows'ta winget yok.")
    progress(None, T("Apple Devices Microsoft Store'dan indiriliyor ve kuruluyor… (birkaç dakika sürebilir)"))
    try:
        r = subprocess.run([wg, "install", "--id", "9NP83LWLPZ9K", "--source", "msstore", "--exact",
                            "--accept-package-agreements", "--accept-source-agreements",
                            "--disable-interactivity", "--silent"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=1800, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        return False, T("winget çalıştırılamadı: {}", e)
    if apple_service_installed():
        return True, T("Apple sürücüsü kuruldu.")
    return False, T("Otomatik kurulum tamamlanamadı (kod {}).", r.returncode)


def stop_running(install_dir: Path) -> None:
    """Kurulu iSync çalışıyorsa kapat (sadece bu klasördeki iSync.exe)."""
    exe = str(install_dir / EXE)
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name='{EXE}'\" | "
          f"Where-Object {{ $_.ExecutablePath -eq '{exe}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=CREATE_NO_WINDOW, capture_output=True)


def shortcut(path: Path, target: Path, workdir: Path, desc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ISYNC_LNK);"
          "$s.TargetPath=$env:ISYNC_TARGET;$s.WorkingDirectory=$env:ISYNC_WD;"
          "$s.IconLocation=$env:ISYNC_TARGET+',0';$s.Description=$env:ISYNC_DESC;$s.Save()")
    env = dict(os.environ, ISYNC_LNK=str(path), ISYNC_TARGET=str(target), ISYNC_WD=str(workdir), ISYNC_DESC=desc)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       creationflags=CREATE_NO_WINDOW, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(T("Kısayol oluşturulamadı: {}", r.stderr.strip()[:200]))


def special_folder(name: str) -> Path:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"[Environment]::GetFolderPath('{name}')"],
                       creationflags=CREATE_NO_WINDOW, capture_output=True, text=True)
    return Path(r.stdout.strip())


def register_uninstall(install_dir: Path, version: str) -> None:
    import winreg

    size_kb = sum(f.stat().st_size for f in install_dir.rglob("*") if f.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        vals = {
            "DisplayName": APP, "DisplayVersion": version, "Publisher": "iSync",
            "InstallLocation": str(install_dir), "DisplayIcon": str(install_dir / EXE),
            "UninstallString": f'"{install_dir / EXE}" --uninstall',
            "QuietUninstallString": f'"{install_dir / EXE}" --uninstall --silent',
            "URLInfoAbout": "https://github.com/",
        }
        for n, v in vals.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))


def install(install_dir: Path, desktop: bool, progress=lambda pct, msg: None) -> Path:
    version = payload_version()
    progress(2, T("Çalışan iSync kapatılıyor…"))
    stop_running(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    # Eski sürüm dosyalarını temizle (kullanıcı verileri burada değil, ~/.isync ve fotoğraf klasöründe)
    progress(5, T("Eski sürüm kaldırılıyor…"))
    for sub in ("_internal",):
        p = install_dir / sub
        if p.exists():
            import shutil

            shutil.rmtree(p, ignore_errors=True)
    with zipfile.ZipFile(resource("payload.zip")) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        total = sum(m.file_size for m in members) or 1
        done = 0
        for m in members:
            target = install_dir / m.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            done += m.file_size
            progress(5 + 80 * done / total, T("Dosyalar kopyalanıyor… {}", m.filename[-60:]))
    progress(88, T("Kısayollar oluşturuluyor…"))
    exe = install_dir / EXE
    shortcut(special_folder("Programs") / f"{APP}.lnk", exe, install_dir, T("iPhone fotoğraf ve yedek aracı"))
    if desktop:
        shortcut(special_folder("Desktop") / f"{APP}.lnk", exe, install_dir, T("iPhone fotoğraf ve yedek aracı"))
    progress(95, T("Kaldırma kaydı yazılıyor…"))
    register_uninstall(install_dir, version)
    try:
        save_lang(LANG)
    except Exception:
        pass
    progress(100, T("Kurulum tamamlandı."))
    return exe


# ---------------------------------------------------------------- arayüz
def gui(args) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    version = payload_version()
    root = tk.Tk()
    root.geometry("640x580")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(resource("iSync.ico")))
    except Exception:
        pass
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass

    # Dil değişince arayüz yeniden çizilir; kullanıcının seçimleri bu değişkenlerde korunur
    dir_var = tk.StringVar(value=str(Path(args.dir) if args.dir else default_dir()))
    desk = tk.BooleanVar(value=not args.no_desktop)
    launch = tk.BooleanVar(value=not args.no_launch)
    drv = tk.BooleanVar(value=True)
    lang_var = tk.StringVar(value=LANG_NAMES.get(LANG, "English"))
    state = {"frm": None}

    def build():
        global LANG
        if state["frm"] is not None:
            state["frm"].destroy()
        root.title(T("{} {} Kurulumu", APP, version))
        frm = ttk.Frame(root, padding=20)
        frm.pack(fill="both", expand=True)
        state["frm"] = frm

        head = ttk.Frame(frm)
        head.pack(fill="x")
        try:
            img = tk.PhotoImage(file=str(resource("iSync.png"))).subsample(4, 4)
            lbl = ttk.Label(head, image=img)
            lbl.image = img
            lbl.pack(side="left", padx=(0, 14))
        except Exception:
            pass
        t = ttk.Frame(head)
        t.pack(side="left", fill="x")
        ttk.Label(t, text=f"{APP} {version}", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(t, text=T("iPhone fotoğraf aktarımı, harita, yedek ve geri yükleme")).pack(anchor="w")

        # Dil seçici (sağ üst)
        lang_box = ttk.Frame(head)
        lang_box.pack(side="right", anchor="n")
        ttk.Label(lang_box, text="🌐").pack(side="left", padx=(0, 4))
        lang_cb = ttk.Combobox(lang_box, textvariable=lang_var, values=list(LANG_NAMES.values()),
                               state="readonly", width=9)
        lang_cb.pack(side="left")

        def on_lang(_e=None):
            global LANG
            code = next((k for k, v in LANG_NAMES.items() if v == lang_var.get()), "en")
            if code != LANG:
                LANG = code
                root.after(0, build)

        lang_cb.bind("<<ComboboxSelected>>", on_lang)

        ttk.Label(frm, text=T("Kurulum klasörü:")).pack(anchor="w", pady=(18, 2))
        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=dir_var).pack(side="left", fill="x", expand=True)

        def browse():
            p = filedialog.askdirectory(initialdir=dir_var.get())
            if p:
                dir_var.set(str(Path(p) / APP) if Path(p).name.lower() != APP.lower() else p)

        ttk.Button(row, text=T("Gözat…"), command=browse).pack(side="left", padx=(6, 0))

        ttk.Checkbutton(frm, text=T("Masaüstüne kısayol koy"), variable=desk).pack(anchor="w", pady=(12, 0))
        ttk.Checkbutton(frm, text=T("Kurulumdan sonra iSync'i başlat"), variable=launch).pack(anchor="w")

        # Apple sürücüsü
        box = ttk.LabelFrame(frm, text=T("iPhone bağlantı sürücüsü"), padding=10)
        box.pack(fill="x", pady=(14, 0))
        apple_lbl = ttk.Label(box, text="", wraplength=560, justify="left")
        apple_lbl.pack(anchor="w")
        drv_chk = ttk.Checkbutton(box, text=T("Apple sürücüsünü de otomatik kur (önerilir · Microsoft Store, ücretsiz)"),
                                  variable=drv)
        btns = ttk.Frame(box)
        store_btn = ttk.Button(btns, text=T("Microsoft Store'da aç"), command=lambda: os.startfile(STORE_URL))
        recheck_btn = ttk.Button(btns, text=T("Tekrar denetle"), command=lambda: refresh_apple())

        def refresh_apple():
            if apple_service_installed():
                apple_lbl.config(text=T("✓ Apple Mobile Device Service bulundu. iPhone bağlantısı hazır."))
                drv.set(False)
                drv_chk.pack_forget()
                btns.pack_forget()
            else:
                apple_lbl.config(text=T("APPLE_MISSING"))
                drv_chk.pack(anchor="w", pady=(6, 0))
                btns.pack(anchor="w", pady=(6, 0))
                store_btn.pack(side="left")
                recheck_btn.pack(side="left", padx=(6, 0))

        refresh_apple()

        bar = ttk.Progressbar(frm, maximum=100)
        bar.pack(fill="x", pady=(16, 4))
        status = ttk.Label(frm, text="", foreground="#555")
        status.pack(anchor="w")
        actions = ttk.Frame(frm)
        actions.pack(side="bottom", fill="x")
        go = ttk.Button(actions, text=T("Kur"))
        cancel = ttk.Button(actions, text=T("Kapat"), command=root.destroy)
        cancel.pack(side="right")
        go.pack(side="right", padx=(0, 8))

        def set_progress(pct, msg):
            def apply():
                if pct is None:  # süresi belirsiz adım (sürücü indirme)
                    bar.config(mode="indeterminate"); bar.start(12)
                else:
                    bar.stop(); bar.config(mode="determinate", value=pct)
                status.config(text=msg)
            root.after(0, apply)

        def run():
            go.config(state="disabled")
            cancel.config(state="disabled")
            lang_cb.config(state="disabled")  # kurulum sürerken dil değişmez
            target = Path(dir_var.get())

            def work():
                try:
                    exe = install(target, desk.get(), set_progress)
                except Exception as e:
                    def fail(e=e):
                        messagebox.showerror(APP, T("Kurulum başarısız:\n{}", e))
                        go.config(state="normal")
                        cancel.config(state="normal")
                        lang_cb.config(state="readonly")
                    root.after(0, fail)
                    return
                drv_msg = ""
                if drv.get() and not apple_service_installed():
                    ok, drv_msg = install_apple_driver(set_progress)
                    set_progress(100, drv_msg)
                    if not ok:
                        root.after(0, lambda: messagebox.showwarning(APP, drv_msg + T("DRIVER_FAIL_TAIL")))
                        try:
                            os.startfile(STORE_URL)
                        except Exception:
                            pass

                def done():
                    refresh_apple()
                    status.config(text=T("Kuruldu: {}", target) + (f" · {drv_msg}" if drv_msg else ""))
                    cancel.config(state="normal", text=T("Bitir"))
                    if launch.get():
                        subprocess.Popen([str(exe)], cwd=str(target), close_fds=True)
                        root.after(1200, root.destroy)
                    else:
                        messagebox.showinfo(APP, T("iSync kuruldu. Başlat menüsünden açabilirsiniz."))

                root.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        go.config(command=run)

    build()
    root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description=f"{APP} kurulumu")
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--dir")
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--driver", action="store_true", help="Apple sürücüsü yoksa winget ile kur")
    ap.add_argument("--lang", choices=list(LANG_NAMES), help="Kurulum ve uygulama dili (tr/en/zh)")
    args = ap.parse_args()
    if args.lang:
        global LANG
        LANG = args.lang
    if args.silent:
        target = Path(args.dir) if args.dir else default_dir()
        exe = install(target, not args.no_desktop, lambda p, m: print(f"{p:5.1f}% {m}"))
        if args.driver:
            print(install_apple_driver(lambda p, m: print(m))[1])
        print("apple_service:", apple_service_installed())
        if not args.no_launch:
            subprocess.Popen([str(exe)], cwd=str(target), close_fds=True)
        return
    gui(args)


if __name__ == "__main__":
    main()
