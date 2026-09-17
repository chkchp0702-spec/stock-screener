import requests, json

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*",
     "Referer": "https://www.nextrade.co.kr/"}

U = "https://www.nextrade.co.kr/brdinfoTime/brdinfoTimeList.do"

for label, kw in [("GET", {}),
                  ("GET big", {"params": {"pageUnit": 1000, "pageSize": 1000}}),
                  ("POST", {"data": {"pageUnit": 1000, "pageSize": 1000}})]:
    try:
        m = requests.post if label == "POST" else requests.get
        r = m(U, headers=H, timeout=20, **kw)
        print(f"\n===== {label} status {r.status_code} len {len(r.text)} =====")
        j = r.json()
        print("KEYS:", list(j.keys()))
        lst = j.get("brdinfoTimeList", [])
        print("COUNT:", len(lst))
        if lst:
            print("FIELDS:", list(lst[0].keys()))
            print("REC0:", json.dumps(lst[0], ensure_ascii=False))
            print("REC1:", json.dumps(lst[1], ensure_ascii=False) if len(lst) > 1 else "-")
    except Exception as e:
        print(f"\n===== {label} 실패 {repr(e)[:200]}")
