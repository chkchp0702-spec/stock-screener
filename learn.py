"""🧪 단타 스스로 배우기 (10/10 사용자: "단타는 장중에 계속 돌리는데, 여태 쌓아온 데이터들도 반영해서 이길 확률이 높은 방향으로 계속 나아가")

쌓인 1분 스냅샷(data/days/*.csv.gz + 오늘 data/snaps)으로 알람 유형마다 「그 신호가 그 시각에 떴다면」을 하루 전체(09:10~14:50)에서
다시 돌려 보고, 실제 알람 장부(data/tracking.csv)와 합쳐 → data/learned.json 을 만든다. screener.py 가 세션 시작과 장중 매시간 읽는다.

  · 유형: 후발주(섹터 발동 뒤 따라가는 종목) · 대장눌림 · 눌림돌파(일반) · 거래폭발
  · 시간대: 09시 · 10시 · 11~12시 · 13시 이후 — 시간대별로 켬/관찰/끔을 정한다 (11:30 이후도 성적이 좋으면 연다)
  · 손잡이: 후발주 대장 유지시간·등락 밴드 · 거래폭발 배수 · 청산 규칙(트레일 폭·시간 손절) 중 성적이 가장 좋은 것
  · 비용: 수수료·세금·체결 미끄러짐 0.3% 를 빼고 본다
  · 과적합 막기: 표본 15건 미만은 바꾸지 않음 · 평균은 표본이 적을수록 0 쪽으로 눌러서(축소) 비교 · 지금 설정보다 확실히(+0.15%p) 나을 때만 바꿈
  · 지나간 날 결과는 data/learn/YYYYMMDD.json 에 저장해 두고(다시 계산 안 함) 오늘 것만 새로 계산 → 장중에도 1분 안에 끝남
"""
from __future__ import annotations
import glob, json, os, sys, itertools, time
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd

KST = timezone(timedelta(hours=9))
BROAD = ["지주", "밸류업", "배당", "기타", "신규상장", "코스피", "코스닥", "KRX", "MSCI", "지수", "우량"]
COST = 0.3                    # 왕복 비용(%) — 수수료·세금·미끄러짐
WIN = (910, 1450)             # 다시 돌려 보는 신규 진입 시간대 (실전은 learned.json 의 slots 로 정함)
SLOTS = ["09시", "10시", "11~12시", "13시 이후"]
CACHE = "data/learn"
OUT = "data/learned.json"
VER = 3                       # 계산 방식이 바뀌면 올림 → 캐시 다시 만듦
PMIN = 25                     # 손잡이(밴드·배수·청산)를 바꾸려면 이만큼 표본이 필요

EXITS = {                                     # (팔 기준) 지금 쓰는 것 = 트레일 2/2·60분
    "트레일 2/2·60분": ("trail", 2, 2, -4, 60),
    "트레일 3/2·60분": ("trail", 3, 2, -4, 60),
    "트레일 2/1.5·45분": ("trail", 2, 1.5, -3, 45),
    "트레일 2/2·90분": ("trail", 2, 2, -4, 90),
    "고정 −3/+5": ("fixed", -3, 5),
    "종가까지": ("close",),
}
DEFAULT = {"fol_hold": 30, "fol_band": [2.5, 5.0], "burst_x": 5.0, "exit": "트레일 2/2·60분"}


def slot_of(hhmm: int) -> str:
    h = hhmm // 100
    return "09시" if h <= 9 else "10시" if h == 10 else "11~12시" if h <= 12 else "13시 이후"


# ── 데이터 ──
def load_day(raw: pd.DataFrame):
    raw = raw[raw["t"].astype(int) >= 900]
    ts = sorted(raw["t"].unique())
    if len(ts) < 30:
        return None
    P = raw.pivot_table(index="t", columns="code", values="종가").sort_index()
    if (P.diff().abs() > 0).sum().sum() == 0:      # 휴장일(가격이 한 번도 안 움직임)
        return None
    E = raw.pivot_table(index="t", columns="code", values="등락률").reindex(P.index)[P.columns]
    V = raw.pivot_table(index="t", columns="code", values="거래대금").reindex(P.index)[P.columns].ffill().fillna(0)
    Q = raw.pivot_table(index="t", columns="code", values="거래량").reindex(P.index)[P.columns].ffill().fillna(0)
    P = P.ffill()
    tm = np.array([int(t[:2]) * 60 + int(t[2:]) for t in P.index])
    D = dict(P=P.values, E=E.ffill().values, V=V.values, Q=Q.values, codes=list(P.columns), tm=tm,
             hhmm=np.array([int(t) for t in P.index]))
    prep(D)
    return D


def read_day(d: str):
    f = f"data/days/{d}.csv.gz"
    parts = []
    if os.path.exists(f):
        parts.append(pd.read_csv(f, dtype={"code": str, "t": str}))
    for g in sorted(glob.glob(f"data/snaps/{d}_*.csv")):
        try:
            s_ = pd.read_csv(g, dtype={"code": str}); s_.insert(0, "t", os.path.basename(g)[9:13]); parts.append(s_)
        except Exception:
            pass
    if not parts:
        return None
    raw = pd.concat(parts).drop_duplicates(subset=["t", "code"], keep="last")
    raw["t"] = raw["t"].astype(str).str.zfill(4)
    return load_day(raw)


def prep(D):
    P, V, Q, tm = D["P"], D["V"], D["Q"], D["tm"]
    n = len(tm)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(Q > 0, V / np.maximum(Q, 1), np.nan)
        D["above"] = P >= vwap
    elapsed = np.maximum(tm - tm[0], 1)[:, None]
    avg = (V - V[0]) / elapsed
    i5 = np.maximum(np.searchsorted(tm, tm - 5, side="left"), 0)
    D["acc"] = ((V - V[i5]) / np.maximum(tm - tm[i5], 1)[:, None]) / np.maximum(avg, 1)
    i1 = np.maximum(np.arange(n) - 1, 0)
    gap1 = (tm - tm[i1])[:, None]
    D["burst"] = np.where(gap1 <= 2, ((V - V[i1]) / np.maximum(gap1, 1)) / np.maximum(avg, 1), 0)
    i2 = np.maximum(np.searchsorted(tm, tm - 2, side="left"), 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        D["px2"] = (P / P[i2] - 1) * 100
    E = np.nan_to_num(D["E"], nan=-99)
    start = np.full(E.shape[1], -1.0); hold = np.zeros_like(E)
    for i in range(n):                      # 대장 유지: +7 넘긴 뒤 +5 아래로 안 빠진 시간 (screener lead_hold 와 같음)
        e = E[i]
        start = np.where((e >= 7) & (start < 0), tm[i], start)
        start = np.where(e < 5, -1, start)
        hold[i] = np.where(start >= 0, tm[i] - start, 0)
    D["hold"] = hold
    with np.errstate(invalid="ignore", divide="ignore"):
        pc = P[0] / (1 + D["E"][0] / 100)
        D["prev"] = np.where(np.isfinite(pc), pc, np.nan)


# ── 청산 ──
def exit_ret(path, p0, rule):
    if len(path) == 0:
        return None
    r = (path / p0 - 1) * 100
    if rule[0] == "close":
        return float(r[-1])
    if rule[0] == "fixed":
        _, sl, t1 = rule
        hs = np.where(r <= sl)[0]; ht = np.where(r >= t1)[0]
        a = hs[0] if len(hs) else 10 ** 9; b = ht[0] if len(ht) else 10 ** 9
        if a == b == 10 ** 9:
            return float(r[-1])
        return float(sl if a < b else t1)
    _, arm, drop, hard, tmin = rule
    peak = -99
    for k, x in enumerate(r):
        peak = max(peak, x)
        if x <= hard:
            return float(hard)
        if peak >= arm and x <= peak - drop:
            return float(x)
        if k + 1 >= tmin and x < 1.0 and peak < 3:
            return float(x)
    return float(r[-1])


# ── 신호 ──
def groups():
    th = pd.read_csv("data/themes.csv", dtype={"code": str, "no": str})
    out = {}
    for (kind, no, gname), g in th.groupby(["kind", "no", "group"]):
        if any(b in str(gname) for b in BROAD):
            continue
        out[f"{kind}{no}"] = (gname, list(g["code"]))
    return out


def theme_state(D, TG):
    """분마다 발동 섹터 (대장 등락·유지·후보 종목)"""
    E, V, hhmm = D["E"], D["V"], D["hhmm"]
    idx = {c: k for k, c in enumerate(D["codes"])}
    En = np.nan_to_num(E, nan=-99)
    act = {}
    for key, (gname, codes) in TG.items():
        ii = [idx[c] for c in codes if c in idx]
        if not (3 <= len(ii) <= 120):
            continue
        Eg, Vg = En[:, ii], V[:, ii]
        thr = 1e10 if len(ii) <= 30 else 3e10
        Em = np.where(Vg >= thr, Eg, -99)
        li = Em.argmax(1); lp = Em.max(1)
        ok = (lp >= 7) & ((Eg >= 3).sum(1) >= 3) & ((Eg >= 2).mean(1) >= 0.3)
        for i in np.where(ok)[0]:
            lead = ii[li[i]]
            act.setdefault(i, []).append((lp[i] + (Eg[i] >= 2).mean() * 20 + (Eg[i] >= 3).sum(), key, ii, lead, lp[i]))
    return act


def sig_follower(D, act, hold_min, band, cap_n=5):
    En = np.nan_to_num(D["E"], nan=-99); V = D["V"]
    sig, used_t, used_c = [], set(), set()
    for i in sorted(act):
        if not (WIN[0] <= D["hhmm"][i] < WIN[1]):
            continue
        for score, key, ii, lead, lp in sorted(act[i], key=lambda x: -x[0]):
            if len(used_t) >= cap_n:
                break
            if key in used_t or D["hold"][i, lead] < hold_min:
                continue
            cap = min(band[1], lp * 0.5)
            fol = [(V[i, k], k) for k in ii if k != lead and k not in used_c and band[0] <= En[i, k] <= cap
                   and V[i, k] >= 1e9 and D["above"][i, k] and D["acc"][i, k] >= 1.5]
            if not fol:
                continue
            fol.sort(reverse=True); used_t.add(key)
            for _, k in fol[:3]:
                used_c.add(k); sig.append((i, k))
    return sig


def detect(p, v, t):
    n = len(p)
    if n < 5:
        return None
    cur_p, prev_p = p[-1], p[-2]
    hi_i = int(np.argmax(p[:-1])); hi_p, hi_t = p[hi_i], t[hi_i]
    if hi_i >= n - 3:
        return None
    lo_i = hi_i + 1 + int(np.argmin(p[hi_i + 1:n - 1])); lo_p, lo_t = p[lo_i], t[lo_i]
    dd = (lo_p / hi_p - 1) * 100
    if not (-9 <= dd <= -2.5) or lo_t - hi_t < 15 or lo_i >= n - 2:
        return None
    rb_p = p[lo_i + 1:n - 1].max()
    if rb_p >= hi_p * 0.995 or rb_p < lo_p * 1.005:
        return None
    if not (prev_p <= rb_p * 1.002 and cur_p > rb_p * 1.003) or cur_p >= hi_p * 0.995:
        return None
    k = n - 2
    while k > 0 and t[-1] - t[k] < 5:
        k -= 1
    pm = lambda i, j: (v[j] - v[i]) / max(t[j] - t[i], 1)
    recent = pm(k, n - 1); corr = pm(hi_i, lo_i) if lo_i > hi_i else 1; rise = pm(0, hi_i) if hi_i > 0 else recent
    if recent < corr * 1.3 or recent < rise * 0.5:
        return None
    return hi_p, lo_p


def sig_pullback(D, act):
    """눌림돌파: 급등(+4%↑) → 조정 → 조정고점 돌파 · 거래대금 30억↑ · VWAP 위 · 손익비 1↑. 발동 섹터 대장이면 「대장눌림」"""
    P, V, tm, hhmm = D["P"], D["V"], D["tm"], D["hhmm"]
    leaders = {}
    for i, lst in act.items():
        for _, _, _, lead, _ in lst:
            leaders.setdefault(lead, set()).add(i)
    hi_all = np.nanmax(P, axis=0)
    cand = np.where((hi_all / D["prev"] - 1) * 100 >= 4)[0]
    sig = []
    for k in cand:
        path, vol = P[:, k], V[:, k]
        if np.isnan(path).any():
            continue
        pc = D["prev"][k]
        for i in range(5, len(tm)):
            if not (WIN[0] <= hhmm[i] < WIN[1]) or vol[i] < 3e9 or not D["above"][i, k]:
                continue
            if not (path[i] > path[i - 1]):
                continue
            r = detect(path[:i + 1], vol[:i + 1], tm[:i + 1])
            if not r:
                continue
            hi_p, lo_p = r
            if (hi_p / pc - 1) * 100 < 4:
                continue
            sl = lo_p * 0.995; risk = path[i] - sl
            if risk <= 0 or (hi_p - path[i]) / risk < 1.0:
                continue
            lead = any(abs(j - i) <= 1 for j in leaders.get(k, ()))
            sig.append((i, k, "대장눌림" if lead else "눌림돌파"))
            break
    return sig


def sig_burst(D, x):
    En = np.nan_to_num(D["E"], nan=-99); hhmm = D["hhmm"]
    ok = (D["burst"] >= x) & (np.nan_to_num(D["px2"], nan=0) >= 1.0) & (En >= 1) & (En <= 8) \
         & (D["V"] >= 2e9) & D["above"] & (hhmm >= max(920, WIN[0]))[:, None] & (hhmm < WIN[1])[:, None]
    sig, used, per_slot = [], set(), {}
    for i in range(len(hhmm)):
        for k in sorted(np.where(ok[i])[0], key=lambda k: -D["burst"][i, k]):
            s = slot_of(hhmm[i])
            if k in used or per_slot.get(s, 0) >= 2:
                continue
            used.add(k); per_slot[s] = per_slot.get(s, 0) + 1; sig.append((i, k))
    return sig


def outcomes(D, sig):
    """신호 → [(시간대, {청산: 수익%})]"""
    out = []
    for s in sig:
        i, k = s[0], s[1]
        p0 = D["P"][i, k]; path = D["P"][i + 1:, k]
        path = path[~np.isnan(path)]
        if np.isnan(p0) or len(path) < 3:          # 결과를 볼 시간이 아직 없음(오늘 막 뜬 신호)
            continue
        out.append({"s": slot_of(int(D["hhmm"][i])), "t": int(D["hhmm"][i]), "c": D["codes"][k],
                    "r": {n: round(exit_ret(path, p0, rule) - COST, 2) for n, rule in EXITS.items()},
                    **({"k": s[2]} if len(s) > 2 else {})})
    return out


FOL_GRID = [(h, b) for h in (0, 15, 30, 45) for b in ((1.5, 4.0), (2.5, 5.0), (3.0, 6.0))]
BURST_GRID = (4.0, 5.0, 7.0)


def day_result(d, TG):
    D = read_day(d)
    if D is None:
        return None
    act = theme_state(D, TG)
    res = {"v": VER, "d": d, "mins": int(len(D["tm"])), "from": int(D["hhmm"][0]), "to": int(D["hhmm"][-1]),
           "fol": {f"{h}|{b[0]}-{b[1]}": outcomes(D, sig_follower(D, act, h, b)) for h, b in FOL_GRID},
           "burst": {str(x): outcomes(D, sig_burst(D, x)) for x in BURST_GRID}}
    pb = outcomes(D, sig_pullback(D, act))
    res["대장눌림"] = [o for o in pb if o.get("k") == "대장눌림"]
    res["눌림돌파"] = [o for o in pb if o.get("k") == "눌림돌파"]
    return res


# ── 모으기 ──
def stat(rs):
    rs = [x for x in rs if x is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    a = np.array(rs); g, l = a[a > 0].sum(), -a[a < 0].sum()
    return {"n": n, "avg": round(float(a.mean()), 2), "win": round(float((a > 0).mean() * 100)), "pf": round(float(g / l), 2) if l > 0 else 9.9,
            "shr": round(float(a.sum() / (n + 10)), 3)}     # 표본이 적을수록 0 쪽으로 누른 평균 (비교용)


def live_stats():
    """실제 알람 장부(tracking.csv) — 근사 손익(안전손절 맞으면 손절, 1차 닿으면 1차, 아니면 종가) − 비용"""
    try:
        t = pd.read_csv("data/tracking.csv", dtype={"code": str})
    except Exception:
        return {}
    alias = {"눌림돌파": "돌파", "대장눌림": "눌림목"}
    out = {}

    def sim(x):
        try:
            sl = (float(x["손절"]) / x["알람가"] - 1) * 100; t1 = (float(x["목표1"]) / x["알람가"] - 1) * 100
            if x["최저"] <= sl: return sl
            if x["최고"] >= t1: return t1
        except Exception:
            pass
        return x["현재"]
    t["sim"] = [sim(x) - COST for _, x in t.iterrows()]
    t["slot"] = [slot_of(int(str(s)[9:13])) for s in t["시각"]]
    for kind, g in t.groupby("유형"):
        k = {v: k for k, v in alias.items()}.get(kind, kind)
        out[k] = {"all": stat(list(g["sim"])), **{s: stat(list(gg["sim"])) for s, gg in g.groupby("slot")}}
    return out


def learn(today_only_refresh=True, verbose=True):
    t0 = time.time()
    os.makedirs(CACHE, exist_ok=True)
    TG = groups()
    today = datetime.now(KST).strftime("%Y%m%d")
    days = sorted({os.path.basename(f)[:8] for f in glob.glob("data/days/*.csv.gz")} |
                  {os.path.basename(f)[:8] for f in glob.glob("data/snaps/*.csv")})
    R = []
    for d in days:
        cf = f"{CACHE}/{d}.json"
        r = None
        if os.path.exists(cf) and d != today:
            try:
                r = json.load(open(cf))
                if r.get("v") != VER:
                    r = None
            except Exception:
                r = None
        if r is None:
            r = day_result(d, TG)
            if r is None:
                if d != today:
                    json.dump({"v": VER, "d": d, "skip": "휴장·자료 부족"}, open(cf, "w"))
                continue
            if d != today:
                json.dump(r, open(cf, "w"), ensure_ascii=False, separators=(",", ":"))
        if r.get("skip"):
            continue
        R.append(r)
        if verbose:
            print(f"{d}: {r['from']}~{r['to']} {r['mins']}분 · 후발주 {len(r['fol'].get('30|2.5-5.0', []))} · "
                  f"눌림 {len(r['눌림돌파'])} · 대장 {len(r['대장눌림'])} · 폭발 {len(r['burst'].get('5.0', []))}")

    def pool(get):
        out = []
        for r in R:
            out += get(r)
        return out

    def by_exit(obs):
        return {e: stat([o["r"][e] for o in obs]) for e in EXITS}

    def by_slot(obs, e):
        return {s: stat([o["r"][e] for o in obs if o["s"] == s]) for s in SLOTS}

    # 1) 청산 규칙: 모든 유형 합쳐 가장 좋은 것 (표본 많아 덜 흔들림)
    cur = DEFAULT["exit"]
    allobs = pool(lambda r: r["fol"].get("30|2.5-5.0", []) + r["대장눌림"] + r["눌림돌파"] + r["burst"].get("5.0", []))
    ex = by_exit(allobs)
    best_e = max(ex, key=lambda e: ex[e].get("shr", -9))
    exit_pick = best_e if ex[best_e].get("n", 0) >= PMIN and ex[best_e]["shr"] >= ex[cur].get("shr", -9) + 0.15 else cur

    # 2) 후발주 손잡이
    fol = {k: stat([o["r"][exit_pick] for o in pool(lambda r, k=k: r["fol"].get(k, []))]) for k in [f"{h}|{b[0]}-{b[1]}" for h, b in FOL_GRID]}
    cur_f = f"{DEFAULT['fol_hold']}|{DEFAULT['fol_band'][0]}-{DEFAULT['fol_band'][1]}"
    best_f = max(fol, key=lambda k: fol[k].get("shr", -9))
    fol_pick = best_f if fol[best_f].get("n", 0) >= PMIN and fol[best_f]["shr"] >= fol[cur_f].get("shr", -9) + 0.15 else cur_f

    # 3) 거래폭발 배수
    bur = {x: stat([o["r"][exit_pick] for o in pool(lambda r, x=x: r["burst"].get(x, []))]) for x in map(str, BURST_GRID)}
    best_b = max(bur, key=lambda x: bur[x].get("shr", -9))
    burst_pick = best_b if bur[best_b].get("n", 0) >= PMIN and bur[best_b]["shr"] >= bur["5.0"].get("shr", -9) + 0.15 else "5.0"

    # 4) 유형 × 시간대: 다시 돌린 결과 + 실제 알람 장부를 합쳐 켬/관찰/끔
    replay = {
        "후발주": pool(lambda r: r["fol"].get(fol_pick, [])),
        "대장눌림": pool(lambda r: r["대장눌림"]),
        "눌림돌파": pool(lambda r: r["눌림돌파"]),
        "거래폭발": pool(lambda r: r["burst"].get(burst_pick, [])),
    }
    live = live_stats()
    types = {}
    open_late = False
    for kind, obs in replay.items():
        sl = by_slot(obs, exit_pick)
        lv = live.get(kind, {})
        rule = {}
        for s in SLOTS:
            a, b = sl[s], lv.get(s, {"n": 0})
            n = a.get("n", 0) + b.get("n", 0)
            tot = a.get("avg", 0) * a.get("n", 0) + b.get("avg", 0) * b.get("n", 0)
            avg = tot / n if n else 0
            shr = tot / (n + 10) if n else 0
            if (n >= 15 and avg < -0.3) or (n >= 10 and avg < -1.0):
                st = "관찰"                       # 앱 푸시 안 함 · 텔레그램은 👀 기록 · 추적 계속
            elif s == "13시 이후" and not (n >= 15 and shr > 0.1):
                st = "닫힘"                       # 오후는 성적이 증명돼야 연다
            elif n >= 15 and shr >= 0.25:
                st = "강함"                       # ⭐ 앞으로 · 상한 +1
            else:
                st = "켬"
            if s == "13시 이후" and st in ("켬", "강함"):
                open_late = True
            rule[s] = {"st": st, "n": n, "avg": round(avg, 2), "replay": a, "live": b}
        ra, la = stat([o["r"][exit_pick] for o in obs]), lv.get("all", {"n": 0})
        n_all = ra.get("n", 0) + la.get("n", 0)
        avg_all = (ra.get("avg", 0) * ra.get("n", 0) + la.get("avg", 0) * la.get("n", 0)) / n_all if n_all else 0
        if n_all >= 20 and avg_all < -0.5:            # 유형 전체가 확실히 지는 중 → 모든 시간대 관찰
            for s in SLOTS:
                if rule[s]["st"] != "닫힘":
                    rule[s]["st"] = "관찰"
        types[kind] = {"slots": rule, "all": ra, "live_all": la, "n_all": n_all, "avg_all": round(avg_all, 2)}

    h, b = fol_pick.split("|")
    b0, b1 = map(float, b.split("-"))
    out = {
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "v": VER,
        "days": [r["d"] for r in R], "n_days": len(R), "cost": COST,
        "params": {"fol_hold": int(h), "fol_band": [b0, b1], "burst_x": float(burst_pick), "exit": exit_pick,
                   "exit_rule": list(EXITS[exit_pick]), "cutoff": 1450 if open_late else 1130},
        "changed": [x for x, c in [("청산 " + exit_pick, exit_pick != cur), (f"후발주 유지 {h}분·밴드 {b0}~{b1}%", fol_pick != cur_f),
                                   (f"거래폭발 ×{burst_pick}", burst_pick != "5.0"), ("오후 신규 알람 열림", open_late)] if c],
        "types": types,
        "grid": {"exit": ex, "fol": fol, "burst": bur},
        "note": f"쌓인 1분 자료 {len(R)}일을 하루 전체로 다시 돌린 결과 + 실제 알람 {sum(v.get('all', {}).get('n', 0) for v in live.values())}건. "
                f"비용 {COST}% 뺀 수익. 15건 미만은 바꾸지 않고, 지금 설정보다 확실히 나을 때만 바꿈.",
        "secs": round(time.time() - t0, 1),
    }
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
    if verbose:
        print(f"→ {OUT} · {out['secs']}초 · 바뀜: {out['changed'] or '없음'}")
        for k, v in types.items():
            print(f"  {k}: " + " · ".join(f"{s} {r['st']}(n{r['n']} {r['avg']:+.2f})" for s, r in v["slots"].items()))
    return out


if __name__ == "__main__":
    learn()
