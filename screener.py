import requests, pandas as pd, os, time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
STAMP = datetime.now(KST).strftime("%Y%m%d_%H%M")

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/"}

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
keep = set(uni["code"])

df = pd.DataFrame(rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df = df[df["code"].isin(keep)].copy()
df["종가"] = num(df["closePriceRaw"])
df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
cur = df[["code", "name", "종가", "등락률", "거래대금"]]
print(STAMP, "|", len(cur), "종목 수집")

os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")

snaps = sorted(f for f in os.listdir("data/snaps") if f.endswith(".csv"))
if len(snaps) < 2:
    raise SystemExit("첫 스냅샷 - 비교는 다음 실행부터")

prev = pd.read_csv(f"data/snaps/{snaps[-2]}", dtype={"code": str})
m = cur.merge(prev[["code", "종가", "거래대금"]], on="code", suffixes=("", "_p"))
m["유입액"] = m["거래대금"] - m["거래대금_p"]
m["가격변화"] = (m["종가"] / m["종가_p"] - 1) * 100
m = m[m["유입액"] > 0].sort_values("유입액", ascending=False)

top = m.head(30)[["code", "name", "종가", "등락률", "가격변화", "유입액", "거래대금"]]
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print("\n=== 직전 대비 자금 유입 상위 20 ===")
print(top.head(20).to_string(index=False))
