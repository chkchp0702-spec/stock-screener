"""
테마 매핑 빌더 - 네이버 증권 새 JSON API
결과: data/themes.csv  (kind, no, group, code, name)
"""
import requests, pandas as pd, os, time, sys, json

BASE = "https://stock.naver.com"
H = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     "Referer": "https://stock.naver.com/", "Accept": "application/json"}
DEBUG = {"n": 0}

def get(path):
    for _ in range(3):
        try:
            r = requests.get(BASE + path, headers=H, timeout=15)
            if r.status_code == 200:
                try: return r.json()
                except Exception:
                    print("JSON 아님", path, r.text[:150]); return None
            print("HTTP", r.status_code, path, r.text[:150])
            if r.status_code in (403, 404, 429): return None
        except Exception as e:
            print("재시도", e)
        time.sleep(1.5)
    return None

def as_list(j):
    if isinstance(j, list): return j
    if isinstance(j, dict):
        for k in ("content", "stocks", "items", "result", "list", "datas"):
            v = j.get(k)
            if isinstance(v, list): return v
            if isinstance(v, dict):
                inner = as_list(v)
                if inner: return inner
    return []

def pick(d, keys):
    for k in keys:
        if d.get(k) not in (None, ""): return str(d[k]).strip()
    return None

def paged(path_fmt, size):
    """startIdx 가 페이지번호/오프셋 어느 쪽이든 새 항목이 없을 때까지 수집"""
    out, seen = [], set()
    for idx in range(0, 30):
        items = as_list(get(path_fmt.format(idx=idx, size=size)))
        new = 0
        for it in items:
            key = json.dumps(it, sort_keys=True, ensure_ascii=False)[:200]
            if key in seen: continue
            seen.add(key); out.append(it); new += 1
        if DEBUG["n"] < 2 and items:
            print("  샘플 키:", list(items[0].keys())[:15]); DEBUG["n"] += 1
        if new == 0 or len(items) < size: break
        time.sleep(0.3)
    return out

rows = []
for kind in ("theme", "upjong"):
    groups = paged(f"/api/domestic/market/{kind}/list?startIdx={{idx}}&pageSize={{size}}&sortType=changeRate", 100)
    gl = []
    for g in groups:
        no = pick(g, ("no", "themeNo", "upjongNo", "groupNo", "code"))
        nm = pick(g, ("name", "themeName", "upjongName", "groupName"))
        if no and nm and (no, nm) not in gl: gl.append((no, nm))
    print(kind, "그룹", len(gl))
    DEBUG["n"] = 0
    for no, gname in gl:
        mem = paged(f"/api/domestic/market/{kind}/{no}/stocklist?marketType=ALL&orderType=quantTop"
                    f"&startIdx={{idx}}&pageSize={{size}}", 100)
        for s in mem:
            code = pick(s, ("itemCode", "itemcode", "code", "stockCode", "reutersCode"))
            name = pick(s, ("stockName", "itemName", "itemname", "name"))
            if code and len(code) == 6:
                rows.append({"kind": kind, "no": no, "group": gname, "code": code.upper(), "name": name})
        time.sleep(0.25)

if not rows:
    print("테마 수집 실패 - 기존 파일 유지"); sys.exit(0)

df = pd.DataFrame(rows).drop_duplicates(subset=["kind", "no", "code"])
df = df[df["code"].str[-1] == "0"]
os.makedirs("data", exist_ok=True)
df.to_csv("data/themes.csv", index=False, encoding="utf-8-sig")
print("저장", len(df), "행 ·", df.groupby("kind")["no"].nunique().to_dict())
print(df[df.kind == "theme"].groupby("group").size().sort_values(ascending=False).head(10))
