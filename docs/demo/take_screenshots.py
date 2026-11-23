"""README ekran görüntülerini demo sunucusundan (SAHTE verilerle) üretir: docs/screenshots/*.png

Edge/Chrome'u başsız açar, DevTools protokolüyle her ekranı yükler ve sayfa hazır olunca
(<html data-ready="1">, docs/demo/shot.js koyar) ekran görüntüsü alır. Ek kütüphane gerekmez.

    python docs/demo/take_screenshots.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "screenshots"
W, H = 1440, 900
SHOTS = [  # (dosya, port, ?shot=)
    ("gallery", 8790, "jobcard"), ("map", 8790, "map"), ("device", 8790, "device"), ("apps", 8790, "apps"),
    ("whatsapp", 8790, "whatsapp"), ("backup", 8790, "backup"), ("wizard", 8791, "wizard"),
]


def find_browser() -> str:
    cands = [os.path.expandvars(p) for p in (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")]
    cands += [shutil.which(n) or "" for n in ("google-chrome", "chromium", "microsoft-edge")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    raise SystemExit("Edge veya Chrome bulunamadı")


class WS:
    """DevTools için asgari WebSocket istemcisi (metin çerçeveleri)."""

    def __init__(self, url: str):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(4096)
        assert b" 101 " in resp.split(b"\r\n")[0], resp[:200]
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def _read(self, k: int) -> bytes:
        while len(self.buf) < k:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError("websocket kapandı")
            self.buf += chunk
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def _frame(self) -> tuple[int, bytes]:
        b1, b2 = self._read(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        return b1 & 0x0F, self._read(ln)

    def send(self, method: str, **params):
        self.n += 1
        data = json.dumps({"id": self.n, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        hdr = bytes([0x81])
        ln = len(data)
        hdr += bytes([0x80 | ln]) if ln < 126 else (bytes([0x80 | 126]) + struct.pack(">H", ln) if ln < 65536
                                                     else bytes([0x80 | 127]) + struct.pack(">Q", ln))
        self.s.sendall(hdr + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))
        want = self.n
        msg = b""
        while True:
            op, payload = self._frame()
            msg += payload
            if op == 0x9:  # ping
                continue
            try:
                obj = json.loads(msg)
            except ValueError:
                continue  # parçalı çerçeve
            msg = b""
            if obj.get("id") == want:
                if "error" in obj:
                    raise RuntimeError(obj["error"])
                return obj.get("result", {})


def wait_http(url: str, timeout: float = 20):
    t = time.time()
    while time.time() - t < timeout:
        try:
            return urllib.request.urlopen(url, timeout=2).read()
        except Exception:
            time.sleep(0.3)
    raise SystemExit(f"yanıt yok: {url}")


def capture(name: str, port: int, shot: str) -> tuple[bool, int]:
    req = urllib.request.Request("http://127.0.0.1:9333/json/new?about:blank", method="PUT")
    target = json.loads(urllib.request.urlopen(req).read())
    try:
        ws = WS(target["webSocketDebuggerUrl"])
        ws.s.settimeout(30)
        ws.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        ws.send("Page.navigate", url=f"http://127.0.0.1:{port}/?shot={shot}")
        t0, ready = time.time(), False
        while time.time() - t0 < 40:
            r = ws.send("Runtime.evaluate", expression="document.documentElement.getAttribute('data-ready')",
                        returnByValue=True)
            if r.get("result", {}).get("value") == "1":
                ready = True
                break
            time.sleep(0.4)
        time.sleep(0.8)  # son resimler ve geçişler
        img = ws.send("Page.captureScreenshot", format="png")["data"]
        (OUT / f"{name}.png").write_bytes(base64.b64decode(img))
        ws.s.close()
        return ready, len(img) * 3 // 4 // 1024
    finally:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:9333/json/close/{target['id']}", timeout=5)
        except Exception:
            pass


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    demo = ROOT / "docs" / "demo" / "demo_server.py"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    srv = [subprocess.Popen([py, str(demo), "--port", "8790"], creationflags=flags),
           subprocess.Popen([py, str(demo), "--port", "8791", "--wizard"], creationflags=flags)]
    prof = tempfile.mkdtemp(prefix="isync-shot-")
    br = subprocess.Popen([find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                           f"--user-data-dir={prof}", "--remote-debugging-port=9333", f"--window-size={W},{H}", "about:blank"],
                          creationflags=flags)
    try:
        wait_http("http://127.0.0.1:8790/api/info")
        wait_http("http://127.0.0.1:8791/api/info")
        wait_http("http://127.0.0.1:9333/json/version")
        for name, port, shot in SHOTS:
            for attempt in range(3):  # başsız tarayıcı ara sıra yanıtsız kalabiliyor: yeniden dene
                try:
                    ready, size = capture(name, port, shot)
                    print(f"{name:10s} {'hazır' if ready else 'ZAMAN AŞIMI'}  {size} KB")
                    break
                except Exception as e:
                    print(f"{name:10s} deneme {attempt + 1} başarısız: {type(e).__name__}")
    finally:
        br.terminate()
        for s in srv:
            s.terminate()
        time.sleep(1)
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
