#!/usr/bin/env python3
"""Generate AICP attestation test vectors (TEST ONLY keys)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "spec" / "test-vectors"

# Fixed clock for reproducible vectors (2023-11-14T22:13:20Z)
FIXED_IAT = 1700000000
FIXED_EXP = 1700086400
ISS = "https://platform.example"
SUB = "card-test-001"
AUD = ISS


def b64url_uint(val: int) -> str:
    b = val.to_bytes((val.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def ec_jwk_public(key: ec.EllipticCurvePrivateKey, kid: str) -> dict:
    pub = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "use": "sig",
        "alg": "ES256",
        "x": b64url_uint(pub.x),
        "y": b64url_uint(pub.y),
    }


def ed_jwk_public(key: ed25519.Ed25519PrivateKey, kid: str) -> dict:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "kid": kid,
        "use": "sig",
        "alg": "EdDSA",
        "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"),
    }


def base_payload(jti: str = "jti-valid-001") -> dict:
    return {
        "iss": ISS,
        "sub": SUB,
        "iat": FIXED_IAT,
        "exp": FIXED_EXP,
        "kid": "test-es256-01",
        "aud": AUD,
        "jti": jti,
        "claims": {"contracts_completed": 3, "completion_rate": 1.0},
    }


def write_json(path: Path, obj: dict | list) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def load_or_create_keys() -> tuple[object, object, object, bool]:
    keys_path = OUT / "test-keys.json"
    OUT.mkdir(parents=True, exist_ok=True)
    if keys_path.exists():
        data = json.loads(keys_path.read_text())
        es_priv = serialization.load_pem_private_key(data["es256_pem"].encode(), password=None)
        ed_priv = serialization.load_pem_private_key(data["ed25519_pem"].encode(), password=None)
        rsa_priv = serialization.load_pem_private_key(data["rsa_pem"].encode(), password=None)
        return es_priv, ed_priv, rsa_priv, False

    es_priv = ec.generate_private_key(ec.SECP256R1())
    ed_priv = ed25519.Ed25519PrivateKey.generate()
    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return es_priv, ed_priv, rsa_priv, True


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    es_priv, ed_priv, rsa_priv, fresh_keys = load_or_create_keys()

    es_kid = "test-es256-01"
    ed_kid = "test-ed25519-01"

    jwks_es = {"keys": [ec_jwk_public(es_priv, es_kid)]}
    jwks_ed = {"keys": [ed_jwk_public(ed_priv, ed_kid)]}
    write_json(OUT / "jwks-es256.json", jwks_es)
    write_json(OUT / "jwks-ed25519.json", jwks_ed)

    if fresh_keys:
        test_keys = {
            "_warning": "TEST ONLY — fixed material for AICP spec vectors. Do not use in production.",
            "fixed_clock_unix": FIXED_IAT,
            "issuer": ISS,
            "es256_pem": es_priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("ascii"),
            "ed25519_pem": ed_priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("ascii"),
            "rsa_pem": rsa_priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("ascii"),
        }
        write_json(OUT / "test-keys.json", test_keys)

    def sign_es(payload: dict, *, headers: dict | None = None, key=es_priv) -> str:
        h = {"alg": "ES256", "typ": "JWT", "kid": payload.get("kid", es_kid)}
        if headers:
            h.update(headers)
        return jwt.encode(payload, key, algorithm="ES256", headers=h)

    def sign_ed(payload: dict) -> str:
        p = dict(payload)
        p["kid"] = ed_kid
        return jwt.encode(
            p,
            ed_priv,
            algorithm="EdDSA",
            headers={"alg": "EdDSA", "typ": "JWT", "kid": ed_kid},
        )

    valid_es = sign_es(base_payload())
    valid_ed = sign_ed(base_payload("jti-valid-ed-001"))

    payload_rs = base_payload("jti-rs256-001")
    payload_rs["kid"] = es_kid
    rs256 = jwt.encode(
        payload_rs,
        rsa_priv,
        algorithm="RS256",
        headers={"alg": "RS256", "typ": "JWT", "kid": es_kid},
    )

    missing_kid_hdr = jwt.encode(
        base_payload("jti-missing-kid-hdr"),
        es_priv,
        algorithm="ES256",
        headers={"alg": "ES256", "typ": "JWT"},
    )

    mismatch = sign_es(base_payload("jti-mismatch"))
    # corrupt payload kid after sign — rebuild with wrong payload kid
    bad_payload = base_payload("jti-mismatch")
    bad_payload["kid"] = "wrong-kid"
    mismatch = sign_es(bad_payload, headers={"kid": es_kid})

    expired = sign_es({**base_payload("jti-expired"), "exp": FIXED_IAT - 3600})
    future_iat = sign_es({**base_payload("jti-future-iat"), "iat": FIXED_IAT + 7200})
    wrong_iss = sign_es({**base_payload("jti-wrong-iss"), "iss": "https://evil.example"})
    wrong_aud = sign_es({**base_payload("jti-wrong-aud"), "aud": "https://other.example"})
    replay = valid_es  # same jti as valid_es

    vectors = [
        {"id": "valid-es256", "jwks": "jwks-es256.json", "jwt": valid_es, "expect": "pass"},
        {"id": "valid-ed25519", "jwks": "jwks-ed25519.json", "jwt": valid_ed, "expect": "pass"},
        {"id": "fail-rs256", "jwks": "jwks-es256.json", "jwt": rs256, "expect": "fail", "reason": "rs256"},
        {"id": "fail-missing-kid", "jwks": "jwks-es256.json", "jwt": missing_kid_hdr, "expect": "fail", "reason": "missing_kid"},
        {"id": "fail-kid-mismatch", "jwks": "jwks-es256.json", "jwt": mismatch, "expect": "fail", "reason": "kid_mismatch"},
        {"id": "fail-expired", "jwks": "jwks-es256.json", "jwt": expired, "expect": "fail", "reason": "expired"},
        {"id": "fail-future-iat", "jwks": "jwks-es256.json", "jwt": future_iat, "expect": "fail", "reason": "future_iat"},
        {"id": "fail-wrong-iss", "jwks": "jwks-es256.json", "jwt": wrong_iss, "expect": "fail", "reason": "wrong_iss"},
        {"id": "fail-wrong-aud", "jwks": "jwks-es256.json", "jwt": wrong_aud, "expect": "fail", "reason": "wrong_aud"},
        {"id": "fail-replay-jti", "jwks": "jwks-es256.json", "jwt": replay, "expect": "fail", "reason": "replay_jti", "pair_with": "valid-es256"},
    ]

    meta = {
        "description": "AICP attestation verification vectors (§6.3.4.1). Fixed clock for iat/exp checks.",
        "fixed_clock_unix": FIXED_IAT,
        "max_iat_skew_seconds": 300,
        "issuer": ISS,
        "vectors": vectors,
    }
    write_json(OUT / "vectors.json", meta)
    print(f"Wrote {len(vectors)} vectors to {OUT}")


if __name__ == "__main__":
    main()
