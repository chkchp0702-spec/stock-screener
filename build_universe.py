from pykrx import stock
import pandas as pd
from datetime import datetime, timedelta
import os

for i in range(10):
    d = (datetime.now()-timedelta(days=i)).strftime("%Y%m%d")
    t = stock.get_market_ohlcv(d, market="KOSPI")
    if not t.empty and t["거래량"].sum() > 0:
        break

fs = []
for m in ["KOSPI", "KOSDAQ"]:
    o = stock.get_market_ohlcv(d, market=m)
    c = stock.get_market_cap(d, market=m)[["시가총액"]]
    f = o.join(c).reset_index().rename(columns={"티커": "code"})
    f["market"] = m
    fs.append(f)

df = pd.concat(fs, ignore_index=True)
df["name"] = df["code"].map(stock.get_market_ticker_name)
df = df[df["code"].str[-1] == "0"]
df = df[~df["name"].str.contains("스팩|리츠", na=False)]
df = df[df["시가총액"].between(5e10, 5e12)]
df = df[df["거래대금"] >= 1e9]
df = df[df["종가"].between(1000, 200000)]
df = df.sort_values("거래대금", ascending=False)

os.makedirs("data", exist_ok=True)
df.to_csv("data/universe_latest.csv", index=False, encoding="utf-8-sig")
print(d, "→ 최종", len(df), "종목")
print(df.head(20).to_string(index=False))
