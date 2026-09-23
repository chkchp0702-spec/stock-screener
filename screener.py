import requests, pandas as pd, os, time, json, html
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
STAMP = NOW.strftime("%Y%m%d_%H%M")
TODAY = NOW.strftime("%Y%m%d")
START = (NOW - timedelta(days=150)).strftime("%Y%m%d")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
HHMM = NOW.hour * 100 + NOW.minute
PRE = HHMM < 900
MAX_ALERTS = 5

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

def link(code, name):
    return f'<a href="https://m.stock.naver.com/domestic/stock/{code}/total">{name}</a>'

def kosdaq_pct():
    j = get("https://m.stock.naver.com/api/index/KOSDAQ/basic")
    try: return float(str(j.get("fluctuationsRatio", "0")).replace(",", ""))
    except Exception: return 0.0

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
    """일봉 필터 + 2차 저항. return (ok, note, resist2)"""
    if today_pct > 20: return False, "상한가 근처", None
    d = daily(code)
    if d is None: return False, "일봉 없음", None
    c, v, hi = d["c"], d["v"], d["h"]
    ma5, ma20 = c.tail(5).mean(), c.tail(20).mean()
    ma60 = c.tail(60).mean() if len(c) >= 60 else None
    up5 = (c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) >= 6 else 0
    if up5 > 30: return False, "5일 +30% 초과", None
    if ma60 and ma5 < ma20 < ma60: return False, "역배열", None
    notes = []
    if ma60 and ma5 > ma20 > ma60: notes.append("정배열")
    elif ma5 > ma20: notes.append("5>20")
    hi20 = hi.iloc[-21:-1].max()
    if price > hi20: notes.append("20일신고가")
    vr = v.iloc[-1] / max(v.iloc[-21:-1].mean(), 1)
    if vr >= 3: notes.append(f"거래량{vr:.0f}배")
    rc = [x for x in [hi20, hi.tail(60).max()] if x and x > price * 1.01]
    return True, " · ".join(notes), (min(rc) if rc else None)

def news(code, n=2):
    j = get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize={n}&page=1")
    out = []
    if not j: return out
    for grp in j[:n]:
        it = grp.get("items", [])
        if it: out.append(html.escape(it[0].get("title", "").replace("&quot;", '"'))[:60])
    return out

def fetch(mkt):
    rows, page, empty = [], 1, 0
    while page <= 60:
        j = get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}?page={page}&pageSize=100", 20)
        items = (j or {}).get("stocks", [])
        if not items:
            empty += 1
            if empty >= 2: break
            page += 1; time.sleep(0.5); continue
        empty = 0; rows += items; page += 1; time.sleep(0.2)
    return rows

# ── 수집 ──
rows = []
for mk in ["KOSPI", "KOSDAQ"]:
    rows += fetch(mk)
uni = pd.read_csv("data/universe_latest.csv", dtype={"code": str})
uni = uni.rename(columns={"거래대금": "전일거래대금", "시가총액": "시총", "등락률": "전일등락"})
df = pd.DataFrame(rows).drop_duplicates(subset=["itemCode"])
num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
df = df.rename(columns={"itemCode": "code", "stockName": "name"})
df = df[df["code"].isin(set(uni["code"]))].copy()
df["종가"] = num(df["closePriceRaw"]); df["등락률"] = num(df["fluctuationsRatio"])
df["거래대금"] = num(df["accumulatedTradingValueRaw"])
df["거래량"] = num(df["accumulatedTradingVolumeRaw"])
cur = df[["code", "name", "종가", "등락률", "거래대금", "거래량"]]
print(f"{STAMP} | {len(cur)}종목")
os.makedirs("data/snaps", exist_ok=True)
cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")
KQ = kosdaq_pct()

if PRE:
    print("프리마켓 - 네이버 NXT 미지원, 스냅샷만 저장"); raise SystemExit(0)

up_ratio = (cur["등락률"] > 0).mean() * 100
WEAK = KQ <= -1.0 or up_ratio < 40
print(f"코스닥 {KQ:+.2f}% · 상승 {up_ratio:.0f}%" + (" · 약세" if WEAK else ""))

# ── 추적 갱신 + 청산 ──
TRACK = "data/tracking.csv"
COLS = ["시각","유형","code","name","알람가","손절","목표1","목표2","손익비","VWAP위",
        "코스닥","상승비율","당일등락","30분","60분","최고","최저","현재","청산알림"]
streak_loss, today_cnt, exits = 0, 0, []
if os.path.exists(TRACK):
    try:
        tr = pd.read_csv(TRACK, dtype={"code": str})
        for c in COLS:
            if c not in tr.columns: tr[c] = None
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
                pm, lm = row.get("최고"), row.get("최저")
                tr.at[i, "최고"] = round(max(chg, pm if not pd.isna(pm) else -99), 2)
                tr.at[i, "최저"] = round(min(chg, lm if not pd.isna(lm) else 99), 2)
                tr.at[i, "현재"] = round(chg, 2)
                flag = str(row.get("청산알림") or "")
                t1, t2, sl = row.get("목표1"), row.get("목표2"), row.get("손절")
                if t2 and not pd.isna(t2) and p >= float(t2) and "2차" not in flag:
                    exits.append(("2차목표", row, p, chg)); flag += "2차"
                elif t1 and not pd.isna(t1) and p >= float(t1) and "1차" not in flag:
                    exits.append(("1차목표", row, p, chg)); flag += "1차"
                elif sl and not pd.isna(sl) and p <= float(sl) and "손절" not in flag:
                    exits.append(("손절", row, p, chg)); flag += "손절"
                tr.at[i, "청산알림"] = flag
        tr.to_csv(TRACK, index=False, encoding="utf-8-sig")
        tt = tr[tr["시각"].astype(str).str.startswith(TODAY)]
        today_cnt = len(tt)
        for v in reversed(list(tt.dropna(subset=["30분"]).sort_values("시각")["30분"])):
            if v < 0: streak_loss += 1
            else: break
    except Exception as e:
        print("추적 실패", e)
print(f"오늘 {today_cnt}건 · 연패 {streak_loss}")

if exits:
    lines = [f"🔔 <b>{NOW.strftime('%H:%M')} 청산</b>", ""]
    for kind, row, p, chg in exits[:6]:
        icon = {"1차목표": "💰", "2차목표": "💎", "손절": "🛑"}[kind]
        lines.append(f"{icon} <b>{kind}</b> {link(row['code'], row['name'])}\n  {int(p):,}원 · {chg:+.1f}%")
    tg("\n".join(lines))

# ── 마감 요약 ──
DONE = "data/summary_done.txt"
if NOW.hour >= 15 and (open(DONE).read().strip() if os.path.exists(DONE) else "") != TODAY:
    try:
        lines = [f"📊 <b>{NOW.strftime('%m/%d')} 마감</b>  코스닥 {KQ:+.1f}% · 상승 {up_ratio:.0f}%"]
        alerted = set()
        if os.path.exists(TRACK):
            tr = pd.read_csv(TRACK, dtype={"code": str})
            t = tr[tr["시각"].astype(str).str.startswith(TODAY)]
            alerted = set(t["code"])
            if len(t):
                sim = []
                for _, x in t.iterrows():
                    try:
                        sl = (float(x["손절"]) / x["알람가"] - 1) * 100
                        t1 = (float(x["목표1"]) / x["알람가"] - 1) * 100
                        if x["최저"] <= sl: sim.append(sl)
                        elif x["최고"] >= t1: sim.append(t1)
                        else: sim.append(x["현재"])
                    except Exception: sim.append(x["현재"])
                win = sum(1 for s in sim if s > 0)
                lines += [f"알람 {len(t)}건 · 승 {win} 패 {len(t)-win}",
                          f"💼 시뮬(손절·1차익절) 평균 <b>{sum(sim)/len(sim):+.2f}%</b> · 종가 평균 {t['현재'].mean():+.2f}%", ""]
                for _, x in t.iterrows():
                    lines.append(f"  {x['name']} 최고 {x['최고']:+.1f}% 최저 {x['최저']:+.1f}% → {x['현재']:+.1f}%")
            else:
                lines.append("알람 없음 (조건 충족 종목 없음)")
        missed = cur[(cur["등락률"] >= 8) & (~cur["code"].isin(alerted)) & (cur["거래대금"] >= 5e9)]
        if len(missed):
            lines += ["", "😶 놓친 급등주: " + ", ".join(f"{x['name']} {x['등락률']:+.0f}%" for _, x in missed.head(5).iterrows())]
        tg("\n".join(lines)); open(DONE, "w").write(TODAY)
    except Exception as e:
        print("요약 실패", e)

if today_cnt >= MAX_ALERTS: print("상한"); raise SystemExit(0)
if streak_loss >= 2: print("2연패 중단"); raise SystemExit(0)
if HHMM >= 1430: print("14:30 이후 신규 알람 중단"); raise SystemExit(0)

# ── 당일 시계열 구축 ──
snaps = sorted(f for f in os.listdir("data/snaps") if f.startswith(TODAY))
reg = [f for f in snaps if int(f[9:13]) >= 900]
if len(reg) < 5:
    print(f"스냅샷 {len(reg)}개 - 패턴 판별에 5개 필요"); raise SystemExit(0)

def tmin(f): return int(f[9:11]) * 60 + int(f[11:13])
series = defaultdict(list)
for f in reg:
    try:
        s = pd.read_csv(f"data/snaps/{f}", dtype={"code": str})
        t = tmin(f)
        for c_, p_, v_ in zip(s["code"], s["종가"], s["거래대금"]):
            if not pd.isna(p_): series[c_].append((t, float(p_), float(v_) if not pd.isna(v_) else 0))
    except Exception: pass

# ── 눌림목 돌파 탐지 ──
def detect(seq):
    """seq: [(t, price, cumval)] 오름차순. return dict or None"""
    if len(seq) < 5: return None
    n = len(seq)
    cur_t, cur_p, cur_v = seq[-1]
    prev_p = seq[-2][1]
    # 1. 당일 고점 (현재 제외)
    hi_i = max(range(n - 1), key=lambda i: seq[i][1])
    hi_t, hi_p = seq[hi_i][0], seq[hi_i][1]
    if hi_i >= n - 3: return None                       # 고점이 너무 최근이면 조정 없음
    # 2. 조정 저점 (고점 이후, 현재 제외)
    lo_i = min(range(hi_i + 1, n - 1), key=lambda i: seq[i][1])
    lo_t, lo_p = seq[lo_i][0], seq[lo_i][1]
    dd = (lo_p / hi_p - 1) * 100
    if not (-9 <= dd <= -2.5): return None
    if lo_t - hi_t < 15: return None                     # 조정 최소 15분
    if lo_i >= n - 2: return None                        # 저점 후 최소 1스냅샷 필요
    # 3. 조정 고점 (저점 이후, 현재 제외) = 반등 시도했다 막힌 자리
    rb_i = max(range(lo_i + 1, n - 1), key=lambda i: seq[i][1])
    rb_p = seq[rb_i][1]
    if rb_p >= hi_p * 0.995: return None                 # 이미 고점 근처면 돌파 아님
    if rb_p < lo_p * 1.005: return None                  # 반등 자체가 없음
    # 4. 돌파: 이전엔 조정고점 아래, 지금은 위
    if not (prev_p <= rb_p * 1.002 and cur_p > rb_p * 1.003): return None
    if cur_p >= hi_p * 0.995: return None                # 이미 당일고점까지 다 올랐으면 늦음
    # 5. 거래대금 재유입: 최근 구간 분당대금 vs 조정구간 평균
    def pm(i, j):
        dt = max(seq[j][0] - seq[i][0], 1); return (seq[j][2] - seq[i][2]) / dt
    recent = pm(n - 2, n - 1)
    corr = pm(hi_i, lo_i) if lo_i > hi_i else 1
    rise = pm(0, hi_i) if hi_i > 0 else recent
    if recent < corr * 1.3: return None                  # 조정 때보다 확실히 늘어야
    if recent < rise * 0.5: return None                  # 상승 때의 절반은 돼야
    return {"hi": hi_p, "lo": lo_p, "rb": rb_p, "dd": dd, "corr_min": lo_t - hi_t,
            "since_lo": cur_t - lo_t, "recent_pm": recent, "vol_ratio": recent / max(corr, 1)}

vw = dict(zip(cur["code"], cur["거래대금"] / cur["거래량"].replace(0, float("nan"))))
uni_map = uni.set_index("code")
cands = []
for _, r in cur.iterrows():
    code = r["code"]
    seq = series.get(code, [])
    if len(seq) < 5 or seq[-1][1] != r["종가"]: continue
    d = detect(seq)
    if not d: continue
    prev_close = r["종가"] / (1 + r["등락률"] / 100) if r["등락률"] > -99 else None
    if not prev_close: continue
    rise_pct = (d["hi"] / prev_close - 1) * 100
    if rise_pct < 4: continue                            # 급등이라 할 만해야
    if r["거래대금"] < 3e9: continue
    v_ = vw.get(code)
    if v_ is None or pd.isna(v_) or r["종가"] < v_: continue   # VWAP 위 필수
    sl = d["lo"] * 0.995
    risk = r["종가"] - sl
    rew1 = d["hi"] - r["종가"]
    rr1 = rew1 / risk if risk > 0 else 0
    if rr1 < (1.5 if WEAK else 1.0): continue
    cands.append({"r": r, "d": d, "rise": rise_pct, "sl": sl, "rr1": rr1, "vwap": v_})
print(f"패턴 후보 {len(cands)}")

SENT = "data/sent.json"; sent = {}
if os.path.exists(SENT):
    try: sent = json.load(open(SENT))
    except Exception: sent = {}
sent = {k: v for k, v in sent.items() if isinstance(v, dict) and str(v.get("t", "")).startswith(TODAY)}

cands.sort(key=lambda x: -x["rr1"])
picked = []
for c in cands:
    if today_cnt + len(picked) >= MAX_ALERTS: break
    r, d = c["r"], c["d"]; code = r["code"]
    if code in sent: continue
    ok, note, res2 = analyze(code, r["등락률"], r["종가"])
    if not ok: print(f"제외 {r['name']}: {note}"); continue
    c["note"], c["res2"] = note, res2
    c["rr2"] = (res2 - r["종가"]) / (r["종가"] - c["sl"]) if res2 else None
    c["news"] = news(code)
    picked.append(c); time.sleep(0.2)

if picked:
    head = f"🎯 <b>{NOW.strftime('%H:%M')} 눌림목 돌파 {len(picked)}건</b>  코스닥 {KQ:+.1f}%"
    if WEAK: head += " ⚠️약세"
    blocks = []
    for c in picked:
        r, d = c["r"], c["d"]; code = r["code"]; p = r["종가"]
        t = f"<b>{link(code, r['name'])}</b> ({code})"
        t += f"\n  {int(p):,}원  당일 {r['등락률']:+.1f}%"
        t += (f"\n  📐 급등 +{c['rise']:.1f}% → 조정 {d['dd']:.1f}% ({d['corr_min']}분)"
              f" → 조정고점 {int(d['rb']):,} 돌파")
        t += f"\n  💧 거래 재유입 ×{d['vol_ratio']:.1f} · VWAP위 ✅"
        if c["note"]: t += f"\n  📈 {c['note']}"
        t += f"\n  🛑 손절 {int(c['sl']):,} ({(c['sl']/p-1)*100:+.1f}%)"
        t += f"\n  💰 1차 {int(d['hi']):,} ({(d['hi']/p-1)*100:+.1f}% · 1:{c['rr1']:.1f})"
        if c["res2"]:
            t += f"\n  💎 2차 {int(c['res2']):,} ({(c['res2']/p-1)*100:+.1f}% · 1:{c['rr2']:.1f})"
        for n in c["news"]: t += f"\n  📰 {n}"
        blocks.append(t)
        sent[code] = {"t": STAMP, "kind": "눌림돌파"}
    tg(head + "\n\n" + "\n\n".join(blocks))
    print("알람", len(picked))
    nt = pd.DataFrame([{"시각": STAMP, "유형": "눌림돌파", "code": c["r"]["code"], "name": c["r"]["name"],
                        "알람가": c["r"]["종가"], "손절": round(c["sl"]), "목표1": round(c["d"]["hi"]),
                        "목표2": round(c["res2"]) if c["res2"] else None, "손익비": round(c["rr1"], 1),
                        "VWAP위": True, "코스닥": round(KQ, 2), "상승비율": round(up_ratio),
                        "당일등락": c["r"]["등락률"], "30분": None, "60분": None,
                        "최고": 0.0, "최저": 0.0, "현재": 0.0, "청산알림": ""} for c in picked])
    if os.path.exists(TRACK):
        try: nt = pd.concat([pd.read_csv(TRACK, dtype={"code": str}), nt], ignore_index=True)
        except Exception: pass
    nt.to_csv(TRACK, index=False, encoding="utf-8-sig")
else:
    print("알람 없음")
json.dump(sent, open(SENT, "w"))
