import os, requests, datetime

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*",
     "Referer": "https://www.nextrade.co.kr/"}


def send(text):
    t, c = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    print(text)
    if not (t and c):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                      data={"chat_id": c, "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=40)
    except Exception as e:
        print(f"TG 전송 예외(도착했을 수 있음): {repr(e)[:80]}")


# 1) NXT 프리마켓 시세
r = requests.get("https://www.nextrade.co.kr/brdinfoTime/brdinfoTimeList.do",
                 headers=H, params={"pageUnit": 1000, "pageSize": 1000}, timeout=20)
lst = r.json().get("brdinfoTimeList", [])
info = lst[0] if lst else {}
cre = str(info.get("creTime", ""))

cand = []
for x in lst:
    try:
        price = float(x.get("curPrc") or 0)
        rate = float(x.get("upDownRate") or 0)
        val = float(x.get("accTrval") or 0)
    except (TypeError, ValueError):
        continue
    if price <= 0 or val < 3e8 or rate < 2.0:
        continue
    cand.append({"code": str(x.get("isuSrdCd", ""))[-6:],
                 "name": x.get("isuAbwdNm", ""),
                 "price": price, "rate": rate, "val": val,
                 "ratio": 0.0, "warn": ""})

print(f"creTime={cre} 전체 {len(lst)} 후보 {len(cand)}")

# 2) 거래대금 상위 30개만 전일 거래대금 조회
cand.sort(key=lambda z: z["val"], reverse=True)
cand = cand[:30]

today = datetime.date.today()
start = (today - datetime.timedelta(days=12)).strftime("%Y%m%d")
end = today.strftime("%Y%m%d")

for c in cand:
    try:
        d = requests.get("https://api.finance.naver.com/siseJson.naver",
                         params={"symbol": c["code"], "requestType": 1,
                                 "startTime": start, "endTime": end,
                                 "timeframe": "day"},
                         headers=H, timeout=8).text
        rows = [s for s in d.replace("'", '"').split("[") if '"2' in s]
        if len(rows) >= 2:
            prev = rows[-2].split(",")
            pv = float(prev[4]) * float(prev[5])
            if pv > 0:
                c["ratio"] = c["val"] / pv
        if not c["ratio"]:
            print(f"  [DBG] {c['name']} rows={len(rows)} raw={d[:120]}")
    except Exception as e:
        print(f"  전일조회 실패 {c['name']}: {repr(e)[:80]}")

    try:
        b = requests.get(f"https://m.stock.naver.com/api/stock/{c['code']}/basic",
                         timeout=6).json()
        if b.get("stockEndType") == "managed" or b.get("isSupervision"):
            c["warn"] = "관리"
    except Exception:
        pass

cand = [c for c in cand if not c["warn"]]

# 3) 점수 = 거래대금40 + 등락률35 + 전일배율25
def pct(items, key):
    s = sorted(items, key=lambda z: z[key])
    n = max(len(s) - 1, 1)
    for i, z in enumerate(s):
        z[key + "_p"] = i / n * 100

for k in ("val", "rate", "ratio"):
    pct(cand, k)
for c in cand:
    c["score"] = c["val_p"] * 0.40 + c["rate_p"] * 0.35 + c["ratio_p"] * 0.25

cand.sort(key=lambda z: z["score"], reverse=True)
top = cand[:5]

# 4) 메시지
now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
head = f"🌅 <b>{now:%m/%d} 프리마켓 TOP5</b>  (기준 {cre[:4] or '--'})"

if not top:
    send(head + "\n\n조건 통과 종목 없음\n"
         f"(전체 {len(lst)} / 프리마켓 거래 미형성일 수 있음)")
else:
    lines = [head, ""]
    for i, c in enumerate(top, 1):
        url = f"https://m.stock.naver.com/domestic/stock/{c['code']}/total"
        lines.append(f"{i}. <a href=\"{url}\">{c['name']}</a>  {c['score']:.0f}점")
        lines.append(f"   {c['price']:,.0f}원 ({c['rate']:+.2f}%)  "
                     f"{c['val']/1e8:.0f}억"
                     + (f"  전일{c['ratio']:.1f}배" if c['ratio'] else ""))
        lines.append("")
    lines.append("※ 참고용 지표입니다. 투자 판단은 본인 책임입니다.")
    send("\n".join(lines))
