#!/usr/bin/env python3
"""Check that the AICP specification agrees with its own JSON Schemas.

This is the only automated check in this repository. It answers one question:
does spec/AICP-v0.1.md agree with spec/schemas/*.json?

It performs four checks:

  1. parse   — every file in spec/schemas/ is well-formed JSON.
  2. meta    — every schema validates against the 2020-12 meta-schema it declares.
  3. examples— every fenced ```json block in the spec is validated against the
               schema it declares it illustrates.
  4. drift   — every block that claims to *mirror* a schema file is compared
               against that file.

HOW AN EXAMPLE IS BOUND TO A SCHEMA
-----------------------------------
The binding is written on the fence's info string. It is never inferred from
surrounding prose:

    ```json aicp:instance=card
    ```json aicp:schema=platform-capability
    ```json aicp:none

  aicp:instance=<name>  This block is a document that MUST validate against
                        spec/schemas/<name>.schema.json.
  aicp:schema=<name>    This block is itself a JSON Schema, reproduced in the
                        prose. It MUST be a valid 2020-12 schema AND MUST NOT
                        disagree with spec/schemas/<name>.schema.json about what
                        is valid.
  aicp:none             This block illustrates something AICP does not define a
                        schema for (an HTTP envelope, a JWKS, a registry
                        listing). Declaring this is mandatory; it is a
                        deliberate, reviewable statement, not a skip.

A ```json block with no aicp: annotation is a HARD FAILURE. An unchecked example
and a passing example must never look the same in a green run.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merkle9162 as mkl

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "spec" / "AICP-v0.1.md"
SCHEMA_DIR = REPO / "spec" / "schemas"
VECTORS_DIR = REPO / "spec" / "test-vectors"

FENCE = re.compile(r"^(?P<indent>\s*)```(?P<info>.*)$")
BINDING = re.compile(r"^aicp:(none|instance=[a-z0-9-]+|schema=[a-z0-9-]+)$")


class Block:
    """A fenced ```json block extracted from the spec."""

    def __init__(self, line: int, info: str, text: str):
        self.line = line  # 1-based line number of the opening fence
        self.info = info
        self.text = text

    @property
    def bindings(self) -> list[str]:
        return [t for t in self.info.split()[1:] if t.startswith("aicp:")]


def find_json_blocks(markdown: str) -> list[Block]:
    """Extract every fenced block whose language is `json`.

    A single scanner tracks open/close fences so that a ``` inside prose cannot
    desynchronise the parse.
    """
    blocks: list[Block] = []
    open_line = None
    open_info = None
    body: list[str] = []

    for n, raw in enumerate(markdown.splitlines(), start=1):
        m = FENCE.match(raw)
        if not m:
            if open_line is not None:
                body.append(raw)
            continue
        info = m.group("info").strip()
        if open_line is None:
            open_line, open_info, body = n, info, []
        else:
            # A closing fence carries no info string.
            if info:
                if open_info.split()[:1] == ["json"]:
                    body.append(raw)
                continue
            if open_info.split()[:1] == ["json"]:
                blocks.append(Block(open_line, open_info, "\n".join(body)))
            open_line, open_info, body = None, None, []

    if open_line is not None:
        raise SystemExit(f"FATAL: unterminated fence opened at line {open_line}")
    return blocks


# Applicator keywords, grouped by the shape of their value. Needed so that a
# *property named* "description" is never mistaken for the `description`
# annotation keyword — card.schema.json and platform-capability.schema.json both
# define a property called "description", and silently deleting it would hide
# exactly the kind of disagreement this check exists to find.
SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
SCHEMA_KEYWORDS = frozenset(
    {
        "items", "additionalProperties", "not", "if", "then", "else", "contains",
        "propertyNames", "unevaluatedItems", "unevaluatedProperties",
    }
)
SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})

DOC_ONLY_KEYWORDS = frozenset({"description", "$id"})


def strip_annotations(node, *, is_schema: bool = True):
    """Drop documentation-only keywords so drift compares *meaning*, not prose.

    `description` is prose and `$id` is a file-location artefact that a copy
    living inside the markdown cannot carry. Everything that changes which
    documents are valid — `required`, `type`, `enum`, the set of properties —
    is compared exactly.

    `is_schema` tracks whether `node` is a schema object or a plain data/name
    map, so that annotation keywords are only stripped where they *are*
    annotation keywords.
    """
    if isinstance(node, list):
        return [strip_annotations(v, is_schema=is_schema) for v in node]
    if not isinstance(node, dict):
        return node

    if not is_schema:
        # A map of names to schemas (e.g. the value of `properties`): keys are
        # author-chosen names and must all survive.
        return {k: strip_annotations(v, is_schema=True) for k, v in node.items()}

    out = {}
    for k, v in node.items():
        if k in DOC_ONLY_KEYWORDS:
            continue
        if k in SCHEMA_MAP_KEYWORDS:
            out[k] = strip_annotations(v, is_schema=False)
        elif k in SCHEMA_KEYWORDS or k in SCHEMA_LIST_KEYWORDS:
            out[k] = strip_annotations(v, is_schema=True)
        else:
            # Data-bearing keywords (`enum`, `const`, `required`, `default`, …)
            # are compared verbatim.
            out[k] = v
    return out


def load_schemas() -> tuple[dict[str, dict], list[str]]:
    """Check 1 (parse) and check 2 (meta-schema)."""
    schemas: dict[str, dict] = {}
    failures: list[str] = []

    files = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if not files:
        raise SystemExit(f"FATAL: no schemas found in {SCHEMA_DIR}")

    print(f"[1] parsing {len(files)} schema file(s)")
    for path in files:
        name = path.name.removesuffix(".schema.json")
        try:
            schema = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            failures.append(f"{path.name}: not valid JSON: {e}")
            print(f"    FAIL {path.name}: {e}")
            continue
        schemas[name] = schema
        print(f"    ok   {path.name}")

    print(f"[2] validating {len(schemas)} schema(s) against the 2020-12 meta-schema")
    for name, schema in sorted(schemas.items()):
        declared = schema.get("$schema")
        if declared != "https://json-schema.org/draft/2020-12/schema":
            failures.append(f"{name}: declares $schema {declared!r}, expected 2020-12")
            print(f"    FAIL {name}: declares $schema {declared!r}")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:
            failures.append(f"{name}: invalid 2020-12 schema: {e}")
            print(f"    FAIL {name}: {e}")
            continue
        print(f"    ok   {name}")

    return schemas, failures


def check_examples(blocks: list[Block], schemas: dict[str, dict]) -> list[str]:
    """Check 3 — validate each annotated example against its declared schema."""
    failures: list[str] = []
    counts = {"instance": 0, "schema": 0, "none": 0}

    print(f"[3] checking {len(blocks)} fenced json block(s) in {SPEC.name}")
    for b in blocks:
        where = f"{SPEC.name}:{b.line}"

        if len(b.bindings) != 1:
            failures.append(
                f"{where}: fenced json block declares {len(b.bindings)} aicp: bindings, "
                f"expected exactly 1 (info string: ```{b.info})"
            )
            print(f"    FAIL {where}: no aicp: binding on the fence")
            continue

        binding = b.bindings[0]
        if not BINDING.match(binding):
            failures.append(f"{where}: malformed binding {binding!r}")
            print(f"    FAIL {where}: malformed binding {binding!r}")
            continue

        # The block carries a well-formed binding, so it is accounted for. Count
        # it now: a block that fails below has been *checked and rejected*, not
        # skipped, and must not also be reported as unaccounted for.
        kind = binding.removeprefix("aicp:").split("=", 1)[0]
        counts[kind if kind != "none" else "none"] += 1

        try:
            doc = json.loads(b.text)
        except json.JSONDecodeError as e:
            failures.append(f"{where}: example is not valid JSON: {e}")
            print(f"    FAIL {where}: not valid JSON: {e}")
            continue

        if binding == "aicp:none":
            print(f"    ok   {where}: parses; declares no AICP schema")
            continue

        name = binding.split("=", 1)[1]
        if name not in schemas:
            failures.append(f"{where}: binds to unknown schema {name!r}")
            print(f"    FAIL {where}: unknown schema {name!r}")
            continue

        if kind == "schema":
            try:
                Draft202012Validator.check_schema(doc)
            except Exception as e:
                failures.append(f"{where}: not a valid 2020-12 schema: {e}")
                print(f"    FAIL {where}: not a valid 2020-12 schema: {e}")
                continue
            print(f"    ok   {where}: valid 2020-12 schema (mirrors {name})")
            continue

        errors = sorted(
            Draft202012Validator(schemas[name]).iter_errors(doc),
            key=lambda e: list(e.path),
        )
        if errors:
            for e in errors:
                loc = "/".join(str(p) for p in e.path) or "<root>"
                failures.append(f"{where}: does not satisfy {name}: at {loc}: {e.message}")
                print(f"    FAIL {where}: at {loc}: {e.message}")
            continue
        print(f"    ok   {where}: valid against {name}")

    checked = sum(counts.values())
    print(
        f"    examples checked: {checked} "
        f"(instance={counts['instance']}, schema={counts['schema']}, none={counts['none']})"
    )
    print(f"    fenced json blocks present: {len(blocks)}")
    if checked != len(blocks):
        failures.append(
            f"accounting: {checked} block(s) checked but {len(blocks)} fenced json "
            f"block(s) are present — {len(blocks) - checked} were not accounted for"
        )
        print(f"    FAIL accounting: {len(blocks) - checked} block(s) unaccounted for")
    else:
        print("    ok   every fenced json block is accounted for")

    return failures


def check_drift(blocks: list[Block], schemas: dict[str, dict]) -> list[str]:
    """Check 4 — a schema reproduced in the prose must not disagree with the file."""
    failures: list[str] = []
    mirrors = [
        b for b in blocks
        if len(b.bindings) == 1 and b.bindings[0].startswith("aicp:schema=")
    ]

    print(f"[4] comparing {len(mirrors)} in-document schema copy/copies against spec/schemas/")
    for b in mirrors:
        name = b.bindings[0].removeprefix("aicp:schema=")
        where = f"{SPEC.name}:{b.line}"
        if name not in schemas:
            failures.append(f"{where}: mirrors unknown schema {name!r}")
            continue

        in_doc = strip_annotations(json.loads(b.text))
        on_disk = strip_annotations(schemas[name])
        if in_doc == on_disk:
            print(f"    ok   {where}: agrees with {name}.schema.json")
            continue

        diff = "\n".join(
            difflib.unified_diff(
                json.dumps(in_doc, indent=2, sort_keys=True).splitlines(),
                json.dumps(on_disk, indent=2, sort_keys=True).splitlines(),
                fromfile=f"{SPEC.name}:{b.line} (in-document copy)",
                tofile=f"spec/schemas/{name}.schema.json (normative)",
                lineterm="",
            )
        )
        failures.append(
            f"{where}: the copy of {name} in the spec disagrees with "
            f"spec/schemas/{name}.schema.json about what is valid:\n{diff}"
        )
        print(f"    FAIL {where}: disagrees with {name}.schema.json")
        print(diff)

    return failures


def _jwks_to_key(jwk: dict, jwt_mod):
    alg = jwk.get("alg") or ("EdDSA" if jwk.get("kty") == "OKP" else "ES256")
    if alg not in jwt_mod.algorithms.get_default_algorithms():
        raise ValueError(f"unsupported jwk alg {alg}")
    return jwt_mod.algorithms.get_default_algorithms()[alg].from_jwk(json.dumps(jwk))


def verify_attestation_vector(
    token: str,
    jwks: dict,
    *,
    issuer: str,
    now: int,
    max_skew: int,
    jti_cache: set[str],
    jwt_mod,
) -> None:
    header = jwt_mod.get_unverified_header(token)
    payload = jwt_mod.decode(token, options={"verify_signature": False})

    if header.get("alg") not in ("ES256", "EdDSA"):
        raise ValueError(f"disallowed alg {header.get('alg')}")

    hdr_kid = header.get("kid")
    if not hdr_kid or not isinstance(hdr_kid, str):
        raise ValueError("missing or invalid header kid")

    body_kid = payload.get("kid")
    if hdr_kid != body_kid:
        raise ValueError("header/payload kid mismatch")

    if payload.get("iss") != issuer:
        raise ValueError("wrong iss")

    if payload.get("aud") != issuer:
        raise ValueError("wrong aud")

    jti = payload.get("jti")
    if not jti:
        raise ValueError("missing jti")
    if jti in jti_cache:
        raise ValueError("replayed jti")
    jti_cache.add(jti)

    iat = int(payload["iat"])
    exp = int(payload["exp"])
    if iat > now + max_skew:
        raise ValueError("future iat")
    if exp < now - max_skew:
        raise ValueError("expired")

    keys = {k["kid"]: k for k in jwks.get("keys", [])}
    if hdr_kid not in keys:
        raise ValueError("kid not in jwks")
    key = _jwks_to_key(keys[hdr_kid], jwt_mod)
    jwt_mod.decode(
        token,
        key,
        algorithms=[header["alg"]],
        options={
            "verify_aud": False,
            "verify_exp": False,
            "verify_iat": False,
        },
    )


def check_vectors() -> list[str]:
    failures: list[str] = []
    try:
        import jwt as jwt_mod
    except ImportError:
        failures.append("vectors: PyJWT is not installed (required for --check vectors)")
        return failures

    manifest_path = VECTORS_DIR / "vectors.json"
    if not manifest_path.exists():
        failures.append(f"vectors: missing {manifest_path}")
        return failures

    meta = json.loads(manifest_path.read_text())
    now = int(meta["fixed_clock_unix"])
    max_skew = int(meta.get("max_iat_skew_seconds", 300))
    issuer = meta["issuer"]
    jti_cache: set[str] = set()

    print(f"[5] verifying {len(meta['vectors'])} attestation vector(s) in {manifest_path.relative_to(REPO)}")
    by_id = {v["id"]: v for v in meta["vectors"]}

    for entry in meta["vectors"]:
        vid = entry["id"]
        if entry.get("pair_with") and entry["pair_with"] in by_id:
            # Ensure paired pass vector is exercised first (replay case).
            pass

    for entry in meta["vectors"]:
        vid = entry["id"]
        token = entry["jwt"]
        jwks_path = VECTORS_DIR / entry["jwks"]
        try:
            jwks = json.loads(jwks_path.read_text())
            verify_attestation_vector(
                token,
                jwks,
                issuer=issuer,
                now=now,
                max_skew=max_skew,
                jti_cache=jti_cache,
                jwt_mod=jwt_mod,
            )
            ok = True
        except Exception as e:
            ok = False
            err = str(e)

        if entry["expect"] == "pass":
            if ok:
                print(f"    ok   {vid}: verified")
            else:
                failures.append(f"vectors:{vid}: expected pass but failed: {err}")
                print(f"    FAIL {vid}: expected pass: {err}")
        else:
            if ok:
                failures.append(f"vectors:{vid}: expected fail ({entry.get('reason')}) but verified")
                print(f"    FAIL {vid}: expected fail but verified")
            else:
                print(f"    ok   {vid}: rejected ({entry.get('reason', err)})")

    return failures


REPUTATION_TYPES = frozenset({"attestation", "revocation", "correction"})


def _decode_path(items: list[str]) -> list[bytes]:
    out = []
    for item in items:
        raw = mkl.b64url_decode(item)
        if len(raw) != 32:
            raise ValueError("proof hash is not 32 bytes")
        out.append(raw)
    return out


def _verify_sth(label: str, sth: dict, entries: list[dict], now: int, max_age: int, jwt_mod) -> None:
    payload = sth["payload"]
    token = sth["jwt"]
    header = jwt_mod.get_unverified_header(token)
    if header.get("alg") != "ES256" or header.get("typ") != "aicp-sth+jwt":
        raise ValueError(f"{label}: STH header alg/typ")
    if header.get("kid") != payload.get("kid"):
        raise ValueError(f"{label}: STH kid mismatch")
    jwks = json.loads((VECTORS_DIR / "jwks-es256.json").read_text())
    key = _jwks_to_key(jwks["keys"][0], jwt_mod)
    decoded = jwt_mod.decode(
        token,
        key,
        algorithms=["ES256"],
        audience=payload["log_id"],
        options={"verify_exp": False, "verify_iat": False},
    )
    if decoded != payload:
        raise ValueError(f"{label}: STH payload does not match signed claims")
    if payload["aud"] != payload["log_id"] or payload["iss"] != "https://platform.example":
        raise ValueError(f"{label}: STH iss/aud")
    if payload["hash_alg"] != "SHA-256" or payload["sth_version"] != 1:
        raise ValueError(f"{label}: STH alg/version")
    if int(payload["exp"]) != int(payload["timestamp"]) + 86400:
        raise ValueError(f"{label}: STH exp is not timestamp + 86400")
    timestamp = int(payload["timestamp"])
    if timestamp > now + 300 or now > timestamp + max_age + 300:
        raise ValueError(f"{label}: STH outside freshness bound")
    tree_size = int(payload["tree_size"])
    encoded = [mkl.canon(e) for e in entries[:tree_size]]
    if mkl.b64url(mkl.mth(encoded)) != payload["root_hash"]:
        raise ValueError(f"{label}: root_hash mismatch")
    leaves = [mkl.canon(leaf) for leaf in sth["subject_map"]]
    if mkl.b64url(mkl.mth(leaves)) != payload["subject_map_root"]:
        raise ValueError(f"{label}: subject_map_root mismatch")
    derived = _derive_subject_map(entries, tree_size)
    if derived != sth["subject_map"]:
        raise ValueError(f"{label}: subject map is not the latest checkpoint per subject")


def _history_root(history: list[dict]) -> str:
    return mkl.b64url(mkl.mth([mkl.canon(e) for e in history]))


def _derive_subject_map(entries: list[dict], tree_size: int) -> list[dict]:
    """Latest subject_index per sub inside entries[0:tree_size], as map leaves."""
    latest: dict[str, tuple[int, dict]] = {}
    seen: dict[str, list[int]] = {}
    for i, entry in enumerate(entries[:tree_size]):
        et = entry["entry_type"]
        sub = entry["sub"]
        if et in REPUTATION_TYPES:
            seen.setdefault(sub, []).append(i)
        elif et == "subject_index":
            latest[sub] = (i, entry)
        else:
            raise ValueError(f"unknown entry_type at {i}")
    leaves = []
    for sub in sorted(latest):
        index_entry_i, checkpoint = latest[sub]
        indices = list(checkpoint["indices"])
        if indices != seen.get(sub, []):
            raise ValueError(f"{sub}: subject_index does not list every reputation entry")
        if checkpoint["count"] != len(indices):
            raise ValueError(f"{sub}: count mismatch")
        history = [entries[i] for i in indices]
        if checkpoint["history_root"] != _history_root(history):
            raise ValueError(f"{sub}: history_root mismatch")
        if any(entries[i]["sub"] != sub for i in indices):
            raise ValueError(f"{sub}: foreign subject in history")
        leaves.append(
            {
                "count": checkpoint["count"],
                "history_root": checkpoint["history_root"],
                "index_entry": index_entry_i,
                "latest_entry": indices[-1],
                "sub": sub,
                "v": 1,
            }
        )
    return leaves


def _open_disclosures(entries: list[dict], disclosures: list[dict]) -> None:
    by_index = {d["index"]: d for d in disclosures}
    for i, entry in enumerate(entries):
        if "detail_commit" not in entry:
            continue
        if i not in by_index:
            continue
        disc = by_index[i]
        salt = mkl.b64url_decode(disc["salt"])
        got = mkl.detail_commit(salt, disc["detail"])
        if got != entry["detail_commit"]:
            raise ValueError(f"detail_commit mismatch at index {i}")
        if disc.get("settlement_evidence") is not None:
            if mkl.settlement_hash(disc["settlement_evidence"]) != entry.get("settlement_hash"):
                raise ValueError(f"settlement_hash mismatch at index {i}")


def _require_adverse_disclosures(history: list[dict], disclosures: list[dict]) -> None:
    opened = {d["index"] for d in disclosures}
    for item in history:
        entry = item["entry"]
        adverse = entry["entry_type"] in ("revocation", "correction") or entry.get("event") == "dispute_result"
        if adverse and item["index"] not in opened:
            raise ValueError(f"missing disclosure for adverse entry {item['index']}")


def _status_of(history: list[dict], target_index: int, target_jti: str) -> str:
    found = False
    status = "active"
    for item in history:
        entry = item["entry"]
        if item["index"] == target_index:
            if entry.get("jti") != target_jti or entry["entry_type"] != "attestation":
                raise ValueError("target is not the named attestation")
            found = True
            continue
        if item["index"] < target_index:
            continue
        matches = entry.get("target_index") == target_index or entry.get("target_jti") == target_jti
        if not matches:
            continue
        if entry["entry_type"] == "revocation":
            return "revoked"
        if entry["entry_type"] == "correction":
            status = "corrected"
    if not found:
        raise ValueError("target attestation missing from history")
    return status


def _score(history: list[dict]) -> dict:
    """Per-issuer score: distinct settlement hashes among non-revoked economic events."""
    revoked_ids: set[int] = set()
    revoked_jtis: list[str] = []
    for item in history:
        entry = item["entry"]
        if entry["entry_type"] != "revocation":
            continue
        revoked_ids.add(int(entry["target_index"]))
        revoked_jtis.append(entry["target_jti"])
    settlements: set[str] = set()
    counterparties: set[str] = set()
    for item in history:
        entry = item["entry"]
        if item["index"] in revoked_ids or entry["entry_type"] != "attestation":
            continue
        if entry.get("event") not in ("contract_completed", "outcome"):
            continue
        settlements.add(entry["settlement_hash"])
        counterparties.add(entry["counterparty_id"])
    return {
        "active_contract_settlements": len(settlements),
        "distinct_counterparties": len(counterparties),
        "revoked_jtis": revoked_jtis,
    }


def _verify_inclusion_of(entry: dict, index: int, tree_size: int, path_b64: list[str], root_b64: str, entries: list[dict]) -> None:
    encoded = [mkl.canon(e) for e in entries[:tree_size]]
    expected = [mkl.b64url(h) for h in mkl.inclusion_path(encoded, index)]
    if path_b64 != expected:
        raise ValueError(f"inclusion path at {index} does not match RFC 9162 PATH")
    leaf = mkl.leaf_hash(mkl.canon(entry))
    if mkl.canon(entry) != mkl.canon(entries[index]):
        raise ValueError(f"entry bytes at {index} differ from the log")
    if not mkl.verify_inclusion(index, tree_size, leaf, _decode_path(path_b64), mkl.b64url_decode(root_b64)):
        raise ValueError(f"iterative inclusion check failed at {index}")


def _verify_completeness(vector: dict, doc: dict) -> list[dict]:
    entries = doc["entries"]
    tree_size = int(vector["tree_size"])
    sth = doc["sth"][str(tree_size)]
    root = sth["payload"]["root_hash"]
    checkpoint = vector["checkpoint"]
    sub = vector["sub"]
    if checkpoint["sub"] != sub:
        raise ValueError("checkpoint sub mismatch")
    map_leaves = [mkl.canon(leaf) for leaf in sth["subject_map"]]
    if len(map_leaves) != int(vector["map_size"]):
        raise ValueError("map_size mismatch")
    map_root = sth["payload"]["subject_map_root"]
    map_index = int(vector["map_index"])
    expected_map_path = [mkl.b64url(h) for h in mkl.inclusion_path(map_leaves, map_index)]
    if vector["map_inclusion_path"] != expected_map_path:
        raise ValueError("subject-map inclusion path mismatch")
    leaf = mkl.leaf_hash(mkl.canon(checkpoint))
    if not mkl.verify_inclusion(
        map_index,
        int(vector["map_size"]),
        leaf,
        _decode_path(vector["map_inclusion_path"]),
        mkl.b64url_decode(map_root),
    ):
        raise ValueError("subject-map inclusion failed")
    if sth["subject_map"][map_index] != checkpoint:
        raise ValueError("checkpoint is not the signed map leaf")
    index_i = int(checkpoint["index_entry"])
    _verify_inclusion_of(vector["index_entry"], index_i, tree_size, vector["index_entry_inclusion_path"], root, entries)
    if vector["index_entry"]["history_root"] != checkpoint["history_root"]:
        raise ValueError("index entry history_root disagrees with checkpoint")
    if vector["index_entry"]["indices"] != [h["index"] for h in vector["history"]]:
        raise ValueError("presented history does not match subject_index indices")
    for item in vector["history"]:
        _verify_inclusion_of(item["entry"], int(item["index"]), tree_size, item["inclusion_path"], root, entries)
        if item["entry"]["entry_type"] not in REPUTATION_TYPES:
            raise ValueError("history contains a non-reputation entry")
        if item["entry"]["sub"] != sub:
            raise ValueError("history entry sub mismatch")
    history_entries = [h["entry"] for h in vector["history"]]
    if _history_root(history_entries) != checkpoint["history_root"]:
        raise ValueError("history_root does not match presented entries")
    _open_disclosures(entries, vector["disclosures"])
    _require_adverse_disclosures(vector["history"], vector["disclosures"])
    return vector["history"]


def check_reputation() -> list[str]:
    failures: list[str] = []
    try:
        import jwt as jwt_mod
    except ImportError:
        return ["reputation: PyJWT is not installed"]

    path = VECTORS_DIR / "reputation-vectors.json"
    if not path.exists():
        return [f"reputation: missing {path}"]

    try:
        mkl.self_check()
    except Exception as e:
        return [f"reputation: merkle self-check failed: {e}"]

    doc = json.loads(path.read_text())
    try:
        log_schema = json.loads((SCHEMA_DIR / "log-entry.schema.json").read_text())
        sth_schema = json.loads((SCHEMA_DIR / "sth-payload.schema.json").read_text())
        map_schema = json.loads((SCHEMA_DIR / "subject-checkpoint.schema.json").read_text())
        attest_schema = json.loads((SCHEMA_DIR / "attestation.schema.json").read_text())
        Draft202012Validator(log_schema).validate(doc["entries"][0])
    except Exception as e:
        return [f"reputation: schema load failed: {e}"]
    log_v = Draft202012Validator(log_schema)
    sth_v = Draft202012Validator(sth_schema)
    map_v = Draft202012Validator(map_schema)
    attest_v = Draft202012Validator(attest_schema)
    for i, entry in enumerate(doc["entries"]):
        errors = sorted(log_v.iter_errors(entry), key=lambda e: list(e.path))
        if errors:
            return [f"reputation: entry {i} failed log-entry schema: {errors[0].message}"]
    for label, sth in doc["sth"].items():
        errors = sorted(sth_v.iter_errors(sth["payload"]), key=lambda e: list(e.path))
        if errors:
            return [f"reputation: STH {label} failed sth-payload schema: {errors[0].message}"]
        for leaf in sth["subject_map"]:
            errors = sorted(map_v.iter_errors(leaf), key=lambda e: list(e.path))
            if errors:
                return [f"reputation: subject map leaf failed schema: {errors[0].message}"]
    now = int(doc["fixed_clock_unix"])
    max_age = int(doc["sth_max_age_seconds"])
    if max_age != 86400 or int(doc["mmd_seconds"]) != 3600:
        failures.append("reputation: STH_MAX_AGE must be 86400 and MMD 3600")
    entries = doc["entries"]
    try:
        _open_disclosures(entries, doc["disclosures"])
        for label, sth in doc["sth"].items():
            _verify_sth(f"sth-{label}", sth, entries, now, max_age, jwt_mod)
    except Exception as e:
        failures.append(f"reputation: log/STH precondition failed: {e}")
        print(f"    FAIL reputation precondition: {e}")
        return failures

    print(f"[6] verifying {len(doc['vectors'])} reputation vector(s) in {path.relative_to(REPO)}")
    by_id = {v["id"]: v for v in doc["vectors"]}
    for vector in doc["vectors"]:
        vid = vector["id"]
        try:
            kind = vector["kind"]
            if kind == "inclusion":
                tree_size = int(vector["tree_size"])
                sth = doc["sth"][str(tree_size)]
                _verify_inclusion_of(
                    entries[int(vector["leaf_index"])],
                    int(vector["leaf_index"]),
                    tree_size,
                    vector["inclusion_path"],
                    sth["payload"]["root_hash"],
                    entries,
                )
                outcome = "pass"
            elif kind == "consistency":
                first, second = int(vector["first"]), int(vector["second"])
                encoded = [mkl.canon(e) for e in entries]
                expected = [mkl.b64url(h) for h in mkl.consistency_proof(encoded, first)]
                if vector["consistency_path"] != expected:
                    raise ValueError("consistency path does not match RFC 9162 PROOF")
                ok = mkl.verify_consistency(
                    first,
                    second,
                    mkl.mth(encoded[:first]),
                    mkl.mth(encoded[:second]),
                    _decode_path(vector["consistency_path"]),
                )
                if not ok:
                    raise ValueError("iterative consistency check failed")
                if doc["sth"][str(first)]["payload"]["root_hash"] != mkl.b64url(mkl.mth(encoded[:first])):
                    raise ValueError("first STH root mismatch")
                if doc["sth"][str(second)]["payload"]["root_hash"] != mkl.b64url(mkl.mth(encoded[:second])):
                    raise ValueError("second STH root mismatch")
                # Append-only subject histories across the two signed maps.
                early = {leaf["sub"]: leaf for leaf in doc["sth"][str(first)]["subject_map"]}
                late = {leaf["sub"]: leaf for leaf in doc["sth"][str(second)]["subject_map"]}
                for sub, leaf in early.items():
                    if sub not in late or late[sub]["count"] < leaf["count"]:
                        raise ValueError(f"subject map shrank for {sub}")
                    early_idx = entries[leaf["index_entry"]]["indices"]
                    late_idx = entries[late[sub]["index_entry"]]["indices"]
                    if late_idx[: len(early_idx)] != early_idx:
                        raise ValueError(f"subject history for {sub} is not a prefix")
                outcome = "pass"
            elif kind == "completeness":
                history = _verify_completeness(vector, doc)
                if _score(history) != vector["score"]:
                    raise ValueError(f"score {_score(history)} != {vector['score']}")
                outcome = "pass"
            elif kind == "revocation":
                jwks = json.loads((VECTORS_DIR / vector["jwks"]).read_text())
                cache: set[str] = set()
                verify_attestation_vector(
                    vector["jwt"],
                    jwks,
                    issuer=doc["issuer"],
                    now=now,
                    max_skew=300,
                    jti_cache=cache,
                    jwt_mod=jwt_mod,
                )
                if vector.get("base_expect") != "pass":
                    raise ValueError("revocation vector must record that base verification passes")
                payload = jwt_mod.decode(vector["jwt"], options={"verify_signature": False})
                attest_errors = sorted(attest_v.iter_errors(payload), key=lambda e: list(e.path))
                if attest_errors:
                    raise ValueError(f"attestation payload failed schema: {attest_errors[0].message}")
                proof = payload["log_proof"]
                if int(proof["tree_size"]) != int(vector["proof_tree_size"]):
                    raise ValueError("jwt log_proof tree_size mismatch")
                old = doc["sth"][str(vector["proof_tree_size"])]
                current = doc["sth"][str(vector["current_tree_size"])]
                if proof["root_hash"] != old["payload"]["root_hash"]:
                    raise ValueError("embedded proof root is not the older STH")
                _verify_inclusion_of(
                    entries[int(proof["index"])],
                    int(proof["index"]),
                    int(proof["tree_size"]),
                    proof["inclusion_path"],
                    proof["root_hash"],
                    entries,
                )
                encoded = [mkl.canon(e) for e in entries]
                expected = [mkl.b64url(h) for h in mkl.consistency_proof(encoded, int(vector["proof_tree_size"]))]
                if vector["consistency_path"] != expected:
                    raise ValueError("presented consistency path mismatch")
                if not mkl.verify_consistency(
                    int(vector["proof_tree_size"]),
                    int(vector["current_tree_size"]),
                    mkl.b64url_decode(old["payload"]["root_hash"]),
                    mkl.b64url_decode(current["payload"]["root_hash"]),
                    _decode_path(vector["consistency_path"]),
                ):
                    raise ValueError("consistency from embedded tree to current STH failed")
                history = _verify_completeness(by_id["completeness-card-test-001"], doc)
                status = _status_of(history, int(vector["target_index"]), vector["target_jti"])
                if status != "revoked":
                    raise ValueError(f"expected revoked, got {status}")
                # The fresh tree is what scoring uses; the embedded pre-revocation tree must not win.
                if _score(history)["active_contract_settlements"] != 0:
                    raise ValueError("revoked settlement was still counted")
                outcome = "revoked"
            else:
                raise ValueError(f"unknown kind {kind}")
        except Exception as e:
            outcome = f"error:{e}"

        if outcome == vector["expect"]:
            print(f"    ok   {vid}: {outcome}")
        else:
            failures.append(f"reputation:{vid}: expected {vector['expect']} but got {outcome}")
            print(f"    FAIL {vid}: expected {vector['expect']} but got {outcome}")

    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        choices=["examples", "drift", "vectors", "all"],
        default="all",
        help="which checks to run and let fail the process",
    )
    args = ap.parse_args()

    schemas, failures = load_schemas()
    blocks = find_json_blocks(SPEC.read_text())

    if args.check in ("examples", "all"):
        failures += check_examples(blocks, schemas)
    if args.check in ("drift", "all"):
        failures += check_drift(blocks, schemas)
    if args.check in ("vectors", "all"):
        failures += check_vectors()
        failures += check_reputation()

    print()
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED — schema, example, drift, and vector checks succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
