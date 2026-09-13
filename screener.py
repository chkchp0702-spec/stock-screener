import requests, pandas as pd, os, time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STAMP = NOW.strftime("%Y%m%d_%H%M")
TODAY = NOW.strftime("%Y%m%d")

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
uni = uni.rename(columns={"거래대금": "전일거래대금", "시가총액": "시총"})
keep = set(uni["code"])

df = pd.DataFrame(rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df = df[df["code"].isin(keep)].copy()
df["종가"] = num(df["closePriceRaw"])
df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
df["거래량"] = num(df["accumulatedTradingVolumeRaw"])
cur = df[["code", "name", "종가", "등락률", "거래대금", "거래량"]]
print(STAMP, "|", len(cur), "종목")

os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")

snaps = sorted(f for f in os.listdir("data/snaps") if f.startswith(TODAY))
if len(snaps) < 2:
    raise SystemExit("첫 스냅샷 - 비교는 다음 실행부터")

prev = pd.read_csv(f"data/snaps/{snaps[-2]}", dtype={"code": str})
m = cur.merge(prev[["code", "종가", "거래대금"]], on="code", suffixes=("", "_p"))
m = m.merge(uni[["code", "전일거래대금", "시총"]], on="code", how="left")

# 1. 최근 5분 거래대금
m["최근5분"] = m["거래대금"] - m["거래대금_p"]
m["가격변화"] = (m["종가"] / m["종가_p"] - 1) * 100

# 2. 회전율 - 시총 대비 5분 거래대금 (bp)
m["회전율"] = m["최근5분"] / m["시총"] * 10000

# 3. 전일 대비 배율 - 장 경과시간 보정
elapsed = max((NOW.hour * 60 + NOW.minute) - 540, 1)   # 9시부터 경과 분
ratio = min(elapsed / 390, 1.0)                        # 정규장 390분
m["전일대비"] = m["거래대금"] / (m["전일거래대금"] * ratio)

# 4. 이전 구간 대비 가속도
if len(snaps) >= 3:
    p2 = pd.read_csv(f"data/snaps/{snaps[-3]}", dtype={"code": str})
    m = m.merge(p2[["code", "거래대금"]].rename(columns={"거래대금": "거래대금_pp"}),
                on="code", how="left")
    base = (m["거래대금_p"] - m["거래대금_pp"]).clip(lower=1)
    m["가속도"] = m["최근5분"] / base
else:
    m["가속도"] = 1.0

m = m[(m["최근5분"] > 3e8) & (m["회전율"].notna())]

rank = lambda s: s.rank(pct=True) * 100
m["점수"] = (rank(m["회전율"]) * 0.4
           + rank(m["전일대비"]) * 0.35
           + rank(m["가속도"]) * 0.25)
m = m.sort_values("점수", ascending=False)

cols = ["code", "name", "종가", "등락률", "가격변화", "점수",
        "회전율", "전일대비", "가속도", "최근5분"]
top = m.head(30)[cols].round(2)
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print("\n=== 종합 점수 상위 20 ===")
print(top.head(20).to_string(index=False))
