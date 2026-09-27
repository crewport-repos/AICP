"""Build spec/test-vectors/reputation-vectors.json (TEST ONLY keys and salts)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import merkle9162 as mkl

KID = "test-es256-01"
LOG_ID = "https://platform.example/.well-known/aicp-log"
SKEW = 300
MAX_AGE = 86400
MMD = 3600
CLOCK = 1700000300


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def es256_jws(header: dict, payload: dict, private_key) -> str:
    signing = (
        mkl.b64url(mkl.canon(header)).encode("ascii")
        + b"."
        + mkl.b64url(mkl.canon(payload)).encode("ascii")
    )
    der = private_key.sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing.decode("ascii") + "." + mkl.b64url(sig)


def hs256_jws(header: dict, payload: dict, secret: str) -> str:
    return jwt.encode(payload, secret, algorithm="HS256", headers=header)


def rs256_jws(header: dict, payload: dict, rsa_priv) -> str:
    return jwt.encode(payload, rsa_priv, algorithm="RS256", headers=header)


def b64s(items: list[bytes]) -> list[str]:
    return [mkl.b64url(h) for h in items]


def corrupt(token: str) -> str:
    """Flip a decoded bit. The last base64url character of a 32-byte digest has unused padding bits, so editing only that character often leaves the hash unchanged."""
    raw = bytearray(mkl.b64url_decode(token))
    raw[0] ^= 0xFF
    return mkl.b64url(bytes(raw))


def sth_payload(iss, tree_size, timestamp, root, map_root, map_size, jti) -> dict:
    return {
        "aud": LOG_ID,
        "exp": timestamp + MAX_AGE,
        "hash_alg": "SHA-256",
        "iat": timestamp,
        "iss": iss,
        "jti": jti,
        "kid": KID,
        "log_id": LOG_ID,
        "root_hash": root,
        "sth_version": 1,
        "subject_map_root": map_root,
        "subject_map_size": map_size,
        "timestamp": timestamp,
        "tree_size": tree_size,
    }


def sign_sth(payload, es_priv, alg_header="ES256") -> str:
    header = {"alg": alg_header, "kid": KID, "typ": "aicp-sth+jwt"}
    return es256_jws(header, payload, es_priv)


def empty_digest() -> str:
    return mkl.b64url(mkl.sha256(b""))


def history_root(entries: list[dict]) -> str:
    return mkl.b64url(mkl.mth([mkl.canon(e) for e in entries]))


def make_checkpoint(sub, hist_entries, indices, index_entry_i) -> dict:
    return {
        "count": len(indices),
        "history_root": history_root(hist_entries),
        "index_entry": index_entry_i,
        "latest_entry": indices[-1],
        "sub": sub,
        "v": 1,
    }


def subject_index(iss, sub, jti, iat, indices, hist) -> dict:
    return {
        "count": len(indices),
        "entry_type": "subject_index",
        "history_root": history_root(hist),
        "iat": iat,
        "indices": indices,
        "iss": iss,
        "jti": jti,
        "sub": sub,
        "v": 1,
    }


def attestation(iss, sub, jti, iat, event, cp, detail, salt, settle_salt, evidence) -> dict:
    body = {
        "counterparty_id": cp,
        "detail_commit": mkl.detail_commit(salt, detail),
        "entry_type": "attestation",
        "event": event,
        "iat": iat,
        "iss": iss,
        "jti": jti,
        "settlement_hash": mkl.settlement_hash(settle_salt, evidence),
        "sub": sub,
        "v": 1,
    }
    return body


def revocation(iss, sub, jti, iat, target_index, target_jti, detail, salt, reason="outcome_reversed") -> dict:
    return {
        "detail_commit": mkl.detail_commit(salt, detail),
        "entry_type": "revocation",
        "iat": iat,
        "iss": iss,
        "jti": jti,
        "reason": reason,
        "sub": sub,
        "target_index": target_index,
        "target_jti": target_jti,
        "v": 1,
    }


def correction(iss, sub, jti, iat, target_index, target_jti, detail, salt) -> dict:
    return {
        "detail_commit": mkl.detail_commit(salt, detail),
        "entry_type": "correction",
        "iat": iat,
        "iss": iss,
        "jti": jti,
        "reason": "outcome_reversed",
        "sub": sub,
        "target_index": target_index,
        "target_jti": target_jti,
        "v": 1,
    }


def tree_bundle(entries: list[dict]):
    encoded = [mkl.canon(e) for e in entries]
    # latest subject_index per sub
    latest = {}
    seen = {}
    for i, entry in enumerate(entries):
        if entry["entry_type"] == "subject_index":
            latest[entry["sub"]] = i
        elif entry["entry_type"] in ("attestation", "revocation", "correction"):
            seen.setdefault(entry["sub"], []).append(i)
    leaves = []
    for sub in sorted(latest, key=lambda s: s.encode("utf-8")):
        idx = latest[sub]
        checkpoint_entry = entries[idx]
        indices = checkpoint_entry["indices"]
        hist = [entries[i] for i in indices]
        leaves.append(make_checkpoint(sub, hist, indices, idx))
    leaf_bytes = [mkl.canon(leaf) for leaf in leaves]
    return encoded, leaves, leaf_bytes


def sign_tree_sth(entries, timestamp, jti, es_priv, iss):
    encoded, leaves, leaf_bytes = tree_bundle(entries)
    root = mkl.b64url(mkl.mth(encoded))
    if leaves:
        map_root = mkl.b64url(mkl.mth(leaf_bytes))
    else:
        map_root = empty_digest()
        root = empty_digest() if not entries else root
    if not entries:
        root = empty_digest()
    payload = sth_payload(iss, len(entries), timestamp, root, map_root, len(leaves), jti)
    token = sign_sth(payload, es_priv)
    return {
        "document": {"sth": token, "tree_size": len(entries)},
        "jwt": token,
        "payload": payload,
        "subject_map": leaves,
    }


def path_at(encoded, tree_size, index) -> list[str]:
    return b64s(mkl.inclusion_path(encoded[:tree_size], index))


def map_path(leaf_bytes, index) -> list[str]:
    return b64s(mkl.inclusion_path(leaf_bytes, index))


def history_proof_for(entries, encoded, leaves, leaf_bytes, sub, tree_size) -> dict:
    leaf = next(item for item in leaves if item["sub"] == sub)
    map_index = leaves.index(leaf)
    indices = entries[leaf["index_entry"]]["indices"]
    return {
        "checkpoint": leaf,
        "entries": [{"entry": entries[i], "index": i} for i in indices],
        "index_entry": entries[leaf["index_entry"]],
        "index_entry_inclusion_path": path_at(encoded, tree_size, leaf["index_entry"]),
        "map_inclusion_path": map_path(leaf_bytes, map_index),
        "map_index": map_index,
        "map_size": len(leaves),
    }


def card_jws(es_priv, iss, sub, jti, entry_jti, index, tree_size, root, inclusion, iat, exp) -> str:
    payload = {
        "aud": iss,
        "claims": {"contracts_completed": 1},
        "exp": exp,
        "iat": iat,
        "iss": iss,
        "jti": jti,
        "kid": KID,
        "log_proof": {
            "entry_jti": entry_jti,
            "inclusion_path": inclusion,
            "index": index,
            "log_id": LOG_ID,
            "root_hash": root,
            "tree_size": tree_size,
        },
        "sub": sub,
    }
    header = {"alg": "ES256", "kid": KID, "typ": "JWT"}
    return es256_jws(header, payload, es_priv)


def disclosure(index, salt: bytes, detail) -> dict:
    return {"detail": detail, "index": index, "salt": mkl.b64url(salt)}


def write_reputation_vectors(es_priv, rsa_priv, out: Path, iss: str, fixed_iat: int, fixed_exp: int) -> None:
    mkl.self_check()
    d_pay = {"amount_minor": 5000, "currency": "USD", "outcome": "fulfilled"}
    d_manifest = {"outcome": "fulfilled"}
    d_rev = {"outcome": "reversed"}
    salt0 = bytes([0x11]) * 16
    salt2 = bytes([0x22]) * 16
    salt4 = bytes([0x33]) * 16
    set0 = bytes([0xA1]) * 16
    set2 = bytes([0xA2]) * 16
    ev0 = {
        "amount_minor": 5000,
        "currency": "USD",
        "kind": "payment",
        "rail": "example-ledger",
        "reference": "pay-001",
    }
    ev2 = {
        "kind": "signed_manifest",
        "rail": "diskuss",
        "reference": "bouts/bout-001/manifest",
    }

    e0 = attestation(iss, "card-test-001", "jti-rep-001", 1700000000, "contract_completed", "cp-alice", d_pay, salt0, set0, ev0)
    e2 = attestation(iss, "card-test-002", "jti-rep-002", 1700000100, "contract_completed", "cp-bob", d_manifest, salt2, set2, ev2)
    e4 = revocation(iss, "card-test-001", "jti-rev-001", 1700000200, 0, "jti-rep-001", d_rev, salt4)
    e1 = subject_index(iss, "card-test-001", "jti-idx-001-1", 1700000000, [0], [e0])
    e3 = subject_index(iss, "card-test-002", "jti-idx-002-1", 1700000100, [2], [e2])
    e5 = subject_index(iss, "card-test-001", "jti-idx-001-2", 1700000200, [0, 4], [e0, e4])
    entries = [e0, e1, e2, e3, e4, e5]
    encoded, leaves6, leaf_bytes6 = tree_bundle(entries)
    encoded4 = encoded[:4]
    _, leaves4, leaf_bytes4 = tree_bundle(entries[:4])

    def sth_for(size, timestamp, jti, subset_entries):
        return sign_tree_sth(subset_entries, timestamp, jti, es_priv, iss)

    sth0 = sign_tree_sth([], 1699990000, "sth-size-0", es_priv, iss)
    sth2 = sth_for(2, 1700000020, "sth-size-2", entries[:2])
    sth4 = sth_for(4, 1700000100, "sth-size-4", entries[:4])
    sth6 = sth_for(6, CLOCK, "sth-size-6", entries)
    cons46 = b64s(mkl.consistency_proof(encoded, 4))

    # Active subject card-test-002, inclusion at size 6.
    hp2 = history_proof_for(entries, encoded, leaves6, leaf_bytes6, "card-test-002", 6)
    root6 = sth6["payload"]["root_hash"]
    root4 = sth4["payload"]["root_hash"]
    jwt2_size6 = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-002", "jti-rep-002", 2, 6, root6,
        path_at(encoded, 6, 2), fixed_iat, fixed_exp,
    )
    pres_inclusion = {
        "attestation": jwt2_size6,
        "disclosures": [disclosure(2, salt2, d_manifest)],
        "history_proof": hp2,
        "sth": sth6["jwt"],
    }

    # Same subject proved at size 4 and rolled forward.
    hp2_at4 = history_proof_for(entries[:4], encoded4, leaves4, leaf_bytes4, "card-test-002", 4)
    # History proof must be against the CURRENT (size 6) tree, not size 4.
    jwt2_size4 = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-002-old", "jti-rep-002", 2, 4, root4,
        path_at(encoded4, 4, 2), fixed_iat, fixed_exp,
    )
    pres_consistency = {
        "attestation": jwt2_size4,
        "consistency_path": cons46,
        "disclosures": [disclosure(2, salt2, d_manifest)],
        "history_proof": hp2,
        "sth": sth6["jwt"],
    }

    hp1 = history_proof_for(entries, encoded, leaves6, leaf_bytes6, "card-test-001", 6)
    jwt1_size4 = card_jws(
        es_priv, iss, "card-test-001", "jti-pres-001", "jti-rep-001", 0, 4, root4,
        path_at(encoded4, 4, 0), fixed_iat, fixed_exp,
    )
    jwt1_size6 = card_jws(
        es_priv, iss, "card-test-001", "jti-pres-001-fresh", "jti-rep-001", 0, 6, root6,
        path_at(encoded, 6, 0), fixed_iat, fixed_exp,
    )
    disc1 = [disclosure(0, salt0, d_pay), disclosure(4, salt4, d_rev)]
    pres_complete = {
        "attestation": jwt1_size6,
        "disclosures": disc1,
        "history_proof": hp1,
        "sth": sth6["jwt"],
    }
    pres_revoked = {
        "attestation": jwt1_size4,
        "consistency_path": cons46,
        "disclosures": disc1,
        "history_proof": hp1,
        "sth": sth6["jwt"],
    }

    def reject(vid, reason, presentation, held=None, extra=None):
        item = {
            "expect": "reject",
            "held_sths": held if held is not None else [sth6["jwt"]],
            "id": vid,
            "presentation": presentation,
            "reason": reason,
        }
        if extra:
            item.update(extra)
        return item

    bad_incl = copy.deepcopy(pres_inclusion)
    bad_incl["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-bad-incl", "jti-rep-002", 2, 6, root6,
        [corrupt(path_at(encoded, 6, 2)[0])] + path_at(encoded, 6, 2)[1:],
        fixed_iat, fixed_exp,
    )

    bad_cons = copy.deepcopy(pres_consistency)
    bad_cons["consistency_path"] = [corrupt(cons46[0])]
    bad_cons["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-bad-cons", "jti-rep-002", 2, 4, root4,
        path_at(encoded4, 4, 2), fixed_iat, fixed_exp,
    )
    bad_cons_undecodable = copy.deepcopy(pres_consistency)
    bad_cons_undecodable["consistency_path"] = ["a"]
    bad_cons_undecodable["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-bad-cons-b64", "jti-rep-002", 2, 4, root4,
        path_at(encoded4, 4, 2), fixed_iat, fixed_exp,
    )

    stale_payload = copy.deepcopy(sth6["payload"])
    stale_payload["timestamp"] = CLOCK - MAX_AGE - SKEW - 1
    stale_payload["iat"] = stale_payload["timestamp"]
    stale_payload["exp"] = stale_payload["timestamp"] + MAX_AGE
    stale_payload["jti"] = "sth-stale"
    stale_jwt = sign_sth(stale_payload, es_priv)
    stale_pres = copy.deepcopy(pres_inclusion)
    stale_pres["sth"] = stale_jwt
    stale_pres["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-stale", "jti-rep-002", 2, 6, root6,
        path_at(encoded, 6, 2), fixed_iat, fixed_exp,
    )

    future_payload = copy.deepcopy(sth6["payload"])
    future_payload["timestamp"] = CLOCK + SKEW + 1
    future_payload["iat"] = future_payload["timestamp"]
    future_payload["exp"] = future_payload["timestamp"] + MAX_AGE
    future_payload["jti"] = "sth-future"
    future_jwt = sign_sth(future_payload, es_priv)
    future_pres = copy.deepcopy(pres_inclusion)
    future_pres["sth"] = future_jwt

    fork_payload = copy.deepcopy(sth6["payload"])
    fork_payload["root_hash"] = corrupt(fork_payload["root_hash"])
    fork_payload["jti"] = "sth-fork"
    fork_jwt = sign_sth(fork_payload, es_priv)

    bad_map = copy.deepcopy(pres_inclusion)
    bad_map["history_proof"]["map_inclusion_path"] = [
        corrupt(p) for p in bad_map["history_proof"]["map_inclusion_path"]
    ] or [corrupt(root6)]
    bad_map["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-bad-map", "jti-rep-002", 2, 6, root6,
        path_at(encoded, 6, 2), fixed_iat, fixed_exp,
    )

    wrong_size = copy.deepcopy(pres_inclusion)
    wrong_size["history_proof"]["map_size"] = 99
    wrong_size["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-map-size", "jti-rep-002", 2, 6, root6,
        path_at(encoded, 6, 2), fixed_iat, fixed_exp,
    )

    dropped = copy.deepcopy(pres_complete)
    dropped["history_proof"]["entries"] = [dropped["history_proof"]["entries"][0]]
    dropped["attestation"] = jwt1_size6

    missing_adv = copy.deepcopy(pres_complete)
    missing_adv["disclosures"] = [disclosure(0, salt0, d_pay)]

    bad_salt = copy.deepcopy(pres_inclusion)
    bad_salt["disclosures"] = [disclosure(2, salt2, {"outcome": "reversed"})]

    short = copy.deepcopy(pres_inclusion)
    short["disclosures"] = [{"detail": d_manifest, "index": 2, "salt": mkl.b64url(bytes([0x01]) * 8)}]

    # Malformed revocation: target_jti matches, target_index does not.
    bad_rev_jti = revocation(iss, "card-test-001", "jti-rev-bad-jti", 1700000200, 99, "jti-rep-001", d_rev, salt4)
    # index 99 is not in history — both must identify the same in-history entry.
    # Build a log where the revocation is included so the checkpoint matches,
    # but target_index points at the subject_index (not the attestation) while target_jti is the attestation.
    bad_rev = revocation(iss, "card-test-001", "jti-rev-jti-only", 1700000200, 1, "jti-rep-001", d_rev, bytes([0x44]) * 16)
    bad_idx_entry = subject_index(iss, "card-test-001", "jti-idx-bad", 1700000200, [0, 2], [e0, bad_rev])
    bad_entries = [e0, e1, bad_rev, bad_idx_entry]
    bad_sth = sign_tree_sth(bad_entries, CLOCK, "sth-bad-rev", es_priv, iss)
    bad_enc, bad_leaves, bad_leaf_bytes = tree_bundle(bad_entries)
    bad_hp = history_proof_for(bad_entries, bad_enc, bad_leaves, bad_leaf_bytes, "card-test-001", 4)
    bad_root = bad_sth["payload"]["root_hash"]
    bad_jwt = card_jws(
        es_priv, iss, "card-test-001", "jti-pres-jti-only", "jti-rep-001", 0, 4, bad_root,
        path_at(bad_enc, 4, 0), fixed_iat, fixed_exp,
    )
    pres_jti_only = {
        "attestation": bad_jwt,
        "disclosures": [disclosure(0, salt0, d_pay), disclosure(2, bytes([0x44]) * 16, d_rev)],
        "history_proof": bad_hp,
        "sth": bad_sth["jwt"],
    }

    bad_rev2 = revocation(iss, "card-test-001", "jti-rev-idx-only", 1700000200, 0, "jti-does-not-match", d_rev, bytes([0x55]) * 16)
    bad_idx2 = subject_index(iss, "card-test-001", "jti-idx-bad2", 1700000200, [0, 2], [e0, bad_rev2])
    bad_entries2 = [e0, e1, bad_rev2, bad_idx2]
    bad_sth2 = sign_tree_sth(bad_entries2, CLOCK, "sth-bad-rev2", es_priv, iss)
    bad_enc2, bad_leaves2, bad_leaf_bytes2 = tree_bundle(bad_entries2)
    bad_hp2 = history_proof_for(bad_entries2, bad_enc2, bad_leaves2, bad_leaf_bytes2, "card-test-001", 4)
    bad_jwt2 = card_jws(
        es_priv, iss, "card-test-001", "jti-pres-idx-only", "jti-rep-001", 0, 4,
        bad_sth2["payload"]["root_hash"], path_at(bad_enc2, 4, 0), fixed_iat, fixed_exp,
    )
    pres_idx_only = {
        "attestation": bad_jwt2,
        "disclosures": [disclosure(0, salt0, d_pay), disclosure(2, bytes([0x55]) * 16, d_rev)],
        "history_proof": bad_hp2,
        "sth": bad_sth2["jwt"],
    }

    # Corrections.
    def correction_case(outcome, tag, salt_c):
        detail = {"outcome": outcome}
        att = attestation(
            iss, "card-corr", f"jti-corr-{tag}", 1700000000, "contract_completed", "cp-corr",
            d_pay, bytes([0x61]) * 16, bytes([0x71]) * 16, ev0,
        )
        # distinct settlement salt per case so hashes differ, evidence reused with different salt
        ev = dict(ev0)
        ev["reference"] = f"pay-corr-{tag}"
        att = attestation(
            iss, "card-corr", f"jti-corr-{tag}", 1700000000, "contract_completed", "cp-corr",
            {"amount_minor": 5000, "currency": "USD", "outcome": "fulfilled"},
            bytes([0x61 if tag == "ok" else 0x62]) * 16,
            bytes([0x71 if tag == "ok" else 0x72]) * 16,
            ev,
        )
        idx1 = subject_index(iss, "card-corr", f"jti-cidx-{tag}-1", 1700000000, [0], [att])
        corr = correction(iss, "card-corr", f"jti-corr-fix-{tag}", 1700000100, 0, att["jti"], detail, salt_c)
        idx2 = subject_index(iss, "card-corr", f"jti-cidx-{tag}-2", 1700000100, [0, 2], [att, corr])
        ents = [att, idx1, corr, idx2]
        bundle = sign_tree_sth(ents, CLOCK, f"sth-corr-{tag}", es_priv, iss)
        enc, leaves, lbs = tree_bundle(ents)
        hp = history_proof_for(ents, enc, leaves, lbs, "card-corr", 4)
        token = card_jws(
            es_priv, iss, "card-corr", f"jti-pres-corr-{tag}", att["jti"], 0, 4,
            bundle["payload"]["root_hash"], path_at(enc, 4, 0), fixed_iat, fixed_exp,
        )
        pres = {
            "attestation": token,
            "disclosures": [
                disclosure(0, bytes([0x61 if tag == "ok" else 0x62]) * 16, {"amount_minor": 5000, "currency": "USD", "outcome": "fulfilled"}),
                disclosure(2, salt_c, detail),
            ],
            "history_proof": hp,
            "sth": bundle["jwt"],
        }
        settlements = 1 if outcome == "fulfilled" else 0
        return pres, {
            "active_contract_settlements": settlements,
            "distinct_counterparties": 1 if settlements else 0,
            "revoked_jtis": [],
        }, bundle

    pres_corr_ok, score_ok, sth_corr_ok = correction_case("fulfilled", "ok", bytes([0x81]) * 16)
    pres_corr_no, score_no, sth_corr_no = correction_case("reversed", "no", bytes([0x82]) * 16)

    # Non-inclusion: left edge, claimed sub sorts before the first leaf.
    right = leaves6[0]
    non_inc = {
        "disclosures": [],
        "history_proof": {
            "entries": [],
            "non_inclusion": {
                "right": {
                    "checkpoint": right,
                    "map_inclusion_path": map_path(leaf_bytes6, 0),
                    "map_index": 0,
                },
                "sub": "card-test-000",
            },
        },
        "sth": sth6["jwt"],
    }
    non_inc_fail = copy.deepcopy(non_inc)
    non_inc_fail["history_proof"]["non_inclusion"]["sub"] = "card-test-001b"

    # RS256 / HS256 / wrong typ STHs.
    rs_header = {"alg": "RS256", "kid": KID, "typ": "aicp-sth+jwt"}
    rs_jwt = rs256_jws(rs_header, sth6["payload"], rsa_priv)
    hs_header = {"alg": "HS256", "kid": KID, "typ": "aicp-sth+jwt"}
    hs_jwt = hs256_jws(hs_header, sth6["payload"], "aicp-test-only-hs256")
    typ_header = {"alg": "ES256", "kid": KID, "typ": "JWT"}
    typ_jwt = es256_jws(typ_header, sth6["payload"], es_priv)

    behind = copy.deepcopy(pres_complete)
    behind["sth"] = sth4["jwt"]
    behind["consistency_path"] = cons46
    behind["attestation"] = jwt1_size6  # log_proof.tree_size 6 > current 4

    # Smaller STH whose root is not the size-6 prefix. The honest 4→6 path fails.
    fork4_payload = copy.deepcopy(sth4["payload"])
    fork4_payload["root_hash"] = corrupt(fork4_payload["root_hash"])
    fork4_payload["jti"] = "sth-size-4-fork"
    fork4_jwt = sign_sth(fork4_payload, es_priv)
    rollback_pres = copy.deepcopy(pres_consistency)
    rollback_pres["attestation"] = card_jws(
        es_priv, iss, "card-test-002", "jti-pres-rollback", "jti-rep-002", 2, 4, root4,
        path_at(encoded4, 4, 2), fixed_iat, fixed_exp,
    )

    staple_revoked = copy.deepcopy(pres_revoked)
    staple_revoked["sth"] = sth4["jwt"]

    vectors = [
        {
            "expect": "pass",
            "held_sths": [sth6["jwt"]],
            "id": "inclusion-entry-0",
            "presentation": pres_inclusion,
            "score": {"active_contract_settlements": 1, "distinct_counterparties": 1, "revoked_jtis": []},
            "status": "active",
        },
        {
            "expect": "pass",
            "held_sths": [sth4["jwt"], sth6["jwt"]],
            "id": "consistency-4-to-6",
            "presentation": pres_consistency,
            "prior_history_proof": hp2_at4,
            "score": {"active_contract_settlements": 1, "distinct_counterparties": 1, "revoked_jtis": []},
            "status": "active",
        },
        {
            "expect": "pass",
            "held_sths": [sth6["jwt"], sth4["jwt"]],
            "id": "sth-order-6-then-4",
            "presentation": pres_consistency,
            "prior_history_proof": hp2_at4,
            "score": {"active_contract_settlements": 1, "distinct_counterparties": 1, "revoked_jtis": []},
            "status": "active",
        },
        {
            "expect": "pass",
            "held_sths": [sth0["jwt"], sth2["jwt"], sth6["jwt"]],
            "id": "older-sth-size-0-and-2",
            "presentation": pres_inclusion,
            "score": {"active_contract_settlements": 1, "distinct_counterparties": 1, "revoked_jtis": []},
            "status": "active",
        },
        {
            "expect": "pass",
            "held_sths": [sth6["jwt"]],
            "id": "completeness-card-test-001",
            "presentation": pres_complete,
            "score": {"active_contract_settlements": 0, "distinct_counterparties": 0, "revoked_jtis": ["jti-rep-001"]},
            "status": "revoked",
        },
        {
            "base_expect": "pass",
            "expect": "revoked",
            "held_sths": [sth6["jwt"]],
            "id": "revoked-jti-rep-001",
            "presentation": pres_revoked,
            "score": {"active_contract_settlements": 0, "distinct_counterparties": 0, "revoked_jtis": ["jti-rep-001"]},
        },
        {
            "base_expect": "pass",
            "expect": "revoked",
            "held_sths": [sth6["jwt"]],
            "id": "staple-older-sth-revoked",
            "presentation": staple_revoked,
            "score": {"active_contract_settlements": 0, "distinct_counterparties": 0, "revoked_jtis": ["jti-rep-001"]},
        },
        {
            "expect": "pass",
            "held_sths": [sth_corr_ok["jwt"]],
            "id": "corrected-fulfilled",
            "presentation": pres_corr_ok,
            "score": score_ok,
            "status": "corrected",
        },
        {
            "expect": "pass",
            "held_sths": [sth_corr_no["jwt"]],
            "id": "corrected-not",
            "presentation": pres_corr_no,
            "score": score_no,
            "status": "corrected",
        },
        {
            "expect": "pass",
            "held_sths": [sth6["jwt"]],
            "id": "non-inclusion-pass",
            "presentation": non_inc,
        },
        {
            "expect": "pass",
            "held_sths": [sth0["jwt"]],
            "id": "empty-log-sth",
            "presentation": {"disclosures": [], "history_proof": {"entries": [], "non_inclusion": {"sub": "card-test-000"}}, "sth": sth0["jwt"]},
        },
        reject("reject-bad-inclusion", "inclusion_failed", bad_incl),
        reject("reject-bad-consistency", "consistency_failed", bad_cons, held=[sth4["jwt"], sth6["jwt"]]),
        reject(
            "reject-junk-path-older-sth",
            "consistency_failed",
            bad_cons,
            held=[sth0["jwt"], sth2["jwt"], sth6["jwt"]],
        ),
        reject("reject-stale-sth", "sth_stale", stale_pres, held=[stale_jwt]),
        reject("reject-future-sth", "sth_stale", future_pres, held=[future_jwt]),
        reject("reject-rollback", "split_view", rollback_pres, held=[sth6["jwt"], fork4_jwt]),
        reject("reject-same-size-fork", "split_view", pres_inclusion, held=[sth6["jwt"], fork_jwt]),
        reject("reject-sth-rs256", "sth_alg", {**pres_inclusion, "sth": rs_jwt}, held=[rs_jwt]),
        reject("reject-sth-hs256", "sth_alg", {**pres_inclusion, "sth": hs_jwt}, held=[hs_jwt]),
        reject("reject-sth-typ", "sth_typ", {**pres_inclusion, "sth": typ_jwt}, held=[typ_jwt]),
        reject("reject-bad-map-inclusion", "map_proof_failed", bad_map),
        reject("reject-wrong-map-size", "map_proof_failed", wrong_size),
        reject("reject-dropped-history", "history_incomplete", dropped),
        reject("reject-missing-adverse-disclosure", "disclosure_missing", missing_adv),
        reject("reject-bad-salt", "disclosure_mismatch", bad_salt),
        reject("reject-short-salt", "disclosure_mismatch", short),
        reject("reject-revocation-jti-only", "history_malformed", pres_jti_only, held=[bad_sth["jwt"]]),
        reject("reject-revocation-index-only", "history_malformed", pres_idx_only, held=[bad_sth2["jwt"]]),
        reject("reject-non-inclusion", "map_proof_failed", non_inc_fail),
        reject("reject-sth-behind", "sth_behind", behind, held=[sth4["jwt"]]),
        reject("reject-log-unreachable", "log_unreachable", {k: v for k, v in pres_inclusion.items() if k != "sth"}, held=[]),
        {
            "expect": "pass",
            "held_sths": [fork4_jwt, sth6["jwt"]],
            "id": "ignore-no-issuer-proof",
            "issuer_proof_overrides": {"4,6": None},
            "presentation": pres_inclusion,
            "score": {"active_contract_settlements": 1, "distinct_counterparties": 1, "revoked_jtis": []},
            "status": "active",
        },
        reject(
            "reject-issuer-proof-empty",
            "split_view",
            pres_inclusion,
            held=[fork4_jwt, sth6["jwt"]],
            extra={"issuer_proof_overrides": {"4,6": []}},
        ),
        reject(
            "reject-issuer-proof-bad-length",
            "split_view",
            pres_inclusion,
            held=[sth4["jwt"], sth6["jwt"]],
            extra={"issuer_proof_overrides": {"4,6": ["!!!"]}},
        ),
        reject(
            "reject-issuer-proof-undecodable",
            "split_view",
            pres_inclusion,
            held=[sth4["jwt"], sth6["jwt"]],
            extra={"issuer_proof_overrides": {"4,6": ["a"]}},
        ),
        reject("reject-presenter-consistency-undecodable", "consistency_failed", bad_cons_undecodable),
        reject(
            "reject-shrunk-history",
            "history_malformed",
            pres_complete,
            extra={"earlier_indices": [0, 4, 2]},
        ),
    ]

    doc = {
        "description": "AICP verifiable reputation vectors (§6.4). TEST ONLY salts and keys. Settlement preimages are monitor-only.",
        "disclosures": [
            disclosure(0, salt0, d_pay),
            disclosure(2, salt2, d_manifest),
            disclosure(4, salt4, d_rev),
        ],
        "entries": entries,
        "fixed_clock_unix": CLOCK,
        "hash_alg": "SHA-256",
        "issuer": iss,
        "mmd_seconds": MMD,
        "monitor_settlements": [
            {"evidence": ev0, "index": 0, "settlement_salt": mkl.b64url(set0)},
            {"evidence": ev2, "index": 2, "settlement_salt": mkl.b64url(set2)},
        ],
        "sth": {
            "0": sth0,
            "2": sth2,
            "4": sth4,
            "6": sth6,
        },
        "sth_max_age_seconds": MAX_AGE,
        "vectors": vectors,
    }
    write_json(out / "reputation-vectors.json", doc)
    # Unused variable kept out; bad_rev_jti was a sketch.
    del bad_rev_jti
    print(f"Wrote reputation vectors ({len(vectors)} cases) to {out}")
