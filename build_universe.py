import requests, pandas as pd, io, os
from datetime import datetime, timedelta

H = {"User-Agent": "Mozilla/5.0",
     "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"}
GEN = "https://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
DL  = "https://data.krx.co.kr/comm/fileDn/download_csv/download.cmd"

def fetch(d):
    p = {"locale": "ko_KR", "mktId": "ALL", "trdDd": d,
         "share": "1", "money": "1", "csvxls_isNo": "false",
         "name": "fileDown", "url": "dbms/MDC/STAT/standard/MDCSTAT01501"}
    otp = requests.post(GEN, data=p, headers=H, timeout=30).text
    r = requests.post(DL, data={"code": otp}, headers=H, timeout=30)
    return pd.read_csv(io.BytesIO(r.content), encoding="euc-kr")

df = None
for i in range(10):
    d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
    try:
        t = fetch(d)
        print(d, "행수", len(t), "| 컬럼", list(t.columns)[:6])
        if len(t) > 100:
            df = t
            break
    except Exception as e:
        print(d, "실패:", repr(e)[:200])

if df is None:
    raise SystemExit("10일치 모두 실패 - 위 로그 확인")

df = df.rename(columns={"종목코드": "code", "종목명": "name", "시장구분": "market"})
df = df[df["market"].isin(["KOSPI", "KOSDAQ"])]
df = df[df["code"].astype(str).str[-1] == "0"]
df = df[~df["name"].str.contains("스팩|리츠", na=False)]
df = df[df["시가총액"].between(5e10, 5e12)]
df = df[df["거래대금"] >= 1e9]
df = df[df["종가"].between(1000, 200000)]
df = df.sort_values("거래대금", ascending=False)

os.makedirs("data", exist_ok=True)
df.to_csv("data/universe_latest.csv", index=False, encoding="utf-8-sig")
print(d, "→ 최종", len(df), "종목")
print(df[["code","name","market","종가","거래대금","시가총액"]].head(20).to_string(index=False))
