#!/usr/bin/env python3
"""Riot API からエメラルド以上のソロランク試合を集め、
レーン対面ごと・bot（ADC×サポート）の組み合わせごとの勝敗を数えて data/ に保存する。

環境変数
  RIOT_API_KEY  必須。GitHub Actions では Secrets から渡す
  PLATFORMS     集めるサーバー（既定: jp1,kr）
  MAX_MINUTES   この時間で切り上げて保存する（既定: 300）
  DAYS          何日前までの試合を見るか（既定: 3）
  PAGES         ダイヤ・エメの各ディビジョンから読むページ数（既定: 1、1ページ約200人）
  RATE_2MIN     2分あたりのリクエスト上限（既定: 95。Production Key なら上げてよい）
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

KEY = os.environ.get("RIOT_API_KEY", "").strip()
PLATFORMS = [p.strip() for p in os.environ.get("PLATFORMS", "jp1,kr").split(",") if p.strip()]
ROUTING = "asia"  # jp1 と kr の試合データはどちらも asia
QUEUE = 420  # ソロ/デュオランク
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "300"))
DAYS = int(os.environ.get("DAYS", "3"))
PAGES = int(os.environ.get("PAGES", "1"))
RATE_2MIN = int(os.environ.get("RATE_2MIN", "95"))
DATA = Path(os.environ.get("DATA_DIR", "data"))

POS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
APEX = ["challengerleagues", "grandmasterleagues", "masterleagues"]
DIVS = [(t, d) for t in ("DIAMOND", "EMERALD") for d in ("I", "II", "III", "IV")]

START = time.time()


def time_left():
    return MAX_MINUTES * 60 - (time.time() - START)


class KeyError_(Exception):
    pass


class Limiter:
    """ホストごとに 1秒19回・2分 RATE_2MIN 回までに抑える。"""

    def __init__(self):
        self.calls = {}

    def wait(self, host):
        q = self.calls.setdefault(host, deque())
        while True:
            now = time.time()
            while q and now - q[0] > 120:
                q.popleft()
            last_sec = sum(1 for t in q if now - t < 1)
            if len(q) < RATE_2MIN and last_sec < 19:
                q.append(now)
                return
            time.sleep(0.25 if last_sec >= 19 else max(0.25, 120 - (now - q[0]) + 0.05))


limiter = Limiter()


def get(host, path, params=None):
    url = f"https://{host}.api.riotgames.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Riot-Token": KEY, "User-Agent": "lol-sup-ban-pick/1.0"})
    for attempt in range(5):
        limiter.wait(host)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 10)
                print(f"  429 rate limited, waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                body = e.read().decode("utf-8", "replace")[:200]
                print(f"  HTTP {e.code} {path} -> {body}", flush=True)
                raise KeyError_(e.code)
            if e.code == 404:
                return None
            if e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  HTTP {e.code} {url}", flush=True)
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2 * (attempt + 1))
    return None


def current_patch():
    with urllib.request.urlopen("https://ddragon.leagueoflegends.com/api/versions.json", timeout=30) as r:
        v = json.load(r)[0]
    return ".".join(v.split(".")[:2])


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def add(d, key, win):
    rec = d.setdefault(str(key), [0, 0])
    rec[0] += 1
    rec[1] += int(bool(win))


def count_match(m, patch, stats):
    info = (m or {}).get("info") or {}
    if info.get("queueId") != QUEUE:
        return False
    if not str(info.get("gameVersion", "")).startswith(patch + "."):
        return False
    if info.get("gameDuration", 0) < 300:  # リメイクを除外
        return False
    lane = {}
    for p in info.get("participants", []):
        lane[(p.get("teamId"), p.get("teamPosition"))] = p
    if any((t, pos) not in lane for t in (100, 200) for pos in POS):
        return False
    for pos in POS:
        L = stats["lanes"].setdefault(pos, {"champs": {}, "vs": {}})
        a, b = lane[(100, pos)], lane[(200, pos)]
        for me, op in ((a, b), (b, a)):
            add(L["champs"], me["championId"], me["win"])
            add(L["vs"].setdefault(str(me["championId"]), {}), op["championId"], me["win"])
    for t in (100, 200):
        adc, sup = lane[(t, "BOTTOM")], lane[(t, "UTILITY")]
        add(stats["duo"].setdefault(str(adc["championId"]), {}), sup["championId"], adc["win"])
    stats["matches"] += 1
    return True


def players(platform):
    out = []
    for a in APEX:
        r = get(platform, f"/lol/league/v4/{a}/by-queue/RANKED_SOLO_5x5")
        out += [e["puuid"] for e in (r or {}).get("entries", []) if e.get("puuid")]
    for t, d in DIVS:
        for page in range(1, PAGES + 1):
            r = get(platform, f"/lol/league/v4/entries/RANKED_SOLO_5x5/{t}/{d}", {"page": page})
            out += [e["puuid"] for e in (r or []) if e.get("puuid")]
    print(f"{platform}: {len(out)} players", flush=True)
    return out


def save(patch, stats, seen):
    (DATA / "patches").mkdir(parents=True, exist_ok=True)
    (DATA / "seen").mkdir(parents=True, exist_ok=True)
    stats["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (DATA / "patches" / f"{patch}.json").write_text(
        json.dumps(stats, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "seen" / f"{patch}.txt").write_text("\n".join(sorted(seen)), encoding="utf-8")
    idx = load_json(DATA / "index.json", {"patches": []})
    idx["patches"] = [p for p in idx["patches"] if p["patch"] != patch]
    idx["patches"].append({"patch": patch, "matches": stats["matches"], "updated": stats["updated"]})
    idx["patches"].sort(key=lambda p: [int(x) for x in p["patch"].split(".")], reverse=True)
    idx.update({"platforms": PLATFORMS, "tier": "EMERALD+", "queue": QUEUE})
    (DATA / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    if not KEY:
        sys.exit("RIOT_API_KEY が設定されていません。")
    # キーそのものは出さず、形だけ確かめる（正しいキーは RGAPI- で始まる 42 文字）
    print(f"key check: starts with RGAPI-={KEY.startswith('RGAPI-')}, length={len(KEY)}", flush=True)
    patch = current_patch()
    print(f"patch {patch}, platforms {PLATFORMS}, {MAX_MINUTES} min", flush=True)
    stats = load_json(DATA / "patches" / f"{patch}.json",
                      {"patch": patch, "matches": 0, "lanes": {}, "duo": {}})
    seen_path = DATA / "seen" / f"{patch}.txt"
    seen = set(seen_path.read_text(encoding="utf-8").split()) if seen_path.exists() else set()
    before = stats["matches"]
    try:
        pool = [pu for pf in PLATFORMS for pu in players(pf)]
        random.shuffle(pool)
        since = int(time.time()) - DAYS * 86400
        new = 0
        for i, puuid in enumerate(pool):
            if time_left() < 60:
                print("time limit reached", flush=True)
                break
            ids = get(ROUTING, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                      {"queue": QUEUE, "type": "ranked", "startTime": since, "count": 20}) or []
            for mid in ids:
                if mid in seen or time_left() < 60:
                    continue
                seen.add(mid)
                if count_match(get(ROUTING, f"/lol/match/v5/matches/{mid}"), patch, stats):
                    new += 1
                    if new % 100 == 0:
                        save(patch, stats, seen)
                        print(f"  +{new} matches (players {i + 1}/{len(pool)}, {time_left() / 60:.0f} min left)", flush=True)
    except KeyError_ as e:
        save(patch, stats, seen)
        sys.exit(f"APIキーが無効か期限切れです (HTTP {e}). GitHub の Settings → Secrets で RIOT_API_KEY を更新してください。"
                 f" ここまでの {stats['matches'] - before} 試合は保存しました。")
    save(patch, stats, seen)
    print(f"done: +{stats['matches'] - before} matches, total {stats['matches']} for patch {patch}", flush=True)


if __name__ == "__main__":
    main()
