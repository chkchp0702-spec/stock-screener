"""🔔 CH Investing 앱 푸시 (이 저장소의 TG_TOKEN 비밀값을 쓰려고 여기 둠) — 공통 — 열쇠 잠금/풀기 (열쇠는 저장소에 암호문으로만, 푸는 값은 GitHub 비밀값 TG_TOKEN 에서 만든다)"""
import base64
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = os.path.join(HERE, "keys.json")       # CH Investing 앱이 공개 열쇠를 여기서 읽음
SUBS = os.path.join(HERE, "subs.json")
SUB_TOPIC = "chkchp-ch-pushsub-k4t9"
TEST_TOPIC = "chkchp-ch-pushtest-k4t9"
ALERT_TOPICS = ["chkchp-ch-" + t for t in ("focus", "danta", "cup", "gap", "accum", "compass", "whale", "report", "kick")]


def box_key():
    s = (os.environ.get("TG_TOKEN") or "").strip()
    if not s:
        print("::error::TG_TOKEN 비밀값이 없음 (stock-screener Settings → Secrets → Actions)", flush=True)
        raise SystemExit(3)
    return hashlib.sha256(("ch-push:" + s).encode()).digest()


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_priv():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import serialization
    K = json.load(open(KEYS, encoding="utf-8"))
    raw = AESGCM(box_key()).decrypt(base64.b64decode(K["box"]["iv"]), base64.b64decode(K["box"]["ct"]), None)
    j = json.loads(raw)
    rsa = serialization.load_pem_private_key(j["rsa"].encode(), None)
    return K, j["vapid"], rsa


def open_sub(rsa, m):
    """앱이 보낸 {k, iv, ct} (RSA-OAEP 로 잠근 AES 열쇠 + AES-GCM 본문) → 구독 정보"""
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = rsa.decrypt(base64.b64decode(m["k"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return json.loads(AESGCM(k).decrypt(base64.b64decode(m["iv"]), base64.b64decode(m["ct"]), None))
