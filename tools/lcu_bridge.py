#!/usr/bin/env python3
"""BPノートのクライアント連携用の補助プログラム。

LoL クライアントの（非公式の）LCU API からチャンプ選択の状況を読み取り、
http://127.0.0.1:8899/draft で BPノートのサイトに渡す。
読み取るだけで、ピックや BAN の操作は一切しない。

使い方：このファイル（または BPNoteLink.exe）を実行すると、ブラウザで BPノートが連携オンの状態で開く。
      このウィンドウは開いたままにしておく。
テスト：環境変数 LCU_MOCK に session の JSON ファイルを指定すると、クライアントなしで動く。
"""
import base64
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import webbrowser
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("BRIDGE_PORT", "8899"))
MOCK = os.environ.get("LCU_MOCK", "")
# 起動したらブラウザで開くページ（?link=1 で連携オンの状態で開く）。BRIDGE_NO_BROWSER=1 で開かない
SITE = "https://blackpanda0609.github.io/lol-sup-ban-pick/?link=1"
NO_BROWSER = os.environ.get("BRIDGE_NO_BROWSER", "") or MOCK
LOCKFILES = [
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
]
POS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "bottom": "BOTTOM", "utility": "UTILITY"}

# LCU は自己署名の証明書なので、検証しない（つなぐ先は自分の PC の中だけ）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

_cred = None  # (port, token)


def find_credentials():
    """起動中の LeagueClientUx の引数から、なければ lockfile から、ポートと合言葉を探す。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\").CommandLine"],
            capture_output=True, text=True, timeout=10).stdout
        port = re.search(r"--app-port=(\d+)", out)
        token = re.search(r"--remoting-auth-token=([\w-]+)", out)
        if port and token:
            return port.group(1), token.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    for lf in LOCKFILES:
        try:
            parts = lf.read_text(encoding="utf-8").split(":")
            return parts[2], parts[3]
        except (OSError, IndexError):
            continue
    return None


def lcu_get(path):
    """LCU に GET する。クライアントが見つからなければ None、チャンプ選択中でなければ {}。"""
    global _cred
    if MOCK:
        return json.loads(Path(MOCK).read_text(encoding="utf-8")) if path.endswith("session") else "ChampSelect"
    for attempt in range(2):
        if not _cred:
            _cred = find_credentials()
            if not _cred:
                return None
        port, token = _cred
        auth = base64.b64encode(f"riot:{token}".encode()).decode()
        req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=3) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code in (401, 403):  # クライアントを再起動すると合言葉が変わる
                _cred = None
                continue
            return {}
        except (urllib.error.URLError, OSError):
            _cred = None  # クライアントが閉じた・ポートが変わった
    return None


def build_draft():
    phase = lcu_get("/lol-gameflow/v1/gameflow-phase")
    if phase is None:
        return {"client": False}
    s = lcu_get("/lol-champ-select/v1/session") or {}
    if not s or not s.get("myTeam"):
        return {"client": True, "champSelect": False, "phase": phase if isinstance(phase, str) else ""}
    me = s.get("localPlayerCellId")
    ally = []
    for p in s.get("myTeam", []):
        cid = p.get("championId") or 0
        ally.append({
            "position": POS.get((p.get("assignedPosition") or "").lower(), ""),
            "championId": str(cid) if cid > 0 else "",
            "intent": str(p.get("championPickIntent") or "") if not cid else "",
            "isMe": p.get("cellId") == me,
        })
    enemy = [str(p.get("championId")) for p in s.get("theirTeam", []) if (p.get("championId") or 0) > 0]
    bans = []
    for group in s.get("actions", []):
        for a in group:
            if a.get("type") == "ban" and a.get("completed") and (a.get("championId") or 0) > 0:
                bans.append(str(a["championId"]))
    return {"client": True, "champSelect": True, "ally": ally, "enemy": enemy, "bans": sorted(set(bans))}


class Server(ThreadingHTTPServer):
    # Windows では標準の設定だと同じポートを2つ目のプログラムも使えてしまうので、独り占めにする
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def _headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # github.io のページから読めるように
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_OPTIONS(self):
        self._headers(204)

    def do_GET(self):
        if self.path.split("?")[0] != "/draft":
            self._headers(404)
            self.wfile.write(b"{}")
            return
        self._headers()
        self.wfile.write(json.dumps(build_draft(), ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):  # アクセスのたびに表示しない
        pass


def main():
    print("BPノート クライアント連携")
    print("  終了するときは、このウィンドウを閉じるか Ctrl+C を押してください。")
    if MOCK:
        print(f"  テストモード：{MOCK} を返します")
    elif find_credentials():
        print("  LoL クライアントが見つかりました。")
    else:
        print("  LoL クライアントがまだ見つかりません。起動すると自動でつながります。")
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        # すでに起動している：ブラウザだけ開いて終わる
        print("  すでに起動しています。BPノートを開きます。")
        if not NO_BROWSER:
            webbrowser.open(SITE)
        time.sleep(3)
        return
    if not NO_BROWSER:
        print("  ブラウザで BPノートを開きます（連携はオンになっています）。")
        webbrowser.open(SITE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
