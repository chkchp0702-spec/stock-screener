"""
모닝 섹터 브리핑 → 텔레그램
- 간밤 글로벌(미국) 섹터 흐름 + 지난 1주 흐름
- 한국 테마 전일 흐름 + NXT 프리마켓 흐름
- 규칙 기반 점수로 '오늘 볼 섹터'와 섹터 내 관심 종목 선정
평일 아침 GitHub Actions에서 실행됨. 설정은 briefing.yaml.

환경변수
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID : 텔레그램 발송 (없으면 출력만)
  FORCE=1   : 휴장일·중복발송·발송시각 체크 무시 (수동 실행)
  NO_WAIT=1 : 08:20까지 대기하지 않음
  DRY_RUN=1 : 텔레그램 발송 안 함
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "briefing_data" / "last_sent.txt"
ARCHIVE_DIR = ROOT / "briefing_data" / "daily"
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}


def log(*a):
    print(f"[{datetime.now(KST):%H:%M:%S}]", *a, flush=True)


def env_on(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")


# ─────────────────────────── 날짜·발송 조건 ───────────────────────────
def is_krx_holiday(d) -> bool:
    if d.weekday() >= 5:
        return True
    if d.month == 12 and d.day == 31:  # 연말 휴장
        return True
    try:
        import holidays
        return d in holidays.country_holidays("KR", years=d.year)
    except Exception as e:  # 패키지 문제 시 평일로 간주
        log("휴일 확인 실패:", e)
        return False


def hhmm_to_dt(day, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=KST)


def already_sent(today) -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text().strip() == today.isoformat()


def mark_sent(today):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(today.isoformat())


# ─────────────────────────── 데이터 수집 ───────────────────────────
def download(tickers: list[str], period="6mo") -> dict[str, pd.DataFrame]:
    """yfinance 일봉. {ticker: DataFrame(Open,High,Low,Close,Volume)}"""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 40):
        chunk = tickers[i:i + 40]
        for attempt in range(3):
            try:
                raw = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
                break
            except Exception as e:
                log("다운로드 재시도", attempt + 1, e)
                time.sleep(5)
        else:
            continue
        for t in chunk:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
                if len(df) >= 2:
                    out[t] = df
            except KeyError:
                pass
    missing = [t for t in tickers if t not in out]
    if missing:
        log("데이터 없음:", ", ".join(missing))
    return out


def drop_today_kr(df: pd.DataFrame, today) -> pd.DataFrame:
    """장 시작 전 yfinance가 오늘 날짜 빈 봉을 붙이는 경우 제거"""
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(KST).tz_localize(None)
    return df[idx.date < today]


def fetch_nxt(codes: list[str]) -> dict[str, dict]:
    """네이버 실시간 시세의 NXT(넥스트레이드) 프리/애프터마켓 정보.
    반환 {코드6자리: {"chg": %, "price": 가격, "session": PRE_MARKET/AFTER_MARKET, "open": bool}}"""
    out = {}

    def num(s):
        try:
            return float(str(s).replace(",", "").replace("+", ""))
        except Exception:
            return None

    def parse(item):
        info = item.get("overMarketPriceInfo") or {}
        if not info:
            return None
        chg = num(info.get("fluctuationsRatio"))
        code = str((info.get("compareToPreviousPrice") or {}).get("code", ""))
        if chg is not None and code in ("4", "5") and chg > 0:  # 하락/하한인데 부호 없을 때
            chg = -chg
        return {
            "chg": chg,
            "price": num(info.get("overPrice")),
            "session": info.get("tradingSessionType", ""),
            "open": info.get("overMarketStatus") == "OPEN",
            "volume": num(info.get("accumulatedTradingVolume")),
        }

    for i in range(0, len(codes), 20):
        chunk = codes[i:i + 20]
        url = "https://polling.finance.naver.com/api/realtime/domestic/stock/" + ",".join(chunk)
        try:
            r = requests.get(url, headers=UA, timeout=10)
            datas = r.json().get("datas", [])
        except Exception as e:
            log("NXT 조회 실패(묶음):", e)
            datas = []
        if not datas and len(chunk) > 1:  # 묶음 조회 실패 시 개별 조회
            for c in chunk:
                try:
                    r = requests.get(url.rsplit("/", 1)[0] + "/" + c, headers=UA, timeout=10)
                    datas += r.json().get("datas", [])
                except Exception:
                    pass
        for item in datas:
            p = parse(item)
            if p and p["chg"] is not None:
                out[str(item.get("itemCode", ""))] = p
    log(f"NXT 시세 {len(out)}/{len(codes)}종목")
    return out


# ─────────────────────────── 지표 계산 ───────────────────────────
def ret(df: pd.DataFrame, n: int) -> float | None:
    c = df["Close"]
    if len(c) <= n:
        return None
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100


def stock_stats(df: pd.DataFrame) -> dict:
    c, v = df["Close"], df["Volume"]
    s = {"close": float(c.iloc[-1]), "r1": ret(df, 1), "r5": ret(df, 5), "r20": ret(df, 20),
         "date": pd.to_datetime(df.index[-1]).date()}
    vol20 = v.iloc[-21:-1].mean() if len(v) > 21 else np.nan
    s["vol_ratio"] = float(v.iloc[-1] / vol20) if vol20 and vol20 > 0 else None
    s["high20"] = len(c) > 20 and c.iloc[-1] >= c.iloc[-20:].max()
    s["high60"] = len(c) > 60 and c.iloc[-1] >= c.iloc[-60:].max()
    if len(c) >= 60:
        ma5, ma20, ma60 = c.rolling(5).mean().iloc[-1], c.rolling(20).mean().iloc[-1], c.rolling(60).mean().iloc[-1]
        s["aligned"] = c.iloc[-1] > ma5 > ma20 > ma60
        s["above20"] = c.iloc[-1] > ma20
    else:
        s["aligned"] = s["above20"] = False
    return s


def zscores(values: dict[str, float | None]) -> dict[str, float | None]:
    vals = {k: v for k, v in values.items() if v is not None and np.isfinite(v)}
    if len(vals) < 3:
        return {k: None for k in values}
    arr = np.array(list(vals.values()))
    sd = arr.std() or 1.0
    z = {k: float(np.clip((v - arr.mean()) / sd, -3, 3)) for k, v in vals.items()}
    return {k: z.get(k) for k in values}


def mean_or_none(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    return float(np.mean(xs)) if xs else None


def fmt_pct(x, digits=1, sign=True):
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def arrow(x):
    if x is None:
        return "⚪"
    return "🔺" if x > 0 else ("🔻" if x < 0 else "⚪")


# ─────────────────────────── 분석 ───────────────────────────
def analyze(cfg: dict, prices: dict, nxt: dict, today) -> dict:
    sc = cfg["scoring"]

    # 글로벌 섹터
    glob = []
    for t, name in cfg["global_sectors"].items():
        df = prices.get(t)
        if df is None:
            continue
        glob.append({"ticker": t, "name": name, "r1": ret(df, 1), "r5": ret(df, 5),
                     "r20": ret(df, 20), "date": pd.to_datetime(df.index[-1]).date()})

    # 매크로
    macro = []
    for m in cfg["macro"]:
        df = prices.get(m["ticker"])
        if df is None:
            continue
        macro.append({**m, "last": float(df["Close"].iloc[-1]), "r1": ret(df, 1), "r5": ret(df, 5)})

    # 테마
    themes = {}
    for name, th in cfg["themes"].items():
        us = [prices[t] for t in th.get("us", []) if t in prices]
        stocks = []
        for tk, sname in th["stocks"].items():
            df = prices.get(tk)
            if df is None:
                continue
            df = drop_today_kr(df, today)
            if len(df) < 6:
                continue
            st = stock_stats(df)
            code = tk.split(".")[0]
            n = nxt.get(code)
            st.update({"ticker": tk, "code": code, "name": sname,
                       "nxt": n["chg"] if n else None, "nxt_session": n["session"] if n else None})
            stocks.append(st)
        themes[name] = {
            "name": name,
            "us_names": [t for t in th.get("us", []) if t in prices],
            "us_1d": mean_or_none([ret(d, 1) for d in us]),
            "us_5d": mean_or_none([ret(d, 5) for d in us]),
            "kr_1d": mean_or_none([s["r1"] for s in stocks]),
            "kr_5d": mean_or_none([s["r5"] for s in stocks]),
            "kr_vol": mean_or_none([s["vol_ratio"] for s in stocks]),
            "nxt": mean_or_none([s["nxt"] for s in stocks if s["nxt_session"] == "PRE_MARKET"]),
            "nxt_after": mean_or_none([s["nxt"] for s in stocks if s["nxt_session"] == "AFTER_MARKET"]),
            "stocks": stocks,
        }

    # 섹터 점수 (항목별 z점수 가중합, 없는 항목은 제외 후 재분배)
    w = sc["weights"]
    zs = {k: zscores({n: t[k] for n, t in themes.items()}) for k in w}
    for n, t in themes.items():
        num = den = 0.0
        for k, wk in w.items():
            z = zs[k][n]
            if z is not None:
                num += wk * z
                den += wk
        t["score"] = num / den if den else None
        t["z"] = {k: zs[k][n] for k in w}

    ranked = sorted([t for t in themes.values() if t["score"] is not None], key=lambda t: -t["score"])

    # 종목 점수: 전체 종목 기준 z점수 → 섹터 안에서 순위
    allst = [s for t in themes.values() for s in t["stocks"]]
    comp = {
        "nxt": (0.35, zscores({s["code"]: (s["nxt"] if s["nxt_session"] == "PRE_MARKET" else None) for s in allst})),
        "r1": (0.20, zscores({s["code"]: s["r1"] for s in allst})),
        "r5": (0.20, zscores({s["code"]: s["r5"] for s in allst})),
        "vol": (0.25, zscores({s["code"]: (np.log(s["vol_ratio"]) if s["vol_ratio"] else None) for s in allst})),
    }
    for s in allst:
        num = den = 0.0
        for wk, zz in comp.values():
            if zz.get(s["code"]) is not None:
                num += wk * zz[s["code"]]
                den += wk
        s["score"] = num / den if den else 0.0
    for t in ranked:
        t["picks"] = sorted(t["stocks"], key=lambda s: -s["score"])[: sc["picks_per_sector"]]

    kr_dates = [s["date"] for t in themes.values() for s in t["stocks"]]
    us_dates = [g["date"] for g in glob]
    return {
        "glob": glob, "macro": macro, "themes": themes, "ranked": ranked,
        "kr_date": max(kr_dates) if kr_dates else None,
        "us_date": max(us_dates) if us_dates else None,
        "nxt_count": sum(1 for t in themes.values() for s in t["stocks"] if s["nxt_session"] == "PRE_MARKET"),
    }


# ─────────────────────────── 문장 생성 (규칙 기반) ───────────────────────────
def theme_reason(t: dict) -> str:
    bits = []
    if t["us_1d"] is not None and abs(t["us_1d"]) >= 0.8:
        us = "/".join(x.lstrip("^") for x in t["us_names"][:2])
        bits.append(f"간밤 미국 {us} {fmt_pct(t['us_1d'])} {'강세' if t['us_1d'] > 0 else '약세'} 연동")
    if t["nxt"] is not None and abs(t["nxt"]) >= 0.5:
        bits.append(f"NXT 프리마켓 평균 {fmt_pct(t['nxt'])}로 {'갭상승' if t['nxt'] > 0 else '갭하락'} 출발 예상됨")
    if t["kr_vol"] is not None and t["kr_vol"] >= 1.5:
        bits.append(f"전일 거래량 평소의 {t['kr_vol']:.1f}배로 수급 유입됨")
    if t["kr_5d"] is not None and abs(t["kr_5d"]) >= 3:
        bits.append(f"1주 {fmt_pct(t['kr_5d'])} {'추세 진행 중' if t['kr_5d'] > 0 else '조정 구간'}")
    elif t["kr_1d"] is not None and abs(t["kr_1d"]) >= 1.5:
        bits.append(f"전일 {fmt_pct(t['kr_1d'])} {'강세' if t['kr_1d'] > 0 else '약세'}")
    if not bits:
        bits.append("항목별 고르게 상대 우위")
    return ". ".join(bits) + "."


def stock_tags(s: dict, vol_surge: float) -> str:
    tags = []
    if s["nxt"] is not None and s["nxt_session"] == "PRE_MARKET":
        tags.append(f"NXT {fmt_pct(s['nxt'])}")
    tags.append(f"전일 {fmt_pct(s['r1'])}")
    if s["vol_ratio"] is not None and s["vol_ratio"] >= vol_surge:
        tags.append(f"거래량 {s['vol_ratio']:.1f}배")
    if s["high60"]:
        tags.append("60일 신고가")
    elif s["high20"]:
        tags.append("20일 신고가")
    if s["aligned"]:
        tags.append("정배열")
    elif not s["above20"]:
        tags.append("20일선 아래")
    return " · ".join(tags)


def build_message(cfg: dict, a: dict, now: datetime, weekly: bool) -> str:
    sc = cfg["scoring"]
    wd = "월화수목금토일"[now.weekday()]
    e = html.escape
    L = []
    L.append(f"📊 <b>모닝 섹터 브리핑</b>  {now:%m/%d}({wd}) {now:%H:%M}")
    L.append("")

    # 1) 간밤 글로벌
    L.append(f"<b>🌍 간밤 글로벌</b> <i>({a['us_date']:%m/%d} 미국 종가)</i>" if a["us_date"] else "<b>🌍 간밤 글로벌</b>")
    idx = [m for m in a["macro"] if not m.get("level")]
    lvl = [m for m in a["macro"] if m.get("level")]
    L.append(" · ".join(f"{e(m['name'])} {fmt_pct(m['r1'])}" for m in idx))
    lv = []
    for m in lvl:
        v = m["last"]
        vs = f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"
        lv.append(f"{e(m['name'])} {vs}{m.get('unit', '')}({fmt_pct(m['r1'])})")
    L.append(" · ".join(lv))
    L.append("")

    # 2) 미국 섹터 전일
    g = sorted([x for x in a["glob"] if x["r1"] is not None], key=lambda x: -x["r1"])
    if g:
        L.append("<b>🇺🇸 미국 섹터 (전일)</b>")
        L.append("강세 " + ", ".join(f"{e(x['name'])} {fmt_pct(x['r1'])}" for x in g[:4]))
        L.append("약세 " + ", ".join(f"{e(x['name'])} {fmt_pct(x['r1'])}" for x in g[-3:][::-1]))
        L.append("")

    # 3) 한국 테마 전일 + NXT
    th = sorted([t for t in a["themes"].values() if t["kr_1d"] is not None], key=lambda t: -t["kr_1d"])
    if th:
        d = f" <i>({a['kr_date']:%m/%d})</i>" if a["kr_date"] else ""
        L.append(f"<b>🇰🇷 한국 테마 (전일)</b>{d}")
        L.append("강세 " + ", ".join(f"{e(t['name'])} {fmt_pct(t['kr_1d'])}" for t in th[:4]))
        L.append("약세 " + ", ".join(f"{e(t['name'])} {fmt_pct(t['kr_1d'])}" for t in th[-3:][::-1]))
        nx = sorted([t for t in a["themes"].values() if t["nxt"] is not None], key=lambda t: -t["nxt"])
        if nx:
            L.append("NXT 프리 " + ", ".join(f"{e(t['name'])} {fmt_pct(t['nxt'])}" for t in nx[:3])
                     + " / " + ", ".join(f"{e(t['name'])} {fmt_pct(t['nxt'])}" for t in nx[-2:][::-1]))
        L.append("")

    # 4) 지난 1주
    n = 5 if weekly else 3
    g5 = sorted([x for x in a["glob"] if x["r5"] is not None], key=lambda x: -x["r5"])
    k5 = sorted([t for t in a["themes"].values() if t["kr_5d"] is not None], key=lambda t: -t["kr_5d"])
    if g5 or k5:
        L.append("<b>📅 지난 1주 흐름</b>" + (" — 주간 정리" if weekly else ""))
        if g5:
            L.append("🇺🇸 상위 " + ", ".join(f"{e(x['name'])} {fmt_pct(x['r5'])}" for x in g5[:n]))
            L.append("🇺🇸 하위 " + ", ".join(f"{e(x['name'])} {fmt_pct(x['r5'])}" for x in g5[-n:][::-1]))
        if k5:
            L.append("🇰🇷 상위 " + ", ".join(f"{e(t['name'])} {fmt_pct(t['kr_5d'])}" for t in k5[:n]))
            L.append("🇰🇷 하위 " + ", ".join(f"{e(t['name'])} {fmt_pct(t['kr_5d'])}" for t in k5[-n:][::-1]))
        if weekly:
            both = [t["name"] for t in a["themes"].values()
                    if (t["z"].get("us_5d") or 0) > 0.5 and (t["z"].get("kr_5d") or 0) > 0.5]
            split = [t["name"] for t in a["themes"].values()
                     if (t["z"].get("us_5d") or 0) > 0.5 and (t["z"].get("kr_5d") or 0) < -0.5]
            if both:
                L.append(f"→ 한·미 동반 강세: {e(', '.join(both))}. 글로벌 주도 흐름으로 해석됨")
            if split:
                L.append(f"→ 미국만 강세: {e(', '.join(split))}. 한국 후행 반영 여부 관찰 필요함")
        L.append("")

    # 5) 오늘 볼 섹터
    L.append("━━━━━━━━━━━━━━")
    L.append("<b>🎯 오늘 이 섹터를 보자</b>")
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    for i, t in enumerate(a["ranked"][: sc["top_sectors"]]):
        L.append("")
        L.append(f"{nums[i]} <b>{e(t['name'])}</b>  <i>점수 {t['score']:+.2f}</i>")
        L.append(f"└ {e(theme_reason(t))}")
        for s in t["picks"]:
            L.append(f"   • <b>{e(s['name'])}</b> {e(stock_tags(s, sc['volume_surge']))}")

    # 6) 피할 섹터
    bottom = a["ranked"][-sc["avoid_sectors"]:][::-1] if len(a["ranked"]) > sc["top_sectors"] else []
    if bottom:
        L.append("")
        L.append("<b>⚠️ 오늘은 피하자</b>")
        for t in bottom:
            L.append(f"• {e(t['name'])} <i>({t['score']:+.2f})</i> — {e(theme_reason(t))}")

    L.append("")
    nxt_note = f"NXT 프리마켓 {a['nxt_count']}종목 반영" if a["nxt_count"] else "NXT 프리마켓 데이터 없음(장전 시세 미반영)"
    L.append(f"<i>{nxt_note} · 규칙 기반 자동 분석 · 투자 판단 참고용</i>")
    return "\n".join(L)


# ─────────────────────────── 텔레그램 ───────────────────────────
def send_telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("텔레그램 Secrets 없음 → 발송 생략")
        return False
    # 4096자 제한: 줄 단위로 분할
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    parts.append(cur)
    ok = True
    for p in parts:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": p, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=20)
        if not r.ok:
            log("텔레그램 오류:", r.status_code, r.text[:300])
            ok = False
    return ok


def send_error(msg: str):
    try:
        send_telegram(f"⚠️ 모닝 브리핑 생성 실패\n<code>{html.escape(msg[:800])}</code>\nActions 로그 확인 필요함")
    except Exception:
        pass


# ─────────────────────────── 메인 ───────────────────────────
def main():
    cfg = yaml.safe_load((ROOT / "briefing.yaml").read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    force = env_on("FORCE")

    if not force:
        if is_krx_holiday(today):
            log("오늘은 한국 휴장일 → 종료")
            return
        if already_sent(today):
            log("오늘 이미 발송함 → 종료")
            return
        if now > hhmm_to_dt(today, cfg["schedule"]["late_cutoff_kst"]):
            log("발송 마감 시각 지남 → 종료")
            return

    # 1) 일봉 데이터 (먼저 받아두고 NXT 시각까지 대기)
    tickers = [m["ticker"] for m in cfg["macro"]] + list(cfg["global_sectors"])
    for th in cfg["themes"].values():
        tickers += th.get("us", []) + list(th["stocks"])
    prices = download(tickers)
    if len(prices) < len(set(tickers)) * 0.5:
        raise RuntimeError(f"시세 데이터 부족: {len(prices)}/{len(set(tickers))}")

    # 2) NXT 프리마켓 데이터가 쌓일 때까지 대기
    target = hhmm_to_dt(today, cfg["schedule"]["send_after_kst"])
    wait = (target - datetime.now(KST)).total_seconds()
    if wait > 0 and not env_on("NO_WAIT"):
        log(f"{target:%H:%M}까지 {wait / 60:.0f}분 대기")
        time.sleep(wait)

    codes = sorted({tk.split(".")[0] for th in cfg["themes"].values() for tk in th["stocks"]})
    nxt = fetch_nxt(codes)

    # 3) 분석 + 메시지
    now = datetime.now(KST)
    weekly = now.weekday() == 0 or env_on("WEEKLY")  # 월요일은 주간 정리 확장
    a = analyze(cfg, prices, nxt, today)
    msg = build_message(cfg, a, now, weekly)
    print("\n" + msg + "\n")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (ARCHIVE_DIR / f"{today.isoformat()}.txt").write_text(msg, encoding="utf-8")
    (ARCHIVE_DIR / "latest.json").write_text(json.dumps({
        "date": today.isoformat(),
        "top": [{"sector": t["name"], "score": round(t["score"], 3),
                 "picks": [s["name"] for s in t["picks"]]} for t in a["ranked"][:cfg["scoring"]["top_sectors"]]],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if env_on("DRY_RUN"):
        log("DRY_RUN → 발송 생략")
        return
    if send_telegram(msg):
        mark_sent(today)
        log("발송 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        import traceback
        traceback.print_exc()
        if not env_on("DRY_RUN"):
            send_error(f"{type(ex).__name__}: {ex}")
        sys.exit(1)
