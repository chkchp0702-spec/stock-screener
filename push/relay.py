"""🔔 앱 푸시 중계 — 지킴이(keeper) 교대 동안 함께 깨어 있으면서
   ntfy 알림 주제(집중·단타·컵·갭·매집·나침반·고래·시황)에 새 글이 오면 앱을 켠 모든 휴대폰에 웹 푸시로 보낸다.
   python push/relay.py <몇 초 동안>
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_priv, open_sub, ALERT_TOPICS, TEST_TOPIC, SUB_TOPIC
import collect

APP = "https://chkchp0702-spec.github.io/daily-app/"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def poll(topics, since):
    url = "https://ntfy.sh/" + ",".join(topics) + f"/json?poll=1&since={since}"
    out = []
    try:
        for ln in urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ch-push"}), timeout=40).read().decode().splitlines():
            m = json.loads(ln)
            if m.get("event") == "message":
                out.append(m)
    except Exception as e:
        log("ntfy 실패", e)
    return sorted(out, key=lambda m: m["time"])


def main(secs):
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    K, vpem, rsa = load_priv()
    vapid = Vapid.from_pem(vpem.encode())
    subs_raw = collect.merge(collect.load(), collect.poll("12h"))
    subs = {}

    def refresh():
        nonlocal subs_raw
        subs_raw = collect.merge(subs_raw, collect.poll("10m"))
        for i, s in list(subs_raw.items()):
            if i not in subs:
                try:
                    subs[i] = open_sub(rsa, s)
                except Exception as e:
                    log("구독 풀기 실패", i, e)
        for i in list(subs):
            if i not in subs_raw:
                subs.pop(i)

    def send(i, payload):
        try:
            webpush(subs[i], json.dumps(payload, ensure_ascii=False), vapid_private_key=vapid,
                    vapid_claims={"sub": APP}, ttl=3600, timeout=20)
            return True
        except WebPushException as e:
            code = getattr(e.response, "status_code", 0)
            log("보내기 실패", i, code)
            if code in (404, 410):          # 앱을 지웠거나 알림을 끈 휴대폰
                subs.pop(i, None); subs_raw.pop(i, None)
                try:
                    urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/" + SUB_TOPIC, data=json.dumps({"op": "off", "id": i}).encode()), timeout=15)
                except Exception:
                    pass
        except Exception as e:
            log("보내기 오류", i, e)
        return False

    refresh()
    log("구독", len(subs))
    end = time.time() + secs
    since = int(time.time()) - 90
    seen = set()
    last_ref = time.time()
    while time.time() < end:
        msgs = poll(ALERT_TOPICS + [TEST_TOPIC], since)
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            since = max(since, m["time"] - 5)
            if m["topic"] == TEST_TOPIC:
                i = (m.get("message") or "").strip()
                if i not in subs:
                    refresh()
                if i in subs:
                    ok = send(i, {"title": "CH Investing", "body": "✅ 알림이 잘 와요! 앞으로 중요한 신호를 이렇게 알려 드릴게요.", "url": APP + "#alarm", "tag": "test"})
                    log("테스트", i, ok)
                continue
            p = {"title": m.get("title") or "CH Investing", "body": (m.get("message") or "")[:300],
                 "url": m.get("click") or APP, "tag": m["topic"].replace("chkchp-ch-", "")}
            n = sum(send(i, p) for i in list(subs))
            log("보냄", p["tag"], p["title"][:30], f"{n}/{len(subs)}")
        if time.time() - last_ref > 300:
            refresh(); last_ref = time.time()
        time.sleep(20)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3600)
