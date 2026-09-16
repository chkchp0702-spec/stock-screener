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

def get(url, timeout=10):
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
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

def daily(code):
    u = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}"
         f"&requestType=1&startTime={START}&endTime={TODAY}&timeframe=day")
    try:
        r = requests.get(u, headers=H, timeout=10)
        arr = json.loads(r.text.strip().replace("'", '"'))
        rows = [{"h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5])}
                for x in arr[1:] if isinstance(x, list) and len(x) > 5]
        return pd.DataFrame(rows) if len(rows) >= 21 else None
    except Exception:
        return None

def analyze(code, today_pct, price):
    d = daily(code)
    if d is None: return False, 0, "일봉 없음", None, None
    c, v, hi, lo = d["c"], d["v"], d["h"], d["l"]
    ma5, ma20 = c.tail(5).mean(), c.tail(20).mean()
    ma60 = c.tail(60).mean() if len(c) >= 60 else None
    up5 = (c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) >= 6 else 0
    hi20 = hi.iloc[-21:-1].max()
    vr = v.iloc[-1] / max(v.iloc[-21:-1].mean(), 1)
    if today_pct > 15: return False, 0, "당일 +15% 초과", None, None
    if up5 > 30:       return False, 0, "5일 +30% 초과", None, None

    notes, bonus = [], 0
    if ma60 and ma5 > ma20 > ma60: bonus += 5; notes.append("정배열")
    elif ma5 > ma20:               bonus += 2; notes.append("5>20")
    if price > hi20: bonus += 5; notes.append("20일신고가")
    if vr >= 3:      bonus += 3; notes.append(f"거래량{vr:.0f}배")

    sc = [x for x in [ma20, lo.tail(5).min(), lo.iloc[-1]] if x and x < price]
    support = max(sc) if sc else None
    rc = [x for x in [hi20, hi.tail(60).max()] if x and x > price]
    resist = min(rc) if rc else None
    return True, bonus, " · ".join(notes), support, resist

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
                pm = row.get("최고")
                tr.at[i, "최고"] = round(max(chg, pm if not pd.isna(pm) else -99), 2)
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

dayhigh, highidx = {}, {}
for idx, f in enumerate(snaps):
    try:
        s = pd.read_csv(f"data/snaps/{f}", dtype={"code": str})
        for c_, p_ in zip(s["code"], s["종가"]):
            if pd.isna(p_): continue
            if c_ not in dayhigh or p_ > dayhigh[c_]:
                dayhigh[c_] = p_; highidx[c_] = idx
    except Exception:
        pass
n_snap = len(snaps)
m["당일고점"] = m["code"].map(dayhigh)
m["고점후"] = m["code"].map(lambda c: (n_snap - 1 - highidx.get(c, 0)) * 10)
m["고점대비"] = (m["종가"] / m["당일고점"] - 1) * 100

m = m[(m["최근5분"] > 3e8) & (m["회전율"].notna())]
if m.empty: print("통과 없음"); raise SystemExit(0)
rank = lambda s: s.rank(pct=True) * 100
m["점수"] = (rank(m["회전율"]) * 0.4 + rank(m["전일대비"]) * 0.35
           + rank(m["가속도"]) * 0.25).clip(upper=100)
m = m.sort_values("점수", ascending=False)
top = m.head(30)[["code","name","종가","등락률","가격변화","점수","회전율","전일대비","가속도","최근5분"]].round(2)
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print(top.head(8).to_string(index=False))

breakout = m[(m["점수"] >= 80) & (m["최근5분"] >= 1e9)
             & (m["전일대비"] >= 2.0) & (m["가격변화"] >= 1.0)].head(8)
pullback = m[(m["점수"] >= 65) & (m["최근5분"] >= 3e8)
             & (m["전일대비"] >= 2.0) & (m["등락률"] >= 2)
             & (m["고점대비"].between(-12, -1.5)) & (m["고점후"] >= 10)
             & (m["가격변화"] >= 0)].head(5)

SENT = "data/sent.json"; sent = {}
if os.path.exists(SENT):
    try: sent = json.load(open(SENT))
    except Exception: sent = {}
sent = {k: v for k, v in sent.items() if v.startswith(TODAY)}

picked = []
for kind, tbl, minscore in [("돌파", breakout, 85), ("눌림목", pullback, 70)]:
    for _, r in tbl.iterrows():
        code = r["code"]
        last = sent.get(code)
        if last and (NOW - datetime.strptime(last, "%Y%m%d_%H%M").replace(tzinfo=KST)).total_seconds() < 1800:
            continue
        if any(p["r"]["code"] == code for p in picked): continue
        ok, bonus, note, sup, res = analyze(code, r["등락률"], r["종가"])
        if not ok:
            print(f"제외 {r['name']}: {note}"); continue
        final = min(r["점수"] + bonus, 100)
        if final < minscore: continue
        if not sup:
            print(f"제외 {r['name']}: 지지선 없음"); continue
        risk = r["종가"] - sup
        rr = (res - r["종가"]) / risk if (res and risk > 0) else 99
        if rr < 1.5:
            print(f"제외 {r['name']}: 손익비 {rr:.1f}"); continue
        picked.append({"kind": kind, "r": r, "final": final, "note": note,
                       "sup": sup, "res": res, "rr": rr, "news": news(code)})
        time.sleep(0.2)

if picked:
    head = f"🔔 <b>{NOW.strftime('%H:%M')} 신호 {len(picked)}건</b>"
    blocks = []
    for p in picked:
        r = p["r"]; code = r["code"]
        tag = "🚀" if p["kind"] == "돌파" else "🔄"
        t = f"{tag} <b>{r['name']}</b> ({code})"
        t += (f"\n  {int(r['종가']):,}원  당일 {r['등락률']:+.1f}%  5분 {r['가격변화']:+.1f}%"
              f"\n  점수 {p['final']:.0f} · 전일대비 {r['전일대비']:.1f}배 · 5분 {r['최근5분']/1e8:.0f}억")
        if p["kind"] == "눌림목":
            t += f"\n  ↩️ 고점대비 {r['고점대비']:+.1f}% · {int(r['고점후'])}분 경과"
        if p["note"]: t += f"\n  📈 {p['note']}"
        if p["res"]:
            t += f"\n  🎯 지지 {int(p['sup']):,} / 저항 {int(p['res']):,} (1:{p['rr']:.1f})"
        else:
            t += f"\n  🎯 지지 {int(p['sup']):,} / 저항 없음(신고가)"
        for n in p["news"]: t += f"\n  📰 {n}"
        blocks.append(t)
        sent[code] = STAMP
    tg(head + "\n\n" + "\n\n".join(blocks))
    print("알람", len(picked), "건")

    nt = pd.DataFrame([{"시각": STAMP, "유형": p["kind"], "code": p["r"]["code"],
                        "name": p["r"]["name"], "알람가": p["r"]["종가"],
                        "점수": round(p["final"], 1), "손익비": round(p["rr"], 1),
                        "당일등락": p["r"]["등락률"], "30분": None, "60분": None,
                        "최고": 0.0, "현재": 0.0} for p in picked])
    if os.path.exists(TRACK):
        try:
            old = pd.read_csv(TRACK, dtype={"code": str})
            nt = pd.concat([old, nt], ignore_index=True)
        except Exception: pass
    nt.to_csv(TRACK, index=False, encoding="utf-8-sig")
else:
    print("알람 없음")

json.dump(sent, open(SENT, "w"))
