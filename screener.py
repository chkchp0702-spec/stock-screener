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

def get(url, timeout=10):
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("텔레그램 미설정"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        print("TG", r.status_code)
    except Exception as e:
        print("TG 실패", e)

# ── 종목별 부가정보 ──
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
        items = grp.get("items", [])
        if items:
            t = items[0].get("title", "").replace("&quot;", '"')
            out.append(html.escape(t)[:60])
    return out

def orderbook_ratio(code):
    """매수잔량 / 매도잔량 (체결강도 대용)"""
    j = get(f"https://m.stock.naver.com/api/stock/{code}/orderbook") \
        or get(f"https://m.stock.naver.com/api/stock/{code}/hoga")
    if not j: return None
    try:
        bid = ask = 0
        for k in ("bidInfos", "askInfos", "buyInfos", "sellInfos"):
            arr = j.get(k, [])
            s = sum(int(str(x.get("count", x.get("volume", 0))).replace(",", "")) for x in arr)
            if k in ("bidInfos", "buyInfos"): bid += s
            else: ask += s
        if not bid and not ask:
            bid = int(str(j.get("totalBidCount", 0)).replace(",", ""))
            ask = int(str(j.get("totalAskCount", 0)).replace(",", ""))
        return round(bid / ask, 2) if ask else None
    except Exception:
        return None

def daily(code, n=70):
    """일봉: 종가·거래량 리스트 (오래된→최신)"""
    j = get(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize={n}&page=1")
    if not j or not isinstance(j, list): return None
    rows = []
    for x in reversed(j):
        try:
            rows.append({
                "c": float(str(x.get("closePrice", "0")).replace(",", "")),
                "v": float(str(x.get("accumulatedTradingVolume", "0")).replace(",", "")),
            })
        except Exception:
            pass
    return pd.DataFrame(rows) if len(rows) >= 21 else None

def daily_check(code, today_pct):
    """일봉 필터. return (통과여부, 가점, 설명)"""
    d = daily(code)
    if d is None: return True, 0, ""
    c, v = d["c"], d["v"]
    ma5, ma20, ma60 = c.tail(5).mean(), c.tail(20).mean(), c.tail(60).mean() if len(c) >= 60 else None
    last = c.iloc[-1]
    prev5 = c.iloc[-6] if len(c) >= 6 else c.iloc[0]
    up5 = (last / prev5 - 1) * 100
    hi20 = c.iloc[-21:-1].max()
    vol_ratio = v.iloc[-1] / max(v.iloc[-21:-1].mean(), 1)

    notes, bonus = [], 0
    if today_pct > 15:      return False, 0, "당일 +15% 초과"
    if up5 > 30:            return False, 0, "5일 +30% 초과"
    if ma60 and ma5 > ma20 > ma60:
        bonus += 5; notes.append("정배열")
    elif ma5 > ma20:
        bonus += 2; notes.append("5>20")
    if last > hi20:
        bonus += 5; notes.append("20일신고가")
    if vol_ratio >= 3:
        bonus += 3; notes.append(f"거래량{vol_ratio:.0f}배")
    return True, bonus, " · ".join(notes)

# ── 시세 수집 ──
def fetch(mkt):
    rows, page = [], 1
    while page <= 40:
        j = get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}"
                f"?page={page}&pageSize=100", 20)
        items = (j or {}).get("stocks", [])
        if not items: break
        rows += items; page += 1
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
    print("첫 스냅샷"); raise SystemExit(0)

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
    m["가속도"] = m["최근5분"] / (m["거래대금_p"] - m["거래대금_pp"]).clip(lower=1)
else:
    m["가속도"] = 1.0

m = m[(m["최근5분"] > 3e8) & (m["회전율"].notna())]
if m.empty:
    print("통과 없음"); raise SystemExit(0)

rank = lambda s: s.rank(pct=True) * 100
m["점수"] = rank(m["회전율"]) * 0.4 + rank(m["전일대비"]) * 0.35 + rank(m["가속도"]) * 0.25
m = m.sort_values("점수", ascending=False)

cols = ["code", "name", "종가", "등락률", "가격변화", "점수", "회전율", "전일대비", "가속도", "최근5분"]
top = m.head(30)[cols].round(2)
top.to_csv("data/signal_latest.csv", index=False, encoding="utf-8-sig")
print(top.head(15).to_string(index=False))

# ── 알람 후보 ──
alert = m[(m["점수"] >= 80) & (m["최근5분"] >= 1e9)
          & (m["전일대비"] >= 2.0) & (m["가격변화"] >= 1.0)].head(10)

SENT = "data/sent.json"
sent = {}
if os.path.exists(SENT):
    try: sent = json.load(open(SENT))
    except Exception: sent = {}
sent = {k: v for k, v in sent.items() if v.startswith(TODAY)}

lines = []
for _, r in alert.iterrows():
    code = r["code"]
    last = sent.get(code)
    if last:
        t = datetime.strptime(last, "%Y%m%d_%H%M").replace(tzinfo=KST)
        if (NOW - t).total_seconds() < 1800: continue

    ok, bonus, note = daily_check(code, r["등락률"])
    if not ok:
        print(f"제외 {r['name']}: {note}"); continue
    final = r["점수"] + bonus
    if final < 85: continue

    sec = sector(code)
    ob = orderbook_ratio(code)
    nw = news(code)
    time.sleep(0.2)

    head = f"<b>{r['name']}</b> ({code})" + (f" · {sec}" if sec else "")
    body = (f"  {int(r['종가']):,}원  당일 {r['등락률']:+.1f}%  5분 {r['가격변화']:+.1f}%\n"
            f"  점수 {final:.0f} · 전일대비 {r['전일대비']:.1f}배 · 5분 {r['최근5분']/1e8:.0f}억")
    extra = []
    if note: extra.append(f"📈 {note}")
    if ob:   extra.append(f"⚖️ 매수/매도잔량 {ob}배")
    block = [head, body] + [f"  {e}" for e in extra] + [f"  📰 {t}" for t in nw]
    lines.append("\n".join(block))
    sent[code] = STAMP

if lines:
    tg(f"🔔 <b>{NOW.strftime('%H:%M')} 신호 {len(lines)}건</b>\n\n" + "\n\n".join(lines))
    print("알람", len(lines), "건")
else:
    print("알람 없음")

json.dump(sent, open(SENT, "w"))
