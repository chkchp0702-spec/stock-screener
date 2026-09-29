"""
테마 매핑 빌더 - 네이버 금융 테마/업종 → 종목 테이블 생성
결과: data/themes.csv  (kind, no, group, code, name)
universe.yml 에서 build_universe.py 다음에 실행.
"""
import requests, pandas as pd, re, os, time, sys

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Referer": "https://finance.naver.com/sise/"}
BASE = "https://finance.naver.com"

def html(url):
    for _ in range(3):
        try:
            r = requests.get(url, headers=H, timeout=15)
            if r.status_code == 200:
                return r.content.decode("cp949", "ignore")
            if r.status_code in (403, 429):
                print("차단?", r.status_code, url); return None
        except Exception as e:
            print("재시도", e)
        time.sleep(1.5)
    return None

RE_GROUP = re.compile(r'sise_group_detail\.naver\?type=(theme|upjong)&(?:amp;)?no=(\d+)"[^>]*>\s*([^<]+?)\s*</a>')
RE_STOCK = re.compile(r'/item/main\.naver\?code=(\d{6})"[^>]*>\s*([^<]+?)\s*</a>')

def group_list(kind):
    seen, out = set(), []
    if kind == "theme":
        for page in range(1, 12):
            t = html(f"{BASE}/sise/theme.naver?&page={page}")
            if not t: break
            found = [(k, n, nm) for k, n, nm in RE_GROUP.findall(t) if k == "theme" and n not in seen]
            if not found: break
            for k, n, nm in found:
                seen.add(n); out.append((n, nm.strip()))
            time.sleep(0.4)
    else:
        t = html(f"{BASE}/sise/sise_group.naver?type=upjong")
        if t:
            for k, n, nm in RE_GROUP.findall(t):
                if k == "upjong" and n not in seen:
                    seen.add(n); out.append((n, nm.strip()))
    print(kind, "그룹", len(out))
    return out

def members(kind, no):
    t = html(f"{BASE}/sise/sise_group_detail.naver?type={kind}&no={no}")
    if not t: return []
    seen, out = set(), []
    for code, name in RE_STOCK.findall(t):
        if code not in seen:
            seen.add(code); out.append((code, name.strip().rstrip("*").strip()))
    return out

rows = []
for kind in ("theme", "upjong"):
    for no, gname in group_list(kind):
        m = members(kind, no)
        for code, name in m:
            rows.append({"kind": kind, "no": no, "group": gname, "code": code, "name": name})
        time.sleep(0.35)

if not rows:
    print("테마 수집 실패 - 기존 파일 유지"); sys.exit(0)

df = pd.DataFrame(rows).drop_duplicates(subset=["kind", "no", "code"])
df = df[df["code"].str[-1] == "0"]                       # 우선주 제외
os.makedirs("data", exist_ok=True)
df.to_csv("data/themes.csv", index=False, encoding="utf-8-sig")
print("저장", len(df), "행 ·", df.groupby("kind")["no"].nunique().to_dict())
print(df[df.kind == "theme"].groupby("group").size().sort_values(ascending=False).head(10))
