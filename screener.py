import re
import requests, pandas as pd, os, time, json, html, subprocess, sys, glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
MAX_ALERTS = 5         # 하루 눌림돌파+거래폭발 상한
MAX_THEME = 5          # 하루 섹터(후발주) 알람 상한 (10/10: 후발주가 유일하게 이기는 유형 — n25 승률 60% · 평균 +0.75%, 10시 n10 73% +1.67% → 3→5)
# 🧪 쌓인 성적으로 이길 확률 높은 쪽으로 (10/10 사용자: "여태 쌓아온 데이터도 반영해서 이길 확률이 높은 방향으로 계속 나아가")
#   유형×시간대 성적이 n≥OBS_N 이고 C 등급(평균 손익 < OBS_PNL)이면 「관찰」로만 기록 — 앱 푸시 안 함, 추적은 계속(데이터는 계속 쌓임)
#   성적이 좋아지면(A/B) 자동으로 다시 푸시. 새 이름 유형은 성적이 쌓일 때까지 옛 유형 성적을 빌려 씀.
OBS_N, OBS_PNL = 8, -0.3
ALIAS = {"눌림돌파": "돌파", "대장눌림": "눌림목"}
MAX_BURST = 2          # 하루 거래폭발 알람 상한

# ── 실행 ──
LOOP = os.environ.get("LOOP", "0") == "1"   # 1이면 한 실행 안에서 1분마다 반복
INTERVAL = 60
COMMIT_EVERY = 10                           # 분
DAY_KEEP = 60                               # 하루 단위 압축 스냅샷 보관 일수 (data/days/)
NEW_CUTOFF = 1130                           # 이 시각 이후 신규 알람 없음 (청산 추적은 계속)

# ── 섹터 후발주 ──
LEAD_HOLD_MIN = 30                          # 대장이 +LEAD_PCT 이상을 유지해야 하는 시간(분)
LEAD_PCT = 7
FOL_MIN_PCT, FOL_MAX_PCT = 2.5, 5.0         # 후발주 등락 범위 (10/03: 3~5% 구간 성적이 가장 좋았음)
FOL_MAX_RATIO = 0.5                         # 대장 대비 상한
LEAD_VAL_SMALL, LEAD_VAL_BIG = 1e10, 3e10   # 대장 거래대금 하한 (그룹 30개 이하 / 초과)
BROAD = ["지주", "밸류업", "배당", "기타", "신규상장", "코스피", "코스닥", "KRX", "MSCI", "지수", "우량"]

# ── 청산 규칙 (10/03: 가격손절이 자주 맞고 반등 → 시간·트레일링으로 변경) ──
HARD_SL = 0.96          # 안전 손절 -4%
T1, T2 = 1.03, 1.06     # 1차 +3% (절반 익절 권고) / 2차 +6%
TRAIL_ARM, TRAIL_DROP = 2.0, 2.0   # 최고 +2% 넘긴 뒤 최고 대비 -2%면 트레일링 청산
TIME_STOP_MIN, TIME_STOP_PCT = 60, 1.0     # 60분 지나도 +1% 미만이면 시간 청산

# ── 거래 폭발 ──
BURST_X = 5.0           # 최근 1분 거래대금 ÷ 당일 분당 평균
BURST_PX = 1.0          # 최근 2분 가격 상승 %
BURST_RANGE = (1.0, 8.0)  # 당일 등락 범위

H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://m.stock.naver.com/", "Accept": "application/json"}

NOW = STAMP = TODAY = START = HHMM = None
def set_clock():
    global NOW, STAMP, TODAY, START, HHMM
    NOW = datetime.now(KST)
    STAMP = NOW.strftime("%Y%m%d_%H%M")
    TODAY = NOW.strftime("%Y%m%d")
    START = (NOW - timedelta(days=150)).strftime("%Y%m%d")
    HHMM = NOW.hour * 100 + NOW.minute

def get(url, timeout=10):
    try:
        r = requests.get(url, headers=H, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def ntfy(msg, topic="chkchp-ch-danta"):
    """CH Investing 앱 알림 탭 → 휴대폰 푸시 (ntfy.sh, 무료·가입 없음)"""
    try:
        txt = re.sub(r"<[^>]+>", "", msg)
        first = txt.strip().split("\n")[0][:80]
        requests.post("https://ntfy.sh/", json={"topic": topic, "title": "⚡ " + first, "message": txt[:900],
                                               "tags": ["zap"], "click": "https://chkchp0702-spec.github.io/daily-app/#danta"}, timeout=10)
    except Exception as e:
        print("ntfy 실패", e)

def feed(rows_):
    """CH Investing 앱 단타 탭이 텔레그램과 같은 순간에 보도록 알람 줄을 그대로 보냄 (푸시 없음, 앱이 읽기만)"""
    try:
        keep = ["시각", "유형", "code", "name", "알람가", "손절", "목표1", "목표2", "당일등락", "손익비"]
        out = [{k: (None if isinstance(r.get(k), float) and r.get(k) != r.get(k) else r.get(k)) for k in keep} for r in rows_]
        requests.post("https://ntfy.sh/chkchp-ch-danta-feed", data=json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"),
                      headers={"Priority": "1"}, timeout=10)
    except Exception as e:
        print("feed 실패", e)

def tg(msg):
    ntfy(msg)
    if not TG_TOKEN or not TG_CHAT: return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        print("TG", r.status_code)
    except Exception as e:
        print("TG 실패", e)

# ── 알람 품질 등급 · 시장 국면 (CH Investing 앱이 매일 계산한 값) ──
APP_RAW = "https://raw.githubusercontent.com/chkchp0702-spec/daily-app/"
_GI = None
def grade_info():
    """danta_stats.json 의 유형×시간대 품질(A/B/C) + 나침반 한국 국면 — 세션마다 한 번만 받기"""
    global _GI
    if _GI is None:
        _GI = {"q": {}, "slot": {}, "all": None, "reg": None}
        try:
            j = requests.get(APP_RAW + "main/archive/x/danta_stats.json", timeout=10).json()
            _GI["q"], _GI["slot"], _GI["all"] = j.get("quality") or {}, j.get("by_slot") or {}, (j.get("all") or {}).get("win")
        except Exception as e:
            print("품질 정보 실패", e)
        try:
            _GI["reg"] = requests.get(APP_RAW + "opdata/compass.json", timeout=15).json()["markets"]["KR"]["regime"]
        except Exception as e:
            print("국면 정보 실패", e)
    return _GI

def slot_now():
    h = NOW.hour
    return "09시" if h <= 9 else "10시" if h == 10 else "11~12시" if h <= 12 else "13시 이후"

def tg_alarm(msg, kinds):
    """새 알람: 텔레그램은 전부(기록용) · 앱 푸시는 등급/국면에 따라 나눠 보내기
       chkchp-ch-danta      : 전부 (단, 한국이 하락장이면 A·B 등급만)
       chkchp-ch-danta-a    : A 등급만
       chkchp-ch-danta-best : 승률이 평균보다 높은 시간대에 뜬 알람만"""
    g = grade_info()
    sl = slot_now()
    def q_of(k):
        q = g["q"].get(f"{k}|{sl}") or {}
        if (q.get("n") or 0) < OBS_N and k in ALIAS:
            q = g["q"].get(f"{ALIAS[k]}|{sl}") or q
        return q
    qs = [q_of(k) for k in kinds]
    grades = [q.get("grade") for q in qs]
    best = min([x for x in grades if x] or ["?"])
    observe = bool(qs) and all((q.get("n") or 0) >= OBS_N and q.get("grade") == "C" and (q.get("pnl") or 0) < OBS_PNL for q in qs)
    if observe:
        q0 = max(qs, key=lambda q: q.get("pnl") or -99)
        msg = (f"👀 <b>관찰 기록</b> — 이 유형·시간대는 지금까지 성적이 나빠요 (n{q0.get('n')} · 승률 {q0.get('win', 0):.0f}% · 평균 {q0.get('pnl', 0):+.2f}%). "
               "따라 사지 말고 지켜보기만. 성적이 좋아지면 다시 알림으로 보내요.\n\n" + msg)
    reg = g["reg"]
    sw = (g["slot"].get(sl) or {}).get("win")
    good_slot = sw is not None and g["all"] is not None and sw >= g["all"]
    full = msg + f"\n\n🏷 품질 {best}" + (f" · {sl} 승률 {sw:.0f}%" if sw is not None else "") + (f" · 🧭 한국 {reg}" if reg else "")
    if observe:
        print("관찰 모드 — 앱 푸시 안 함", kinds, sl)
    elif reg == "하락장" and best not in ("A", "B"):
        full += "\n(하락장이라 C등급은 앱 푸시를 줄였어요)"
    else:
        ntfy(("⭐ " if best == "A" else "") + full)
    if best == "A" and not observe:
        ntfy(full, "chkchp-ch-danta-a")
    if good_slot and not observe:
        ntfy(full, "chkchp-ch-danta-best")
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": full, "parse_mode": "HTML", "disable_web_page_preview": "true"}, timeout=15)
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

def flag_of(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) or str(v) == "nan" else str(v)

def sim_pct(x):
    """최고·최저·종가만으로 근사: 안전손절 맞으면 -4, 1차 닿으면 +3, 아니면 종가"""
    try:
        sl = (float(x["손절"]) / x["알람가"] - 1) * 100
        t1 = (float(x["목표1"]) / x["알람가"] - 1) * 100
        if x["최저"] <= sl: return sl
        if x["최고"] >= t1: return t1
        return x["현재"]
    except Exception:
        return x["현재"]

# ── 저장소: 스냅샷은 로컬 1분 파일 + 하루 단위 압축본(data/days/)만 커밋 ──
TRACKED = ["data/tracking.csv", "data/sent.json", "data/summary_done.txt", "data/next_done.txt",
           "data/days", "data/theme_log.csv", "data/paused.txt", "data/tg_offset.txt", "data/session_end.txt"]

NEW_ALARM = [False]                            # 새 알람이 생기면 10분 기다리지 않고 바로 커밋

def git_commit():
    try:
        for p in TRACKED:
            if os.path.exists(p): subprocess.run(["git", "add", "-A", p], check=False)
        subprocess.run(["git", "add", "-u", "data/snaps"], check=False)   # 삭제만 반영, 1분 파일은 커밋 안 함
        r = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if r.returncode == 0: return
        subprocess.run(["git", "commit", "-q", "-m", f"scan {NOW.strftime('%H:%M')}"], check=False)
        subprocess.run(["git", "pull", "--rebase", "-q"], check=False)
        subprocess.run(["git", "push", "-q"], check=False)
        print("커밋", NOW.strftime("%H:%M"))
    except Exception as e:
        print("커밋 실패", e)

def save_day():
    """오늘 1분 스냅샷 전부 → data/days/YYYYMMDD.csv.gz (백테스트용)"""
    try:
        os.makedirs("data/days", exist_ok=True)
        fs = sorted(glob.glob(f"data/snaps/{TODAY}_*.csv"))
        if not fs: return
        parts = []
        for f in fs:
            s_ = pd.read_csv(f, dtype={"code": str}); s_.insert(0, "t", os.path.basename(f)[9:13]); parts.append(s_)
        pd.concat(parts).to_csv(f"data/days/{TODAY}.csv.gz", index=False, compression="gzip")
        cut = (datetime.now(KST) - timedelta(days=DAY_KEEP)).strftime("%Y%m%d")
        for f in glob.glob("data/days/*.csv.gz"):
            if os.path.basename(f)[:8] < cut: os.remove(f)
        for f in os.listdir("data/snaps"):
            if f[:8] < TODAY: os.remove(f"data/snaps/{f}")
        print("하루 압축 저장", len(fs))
    except Exception as e:
        print("압축 실패", e)

def load_series():
    """오늘 시계열: 압축본(오전 세션 것) + 로컬 1분 파일. return (series, 스냅 수)"""
    rows = {}
    gz = f"data/days/{TODAY}.csv.gz"
    if os.path.exists(gz):
        try:
            d = pd.read_csv(gz, dtype={"code": str, "t": str})
            for t, g in d.groupby("t"): rows[t] = g
        except Exception: pass
    for f in sorted(glob.glob(f"data/snaps/{TODAY}_*.csv")):
        t = os.path.basename(f)[9:13]
        if t not in rows:
            try: rows[t] = pd.read_csv(f, dtype={"code": str})
            except Exception: pass
    series = defaultdict(list)
    n = 0
    for t in sorted(rows):
        if int(t) < 900: continue
        n += 1; tm = int(t[:2]) * 60 + int(t[2:])
        g = rows[t]
        for c_, p_, v_ in zip(g["code"], g["종가"], g["거래대금"]):
            if not pd.isna(p_): series[c_].append((tm, float(p_), float(v_) if not pd.isna(v_) else 0))
    return series, n

# ── 텔레그램 명령: /쉼 (오늘 신규 알람 끔) · /재개 ──
def tg_commands():
    if not TG_TOKEN: return
    try:
        off = int(open("data/tg_offset.txt").read().strip()) if os.path.exists("data/tg_offset.txt") else 0
        j = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": off + 1, "timeout": 0}, timeout=10).json()
        for u in j.get("result", []):
            off = max(off, u["update_id"])
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(TG_CHAT): continue
            txt = (m.get("text") or "").strip()
            if txt in ("/쉼", "쉼", "/off"):
                open("data/paused.txt", "w").write(TODAY); tg("😴 오늘 신규 알람 쉬어요 (청산 추적은 계속). /재개 로 다시 켜요")
            elif txt in ("/재개", "재개", "/on"):
                if os.path.exists("data/paused.txt"): os.remove("data/paused.txt")
                tg("▶️ 알람 다시 켰어요")
            elif txt in ("/상태", "상태"):
                tg(f"⏱ {NOW.strftime('%H:%M')} 돌아가는 중 · 오늘 알람 {'OFF' if paused() else 'ON'}")
        open("data/tg_offset.txt", "w").write(str(off))
    except Exception as e:
        print("명령 확인 실패", e)

def paused():
    return os.path.exists("data/paused.txt") and open("data/paused.txt").read().strip() == TODAY

def theme_yesterday():
    """전 거래일에 발동했던 테마 키 집합"""
    try:
        if not os.path.exists("data/theme_log.csv"): return set()
        d = pd.read_csv("data/theme_log.csv", dtype=str)
        prev = sorted(set(d["date"]) - {TODAY})
        if not prev: return set()
        return set(d[d["date"] == prev[-1]]["key"])
    except Exception:
        return set()

def theme_log(key, name, lead, lp):
    try:
        new = not os.path.exists("data/theme_log.csv")
        d = pd.read_csv("data/theme_log.csv", dtype=str) if not new else pd.DataFrame(columns=["date", "key", "name", "lead", "lead_pct", "t"])
        if ((d["date"] == TODAY) & (d["key"] == key)).any(): return
        d.loc[len(d)] = [TODAY, key, name, lead, f"{lp:.1f}", NOW.strftime("%H:%M")]
        d.to_csv("data/theme_log.csv", index=False, encoding="utf-8-sig")
    except Exception as e:
        print("테마로그 실패", e)


def scan():
    set_clock()
    rows = []
    for mk in ["KOSPI", "KOSDAQ"]:
        rows += fetch(mk)
    uni = pd.read_csv("data/universe_latest.csv", dtype={"code": str})
    df = pd.DataFrame(rows).drop_duplicates(subset=["itemCode"])
    num = lambda s: pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce")
    df = df.rename(columns={"itemCode": "code", "stockName": "name"})
    df = df[df["code"].isin(set(uni["code"]))].copy()
    if len(df) < 200:
        print(f"{STAMP} | 수집 부족 {len(df)}종목 - 건너뜀"); return
    df["종가"] = num(df["closePriceRaw"]); df["등락률"] = num(df["fluctuationsRatio"])
    df["거래대금"] = num(df["accumulatedTradingValueRaw"])
    df["거래량"] = num(df["accumulatedTradingVolumeRaw"])
    cur = df[["code", "name", "종가", "등락률", "거래대금", "거래량"]]
    print(f"{STAMP} | {len(cur)}종목")
    os.makedirs("data/snaps", exist_ok=True)
    cur.to_csv(f"data/snaps/{STAMP}.csv", index=False, encoding="utf-8-sig")
    KQ = kosdaq_pct()
    tg_commands()

    if HHMM < 900:
        print("09:00 전 - 스냅샷만 저장"); return

    up_ratio = (cur["등락률"] > 0).mean() * 100
    WEAK = KQ <= -1.0 or up_ratio < 40
    print(f"코스닥 {KQ:+.2f}% · 상승 {up_ratio:.0f}%" + (" · 약세" if WEAK else ""))

    # ── 추적 갱신 + 청산 ──
    TRACK = "data/tracking.csv"
    COLS = ["시각","유형","code","name","알람가","손절","목표1","목표2","손익비","VWAP위",
            "코스닥","상승비율","당일등락","30분","60분","최고","최저","현재","청산알림"]
    streak_loss, today_cnt, burst_cnt, exits = 0, 0, 0, []
    if os.path.exists(TRACK):
        try:
            tr = pd.read_csv(TRACK, dtype={"code": str})
            for c in COLS:
                if c not in tr.columns: tr[c] = None
            px = dict(zip(cur["code"], cur["종가"]))
            for i, row in tr.iterrows():
                if not str(row["시각"]).startswith(TODAY): continue
                p = px.get(row["code"])
                if p is None or pd.isna(row.get("알람가")): continue
                t0 = datetime.strptime(str(row["시각"]), "%Y%m%d_%H%M").replace(tzinfo=KST)
                mins = (NOW - t0).total_seconds() / 60
                chg = (p / row["알람가"] - 1) * 100
                if mins >= 30 and pd.isna(row.get("30분")): tr.at[i, "30분"] = round(chg, 2)
                if mins >= 60 and pd.isna(row.get("60분")): tr.at[i, "60분"] = round(chg, 2)
                pm, lm = row.get("최고"), row.get("최저")
                hi = round(max(chg, pm if not pd.isna(pm) else -99), 2)
                tr.at[i, "최고"] = hi
                tr.at[i, "최저"] = round(min(chg, lm if not pd.isna(lm) else 99), 2)
                tr.at[i, "현재"] = round(chg, 2)
                flag = flag_of(row.get("청산알림"))
                if any(k in flag for k in ("손절", "트레일", "시간", "2차")): tr.at[i, "청산알림"] = flag; continue
                t1, t2, sl = row.get("목표1"), row.get("목표2"), row.get("손절")
                if t2 and not pd.isna(t2) and p >= float(t2):
                    exits.append(("2차목표", row, p, chg)); flag += "2차"
                elif t1 and not pd.isna(t1) and p >= float(t1) and "1차" not in flag:
                    exits.append(("1차목표", row, p, chg)); flag += "1차"
                elif sl and not pd.isna(sl) and p <= float(sl):
                    exits.append(("손절", row, p, chg)); flag += "손절"
                elif hi >= TRAIL_ARM and chg <= hi - TRAIL_DROP:
                    exits.append(("트레일링", row, p, chg)); flag += "트레일"
                elif mins >= TIME_STOP_MIN and chg < TIME_STOP_PCT and "1차" not in flag:
                    exits.append(("시간청산", row, p, chg)); flag += "시간"
                tr.at[i, "청산알림"] = flag
            tr.to_csv(TRACK, index=False, encoding="utf-8-sig")
            tt = tr[tr["시각"].astype(str).str.startswith(TODAY)]
            today_cnt = int((tt["유형"] != "후발주").sum())
            burst_cnt = int((tt["유형"] == "거래폭발").sum())
            for v in reversed(list(tt.dropna(subset=["30분"]).sort_values("시각")["30분"])):
                if v < 0: streak_loss += 1
                else: break
        except Exception as e:
            print("추적 실패", e)
    print(f"오늘 {today_cnt}건 · 연패 {streak_loss}")

    if exits:
        lines = [f"🔔 <b>{NOW.strftime('%H:%M')} 청산</b>", ""]
        for kind, row, p, chg in exits[:6]:
            icon = {"1차목표": "💰", "2차목표": "💎", "손절": "🛑", "트레일링": "📉", "시간청산": "⏱"}[kind]
            tip = {"1차목표": " · 절반 익절, 나머지는 트레일링", "트레일링": f" · 최고 {row['최고']:+.1f}% 대비 -{TRAIL_DROP:.0f}%",
                   "시간청산": f" · {TIME_STOP_MIN}분 지나도 힘 없음"}.get(kind, "")
            lines.append(f"{icon} <b>{kind}</b> {link(row['code'], row['name'])}\n  {int(p):,}원 · {chg:+.1f}%{tip}")
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
                    sim = [sim_pct(x) for _, x in t.iterrows()]
                    win = sum(1 for s in sim if s > 0)
                    lines += [f"알람 {len(t)}건 · 승 {win} 패 {len(t)-win}",
                              f"💼 시뮬 평균 <b>{sum(sim)/len(sim):+.2f}%</b> · 종가 평균 {t['현재'].mean():+.2f}%"]
                    for kind, g in t.groupby("유형"):
                        s_ = [sim_pct(x) for _, x in g.iterrows()]
                        lines.append(f"   {kind} {len(g)}건 · 시뮬 {sum(s_)/len(s_):+.2f}%")
                    lines.append("")
                    for _, x in t.iterrows():
                        lines.append(f"  {x['name']} 최고 {x['최고']:+.1f}% 최저 {x['최저']:+.1f}% → {x['현재']:+.1f}% {flag_of(x['청산알림'])}")
                else:
                    lines.append("알람 없음" + (" (오늘 /쉼)" if paused() else " (조건 충족 종목 없음)"))
            missed = cur[(cur["등락률"] >= 8) & (~cur["code"].isin(alerted)) & (cur["거래대금"] >= 5e9)]
            if len(missed):
                lines += ["", "😶 놓친 급등주: " + ", ".join(f"{x['name']} {x['등락률']:+.0f}%" for _, x in missed.head(5).iterrows())]
            tg("\n".join(lines)); open(DONE, "w").write(TODAY)
        except Exception as e:
            print("요약 실패", e)

    if HHMM >= NEW_CUTOFF:
        print(f"{NEW_CUTOFF//100}:{NEW_CUTOFF%100:02d} 이후 신규 알람 없음"); return
    if paused():
        print("오늘 /쉼"); return

    # ── 당일 시계열 ──
    series, nreg = load_series()
    now_min = NOW.hour * 60 + NOW.minute

    SENT = "data/sent.json"; sent = {}
    if os.path.exists(SENT):
        try: sent = json.load(open(SENT))
        except Exception: sent = {}
    sent = {k: v for k, v in sent.items() if isinstance(v, dict) and str(v.get("t", "")).startswith(TODAY)}
    vw = dict(zip(cur["code"], cur["거래대금"] / cur["거래량"].replace(0, float("nan"))))
    prev_close = {c: p / (1 + e / 100) for c, p, e in zip(cur["code"], cur["종가"], cur["등락률"]) if e > -99}

    def pm_recent(seq, window):
        """최근 window분 분당 거래대금, 당일 평균 분당 거래대금"""
        t2, _, v2 = seq[-1]; j = len(seq) - 2
        while j > 0 and t2 - seq[j][0] < window: j -= 1
        t1, _, v1 = seq[j]; t0, _, v0 = seq[0]
        return (v2 - v1) / max(t2 - t1, 1), (v2 - v0) / max(t2 - t0, 1)

    def accel(code, window=5):
        seq = series.get(code, [])
        if len(seq) < 3: return 0.0
        r, a = pm_recent(seq, window); return r / max(a, 1)

    def lead_hold(code, pct_now, price_now):
        seq = series.get(code, [])
        if len(seq) < 2 or pct_now <= -99: return 0
        pc0 = price_now / (1 + pct_now / 100); start = None
        for t, p, _ in seq:
            pc = (p / pc0 - 1) * 100
            if pc >= LEAD_PCT and start is None: start = t
            elif pc < LEAD_PCT - 2: start = None
        return (now_min - start) if start is not None else 0

    def add_track(rows_):
        nt = pd.DataFrame(rows_)
        if os.path.exists(TRACK):
            try: nt = pd.concat([pd.read_csv(TRACK, dtype={"code": str}), nt], ignore_index=True)
            except Exception: pass
        nt.to_csv(TRACK, index=False, encoding="utf-8-sig")
        NEW_ALARM[0] = True                       # 앱이 바로 보도록 이번 바퀴 끝에 즉시 저장
        feed(rows_)

    def trow(kind, c, name, p, pct, sl, t1, t2, rr):
        return {"시각": STAMP, "유형": kind, "code": c, "name": name, "알람가": p,
                "손절": round(sl), "목표1": round(t1), "목표2": round(t2) if t2 else None, "손익비": rr,
                "VWAP위": True, "코스닥": round(KQ, 2), "상승비율": round(up_ratio),
                "당일등락": pct, "30분": None, "60분": None, "최고": 0.0, "최저": 0.0, "현재": 0.0, "청산알림": ""}

    # ── 섹터 발동 → 후발주 ──
    active_ctx, lead_codes, theme_alerts = {}, {}, []
    ydy = theme_yesterday()
    if os.path.exists("data/themes.csv") and nreg >= 3 and HHMM >= 910:
        try:
            th = pd.read_csv("data/themes.csv", dtype={"code": str, "no": str})
            px = cur.set_index("code")
            th = th[th["code"].isin(px.index)]
            groups = []
            for (kind, no, gname), g in th.groupby(["kind", "no", "group"]):
                if any(b in str(gname) for b in BROAD): continue
                codes = list(g["code"])
                if not (3 <= len(codes) <= 120): continue
                m = px.loc[codes].sort_values("등락률", ascending=False)
                big = m[m["거래대금"] >= (LEAD_VAL_SMALL if len(codes) <= 30 else LEAD_VAL_BIG)]
                if big.empty: continue
                lead = big.iloc[0]; lp = lead["등락률"]
                if lp < LEAD_PCT: continue
                up = m[m["등락률"] >= 3]
                breadth = (m["등락률"] >= 2).mean()
                if len(up) < 3 or breadth < 0.3: continue
                key = f"{kind}{no}"
                theme_log(key, gname, lead["name"], lp)
                again = key in ydy
                hold = lead_hold(lead.name, lp, lead["종가"])
                rank = {c: i + 1 for i, c in enumerate(m.index)}
                lead_codes.setdefault(lead.name, gname)
                for c in m.index[:5]:
                    if m.loc[c, "등락률"] >= 1:
                        active_ctx.setdefault(c, f"🏷 {gname} · 대장 {lead['name']} {lp:+.0f}% · {rank[c]}등" + (" · 🔁2일째" if again else ""))
                if hold < LEAD_HOLD_MIN:
                    print(f"  {gname}: 대장 {lead['name']} {lp:+.1f}% 유지 {hold}분 - 대기"); continue
                groups.append({"key": key, "name": gname, "lead": lead, "m": m, "hold": hold, "again": again,
                               "score": lp + breadth * 20 + len(up) + (10 if again else 0)})
            groups.sort(key=lambda x: -x["score"])
            print(f"발동 섹터 {len(groups)}: " + ", ".join(g['name'] for g in groups[:5]))
            theme_today = sum(1 for v in sent.values() if v.get("kind") == "테마")
            for g in groups:
                if len(theme_alerts) + theme_today >= MAX_THEME: break
                if f"T:{g['key']}" in sent: continue
                lead, m = g["lead"], g["m"]
                cap = min(FOL_MAX_PCT, lead["등락률"] * FOL_MAX_RATIO)
                fol = []
                for c, r in m.iterrows():
                    if c == lead.name or c in sent: continue
                    if not (FOL_MIN_PCT <= r["등락률"] <= cap): continue
                    if r["거래대금"] < 1e9: continue
                    v_ = vw.get(c)
                    if v_ is None or pd.isna(v_) or r["종가"] < v_: continue
                    ac = accel(c)
                    if ac < 1.5: continue
                    fol.append((c, r, ac))
                fol.sort(key=lambda x: -x[1]["거래대금"])
                if not fol: continue
                theme_alerts.append((g, fol[:3]))
                sent[f"T:{g['key']}"] = {"t": STAMP, "kind": "테마"}
                for c, r, ac in fol[:3]: sent[c] = {"t": STAMP, "kind": "후발주"}
        except Exception as e:
            print("섹터 실패", e)

    if theme_alerts:
        lines = [f"🔥 <b>{NOW.strftime('%H:%M')} 섹터 발동</b>  코스닥 {KQ:+.1f}%" + (" ⚠️약세" if WEAK else "")]
        rows_ = []
        for g, fol in theme_alerts:
            lead, m = g["lead"], g["m"]
            upn = int((m["등락률"] >= 3).sum())
            lines += ["", f"🏷 <b>{g['name']}</b>  {upn}/{len(m)} 종목 +3%↑" + (" · 🔁 2일째" if g["again"] else ""),
                      f"  👑 대장 {link(lead.name, lead['name'])} {lead['등락률']:+.1f}% · {lead['거래대금']/1e8:.0f}억 · {g['hold']}분 유지"]
            for n_ in news(lead.name, 1)[:1]: lines.append(f"  📰 {n_}")
            for c, r, ac in fol:
                p = r["종가"]; sl, t1, t2 = p * HARD_SL, p * T1, p * T2
                lines.append(f"  ➡️ {link(c, r['name'])} {r['등락률']:+.1f}% · 거래 가속 ×{ac:.1f} · VWAP위 ✅"
                             f"\n     🛑 {int(sl):,}  💰 {int(t1):,}  💎 {int(t2):,}")
                rows_.append(trow("후발주", c, r["name"], p, r["등락률"], sl, t1, t2, 0.8))
        lines += ["", f"<i>청산: 1차 닿으면 절반 · 최고 대비 -{TRAIL_DROP:.0f}% · {TIME_STOP_MIN}분 무반응 · 안전손절 -4%</i>"]
        tg_alarm("\n".join(lines), ["후발주"]); print("섹터 알람", len(theme_alerts))
        add_track(rows_); json.dump(sent, open(SENT, "w"))

    if today_cnt >= MAX_ALERTS: print("상한"); json.dump(sent, open(SENT, "w")); return
    if streak_loss >= 2: print("2연패 중단"); json.dump(sent, open(SENT, "w")); return
    if nreg < 5 or HHMM < 920:
        print(f"스냅샷 {nreg}개 - 패턴 판별 대기"); json.dump(sent, open(SENT, "w")); return

    # ── ⚡ 거래 폭발 (1분 거래대금이 당일 평균의 BURST_X배 + 2분 내 +BURST_PX%) ──
    bursts = []
    if burst_cnt < MAX_BURST:
        for _, r in cur.iterrows():
            code = r["code"]; seq = series.get(code, [])
            if len(seq) < 6 or code in sent or seq[-1][1] != r["종가"]: continue
            if not (BURST_RANGE[0] <= r["등락률"] <= BURST_RANGE[1]): continue
            if r["거래대금"] < 2e9: continue
            if seq[-1][0] - seq[-2][0] > 2: continue            # 1분 데이터일 때만
            rec, avg = pm_recent(seq, 1)
            if avg <= 0 or rec / avg < BURST_X: continue
            j = len(seq) - 2
            while j > 0 and seq[-1][0] - seq[j][0] < 2: j -= 1
            px2 = (seq[-1][1] / seq[j][1] - 1) * 100
            if px2 < BURST_PX: continue
            v_ = vw.get(code)
            if v_ is None or pd.isna(v_) or r["종가"] < v_: continue
            bursts.append((code, r, rec / avg, px2))
        bursts.sort(key=lambda x: -x[2])
        bursts = bursts[:MAX_BURST - burst_cnt]
    if bursts:
        lines = [f"⚡ <b>{NOW.strftime('%H:%M')} 거래 폭발</b>  코스닥 {KQ:+.1f}%" + (" ⚠️약세" if WEAK else "")]
        rows_ = []
        for code, r, x, px2 in bursts:
            p = r["종가"]; sl, t1, t2 = p * HARD_SL, p * T1, p * T2
            lines += ["", f"<b>{link(code, r['name'])}</b> {int(p):,}원 · 당일 {r['등락률']:+.1f}%",
                      f"  💥 1분 거래대금 평균의 ×{x:.0f} · 2분 {px2:+.1f}% · VWAP위 ✅"]
            if code in active_ctx: lines.append(f"  {active_ctx[code]}")
            lines.append(f"  🛑 {int(sl):,}  💰 {int(t1):,}  💎 {int(t2):,}")
            rows_.append(trow("거래폭발", code, r["name"], p, r["등락률"], sl, t1, t2, 0.8))
            sent[code] = {"t": STAMP, "kind": "거래폭발"}
        tg_alarm("\n".join(lines), ["거래폭발"]); print("폭발 알람", len(bursts))
        add_track(rows_); today_cnt += len(bursts)

    # ── 🎯 눌림목 돌파 (대장주 우선) ──
    def detect(seq):
        if len(seq) < 5: return None
        n = len(seq)
        cur_t, cur_p, cur_v = seq[-1]
        prev_p = seq[-2][1]
        hi_i = max(range(n - 1), key=lambda i: seq[i][1])
        hi_t, hi_p = seq[hi_i][0], seq[hi_i][1]
        if hi_i >= n - 3: return None
        lo_i = min(range(hi_i + 1, n - 1), key=lambda i: seq[i][1])
        lo_t, lo_p = seq[lo_i][0], seq[lo_i][1]
        dd = (lo_p / hi_p - 1) * 100
        if not (-9 <= dd <= -2.5): return None
        if lo_t - hi_t < 15: return None
        if lo_i >= n - 2: return None
        rb_i = max(range(lo_i + 1, n - 1), key=lambda i: seq[i][1])
        rb_p = seq[rb_i][1]
        if rb_p >= hi_p * 0.995: return None
        if rb_p < lo_p * 1.005: return None
        if not (prev_p <= rb_p * 1.002 and cur_p > rb_p * 1.003): return None
        if cur_p >= hi_p * 0.995: return None
        def pm(i, j):
            dt = max(seq[j][0] - seq[i][0], 1); return (seq[j][2] - seq[i][2]) / dt
        k = n - 2
        while k > 0 and cur_t - seq[k][0] < 5: k -= 1
        recent = pm(k, n - 1)
        corr = pm(hi_i, lo_i) if lo_i > hi_i else 1
        rise = pm(0, hi_i) if hi_i > 0 else recent
        if recent < corr * 1.3: return None
        if recent < rise * 0.5: return None
        return {"hi": hi_p, "lo": lo_p, "rb": rb_p, "dd": dd, "corr_min": lo_t - hi_t,
                "since_lo": cur_t - lo_t, "recent_pm": recent, "vol_ratio": recent / max(corr, 1)}

    cands = []
    for _, r in cur.iterrows():
        code = r["code"]
        seq = series.get(code, [])
        if len(seq) < 5 or seq[-1][1] != r["종가"] or code in sent: continue
        d = detect(seq)
        if not d: continue
        pc0 = prev_close.get(code)
        if not pc0: continue
        rise_pct = (d["hi"] / pc0 - 1) * 100
        if rise_pct < 4: continue
        if r["거래대금"] < 3e9: continue
        v_ = vw.get(code)
        if v_ is None or pd.isna(v_) or r["종가"] < v_: continue
        sl = d["lo"] * 0.995
        risk = r["종가"] - sl
        rr1 = (d["hi"] - r["종가"]) / risk if risk > 0 else 0
        if rr1 < (1.5 if WEAK else 1.0): continue
        cands.append({"r": r, "d": d, "rise": rise_pct, "sl": sl, "rr1": rr1, "lead": code in lead_codes})
    print(f"패턴 후보 {len(cands)} (대장 {sum(c['lead'] for c in cands)})")

    cands.sort(key=lambda x: (-x["lead"], -x["rr1"]))     # 대장주 먼저
    picked = []
    for c in cands:
        if today_cnt + len(picked) >= MAX_ALERTS: break
        r, d = c["r"], c["d"]; code = r["code"]
        ok, note, res2 = analyze(code, r["등락률"], r["종가"])
        if not ok: print(f"제외 {r['name']}: {note}"); continue
        c["note"], c["res2"] = note, res2
        c["rr2"] = (res2 - r["종가"]) / (r["종가"] - c["sl"]) if res2 else None
        c["news"] = news(code)
        picked.append(c); time.sleep(0.2)

    if picked:
        head = f"🎯 <b>{NOW.strftime('%H:%M')} 눌림목 돌파 {len(picked)}건</b>  코스닥 {KQ:+.1f}%"
        if WEAK: head += " ⚠️약세"
        blocks, rows_ = [], []
        for c in picked:
            r, d = c["r"], c["d"]; code = r["code"]; p = r["종가"]
            t = f"<b>{link(code, r['name'])}</b> ({code})" + (f"  👑 <b>{lead_codes[code]} 대장</b>" if c["lead"] else "")
            t += f"\n  {int(p):,}원  당일 {r['등락률']:+.1f}%"
            t += (f"\n  📐 급등 +{c['rise']:.1f}% → 조정 {d['dd']:.1f}% ({d['corr_min']}분)"
                  f" → 조정고점 {int(d['rb']):,} 돌파")
            t += f"\n  💧 거래 재유입 ×{d['vol_ratio']:.1f} · VWAP위 ✅"
            if c["note"]: t += f"\n  📈 {c['note']}"
            t += f"\n  🛑 손절 {int(c['sl']):,} ({(c['sl']/p-1)*100:+.1f}%)"
            t += f"\n  💰 1차 {int(d['hi']):,} ({(d['hi']/p-1)*100:+.1f}% · 1:{c['rr1']:.1f})"
            if c["res2"]:
                t += f"\n  💎 2차 {int(c['res2']):,} ({(c['res2']/p-1)*100:+.1f}% · 1:{c['rr2']:.1f})"
            if code in active_ctx and not c["lead"]: t += f"\n  {active_ctx[code]}"
            for n_ in c["news"]: t += f"\n  📰 {n_}"
            blocks.append(t)
            sent[code] = {"t": STAMP, "kind": "눌림돌파"}
            rows_.append(trow("대장눌림" if c["lead"] else "눌림돌파", code, r["name"], p, r["등락률"],
                              c["sl"], d["hi"], c["res2"], round(c["rr1"], 1)))
        tg_alarm(head + "\n\n" + "\n\n".join(blocks), sorted({"대장눌림" if c["lead"] else "눌림돌파" for c in picked}))
        print("알람", len(picked)); add_track(rows_)
    else:
        print("알람 없음")
    json.dump(sent, open(SENT, "w"))


# ── 실행 ──
set_clock()
if not LOOP:
    scan()
    sys.exit(0)

END = 12 * 60 + 2 if HHMM < 1202 else 15 * 60 + 36
if NOW.weekday() >= 5 or HHMM >= 1536:
    print("장 시간 외 - 종료"); sys.exit(0)
print(f"반복 모드 시작 {NOW.strftime('%H:%M')} → {END//60:02d}:{END%60:02d}")
last_commit = time.time()
while True:
    t0 = time.time()
    try:
        scan()
    except Exception as e:
        print("scan 오류", e)
    if NEW_ALARM[0] or time.time() - last_commit >= COMMIT_EVERY * 60:
        NEW_ALARM[0] = False
        git_commit(); last_commit = time.time()
    set_clock()
    if NOW.hour * 60 + NOW.minute >= END: break
    time.sleep(max(5, INTERVAL - (time.time() - t0)))
save_day()
open("data/session_end.txt", "w").write("am" if END < 13 * 60 else "pm")
git_commit()
print("세션 종료", NOW.strftime("%H:%M"))
