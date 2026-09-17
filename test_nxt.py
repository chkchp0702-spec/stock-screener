import os, requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*",
     "Referer": "https://www.nextrade.co.kr/"}

U = "https://www.nextrade.co.kr/brdinfoTime/brdinfoTimeList.do"

r = requests.get(U, headers=H, params={"pageUnit": 1000, "pageSize": 1000}, timeout=20)
lst = r.json().get("brdinfoTimeList", [])

live = [x for x in lst if x.get("curPrc")]
mv = [x for x in live if abs(float(x.get("upDownRate") or 0)) > 0.01]

s = lst[0] if lst else {}
msg = [f"NXT 테스트 {s.get('nowDd','')} {s.get('nowTime','')}",
       f"creTime={s.get('creTime','')}",
       f"전체 {len(lst)} / 가격있음 {len(live)} / 등락 {len(mv)}", ""]

top = sorted(live, key=lambda x: x.get("accTrval") or 0, reverse=True)[:10]
msg.append("[거래대금 상위 10]")
for x in top:
    msg.append(f"{x['isuAbwdNm']} {x['curPrc']:,} ({x['upDownRate']:+}%) "
               f"{(x.get('accTrval') or 0)//100000000}억")

up = sorted(mv, key=lambda x: float(x.get("upDownRate") or 0), reverse=True)[:10]
msg.append("")
msg.append("[등락률 상위 10]")
for x in up:
    msg.append(f"{x['isuAbwdNm']} {x['curPrc']:,} ({x['upDownRate']:+}%) "
               f"{(x.get('accTrval') or 0)//100000000}억")

text = "\n".join(msg)
print(text)

t, c = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
if t and c:
    requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                  data={"chat_id": c, "text": text}, timeout=15)
