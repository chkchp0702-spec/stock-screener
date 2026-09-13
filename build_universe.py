import requests, pandas as pd, os, time

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/"}

ETF_KEYS = ("KODEX|TIGER|KBSTAR|ARIRANG|HANARO|KOSEF|SOL |ACE |PLUS |RISE |"
            "TIMEFOLIO|히어로즈|마이티|파워|스팩|리츠|레버리지|인버스|선물|ETN")

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
        time.sleep(0.3)
    print(mkt, "수집", len(rows))
    return rows

all_rows = []
for m in ["KOSPI", "KOSDAQ"]:
    for it in fetch(m):
        it["market"] = m
        all_rows.append(it)

if not all_rows:
    raise SystemExit("수집 실패")

df = pd.DataFrame(all_rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df["종가"] = num(df["closePriceRaw"])
df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
df["시가총액"] = num(df["marketValueRaw"])

print("필터 전", len(df))
df = df[df["code"].astype(str).str[-1] == "0"]
df = df[~df["name"].str.contains(ETF_KEYS, na=False)]
df = df[df["시가총액"] >= 5e10]
df = df[df["거래대금"] >= 1e9]
df = df[df["종가"] >= 1000]
df = df.sort_values("거래대금", ascending=False)

out = df[["code", "name", "market", "종가", "등락률", "거래대금", "시가총액"]]
os.makedirs("data", exist_ok=True)
out.to_csv("data/universe_latest.csv", index=False, encoding="utf-8-sig")
print("최종", len(out), "종목")
print(out.head(20).to_string(index=False))
