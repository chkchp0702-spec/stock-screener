"""주말 자동 백테스트
data/days/*.csv.gz (하루 단위 1분 스냅샷)로 후발주·대장눌림·거래폭발 전략을
여러 파라미터 조합으로 돌려서 성적표를 텔레그램으로 보낸다.
결과 전체는 data/backtest/YYYYMMDD.csv 에 저장.
"""
import os, glob, json, requests, itertools
import numpy as np, pandas as pd
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TG_TOKEN = os.environ.get("TG_TOKEN", ""); TG_CHAT = os.environ.get("TG_CHAT", "")
BROAD = ["지주", "밸류업", "배당", "기타", "신규상장", "코스피", "코스닥", "KRX", "MSCI", "지수", "우량"]
WIN = (910, 1130)          # 신규 알람 시간대
MIN_SNAPS = 100            # 1분 데이터인 날만 (10분 데이터 날은 제외)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: print(msg); return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=15)

# ── 데이터 로드: 하루 → 분×종목 행렬 ──
def load_days():
    days = {}
    srcs = {}
    for f in glob.glob("data/days/*.csv.gz"):
        srcs[os.path.basename(f)[:8]] = ("gz", f)
    for f in glob.glob("data/snaps/*.csv"):
        d = os.path.basename(f)[:8]
        if d not in srcs: srcs[d] = ("snaps", None)
    for d, (kind, f) in sorted(srcs.items()):
        if kind == "gz":
            raw = pd.read_csv(f, dtype={"code": str, "t": str})
        else:
            parts = []
            for g in sorted(glob.glob(f"data/snaps/{d}_*.csv")):
                s_ = pd.read_csv(g, dtype={"code": str}); s_.insert(0, "t", os.path.basename(g)[9:13]); parts.append(s_)
            if not parts: continue
            raw = pd.concat(parts)
        raw = raw[raw["t"].astype(int) >= 900]
        ts = sorted(raw["t"].unique())
        if len(ts) < MIN_SNAPS: print(f"{d}: 스냅 {len(ts)}개 - 10분 데이터, 제외"); continue
        P = raw.pivot_table(index="t", columns="code", values="종가").sort_index()
        E = raw.pivot_table(index="t", columns="code", values="등락률").reindex(P.index)[P.columns]
        V = raw.pivot_table(index="t", columns="code", values="거래대금").reindex(P.index)[P.columns].fillna(0)
        Q = raw.pivot_table(index="t", columns="code", values="거래량").reindex(P.index)[P.columns].fillna(0)
        names = dict(zip(raw["code"], raw["name"]))
        tm = np.array([int(t[:2]) * 60 + int(t[2:]) for t in P.index])
        days[d] = dict(P=P.values, E=E.values, V=V.values, Q=Q.values, codes=list(P.columns), tm=tm,
                       hhmm=np.array([int(t) for t in P.index]), names=names)
        print(f"{d}: {len(ts)}분 × {P.shape[1]}종목")
    return days

def prep(D):
    """파생 지표: VWAP위, 가속(5분/당일), 대장 유지시간, 1분 폭발비"""
    P, V, Q, tm = D["P"], D["V"], D["Q"], D["tm"]
    n = len(tm)
    vwap = np.where(Q > 0, V / np.maximum(Q, 1), np.nan)
    D["above"] = P >= vwap
    elapsed = np.maximum(tm - tm[0], 1)[:, None]
    avg = (V - V[0]) / elapsed
    i5 = np.searchsorted(tm, tm - 5, side="left"); i5 = np.maximum(i5, 0)
    dt5 = np.maximum(tm - tm[i5], 1)[:, None]
    rec5 = (V - V[i5]) / dt5
    D["acc"] = rec5 / np.maximum(avg, 1)
    i1 = np.maximum(np.arange(n) - 1, 0); dt1 = np.maximum(tm - tm[i1], 1)[:, None]
    D["burst"] = ((V - V[i1]) / dt1) / np.maximum(avg, 1)
    i2 = np.searchsorted(tm, tm - 2, side="left"); i2 = np.maximum(i2, 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        D["px2"] = (P / P[i2] - 1) * 100
    # 대장 유지: +7 넘긴 뒤 +5 아래로 안 빠진 시간
    E = D["E"]; start = np.full(E.shape[1], -1.0); hold = np.zeros_like(E)
    for i in range(n):
        e = np.nan_to_num(E[i], nan=-99)
        start = np.where((e >= 7) & (start < 0), tm[i], start)
        start = np.where(e < 5, -1, start)
        hold[i] = np.where(start >= 0, tm[i] - start, 0)
    D["hold"] = hold

def exit_ret(path, p0, rule):
    """path: 진입 다음 분부터 종가까지 가격. rule: ('fixed',sl,t1) | ('close',) | ('trail',arm,drop,hard,tmin)"""
    if len(path) == 0: return 0.0
    r = (path / p0 - 1) * 100
    if rule[0] == "close": return r[-1]
    if rule[0] == "fixed":
        _, sl, t1 = rule
        hs = np.where(r <= sl)[0]; ht = np.where(r >= t1)[0]
        a = hs[0] if len(hs) else 10**9; b = ht[0] if len(ht) else 10**9
        if a == b == 10**9: return r[-1]
        return sl if a < b else t1
    _, arm, drop, hard, tmin = rule
    peak = -99
    for k, x in enumerate(r):
        peak = max(peak, x)
        if x <= hard: return hard
        if peak >= arm and x <= peak - drop: return x
        if k + 1 >= tmin and x < 1.0 and peak < 3: return x
    return r[-1]

EXITS = {"고정 -3/+5": ("fixed", -3, 5), "고정 -2.5/+3": ("fixed", -2.5, 3), "고정 -4/+3": ("fixed", -4, 3),
         "종가보유": ("close",), "트레일(새규칙)": ("trail", 2, 2, -4, 60)}

def run_follower(D, th_groups, hold_min, band, ratio):
    """후발주 신호 목록 [(i, code, key)]"""
    P, E, V, tm, hhmm = D["P"], D["E"], D["V"], D["tm"], D["hhmm"]
    idx = {c: k for k, c in enumerate(D["codes"])}
    sig, used_theme, used_code, n_theme = [], set(), set(), 0
    En = np.nan_to_num(E, nan=-99)
    cand_minutes = {}
    for key, (gname, codes) in th_groups.items():
        ii = [idx[c] for c in codes if c in idx]
        if not (3 <= len(ii) <= 120): continue
        Eg, Vg = En[:, ii], V[:, ii]
        thr = 1e10 if len(ii) <= 30 else 3e10
        Em = np.where(Vg >= thr, Eg, -99)
        li = Em.argmax(1); lp = Em.max(1)
        up = (Eg >= 3).sum(1); br = (Eg >= 2).mean(1)
        lead_global = np.array(ii)[li]
        hold = D["hold"][np.arange(len(tm)), lead_global]
        ok = (lp >= 7) & (up >= 3) & (br >= 0.3) & (hold >= hold_min) & (hhmm >= WIN[0]) & (hhmm < WIN[1])
        for i in np.where(ok)[0]:
            cand_minutes.setdefault(i, []).append((lp[i] + br[i] * 20 + up[i], key, ii, lead_global[i], lp[i]))
    for i in sorted(cand_minutes):
        for score, key, ii, lead, lp in sorted(cand_minutes[i], key=lambda x: -x[0]):
            if n_theme >= 3: break
            if key in used_theme: continue
            cap = min(band[1], lp * ratio)
            fol = []
            for k in ii:
                if k == lead or k in used_code: continue
                e = En[i, k]
                if not (band[0] <= e <= cap): continue
                if V[i, k] < 1e9 or not D["above"][i, k] or D["acc"][i, k] < 1.5: continue
                fol.append((V[i, k], k))
            if not fol: continue
            fol.sort(reverse=True)
            used_theme.add(key); n_theme += 1
            for _, k in fol[:3]:
                used_code.add(k); sig.append((i, k, key))
    return sig

def run_leader_pullback(D, th_groups):
    """발동 섹터(유지시간 무관) 대장의 눌림목 돌파"""
    P, E, V, tm, hhmm = D["P"], D["E"], D["V"], D["tm"], D["hhmm"]
    idx = {c: k for k, c in enumerate(D["codes"])}
    En = np.nan_to_num(E, nan=-99)
    leaders = {}   # k -> set(minutes active)
    for key, (gname, codes) in th_groups.items():
        ii = [idx[c] for c in codes if c in idx]
        if not (3 <= len(ii) <= 120): continue
        Eg, Vg = En[:, ii], V[:, ii]
        thr = 1e10 if len(ii) <= 30 else 3e10
        Em = np.where(Vg >= thr, Eg, -99); li = Em.argmax(1); lp = Em.max(1)
        ok = (lp >= 7) & ((Eg >= 3).sum(1) >= 3) & ((Eg >= 2).mean(1) >= 0.3)
        for i in np.where(ok)[0]: leaders.setdefault(ii[li[i]], set()).add(i)
    sig, used = [], set()
    for k, mins in leaders.items():
        path = P[:, k]
        if np.isnan(path).any(): continue
        for i in sorted(mins):
            if not (WIN[0] <= hhmm[i] < WIN[1]) or k in used or i < 5: continue
            if detect(path[:i + 1], V[:i + 1, k], tm[:i + 1]) and D["above"][i, k]:
                sig.append((i, k, "lead")); used.add(k); break
    return sig

def detect(p, v, t):
    n = len(p)
    if n < 5: return False
    cur_p, prev_p = p[-1], p[-2]
    hi_i = int(np.argmax(p[:-1])); hi_p, hi_t = p[hi_i], t[hi_i]
    if hi_i >= n - 3: return False
    seg = p[hi_i + 1:n - 1]; lo_i = hi_i + 1 + int(np.argmin(seg)); lo_p, lo_t = p[lo_i], t[lo_i]
    dd = (lo_p / hi_p - 1) * 100
    if not (-9 <= dd <= -2.5) or lo_t - hi_t < 15 or lo_i >= n - 2: return False
    rb_p = p[lo_i + 1:n - 1].max()
    if rb_p >= hi_p * 0.995 or rb_p < lo_p * 1.005: return False
    if not (prev_p <= rb_p * 1.002 and cur_p > rb_p * 1.003) or cur_p >= hi_p * 0.995: return False
    k = n - 2
    while k > 0 and t[-1] - t[k] < 5: k -= 1
    pm = lambda i, j: (v[j] - v[i]) / max(t[j] - t[i], 1)
    recent = pm(k, n - 1); corr = pm(hi_i, lo_i) if lo_i > hi_i else 1; rise = pm(0, hi_i) if hi_i > 0 else recent
    return recent >= corr * 1.3 and recent >= rise * 0.5

def run_burst(D, x=5.0, px=1.0, rng=(1, 8)):
    E, V, hhmm = D["E"], D["V"], D["hhmm"]
    En = np.nan_to_num(E, nan=-99)
    ok = (D["burst"] >= x) & (np.nan_to_num(D["px2"], nan=0) >= px) & (En >= rng[0]) & (En <= rng[1]) \
         & (V >= 2e9) & D["above"] & (hhmm >= 920)[:, None] & (hhmm < WIN[1])[:, None]
    sig, used, per_day = [], set(), 0
    for i in range(len(hhmm)):
        ks = np.where(ok[i])[0]
        if not len(ks) or per_day >= 2: continue
        for k in sorted(ks, key=lambda k: -D["burst"][i, k]):
            if k in used or per_day >= 2: continue
            used.add(k); per_day += 1; sig.append((i, k, "burst"))
    return sig

def evaluate(D, sig, rule):
    out = []
    for i, k, key in sig:
        p0 = D["P"][i, k]; path = D["P"][i + 1:, k]
        path = path[~np.isnan(path)]
        if np.isnan(p0) or not len(path): continue
        out.append(exit_ret(path, p0, rule))
    return out

# ── 메인 ──
days = load_days()
if not days:
    tg("🧪 백테스트: 1분 데이터가 아직 없어요"); raise SystemExit(0)
th = pd.read_csv("data/themes.csv", dtype={"code": str, "no": str})
th_groups = {}
for (kind, no, gname), g in th.groupby(["kind", "no", "group"]):
    if any(b in str(gname) for b in BROAD): continue
    th_groups[f"{kind}{no}"] = (gname, list(g["code"]))
for D in days.values(): prep(D)

rows = []
strategies = {}
for hold_min, band, ratio in itertools.product([0, 20, 30], [(1, 4), (2.5, 5), (3, 6)], [0.5]):
    strategies[f"후발주 유지{hold_min}분 밴드{band[0]}~{band[1]}%"] = lambda D, h=hold_min, b=band, r=ratio: run_follower(D, th_groups, h, b, r)
strategies["대장 눌림목"] = lambda D: run_leader_pullback(D, th_groups)
for x in [4, 5, 7]:
    strategies[f"거래폭발 ×{x}"] = lambda D, x=x: run_burst(D, x)

for sname, fn in strategies.items():
    sigs = {d: fn(D) for d, D in days.items()}
    n = sum(len(s) for s in sigs.values())
    for ename, rule in EXITS.items():
        rets = []
        for d, D in days.items(): rets += evaluate(D, sigs[d], rule)
        if not rets:
            rows.append(dict(전략=sname, 청산=ename, 건수=0, 평균=np.nan, 승률=np.nan, 합계=0)); continue
        r = np.array(rets)
        rows.append(dict(전략=sname, 청산=ename, 건수=len(r), 평균=round(r.mean(), 2),
                         승률=round((r > 0).mean() * 100), 합계=round(r.sum(), 1)))
    print(sname, n)
res = pd.DataFrame(rows)
os.makedirs("data/backtest", exist_ok=True)
res.to_csv(f"data/backtest/{NOW.strftime('%Y%m%d')}.csv", index=False, encoding="utf-8-sig")

# ── 텔레그램 요약 ──
dl = sorted(days)
lines = [f"🧪 <b>주간 백테스트</b>  {dl[0][4:6]}/{dl[0][6:]}~{dl[-1][4:6]}/{dl[-1][6:]} · {len(dl)}일 (1분 데이터)", ""]
best = res[res["건수"] >= 5].sort_values("평균", ascending=False)
lines.append("<b>🏆 평균 수익 상위 (5건 이상)</b>")
for _, x in best.head(8).iterrows():
    lines.append(f"  {x['평균']:+.2f}% · 승 {x['승률']:.0f}% · {x['건수']}건  {x['전략']} / {x['청산']}")
lines += ["", "<b>📌 지금 쓰는 설정</b>"]
cur = res[(res["전략"] == "후발주 유지30분 밴드2.5~5%")]
for _, x in cur.iterrows():
    lines.append(f"  {x['청산']}: {x['평균']:+.2f}% · 승 {x['승률']:.0f}% · {x['건수']}건" if x["건수"] else f"  {x['청산']}: 0건")
lines += ["", "<b>⚖️ 청산 규칙별 (전체 전략 합산)</b>"]
for ename, g in res[res["건수"] > 0].groupby("청산"):
    w = (g["평균"] * g["건수"]).sum() / g["건수"].sum()
    lines.append(f"  {ename}: {w:+.2f}% ({g['건수'].sum()}건)")
lines += ["", "<b>🎯 대장눌림 / ⚡ 폭발</b>"]
for s in ["대장 눌림목", "거래폭발 ×5"]:
    g = res[(res["전략"] == s) & (res["청산"] == "트레일(새규칙)")]
    if len(g) and g.iloc[0]["건수"]:
        x = g.iloc[0]; lines.append(f"  {s}: {x['평균']:+.2f}% · 승 {x['승률']:.0f}% · {x['건수']}건")
    else: lines.append(f"  {s}: 0건")
lines += ["", "<i>전체 표: data/backtest/ 폴더. 20건 미만은 참고만.</i>"]
tg("\n".join(lines))
print("\n".join(lines))
