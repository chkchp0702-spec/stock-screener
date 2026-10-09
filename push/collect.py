"""🔔 앱 푸시 구독 모으기: ntfy(12시간 보관) → archive/x/push_subs.json (암호문 그대로 · 켬/끔 최신 것만)"""
import datetime as dt
import json
import os
import sys
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SUBS, SUB_TOPIC


def poll(since="all"):
    req = urllib.request.Request(f"https://ntfy.sh/{SUB_TOPIC}/json?poll=1&since={since}", headers={"User-Agent": "ch-push"})
    out = []
    for ln in urllib.request.urlopen(req, timeout=40).read().decode().splitlines():
        try:
            m = json.loads(ln)
            if m.get("event") == "message":
                out.append(m)
        except Exception:
            pass
    return sorted(out, key=lambda m: m["time"])


def merge(subs, msgs):
    for m in msgs:
        try:
            b = json.loads(m["message"])
        except Exception:
            continue
        i = str(b.get("id", ""))[:40]
        if not i:
            continue
        if b.get("op") == "on" and all(k in b for k in ("k", "iv", "ct")):
            old = subs.get(i)
            if not old or old.get("t", 0) <= m["time"]:
                subs[i] = {"k": b["k"], "iv": b["iv"], "ct": b["ct"], "t": m["time"]}
        elif b.get("op") == "off":
            if i in subs and subs[i].get("t", 0) <= m["time"]:
                subs.pop(i)
    return subs


def load():
    try:
        return json.load(open(SUBS, encoding="utf-8")).get("subs", {})
    except Exception:
        return {}


if __name__ == "__main__":
    subs = merge(load(), poll())
    os.makedirs(os.path.dirname(SUBS), exist_ok=True)
    json.dump({"at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"), "n": len(subs), "subs": subs},
              open(SUBS, "w", encoding="utf-8"), separators=(",", ":"))
    print("푸시 구독", len(subs))
