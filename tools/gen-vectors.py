#!/usr/bin/env python3
"""Generate AICP attestation and verifiable-reputation test vectors (TEST ONLY keys)."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merkle9162 as mkl

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


def main(only: str = "all") -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    es_priv, ed_priv, rsa_priv, fresh_keys = load_or_create_keys()

    es_kid = "test-es256-01"
    ed_kid = "test-ed25519-01"

    if only in ("all", "attestations"):
        jwks_es = {"keys": [ec_jwk_public(es_priv, es_kid)]}
        jwks_ed = {"keys": [ed_jwk_public(ed_priv, ed_kid)]}
        write_json(OUT / "jwks-es256.json", jwks_es)
        write_json(OUT / "jwks-ed25519.json", jwks_ed)

    if fresh_keys and only in ("all", "attestations"):
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

    if only == "reputation":
        write_reputation_vectors(es_priv)
        return

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
    write_reputation_vectors(es_priv)


def es256_jws(header: dict, payload: dict, private_key) -> str:
    """Compact JWS over RFC 8785 canonical JSON (header and payload)."""
    signing = mkl.b64url(mkl.canon(header)).encode("ascii") + b"." + mkl.b64url(mkl.canon(payload)).encode("ascii")
    der = private_key.sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing.decode("ascii") + "." + mkl.b64url(sig)


def write_reputation_vectors(es_priv) -> None:
    """§6.4 vectors: inclusion, consistency, completeness, revocation."""
    mkl.self_check()
    iss = ISS
    kid = "test-es256-01"

    details = {
        0: {"amount_minor": 5000, "currency": "USD", "outcome": "fulfilled"},
        2: {"amount_minor": 2500, "currency": "USD", "outcome": "fulfilled"},
        4: {"note": "delivery-rejected", "outcome": "reversed"},
    }
    salts = {
        0: bytes([0x11]) * 16,
        2: bytes([0x22]) * 16,
        4: bytes([0x33]) * 16,
    }
    evidence = {
        0: "settle:platform.example:cp-alice:5000:USD:1",
        2: "settle:platform.example:cp-bob:2500:USD:1",
    }

    def att(index: int, sub: str, jti: str, event: str, iat: int, cp: str) -> dict:
        return {
            "counterparty_id": cp,
            "detail_commit": mkl.detail_commit(salts[index], details[index]),
            "entry_type": "attestation",
            "event": event,
            "iat": iat,
            "iss": iss,
            "jti": jti,
            "settlement_hash": mkl.settlement_hash(evidence[index]),
            "sub": sub,
            "v": 1,
        }

    e0 = att(0, "card-test-001", "jti-rep-001", "contract_completed", 1700000000, "cp-alice")
    e2 = att(2, "card-test-002", "jti-rep-002", "contract_completed", 1700000100, "cp-bob")
    e4 = {
        "detail_commit": mkl.detail_commit(salts[4], details[4]),
        "entry_type": "revocation",
        "iat": 1700000200,
        "iss": iss,
        "jti": "jti-rev-001",
        "reason": "outcome_reversed",
        "sub": "card-test-001",
        "target_index": 0,
        "target_jti": "jti-rep-001",
        "v": 1,
    }

    def history_root(history: list[dict]) -> str:
        return mkl.b64url(mkl.mth([mkl.canon(e) for e in history]))

    def index_entry(sub: str, jti: str, iat: int, indices: list[int], history: list[dict]) -> dict:
        return {
            "count": len(indices),
            "entry_type": "subject_index",
            "history_root": history_root(history),
            "iat": iat,
            "indices": indices,
            "iss": iss,
            "jti": jti,
            "sub": sub,
            "v": 1,
        }

    e1 = index_entry("card-test-001", "jti-idx-001-1", 1700000000, [0], [e0])
    e3 = index_entry("card-test-002", "jti-idx-002-1", 1700000100, [2], [e2])
    e5 = index_entry("card-test-001", "jti-idx-001-2", 1700000200, [0, 4], [e0, e4])
    entries = [e0, e1, e2, e3, e4, e5]
    encoded = [mkl.canon(e) for e in entries]

    def checkpoint(sub: str, count: int, hist: list[dict], index_entry_i: int, latest: int) -> dict:
        return {
            "count": count,
            "history_root": history_root(hist),
            "index_entry": index_entry_i,
            "latest_entry": latest,
            "sub": sub,
            "v": 1,
        }

    map4 = [
        checkpoint("card-test-001", 1, [e0], 1, 0),
        checkpoint("card-test-002", 1, [e2], 3, 2),
    ]
    map6 = [
        checkpoint("card-test-001", 2, [e0, e4], 5, 4),
        checkpoint("card-test-002", 1, [e2], 3, 2),
    ]

    def sth_payload(tree_size: int, timestamp: int, subject_map: list[dict], jti: str) -> dict:
        root = mkl.mth(encoded[:tree_size])
        smr = mkl.mth([mkl.canon(leaf) for leaf in subject_map])
        return {
            "aud": "https://platform.example/.well-known/aicp-log",
            "exp": timestamp + 86400,
            "hash_alg": "SHA-256",
            "iat": timestamp,
            "iss": iss,
            "jti": jti,
            "kid": kid,
            "log_id": "https://platform.example/.well-known/aicp-log",
            "root_hash": mkl.b64url(root),
            "sth_version": 1,
            "subject_map_root": mkl.b64url(smr),
            "timestamp": timestamp,
            "tree_size": tree_size,
        }

    sth4 = sth_payload(4, 1700000100, map4, "sth-size-4")
    sth6 = sth_payload(6, 1700000300, map6, "sth-size-6")
    header = {"alg": "ES256", "kid": kid, "typ": "aicp-sth+jwt"}
    sth4_jwt = es256_jws(header, sth4, es_priv)
    sth6_jwt = es256_jws(header, sth6, es_priv)

    def path_b64(tree_size: int, index: int) -> list[str]:
        return [mkl.b64url(h) for h in mkl.inclusion_path(encoded[:tree_size], index)]

    map_encoded = [mkl.canon(leaf) for leaf in map6]
    cons = [mkl.b64url(h) for h in mkl.consistency_proof(encoded, 4)]

    log_proof_size4 = {
        "inclusion_path": path_b64(4, 0),
        "index": 0,
        "log_id": "https://platform.example/.well-known/aicp-log",
        "root_hash": sth4["root_hash"],
        "tree_size": 4,
    }
    attest_payload = {
        "aud": iss,
        "claims": {"contracts_completed": 1, "completion_rate": 1.0},
        "exp": FIXED_EXP,
        "iat": FIXED_IAT,
        "iss": iss,
        "jti": "jti-rep-001",
        "kid": kid,
        "log_proof": log_proof_size4,
        "sub": "card-test-001",
    }
    attest_header = {"alg": "ES256", "kid": kid, "typ": "JWT"}
    attest_jwt = es256_jws(attest_header, attest_payload, es_priv)

    disclosures = [
        {
            "detail": details[i],
            "index": i,
            "salt": mkl.b64url(salts[i]),
            "settlement_evidence": evidence.get(i),
        }
        for i in (0, 2, 4)
    ]

    history = []
    for i in (0, 4):
        history.append(
            {
                "entry": entries[i],
                "inclusion_path": path_b64(6, i),
                "index": i,
            }
        )

    completeness = {
        "checkpoint": map6[0],
        "disclosures": [d for d in disclosures if d["index"] in (0, 4)],
        "expect": "pass",
        "history": history,
        "id": "completeness-card-test-001",
        "index_entry": entries[5],
        "index_entry_inclusion_path": path_b64(6, 5),
        "kind": "completeness",
        "map_inclusion_path": [mkl.b64url(h) for h in mkl.inclusion_path(map_encoded, 0)],
        "map_index": 0,
        "map_size": 2,
        "score": {
            "active_contract_settlements": 0,
            "distinct_counterparties": 0,
            "revoked_jtis": ["jti-rep-001"],
        },
        "sub": "card-test-001",
        "tree_size": 6,
    }

    doc = {
        "description": "AICP verifiable reputation vectors (§6.4). TEST ONLY salts and keys.",
        "disclosures": disclosures,
        "entries": entries,
        "fixed_clock_unix": 1700000300,
        "hash_alg": "SHA-256",
        "issuer": iss,
        "mmd_seconds": 3600,
        "sth": {
            "4": {"jwt": sth4_jwt, "payload": sth4, "subject_map": map4},
            "6": {"jwt": sth6_jwt, "payload": sth6, "subject_map": map6},
        },
        "sth_max_age_seconds": 86400,
        "vectors": [
            {
                "expect": "pass",
                "id": "inclusion-entry-0",
                "inclusion_path": path_b64(6, 0),
                "kind": "inclusion",
                "leaf_index": 0,
                "tree_size": 6,
            },
            {
                "consistency_path": cons,
                "expect": "pass",
                "first": 4,
                "id": "consistency-4-to-6",
                "kind": "consistency",
                "second": 6,
            },
            completeness,
            {
                "base_expect": "pass",
                "consistency_path": cons,
                "current_tree_size": 6,
                "expect": "revoked",
                "id": "revoked-jti-rep-001",
                "jwt": attest_jwt,
                "jwks": "jwks-es256.json",
                "kind": "revocation",
                "proof_tree_size": 4,
                "sub": "card-test-001",
                "target_index": 0,
                "target_jti": "jti-rep-001",
            },
        ],
    }
    write_json(OUT / "reputation-vectors.json", doc)
    print("Wrote reputation vectors")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        choices=["all", "attestations", "reputation"],
        default="all",
        help="which vectors to regenerate (ECDSA signatures are not deterministic)",
    )
    main(ap.parse_args().only)
