import requests, pandas as pd, os
from datetime import datetime, timedelta

URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
H = {"User-Agent": "Mozilla/5.0",
     "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
     "X-Requested-With": "XMLHttpRequest"}

def fetch(d):
    p = {"bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
         "locale": "ko_KR", "mktId": "ALL", "trdDd": d,
         "share": "1", "money": "1", "csvxls_isNo": "false"}
    j = requests.post(URL, data=p, headers=H, timeout=30).json()
    return pd.DataFrame(j.get("OutBlock_1", []))

df = None
for i in range(10):
    d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
    try:
        t = fetch(d)
        print(d, "행수", len(t))
        if len(t) > 100:
            df = t
            break
    except Exception as e:
        print(d, "실패:", repr(e)[:150])

if df is None:
    raise SystemExit("실패 - 위 로그 확인")

num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"ISU_SRT_CD": "code", "ISU_ABBRV": "name", "MKT_NM": "market"})
df["종가"] = num(df["TDD_CLSPRC"])
df["거래대금"] = num(df["ACC_TRDVAL"])
df["시가총액"] = num(df["MKTCAP"])
df["등락률"] = num(df["FLUC_RT"])

df = df[df["market"].isin(["KOSPI", "KOSDAQ"])]
df = df[df["code"].astype(str).str[-1] == "0"]
df = df[~df["name"].str.contains("스팩|리츠", na=False)]
df = df[df["시가총액"].between(5e10, 5e12)]
df = df[df["거래대금"] >= 1e9]
df = df[df["종가"].between(1000, 200000)]
df = df.sort_values("거래대금", ascending=False)

out = df[["code", "name", "market", "종가", "등락률", "거래대금", "시가총액"]]
os.makedirs("data", exist_ok=True)
out.to_csv("data/universe_latest.csv", index=False, encoding="utf-8-sig")
print(d, "→ 최종", len(out), "종목")
print(out.head(20).to_string(index=False))
