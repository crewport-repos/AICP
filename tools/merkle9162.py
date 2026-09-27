"""RFC 9162 Merkle trees for the AICP verifiable-reputation log.

Generation uses the recursive definitions in RFC 9162 §2.1.3.1 and §2.1.4.1.
Verification uses the iterative algorithms in §2.1.3.2 and §2.1.4.2.
The two paths are intentionally separate so a single shared bug cannot
silently agree with itself.
"""

from __future__ import annotations

import base64
import hashlib
import json


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def canon(obj: object) -> bytes:
    """Informative ASCII stand-in for RFC 8785.

    RFC 8785 is normative. This sorted-keys encoding is equivalent only for
    ASCII strings that need no escaping. Objects, arrays, strings, integers,
    and booleans only. No floats.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def leaf_hash(data: bytes) -> bytes:
    """RFC 9162 §2.1.1: HASH(0x00 || d)."""
    return sha256(b"\x00" + data)


def node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 9162 §2.1.1: HASH(0x01 || left || right)."""
    return sha256(b"\x01" + left + right)


def _k_largest_pow2_less_than(n: int) -> int:
    """Largest power of two strictly smaller than n (n > 1)."""
    if n <= 1:
        raise ValueError("n must be > 1")
    k = 1 << (n.bit_length() - 1)
    if k == n:
        k >>= 1
    return k


def mth(entries: list[bytes]) -> bytes:
    """Merkle Tree Hash, RFC 9162 §2.1.1."""
    n = len(entries)
    if n == 0:
        return sha256(b"")
    if n == 1:
        return leaf_hash(entries[0])
    k = _k_largest_pow2_less_than(n)
    return node_hash(mth(entries[:k]), mth(entries[k:]))


def inclusion_path(entries: list[bytes], index: int) -> list[bytes]:
    """PATH(index, D_n), RFC 9162 §2.1.3.1."""
    n = len(entries)
    if index < 0 or index >= n:
        raise ValueError("leaf index out of range")
    if n == 1:
        return []
    k = _k_largest_pow2_less_than(n)
    if index < k:
        return inclusion_path(entries[:k], index) + [mth(entries[k:])]
    return inclusion_path(entries[k:], index - k) + [mth(entries[:k])]


def _subproof(entries: list[bytes], m: int, complete: bool) -> list[bytes]:
    n = len(entries)
    if m > n or m < 0:
        raise ValueError("bad consistency bounds")
    if m == n:
        return [] if complete else [mth(entries)]
    k = _k_largest_pow2_less_than(n)
    if m <= k:
        return _subproof(entries[:k], m, complete) + [mth(entries[k:])]
    return _subproof(entries[k:], m - k, False) + [mth(entries[:k])]


def consistency_proof(entries: list[bytes], first: int) -> list[bytes]:
    """PROOF(first, D_n) for 0 < first < n, RFC 9162 §2.1.4.1."""
    n = len(entries)
    if not (0 < first < n):
        raise ValueError("need 0 < first < tree_size")
    return _subproof(entries, first, True)


def verify_inclusion(
    leaf_index: int,
    tree_size: int,
    leaf: bytes,
    path: list[bytes],
    root: bytes,
) -> bool:
    """RFC 9162 §2.1.3.2."""
    if tree_size < 1 or leaf_index < 0 or leaf_index >= tree_size:
        return False
    fn = leaf_index
    sn = tree_size - 1
    r = leaf
    for p in path:
        if sn == 0:
            return False
        if (fn & 1) == 1 or fn == sn:
            r = node_hash(p, r)
            if (fn & 1) == 0:
                while (fn & 1) == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def verify_consistency(
    first: int,
    second: int,
    first_hash: bytes,
    second_hash: bytes,
    path: list[bytes],
) -> bool:
    """RFC 9162 §2.1.4.2."""
    if not (0 < first < second):
        return False
    proof = list(path)
    if len(proof) == 0:
        return False
    if _is_pow2(first):
        proof = [first_hash] + proof
    fn = first - 1
    sn = second - 1
    if (fn & 1) == 1:
        while (fn & 1) == 1:
            fn >>= 1
            sn >>= 1
    fr = proof[0]
    sr = proof[0]
    for c in proof[1:]:
        if sn == 0:
            return False
        if (fn & 1) == 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if (fn & 1) == 0:
                while (fn & 1) == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == first_hash and sr == second_hash and sn == 0


def detail_commit(salt: bytes, detail: object) -> str:
    """§6.4.9: base64url(SHA-256("aicp-detail-v1" || 0x00 || salt || 0x00 || canon(detail)))."""
    if len(salt) < 16:
        raise ValueError("salt must be at least 16 bytes")
    body = b"aicp-detail-v1\x00" + salt + b"\x00" + canon(detail)
    return b64url(sha256(body))


def settlement_hash(salt: bytes, evidence: object) -> str:
    """§6.4.8: base64url(SHA-256("aicp-settlement-v1" || 0x00 || salt || 0x00 || JCS(evidence))).

    One salt of at least 16 bytes is reused for every attestation of that settlement.
    """
    if len(salt) < 16:
        raise ValueError("settlement salt must be at least 16 bytes")
    body = b"aicp-settlement-v1\x00" + salt + b"\x00" + canon(evidence)
    return b64url(sha256(body))


def self_check() -> None:
    """Cross-check recursive generation against iterative verification."""
    entries = [f"d{i}".encode("ascii") for i in range(9)]
    for n in range(1, len(entries) + 1):
        chunk = entries[:n]
        root = mth(chunk)
        for i in range(n):
            path = inclusion_path(chunk, i)
            if not verify_inclusion(i, n, leaf_hash(chunk[i]), path, root):
                raise AssertionError(f"inclusion failed n={n} i={i}")
        for first in range(1, n):
            proof = consistency_proof(chunk, first)
            if not verify_consistency(first, n, mth(entries[:first]), root, proof):
                raise AssertionError(f"consistency failed first={first} second={n}")
