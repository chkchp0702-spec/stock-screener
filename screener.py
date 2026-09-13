import requests, pandas as pd, os, time, json
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STAMP = NOW.strftime("%Y%m%d_%H%M")

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/"}

uni = pd.read_csv("data/universe_latest.csv", dtype={"code": str})
codes = uni["code"].tolist()
print("유니버스", len(codes), "종목 |", STAMP)

def snap(code):
    u = f"https://m.stock.naver.com/api/stock/{code}/integration"
    r = requests.get(u, headers=H, timeout=10)
    if r.status_code != 200:
        return None
    j = r.json()
    d = j.get("totalInfos", [])
    kv = {x.get("code"): x.get("value") for x in d}
    return {
        "code": code,
        "거래대금": kv.get("accumulatedTradingValue"),
        "거래량": kv.get("accumulatedTradingVolume"),
        "종가": j.get("dealTrendInfos", [{}])[0].get("closePrice") if j.get("dealTrendInfos") else None,
    }

rows = []
for i, c in enumerate(codes):
    try:
        s = snap(c)
        if s:
            rows.append(s)
    except Exception:
        pass
    if i % 100 == 0:
        print("진행", i)
    time.sleep(0.08)

cur = pd.DataFrame(rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", "").str.replace("억", ""), errors="coerce")
for col in ["거래대금", "거래량", "종가"]:
    cur[col] = num(cur[col])

os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")

# 직전 스냅샷과 비교
snaps = sorted(os.listdir("data/snaps"))
if len(snaps) < 2:
    print("첫 스냅샷 저장 완료 - 비교는 다음 실행부터")
    raise SystemExit(0)

prev = pd.read_csv(f"data/snaps/{snaps[-2]}", dtype={"code": str})
m = cur.merge(prev, on="code", suffixes=("", "_p"))
m["증가액"] = m["거래대금"] - m["거래대금_p"]
m["가격변화"] = (m["종가"] / m["종가_p"] - 1) * 100

m = m.merge(uni[["code", "name", "market"]], on="code", how="left")
m = m[m["증가액"] > 0].sort_values("증가액", ascending=False)

top = m.head(30)[["code", "name", "market", "종가", "가격변화", "증가액", "거래대금"]]
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print("\n=== 5분간 자금 유입 상위 20 ===")
print(top.head(20).to_string(index=False))
