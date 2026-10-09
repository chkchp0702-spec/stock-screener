"""🔔 처음 한 번: 푸시 열쇠 만들기 → archive/x/push_keys.json (공개 열쇠 2개 + 잠근 비밀 열쇠)"""
import base64
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import KEYS, box_key, b64u
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if os.path.exists(KEYS) and "--force" not in sys.argv:
    print("열쇠 이미 있음")
    sys.exit(0)
bk = box_key()
v = ec.generate_private_key(ec.SECP256R1())
vpub = v.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
r = rsa.generate_private_key(public_exponent=65537, key_size=2048)
rpub = r.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
pem = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
iv = os.urandom(12)
ct = AESGCM(bk).encrypt(iv, json.dumps({"vapid": pem(v), "rsa": pem(r)}).encode(), None)
json.dump({"vapid_pub": b64u(vpub), "rsa_pub": base64.b64encode(rpub).decode(),
           "box": {"iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()},
           "note": "비밀 열쇠는 TG_TOKEN 으로 잠겨 있음. TG_TOKEN 을 바꾸면 setup.py --force 로 다시 만들고 모두 다시 켜야 함"},
          open(KEYS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("열쇠 만듦")
