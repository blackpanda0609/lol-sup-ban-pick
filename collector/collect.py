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
  TIMELINE_RATE スキル順・購入順のためにタイムラインも取る試合の割合（既定: 0.3）
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
TIMELINE_RATE = float(os.environ.get("TIMELINE_RATE", "0.3"))
SKILL = {1: "Q", 2: "W", 3: "E"}
# ビルドの集計で残す件数（多すぎるとファイルが大きくなるので、試合数の多い順にこれだけ残す）
KEEP = {"runes": 15, "spells": 6, "items": 25, "boots": 8, "core": 15, "start": 8, "order": 6, "early": 8}
LEGEND, BOOTS = set(), set()

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


def current_version():
    with urllib.request.urlopen("https://ddragon.leagueoflegends.com/api/versions.json", timeout=30) as r:
        return json.load(r)[0]


def load_items(version):
    """完成アイテム（これ以上アップグレードしない2200G以上のもの）と靴を Data Dragon から調べる。"""
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)["data"]
    for iid, it in data.items():
        gold = it.get("gold", {})
        if not gold.get("purchasable") or not it.get("maps", {}).get("11", False):
            continue
        if "Boots" in it.get("tags", []) and gold.get("total", 0) >= 900:
            BOOTS.add(int(iid))
        elif not it.get("into") and gold.get("total", 0) >= 2200:
            LEGEND.add(int(iid))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def add(d, key, win):
    rec = d.setdefault(str(key), [0, 0])
    rec[0] += 1
    rec[1] += int(bool(win))


def count_build(b, p, win):
    """試合データだけでわかるもの：ルーン、サモナースペル、最終的に持っていた完成アイテムと靴。"""
    b["g"] += 1
    perks = p.get("perks") or {}
    st = perks.get("styles") or []
    if len(st) >= 2:
        sh = perks.get("statPerks") or {}
        pri = ",".join(str(s.get("perk")) for s in st[0].get("selections", []))
        sub = ",".join(str(s.get("perk")) for s in st[1].get("selections", []))
        key = f"{st[0].get('style')}:{pri}|{st[1].get('style')}:{sub}|{sh.get('offense')},{sh.get('flex')},{sh.get('defense')}"
        add(b.setdefault("runes", {}), key, win)
    sp = sorted([p.get("summoner1Id", 0), p.get("summoner2Id", 0)])
    add(b.setdefault("spells", {}), f"{sp[0]},{sp[1]}", win)
    for it in {p.get(f"item{i}", 0) for i in range(6)}:
        if it in LEGEND:
            add(b.setdefault("items", {}), it, win)
        elif it in BOOTS:
            add(b.setdefault("boots", {}), it, win)


def max_order(seq):
    """スキルを上げた順（1=Q,2=W,3=E,4=R）から、最大まで上げた順番（例 "QEW"）を出す。"""
    lv, reach, first = {1: 0, 2: 0, 3: 0}, {}, {}
    for i, x in enumerate(seq):
        if x not in lv:
            continue
        lv[x] += 1
        first.setdefault(x, i)
        if lv[x] == 5:
            reach.setdefault(x, i)
    if len([x for x in seq if x in lv]) < 9:
        return ""
    return "".join(SKILL[x] for x in sorted(lv, key=lambda x: (reach.get(x, 999), -lv[x], first.get(x, 999))))


def count_timeline(tl, targets):
    """タイムラインでわかるもの：スキルの上げ順、最初の3レベル、スタートアイテム、完成アイテムを買った順（コア3つ）。"""
    seq = {pid: [] for pid in targets}
    buys = {pid: [] for pid in targets}
    for fr in ((tl or {}).get("info") or {}).get("frames", []):
        for ev in fr.get("events", []):
            pid = ev.get("participantId")
            if pid not in targets:
                continue
            t = ev.get("type")
            if t == "SKILL_LEVEL_UP" and ev.get("levelUpType") == "NORMAL":
                seq[pid].append(ev.get("skillSlot"))
            elif t == "ITEM_PURCHASED":
                buys[pid].append((ev.get("timestamp", 0), ev.get("itemId")))
            elif t == "ITEM_UNDO":  # 買い直しは取り消す
                for i in range(len(buys[pid]) - 1, -1, -1):
                    if buys[pid][i][1] == ev.get("beforeId"):
                        buys[pid].pop(i)
                        break
    for pid, (b, win) in targets.items():
        b["tl"] = b.get("tl", 0) + 1
        s = [x for x in seq[pid] if x in SKILL]
        if len(s) >= 3:
            add(b.setdefault("early", {}), "".join(SKILL[x] for x in s[:3]), win)
        o = max_order(seq[pid])
        if o:
            add(b.setdefault("order", {}), o, win)
        start = sorted(i for ts, i in buys[pid] if ts <= 60000 and i)
        if start:
            add(b.setdefault("start", {}), ",".join(map(str, start)), win)
        core = []
        for ts, i in buys[pid]:
            if i in LEGEND and i not in core:
                core.append(i)
            if len(core) == 3:
                break
        if len(core) == 3:
            add(b.setdefault("core", {}), ",".join(map(str, core)), win)


def count_match(m, patch, stats, seed=None, builds=None):
    """seed: この試合を見つけたプレイヤーの puuid。
    今エメラルド以上にいる人の最近の試合は勝ちが多い（勝ったから今そのランクにいる）ので、
    その人のレーン（とbotなら両チームのADC×サポート）は数えない。残りの8人は選び方と関係がないので偏らない。"""
    """数えたら、タイムライン用に {participantId: (ビルド集計先, 勝ったか)} を返す。数えなかったら None。"""
    info = (m or {}).get("info") or {}
    if info.get("queueId") != QUEUE:
        return None
    if not str(info.get("gameVersion", "")).startswith(patch + "."):
        return None
    if info.get("gameDuration", 0) < 300:  # リメイクを除外
        return None
    lane = {}
    for p in info.get("participants", []):
        lane[(p.get("teamId"), p.get("teamPosition"))] = p
    if any((t, pos) not in lane for t in (100, 200) for pos in POS):
        return None
    targets = {}
    dur = int(info.get("gameDuration", 0))
    seed_pos = next((p.get("teamPosition") for p in info.get("participants", []) if seed and p.get("puuid") == seed), None)
    for pos in POS:
        L = stats["lanes"].setdefault(pos, {"champs": {}, "vs": {}, "n": 0})
        if pos == seed_pos:
            continue
        L["n"] = L.get("n", 0) + 1  # このレーンを数えた試合数（ピック率の分母）
        ext = L.setdefault("ext", {})
        a, b = lane[(100, pos)], lane[(200, pos)]
        for me, op in ((a, b), (b, a)):
            add(L["champs"], me["championId"], me["win"])
            add(L["vs"].setdefault(str(me["championId"]), {}), op["championId"], me["win"])
            # [試合数, 試合時間(秒), 敵にCCを当てた時間(秒), 物理・魔法・確定ダメージ(対チャンピオン)]
            e = ext.setdefault(str(me["championId"]), [0, 0, 0, 0, 0, 0])
            e[0] += 1
            e[1] += dur
            e[2] += int(me.get("timeCCingOthers", 0) or 0)
            e[3] += int(me.get("physicalDamageDealtToChampions", 0) or 0)
            e[4] += int(me.get("magicDamageDealtToChampions", 0) or 0)
            e[5] += int(me.get("trueDamageDealtToChampions", 0) or 0)
            # 試合時間ごとの [試合数, 勝利数]：25分未満 / 25〜30分 / 30〜35分 / 35分以上（アーリー型かレイト型かの判定に使う）
            tb = L.setdefault("time", {}).setdefault(str(me["championId"]), [[0, 0], [0, 0], [0, 0], [0, 0]])
            k = 0 if dur < 1500 else 1 if dur < 1800 else 2 if dur < 2100 else 3
            tb[k][0] += 1
            tb[k][1] += int(bool(me["win"]))
            if builds is not None:
                b = builds.setdefault(pos, {}).setdefault(str(me["championId"]), {"g": 0, "tl": 0})
                count_build(b, me, me["win"])
                targets[me.get("participantId")] = (b, me["win"])
    for t in (100, 200):
        if seed_pos in ("BOTTOM", "UTILITY"):
            break
        adc, sup = lane[(t, "BOTTOM")], lane[(t, "UTILITY")]
        add(stats["duo"].setdefault(str(adc["championId"]), {}), sup["championId"], adc["win"])
    # ban：同じ試合で両チームが同じチャンプをbanしても1回と数える
    banned = {b.get("championId", -1) for t in info.get("teams", []) for b in t.get("bans", [])}
    for c in banned:
        if c and c > 0:
            stats["bans"][str(c)] = stats["bans"].get(str(c), 0) + 1
    stats["ban_matches"] += 1
    stats["matches"] += 1
    return targets


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


def prune(builds):
    for champs in builds.values():
        for b in champs.values():
            for k, n in KEEP.items():
                if k in b and len(b[k]) > n:
                    b[k] = dict(sorted(b[k].items(), key=lambda kv: -kv[1][0])[:n])


def save(patch, stats, seen, builds=None):
    (DATA / "patches").mkdir(parents=True, exist_ok=True)
    (DATA / "seen").mkdir(parents=True, exist_ok=True)
    stats["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (DATA / "patches" / f"{patch}.json").write_text(
        json.dumps(stats, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "seen" / f"{patch}.txt").write_text("\n".join(sorted(seen)), encoding="utf-8")
    if builds is not None:
        (DATA / "builds").mkdir(parents=True, exist_ok=True)
        prune(builds)
        (DATA / "builds" / f"{patch}.json").write_text(
            json.dumps(builds, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
    version = current_version()
    patch = ".".join(version.split(".")[:2])
    load_items(version)
    print(f"items: {len(LEGEND)} completed, {len(BOOTS)} boots", flush=True)
    print(f"patch {patch}, platforms {PLATFORMS}, {MAX_MINUTES} min", flush=True)
    stats = load_json(DATA / "patches" / f"{patch}.json",
                      {"patch": patch, "matches": 0, "lanes": {}, "duo": {}})
    # ban は途中から集計を始めたので、分母（ban を数えた試合数）を別に持つ
    stats.setdefault("bans", {})
    stats.setdefault("ban_matches", 0)
    # これまでの試合は全レーンを数えていたので、レーンごとの分母は試合数と同じ
    for L in stats["lanes"].values():
        L.setdefault("n", stats["matches"])
    builds = load_json(DATA / "builds" / f"{patch}.json", {})
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
                targets = count_match(get(ROUTING, f"/lol/match/v5/matches/{mid}"), patch, stats, seed=puuid, builds=builds)
                if targets is not None:
                    new += 1
                    if targets and random.random() < TIMELINE_RATE:
                        tl = get(ROUTING, f"/lol/match/v5/matches/{mid}/timeline")
                        if tl:
                            count_timeline(tl, targets)
                    if new % 100 == 0:
                        save(patch, stats, seen, builds)
                        print(f"  +{new} matches (players {i + 1}/{len(pool)}, {time_left() / 60:.0f} min left)", flush=True)
    except KeyError_ as e:
        save(patch, stats, seen, builds)
        sys.exit(f"APIキーが無効か期限切れです (HTTP {e}). GitHub の Settings → Secrets で RIOT_API_KEY を更新してください。"
                 f" ここまでの {stats['matches'] - before} 試合は保存しました。")
    save(patch, stats, seen, builds)
    print(f"done: +{stats['matches'] - before} matches, total {stats['matches']} for patch {patch}", flush=True)


if __name__ == "__main__":
    main()
