import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

tests = [
    ("메인", "https://www.nextrade.co.kr/"),
    ("시세1", "https://www.nextrade.co.kr/brdinfoTime/brdinfoTimeList.do"),
    ("시세2", "https://www.nextrade.co.kr/menu/mrktinfo/MRKT_0001/"),
    ("API", "https://api.nextrade.co.kr/v1/quotes"),
]

for name, url in tests:
    try:
        r = requests.get(url, headers=H, timeout=15)
        print(f"=== {name} === status {r.status_code} len {len(r.text)}")
        print(r.text[:300].replace("\n", " "))
    except Exception as e:
        print(f"=== {name} === 실패 {repr(e)[:120]}")
