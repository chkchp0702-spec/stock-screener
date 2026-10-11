"""🔁 저장소 올리기 도우미 (10/11)

  · push_main()  : main 에 올리기 — 「git pull --rebase → git push」를 최대 5번(5·10·20·40초 쉬며) 다시 해 봄.
                   그래도 안 되면 단타 알림(ntfy chkchp-ch-danta + 텔레그램)으로 알리고 False. (-X theirs 같은 덮어쓰기는 안 씀)
  · days_pull()  : 하루 압축본(data/days/*.csv.gz)을 `days` 브랜치에서 받아 옴 (learn.py·backtest.py·오후 세션이 씀)
  · days_push()  : data/days 를 `days` 브랜치에 커밋 하나로 덮어써 올림 (force) → main 기록이 하루 10~15MB씩 불어나지 않음.
                   올리기 전에 원격 것을 한 번 더 받아 합치므로(없는 날만 채움) 다른 날 자료가 사라지지 않음. 60일 지난 것은 뺌.

명령줄: python gitsync.py push-main | days-pull | days-push   (실패하면 종료 코드 1 → 워크플로 빨간불)
"""
from __future__ import annotations
import glob, json, os, subprocess, sys, tarfile, io, tempfile, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
DAYS_DIR = "data/days"
DAYS_BRANCH = "days"
DAY_KEEP = 60                       # screener.py DAY_KEEP 와 같음
TRIES = 5
ALERT_TOPIC = "chkchp-ch-danta"     # 이미 쓰는 단타 알림 주제 (새 주제 안 만듦)


def run(*a, check=False, env=None, inp=None):
    r = subprocess.run(list(a), capture_output=True, env=env, input=inp)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(a)} → {r.returncode}: {r.stderr.decode(errors='replace')[-400:]}")
    return r


def alert(msg: str):
    """screener.tg 와 같은 길: ntfy 단타 주제 + 텔레그램(비밀값 있을 때)"""
    print("⚠️", msg)
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://ntfy.sh/", data=json.dumps({"topic": ALERT_TOPIC, "title": "⚠️ " + msg.split("\n")[0][:80], "message": msg[:900],
                                                  "tags": ["warning"]}).encode(), headers={"Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print("ntfy 실패", e)
    tok, chat = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if tok and chat:
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage",
                                   data=urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode(), timeout=15)
        except Exception as e:
            print("TG 실패", e)


def _where():
    return f"{os.environ.get('GITHUB_WORKFLOW', '로컬')} {datetime.now(KST).strftime('%m/%d %H:%M')}"


def push_main(alert_fn=alert, quiet_alert=False) -> bool:
    """커밋은 이미 해 둔 상태에서 호출. 성공 True"""
    err = ""
    for k in range(TRIES):
        if k:
            time.sleep(5 * 2 ** (k - 1))
        r = run("git", "pull", "--rebase", "--autostash", "-q", "origin", "main")   # 안 올리는 수정 파일이 있어도 막히지 않게
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[-300:]
            run("git", "rebase", "--abort")
            print(f"pull --rebase 실패 ({k + 1}/{TRIES})", err)
            continue
        r = run("git", "push", "-q", "origin", "HEAD:main")
        if r.returncode == 0:
            if k: print(f"push 성공 ({k + 1}번째)")
            return True
        err = r.stderr.decode(errors="replace")[-300:]
        print(f"push 실패 ({k + 1}/{TRIES})", err)
    if not quiet_alert:
        alert_fn(f"⚠️ 단타 저장소 올리기 실패 ({_where()}) — {TRIES}번 다시 해도 안 됨. 알람 장부·학습 결과가 앱에 안 보일 수 있어요.\n{err.strip()[-200:]}")
    return False


def _fetch_days():
    """원격 days 브랜치 받기 → 커밋 해시 / None(브랜치 없음). 받기 자체가 실패하면 예외"""
    r = run("git", "ls-remote", "--exit-code", "origin", f"refs/heads/{DAYS_BRANCH}")
    if r.returncode == 2:
        return None
    if r.returncode != 0:
        raise RuntimeError("ls-remote 실패: " + r.stderr.decode(errors="replace")[-200:])
    for k in range(3):
        f = run("git", "fetch", "-q", "--depth", "1", "origin", f"+refs/heads/{DAYS_BRANCH}:refs/remotes/origin/{DAYS_BRANCH}")
        if f.returncode == 0:
            return run("git", "rev-parse", f"refs/remotes/origin/{DAYS_BRANCH}", check=True).stdout.decode().strip()
        time.sleep(5 * (k + 1))
    raise RuntimeError("days 받기 실패: " + f.stderr.decode(errors="replace")[-200:])


def _extract(commit, only_missing=True):
    """commit 의 data/days 를 작업 폴더에 풀기 (main 의 index 는 건드리지 않음)"""
    os.makedirs(DAYS_DIR, exist_ok=True)
    r = run("git", "archive", "--format=tar", commit, DAYS_DIR)
    if r.returncode != 0:
        return 0
    n = 0
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        for m in t.getmembers():
            if not m.isfile() or not m.name.startswith(DAYS_DIR + "/") or "/" in m.name[len(DAYS_DIR) + 1:] or ".." in m.name:
                continue
            if only_missing and os.path.exists(m.name):
                continue
            with open(m.name, "wb") as fo:
                fo.write(t.extractfile(m).read())
            n += 1
    return n


def days_pull() -> bool:
    try:
        c = _fetch_days()
    except Exception as e:
        print("days 받기 실패", e); return False
    if c is None:
        print("days 브랜치 없음 — 건너뜀"); return True
    n = _extract(c)
    print(f"days 브랜치 → {DAYS_DIR} {n}개 받음 (전체 {len(glob.glob(DAYS_DIR + '/*.csv.gz'))}개)")
    return True


def days_push() -> bool:
    files = sorted(glob.glob(f"{DAYS_DIR}/*.csv.gz"))
    if not files:
        print("올릴 하루 압축본 없음"); return True
    err = ""
    for k in range(TRIES):
        if k:
            time.sleep(5 * 2 ** (k - 1))
        try:
            parent = _fetch_days()
            if parent:
                _extract(parent)                        # 원격에만 있는 날 채우기 (합치기)
            cut = (datetime.now(KST) - timedelta(days=DAY_KEEP)).strftime("%Y%m%d")
            keep = [f for f in sorted(glob.glob(f"{DAYS_DIR}/*.csv.gz")) if os.path.basename(f)[:8] >= cut]
            with tempfile.TemporaryDirectory() as td:
                env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "idx"))
                run("git", "add", "-f", *keep, check=True, env=env)
                tree = run("git", "write-tree", check=True, env=env).stdout.decode().strip()
            if parent and run("git", "rev-parse", f"{parent}^{{tree}}", check=True).stdout.decode().strip() == tree:
                print("days 브랜치 이미 최신"); return True
            msg = f"📦 하루 압축본 {len(keep)}일 ({os.path.basename(keep[0])[:8]}~{os.path.basename(keep[-1])[:8]}) — 커밋 하나로 덮어씀"
            c = run("git", "commit-tree", tree, "-m", msg, check=True,
                    env=dict(os.environ, GIT_AUTHOR_NAME="github-actions", GIT_AUTHOR_EMAIL="actions@github.com",
                             GIT_COMMITTER_NAME="github-actions", GIT_COMMITTER_EMAIL="actions@github.com")).stdout.decode().strip()
            lease = f"--force-with-lease=refs/heads/{DAYS_BRANCH}:{parent or ''}"
            r = run("git", "push", "-q", lease, "origin", f"{c}:refs/heads/{DAYS_BRANCH}")
            if r.returncode == 0:
                print("days 브랜치 올림", msg); return True
            err = r.stderr.decode(errors="replace")[-300:]
        except Exception as e:
            err = str(e)
        print(f"days 올리기 실패 ({k + 1}/{TRIES})", err)
    alert(f"⚠️ 단타 하루 압축본(days 브랜치) 올리기 실패 ({_where()}) — 오늘 1분 자료가 학습에 안 들어갈 수 있어요.\n{err.strip()[-200:]}")
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    ok = {"push-main": push_main, "days-pull": days_pull, "days-push": days_push}.get(cmd)
    if not ok:
        print(__doc__); sys.exit(2)
    sys.exit(0 if ok() else 1)
