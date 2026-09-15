import requests, pandas as pd, os, time, json, html
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STAMP = NOW.strftime("%Y%m%d_%H%M")
TODAY = NOW.strftime("%Y%m%d")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/"}

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("텔레그램 미설정"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        print("TG", r.status_code)
    except Exception as e:
        print("TG 실패", e)

def sector(code):
    try:
        u = f"https://m.stock.naver.com/api/stock/{code}/basic"
        j = requests.get(u, headers=H, timeout=10).json()
        return j.get("industryCodeType", {}).get("industryGroupKor") or ""
    except Exception:
        return ""

def news(code, n=2):
    try:
        u = (f"https://m.stock.naver.com/api/news/stock/{code}"
             f"?pageSize={n}&page=1")
        j = requests.get(u, headers=H, timeout=10).json()
        out = []
        for grp in j[:n]:
            items = grp.get("items", [])
            if items:
                t = items[0].get("officeName", "")
                title = items[0].get("title", "").replace("&quot;", '"')
                out.append(html.escape(title)[:60])
        return out
    except Exception:
        return []

def fetch(mkt):
    rows, page = [], 1
    while page <= 40:
        u = (f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}"
             f"?page={page}&pageSize=100")
        r = requests.get(u, headers=H, timeout=20)
        if r.status_code != 200:
            break
        items = r.json().get("stocks", [])
        if not items:
            break
        rows += items
        page += 1
        time.sleep(0.2)
    return rows

rows = []
for m in ["KOSPI", "KOSDAQ"]:
    rows += fetch(m)

uni = pd.read_csv("data/universe_latest.csv", dtype={"code": str})
uni = uni.rename(columns={"거래대금": "전일거래대금", "시가총액": "시총"})
keep = set(uni["code"])

df = pd.DataFrame(rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df = df[df["code"].isin(keep)].copy()
df["종가"] = num(df["closePriceRaw"])
df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
cur = df[["code", "name", "종가", "등락률", "거래대금"]]
print(STAMP, "|", len(cur), "종목")

os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")

snaps = sorted(f for f in os.listdir("data/snaps") if f.startswith(TODAY))
if len(snaps) < 2:
    print("첫 스냅샷")
    raise SystemExit(0)

prev = pd.read_csv(f"data/snaps/{snaps[-2]}", dtype={"code": str})
m = cur.merge(prev[["code", "종가", "거래대금"]], on="code", suffixes=("", "_p"))
m = m.merge(uni[["code", "전일거래대금", "시총"]], on="code", how="left")

m["최근5분"] = m["거래대금"] - m["거래대금_p"]
m["가격변화"] = (m["종가"] / m["종가_p"] - 1) * 100
m["회전율"] = m["최근5분"] / m["시총"] * 10000

elapsed = max((NOW.hour * 60 + NOW.minute) - 540, 1)
ratio = min(elapsed / 390, 1.0)
m["전일대비"] = m["거래대금"] / (m["전일거래대금"] * ratio)

if len(snaps) >= 3:
    p2 = pd.read_csv(f"data/snaps/{snaps[-3]}", dtype={"code": str})
    m = m.merge(p2[["code", "거래대금"]].rename(columns={"거래대금": "거래대금_pp"}),
                on="code", how="left")
    base = (m["거래대금_p"] - m["거래대금_pp"]).clip(lower=1)
    m["가속도"] = m["최근5분"] / base
else:
    m["가속도"] = 1.0

m = m[(m["최근5분"] > 3e8) & (m["회전율"].notna())]
if m.empty:
    print("통과 없음"); raise SystemExit(0)

rank = lambda s: s.rank(pct=True) * 100
m["점수"] = (rank(m["회전율"]) * 0.4 + rank(m["전일대비"]) * 0.35
           + rank(m["가속도"]) * 0.25)
m = m.sort_values("점수", ascending=False)

cols = ["code", "name", "종가", "등락률", "가격변화", "점수",
        "회전율", "전일대비", "가속도", "최근5분"]
top = m.head(30)[cols].round(2)
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print(top.head(20).to_string(index=False))

alert = m[(m["점수"] >= 85) & (m["최근5분"] >= 1e9)
          & (m["전일대비"] >= 2.0) & (m["가격변화"] >= 1.0)]

SENT = "data/sent.json"
sent = {}
if os.path.exists(SENT):
    try:
        sent = json.load(open(SENT))
    except Exception:
        sent = {}
sent = {k: v for k, v in sent.items() if v.startswith(TODAY)}

new = []
for _, r in alert.iterrows():
    last = sent.get(r["code"])
    if last:
        t = datetime.strptime(last, "%Y%m%d_%H%M").replace(tzinfo=KST)
        if (NOW - t).total_seconds() < 1800:
            continue
    new.append(r)
    sent[r["code"]] = STAMP

if new:
    lines = [f"🔔 <b>{NOW.strftime('%H:%M')} 신호 {len(new)}건</b>", ""]
    for r in new[:8]:
        sec = sector(r["code"])
        tag = f" · {sec}" if sec else ""
        lines.append(
            f"<b>{r['name']}</b> ({r['code']}){tag}\n"
            f"  {int(r['종가']):,}원  당일 {r['등락률']:+.1f}%  "
            f"5분 {r['가격변화']:+.1f}%\n"
            f"  점수 {r['점수']:.0f} · 전일대비 {r['전일대비']:.1f}배 "
            f"· 5분 {r['최근5분']/1e8:.0f}억")
        for t in news(r["code"]):
            lines.append(f"  📰 {t}")
        lines.append("")
        time.sleep(0.2)
    tg("\n".join(lines))
    print("알람", len(new), "건")
else:
    print("알람 없음")

json.dump(sent, open(SENT, "w"))
