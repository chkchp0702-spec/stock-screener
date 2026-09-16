import requests, pandas as pd, os, time, json, html
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STAMP = NOW.strftime("%Y%m%d_%H%M")
TODAY = NOW.strftime("%Y%m%d")
START = (NOW - timedelta(days=150)).strftime("%Y%m%d")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/", "Accept": "application/json"}

def get(url, timeout=10, debug=False):
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        if debug: print(f"[DBG] {r.status_code} {url}\n      {r.text[:200]}")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        if debug: print(f"[DBG] ERR {e}")
        return None

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        print("TG", r.status_code)
    except Exception as e:
        print("TG 실패", e)

def daily(code, dbg=False):
    u = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}"
         f"&requestType=1&startTime={START}&endTime={TODAY}&timeframe=day")
    try:
        r = requests.get(u, headers=H, timeout=10)
        if dbg: print(f"[DBG] daily {r.status_code}")
        arr = json.loads(r.text.strip().replace("'", '"'))
        rows = [{"h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5])}
                for x in arr[1:] if isinstance(x, list) and len(x) > 5]
        return pd.DataFrame(rows) if len(rows) >= 21 else None
    except Exception as e:
        if dbg: print("[DBG] daily err", e)
        return None

def analyze(code, today_pct, price, dbg=False):
    """일봉 분석: (통과, 가점, 설명, 지지, 저항)"""
    d = daily(code, dbg)
    if d is None: return False, 0, "일봉 없음", None, None
    c, v, hi, lo = d["c"], d["v"], d["h"], d["l"]
    ma5, ma20 = c.tail(5).mean(), c.tail(20).mean()
    ma60 = c.tail(60).mean() if len(c) >= 60 else None
    last = c.iloc[-1]
    up5 = (last / c.iloc[-6] - 1) * 100 if len(c) >= 6 else 0
    hi20 = hi.iloc[-21:-1].max()
    vr = v.iloc[-1] / max(v.iloc[-21:-1].mean(), 1)
    if today_pct > 15: return False, 0, "당일 +15% 초과", None, None
    if up5 > 30:       return False, 0, "5일 +30% 초과", None, None

    notes, bonus = [], 0
    if ma60 and ma5 > ma20 > ma60: bonus += 5; notes.append("정배열")
    elif ma5 > ma20:               bonus += 2; notes.append("5>20")
    if price > hi20:               bonus += 5; notes.append("20일신고가")
    if vr >= 3:                    bonus += 3; notes.append(f"거래량{vr:.0f}배")

    # 지지: 20일 이평, 최근 5일 저가 중 현재가 아래에서 가장 가까운 값
    cands = [x for x in [ma20, lo.tail(5).min(), lo.iloc[-1]] if x and x < price]
    support = max(cands) if cands else None
    # 저항: 20일 고가, 60일 고가 중 현재가 위에서 가장 가까운 값
    r_cands = [x for x in [hi20, hi.tail(60).max()] if x and x > price]
    resist = min(r_cands) if r_cands else None
    return True, bonus, " · ".join(notes), support, resist

def sector(code):
    j = get(f"https://m.stock.naver.com/api/stock/{code}/basic")
    if not j: return ""
    for k in ("industryGroupKor", "industryName", "sectorName"):
        v = j.get(k) or (j.get("industryCodeType") or {}).get(k)
        if v: return v
    return ""

def news(code, n=2):
    j = get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize={n}&page=1")
    out = []
    if not j: return out
    for grp in j[:n]:
        it = grp.get("items", [])
        if it: out.append(html.escape(it[0].get("title", "").replace("&quot;", '"'))[:60])
    return out

def fetch(mkt):
    rows, page = [], 1
    while page <= 40:
        j = get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}?page={page}&pageSize=100", 20)
        items = (j or {}).get("stocks", [])
        if not items: break
        rows += items; page += 1; time.sleep(0.2)
    return rows

# ── 시세 수집 ──
rows = []
for mk in ["KOSPI", "KOSDAQ"]:
    rows += fetch(mk)

uni = pd.read_csv("data/universe_latest.csv", dtype={"code": str})
uni = uni.rename(columns={"거래대금": "전일거래대금", "시가총액": "시총"})
df = pd.DataFrame(rows)
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df = df[df["code"].isin(set(uni["code"]))].copy()
df["종가"] = num(df["closePriceRaw"]); df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
cur = df[["code", "name", "종가", "등락률", "거래대금"]]
print(STAMP, "|", len(cur), "종목")

os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")

# ── 성과 추적 갱신 ──
TRACK = "data/tracking.csv"
if os.path.exists(TRACK):
    try:
        tr = pd.read_csv(TRACK, dtype={"code": str})
        px = dict(zip(cur["code"], cur["종가"]))
        for i, row in tr.iterrows():
            p = px.get(row["code"])
            if p is None or pd.isna(row.get("알람가")): continue
            t0 = datetime.strptime(str(row["시각"]), "%Y%m%d_%H%M").replace(tzinfo=KST)
            mins = (NOW - t0).total_seconds() / 60
            chg = (p / row["알람가"] - 1) * 100
            if mins >= 30 and pd.isna(row.get("30분")): tr.at[i, "30분"] = round(chg, 2)
            if mins >= 60 and pd.isna(row.get("60분")): tr.at[i, "60분"] = round(chg, 2)
            if str(row["시각"]).startswith(TODAY):
                tr.at[i, "최고"] = round(max(chg, row.get("최고", -99) if not pd.isna(row.get("최고")) else -99), 2)
                tr.at[i, "현재"] = round(chg, 2)
        tr.to_csv(TRACK, index=False, encoding="utf-8-sig")
    except Exception as e:
        print("추적 갱신 실패", e)

snaps = sorted(f for f in os.listdir("data/snaps") if f.startswith(TODAY))
if len(snaps) < 2:
    print("첫 스냅샷"); raise SystemExit(0)

prev = pd.read_csv(f"data/snaps/{snaps[-2]}", dtype={"code": str})
m = cur.merge(prev[["code", "종가", "거래대금"]], on="code", suffixes=("", "_p"))
m = m.merge(uni[["code", "전일거래대금", "시총"]], on="code", how="left")
m["최근5분"] = m["거래대금"] - m["거래대금_p"]
m["가격변화"] = (m["종가"] / m["종가_p"] - 1) * 100
m["회전율"] = m["최근5분"] / m["시총"] * 10000
elapsed = max((NOW.hour * 60 + NOW.minute) - 540, 1)
m["전일대비"] = m["거래대금"] / (m["전일거래대금"] * min(elapsed / 390, 1.0))
if len(snaps) >= 3:
    p2 = pd.read_csv(f"data/snaps/{snaps[-3]}", dtype={"code": str})
    m = m.merge(p2[["code", "거래대금"]].rename(columns={"거래대금": "거래대금_pp"}), on="code", how="left")
    m["가속도"] = m["최근5분"] / (m["거래대금_p"] - m["거래대금_pp"]).clip(lower=1)
else:
    m["가속도"] = 1.0

m = m[(m["최근5분"] > 3e8) & (m["회전율"].notna())]
if m.empty: print("통과 없음"); raise SystemExit(0)
rank = lambda s: s.rank(pct=True) * 100
m["점수"] = rank(m["회전율"]) * 0.4 + rank(m["전일대비"]) * 0.35 + rank(m["가속도"]) * 0.25
m = m.sort_values("점수", ascending=False)
top = m.head(30)[["code","name","종가","등락률","가격변화","점수","회전율","전일대비","가속도","최근5분"]].round(2)
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print(top.head(8).to_string(index=False))

alert = m[(m["점수"] >= 80) & (m["최근5분"] >= 1e9)
          & (m["전일대비"] >= 2.0) & (m["가격변화"] >= 1.0)].head(10)

SENT = "data/sent.json"; sent = {}
if os.path.exists(SENT):
    try: sent = json.load(open(SENT))
    except Exception: sent = {}
sent = {k: v for k, v in sent.items() if v.startswith(TODAY)}

picked, first = [], True
for _, r in alert.iterrows():
    code = r["code"]
    last = s
