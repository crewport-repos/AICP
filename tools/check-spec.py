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
SKEW_SECONDS = 300
EMPTY_ROOT = mkl.b64url(mkl.sha256(b""))


class ReputationFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _decode_path(items: list[str]) -> list[bytes]:
    out = []
    for item in items:
        raw = mkl.b64url_decode(item)
        if len(raw) != 32:
            raise ReputationFailure("inclusion_failed")
        out.append(raw)
    return out


def _sub_lt(left: str, right: str) -> bool:
    """Byte-wise UTF-8 order. No Unicode normalization."""
    return left.encode("utf-8") < right.encode("utf-8")


def _contains_float(node) -> bool:
    if isinstance(node, bool):
        return False
    if isinstance(node, float):
        return True
    if isinstance(node, dict):
        return any(_contains_float(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_float(v) for v in node)
    return False


def _history_root(history: list[dict]) -> str:
    return mkl.b64url(mkl.mth([mkl.canon(e) for e in history]))


def _match_target(history: list[dict], entry: dict) -> dict:
    """Revocation and correction match target_index AND target_jti to one attestation."""
    target_index = entry.get("target_index")
    target_jti = entry.get("target_jti")
    if not isinstance(target_index, int) or not isinstance(target_jti, str):
        raise ReputationFailure("history_malformed")
    found = None
    for item in history:
        if item["index"] == target_index:
            found = item["entry"]
            break
    if found is None or found.get("entry_type") != "attestation" or found.get("jti") != target_jti:
        raise ReputationFailure("history_malformed")
    if found.get("sub") != entry.get("sub"):
        raise ReputationFailure("history_malformed")
    return found


def _status_of(history: list[dict], target_index: int, target_jti: str) -> str:
    for item in history:
        if item["entry"]["entry_type"] in ("revocation", "correction"):
            _match_target(history, item["entry"])
    found = False
    status = "active"
    for item in history:
        entry = item["entry"]
        if item["index"] == target_index:
            if entry.get("jti") != target_jti or entry.get("entry_type") != "attestation":
                raise ReputationFailure("history_malformed")
            found = True
            continue
        if not found:
            continue
        if entry["entry_type"] not in ("revocation", "correction"):
            continue
        targeted = _match_target(history, entry)
        if targeted.get("jti") != target_jti:
            continue
        if entry["entry_type"] == "revocation":
            return "revoked"
        status = "corrected"
    if not found:
        raise ReputationFailure("history_incomplete")
    return status


def _latest_correction(history: list[dict], target_index: int, target_jti: str) -> dict | None:
    latest = None
    for item in history:
        entry = item["entry"]
        if item["index"] <= target_index or entry["entry_type"] != "correction":
            continue
        targeted = _match_target(history, entry)
        if targeted.get("jti") == target_jti:
            latest = item
    return latest


def _opened_detail(disclosures: list[dict], index: int) -> dict:
    for item in disclosures:
        if item["index"] == index:
            return item["detail"]
    raise ReputationFailure("disclosure_missing")


def _counts_contract(entry: dict, status: str, history: list[dict], disclosures: list[dict], index: int) -> bool:
    event = entry.get("event")
    if status == "revoked" or event == "rating" or event == "dispute_result":
        return False
    if event == "contract_completed":
        if status == "active":
            return True
        if status != "corrected":
            return False
        corr = _latest_correction(history, index, entry["jti"])
        if corr is None:
            return False
        return _opened_detail(disclosures, corr["index"]).get("outcome") == "fulfilled"
    if event == "outcome":
        if status == "corrected":
            corr = _latest_correction(history, index, entry["jti"])
            if corr is None:
                return False
            detail = _opened_detail(disclosures, corr["index"])
        elif status == "active":
            detail = _opened_detail(disclosures, index)
        else:
            return False
        return detail.get("outcome") == "fulfilled"
    return False


def _score(history: list[dict], disclosures: list[dict] | None = None) -> dict:
    """Distinct settlement hashes. contract_completed counts; outcome counts only if fulfilled."""
    disclosures = disclosures or []
    revoked_jtis: list[str] = []
    settlements: set[str] = set()
    counterparties: set[str] = set()
    for item in history:
        entry = item["entry"]
        if entry["entry_type"] != "attestation":
            continue
        status = _status_of(history, item["index"], entry["jti"])
        if status == "revoked":
            revoked_jtis.append(entry["jti"])
            continue
        if entry.get("event") == "rating":
            continue
        if not _counts_contract(entry, status, history, disclosures, item["index"]):
            continue
        settlements.add(entry["settlement_hash"])
        counterparties.add(entry["counterparty_id"])
    return {
        "active_contract_settlements": len(settlements),
        "distinct_counterparties": len(counterparties),
        "revoked_jtis": revoked_jtis,
    }


def _decode_sth(token: str, jwks: dict, now: int, jwt_mod) -> dict:
    header = jwt_mod.get_unverified_header(token)
    alg = header.get("alg")
    if alg not in ("ES256", "EdDSA"):
        raise ReputationFailure("sth_alg")
    if header.get("typ") != "aicp-sth+jwt":
        raise ReputationFailure("sth_typ")
    keys = {k["kid"]: k for k in jwks.get("keys", [])}
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in keys:
        raise ReputationFailure("sth_alg")
    key = _jwks_to_key(keys[kid], jwt_mod)
    payload = jwt_mod.decode(
        token,
        key,
        algorithms=[alg],
        options={"verify_aud": False, "verify_exp": False, "verify_iat": False},
    )
    if payload.get("hash_alg") != "SHA-256" or payload.get("sth_version") != 1:
        raise ReputationFailure("sth_typ")
    if payload.get("aud") != payload.get("log_id"):
        raise ReputationFailure("sth_typ")
    if int(payload.get("iat")) != int(payload.get("timestamp")):
        raise ReputationFailure("sth_typ")
    if int(payload.get("exp")) != int(payload["timestamp"]) + 86400:
        raise ReputationFailure("sth_typ")
    if "subject_map_size" not in payload:
        raise ReputationFailure("sth_typ")
    timestamp = int(payload["timestamp"])
    if now > timestamp + 86400 + SKEW_SECONDS or timestamp > now + SKEW_SECONDS:
        raise ReputationFailure("sth_stale")
    payload["tree_size"] = int(payload["tree_size"])
    payload["subject_map_size"] = int(payload["subject_map_size"])
    payload["timestamp"] = timestamp
    return payload


def _accept_sth(store: dict[str, list[dict]], sth: dict) -> None:
    """Persist a fresh STH. Same-size disagreement is a fork. A smaller tree is not."""
    previous = store.setdefault(sth["log_id"], [])
    for old in previous:
        if sth["tree_size"] == old["tree_size"]:
            same = (
                sth["root_hash"] == old["root_hash"]
                and sth["subject_map_root"] == old["subject_map_root"]
                and sth["subject_map_size"] == old["subject_map_size"]
            )
            if not same:
                raise ReputationFailure("split_view")
    if any(old.get("jti") == sth.get("jti") for old in previous):
        return
    previous.append(sth)


def _reconcile_older(held: list[dict], current: dict, issuer_proofs: dict[tuple[int, int], list[str]] | None) -> None:
    """Decide freeze from the issuer consistency endpoint, never from the presenter's path.

    Fetch a proof only for exactly the two held sizes. tree_size 0 is a prefix of every
    later tree. A missing issuer proof for the pair means ignore the older STH and do not
    freeze. An empty proof array is a failed proof and is `split_view`.
    """
    older = [sth for sth in held if sth["tree_size"] < current["tree_size"]]
    if not older:
        return
    proofs = issuer_proofs or {}
    current_root = mkl.b64url_decode(current["root_hash"])
    for sth in older:
        if sth["tree_size"] == 0:
            continue
        pair = (sth["tree_size"], current["tree_size"])
        if pair not in proofs:
            continue
        path = proofs[pair]
        if not path:
            raise ReputationFailure("split_view")
        try:
            nodes = _decode_path(path)
        except ReputationFailure:
            raise ReputationFailure("split_view") from None
        if not mkl.verify_consistency(
            sth["tree_size"],
            current["tree_size"],
            mkl.b64url_decode(sth["root_hash"]),
            current_root,
            nodes,
        ):
            raise ReputationFailure("split_view")


def _issuer_consistency_proofs(entries: list[dict]) -> dict[tuple[int, int], list[str]]:
    """What GET consistency?first=&second= returns for the fixture log. Not a presenter path."""
    encoded = [mkl.canon(entry) for entry in entries]
    proofs: dict[tuple[int, int], list[str]] = {}
    n = len(encoded)
    for first in range(1, n):
        for second in range(first + 1, n + 1):
            proofs[(first, second)] = [mkl.b64url(node) for node in mkl.consistency_proof(encoded[:second], first)]
    return proofs


def _issuer_proofs_for_vector(entries: list[dict], vector: dict) -> dict[tuple[int, int], list[str]]:
    """Fixture issuer proofs, with optional per-vector overrides for grading."""
    proofs = _issuer_consistency_proofs(entries)
    overrides = vector.get("issuer_proof_overrides")
    if not overrides:
        return proofs
    for key, value in overrides.items():
        first_s, second_s = key.split(",", 1)
        pair = (int(first_s), int(second_s))
        if value is None:
            proofs.pop(pair, None)
        else:
            proofs[pair] = value
    return proofs


def _verify_leaf(index: int, tree_size: int, entry: dict, path: list[str], root_b64: str, code: str) -> None:
    leaf = mkl.leaf_hash(mkl.canon(entry))
    try:
        nodes = _decode_path(path)
    except ReputationFailure:
        raise ReputationFailure(code) from None
    if not mkl.verify_inclusion(index, tree_size, leaf, nodes, mkl.b64url_decode(root_b64)):
        raise ReputationFailure(code)


def _check_disclosures(entries: list[dict], disclosures: list[dict]) -> None:
    seen: set[int] = set()
    by_index = {item["index"]: item["entry"] for item in entries}
    for item in disclosures:
        index = item["index"]
        if index in seen or index not in by_index:
            raise ReputationFailure("disclosure_mismatch")
        seen.add(index)
        if _contains_float(item.get("detail")):
            raise ReputationFailure("disclosure_mismatch")
        try:
            salt = mkl.b64url_decode(item["salt"])
        except Exception as exc:
            raise ReputationFailure("disclosure_mismatch") from exc
        if len(salt) < 16:
            raise ReputationFailure("disclosure_mismatch")
        try:
            got = mkl.detail_commit(salt, item["detail"])
        except ValueError as exc:
            raise ReputationFailure("disclosure_mismatch") from exc
        if got != by_index[index].get("detail_commit"):
            raise ReputationFailure("disclosure_mismatch")
    for item in entries:
        entry = item["entry"]
        must = entry["entry_type"] in ("revocation", "correction") or entry.get("event") in (
            "dispute_result",
            "outcome",
        )
        if must and item["index"] not in seen:
            raise ReputationFailure("disclosure_missing")


def _verify_neighbor(neighbor: dict, signed_size: int, map_root: str) -> None:
    _verify_leaf(
        int(neighbor["map_index"]),
        signed_size,
        neighbor["checkpoint"],
        neighbor["map_inclusion_path"],
        map_root,
        "map_proof_failed",
    )


def _verify_non_inclusion(proof: dict, sth: dict) -> None:
    claimed = proof["sub"]
    size = int(sth["subject_map_size"])
    root = sth["subject_map_root"]
    left = proof.get("left")
    right = proof.get("right")
    if size == 0:
        if root != EMPTY_ROOT or left or right:
            raise ReputationFailure("map_proof_failed")
        return
    if left and right:
        if int(right["map_index"]) != int(left["map_index"]) + 1:
            raise ReputationFailure("map_proof_failed")
        _verify_neighbor(left, size, root)
        _verify_neighbor(right, size, root)
        if not (
            _sub_lt(left["checkpoint"]["sub"], claimed)
            and _sub_lt(claimed, right["checkpoint"]["sub"])
        ):
            raise ReputationFailure("map_proof_failed")
        if not _sub_lt(left["checkpoint"]["sub"], right["checkpoint"]["sub"]):
            raise ReputationFailure("map_proof_failed")
        return
    if right and not left:
        if int(right["map_index"]) != 0:
            raise ReputationFailure("map_proof_failed")
        _verify_neighbor(right, size, root)
        if not _sub_lt(claimed, right["checkpoint"]["sub"]):
            raise ReputationFailure("map_proof_failed")
        return
    if left and not right:
        if int(left["map_index"]) != size - 1:
            raise ReputationFailure("map_proof_failed")
        _verify_neighbor(left, size, root)
        if not _sub_lt(left["checkpoint"]["sub"], claimed):
            raise ReputationFailure("map_proof_failed")
        return
    raise ReputationFailure("map_proof_failed")


def _verify_history(hp: dict, sth: dict, disclosures: list[dict], earlier_indices: list[int] | None) -> list[dict]:
    if "non_inclusion" in hp:
        if hp.get("entries"):
            raise ReputationFailure("history_malformed")
        _verify_non_inclusion(hp["non_inclusion"], sth)
        return []
    if int(hp["map_size"]) != int(sth["subject_map_size"]):
        raise ReputationFailure("map_proof_failed")
    _verify_neighbor(
        {
            "checkpoint": hp["checkpoint"],
            "map_index": hp["map_index"],
            "map_inclusion_path": hp["map_inclusion_path"],
        },
        int(sth["subject_map_size"]),
        sth["subject_map_root"],
    )
    checkpoint = hp["checkpoint"]
    index_entry = hp["index_entry"]
    _verify_leaf(
        int(checkpoint["index_entry"]),
        int(sth["tree_size"]),
        index_entry,
        hp["index_entry_inclusion_path"],
        sth["root_hash"],
        "inclusion_failed",
    )
    indices = list(index_entry["indices"])
    if index_entry.get("count") != len(indices) or checkpoint.get("count") != len(indices):
        raise ReputationFailure("history_malformed")
    if not indices or checkpoint.get("latest_entry") != indices[-1]:
        raise ReputationFailure("history_malformed")
    if int(checkpoint["index_entry"]) <= int(checkpoint["latest_entry"]):
        raise ReputationFailure("history_malformed")
    if checkpoint.get("history_root") != index_entry.get("history_root"):
        raise ReputationFailure("history_incomplete")
    if checkpoint.get("sub") != index_entry.get("sub"):
        raise ReputationFailure("history_malformed")
    presented = hp["entries"]
    if [item["index"] for item in presented] != indices:
        raise ReputationFailure("history_incomplete")
    bodies = [item["entry"] for item in presented]
    if _history_root(bodies) != checkpoint["history_root"]:
        raise ReputationFailure("history_incomplete")
    for item in presented:
        entry = item["entry"]
        if entry.get("entry_type") not in REPUTATION_TYPES or entry.get("sub") != checkpoint["sub"]:
            raise ReputationFailure("history_malformed")
    if earlier_indices is not None and indices[: len(earlier_indices)] != list(earlier_indices):
        raise ReputationFailure("history_malformed")
    _check_disclosures(presented, disclosures)
    return presented


def _verify_attestation_unscoped(token: str, jwks: dict, issuer: str, now: int, jwt_mod) -> dict:
    header = jwt_mod.get_unverified_header(token)
    if header.get("alg") not in ("ES256", "EdDSA"):
        raise ReputationFailure("attestation_alg")
    payload = jwt_mod.decode(token, options={"verify_signature": False})
    keys = {k["kid"]: k for k in jwks.get("keys", [])}
    kid = header.get("kid")
    if not kid or kid != payload.get("kid") or kid not in keys:
        raise ReputationFailure("attestation_alg")
    if payload.get("iss") != issuer or payload.get("aud") != issuer:
        raise ReputationFailure("attestation_alg")
    key = _jwks_to_key(keys[kid], jwt_mod)
    jwt_mod.decode(
        token,
        key,
        algorithms=[header["alg"]],
        options={"verify_aud": False, "verify_exp": False, "verify_iat": False},
    )
    iat = int(payload["iat"])
    exp = int(payload["exp"])
    if iat > now + SKEW_SECONDS or exp < now - SKEW_SECONDS:
        raise ReputationFailure("attestation_alg")
    if not payload.get("jti"):
        raise ReputationFailure("attestation_alg")
    return payload


def evaluate_presentation(
    presentation: dict,
    held_sths: list[str],
    jwks: dict,
    now: int,
    jwt_mod,
    jti_cache: set[str],
    earlier_indices: list[int] | None = None,
    issuer_proofs: dict[tuple[int, int], list[str]] | None = None,
) -> dict:
    """Presentation-only verifier. Issuer consistency proofs are a separate fetch."""
    tokens = list(held_sths)
    if presentation.get("sth"):
        tokens.append(presentation["sth"])
    decoded: list[dict] = []
    errors: list[str] = []
    for token in tokens:
        try:
            decoded.append(_decode_sth(token, jwks, now, jwt_mod))
        except ReputationFailure as exc:
            errors.append(exc.code)
    if not decoded:
        if not tokens:
            raise ReputationFailure("log_unreachable")
        raise ReputationFailure(errors[-1])
    store: dict[str, list[dict]] = {}
    for sth in decoded:
        _accept_sth(store, sth)
    log_ids = {sth["log_id"] for sth in decoded}
    if len(log_ids) != 1:
        raise ReputationFailure("log_unreachable")
    held = store[next(iter(log_ids))]
    current = max(held, key=lambda item: (item["tree_size"], item["timestamp"]))
    _reconcile_older(held, current, issuer_proofs)
    history_proof = presentation["history_proof"]
    if "non_inclusion" in history_proof:
        _verify_history(history_proof, current, presentation.get("disclosures") or [], earlier_indices)
        return {"jti_recorded": False, "status": "absent"}
    token = presentation["attestation"]
    payload = _verify_attestation_unscoped(token, jwks, current["iss"], now, jwt_mod)
    proof = payload.get("log_proof")
    if not isinstance(proof, dict) or "entry_jti" not in proof:
        raise ReputationFailure("inclusion_failed")
    if proof.get("log_id") != current["log_id"]:
        raise ReputationFailure("log_unreachable")
    proof_size = int(proof["tree_size"])
    if proof_size > current["tree_size"]:
        raise ReputationFailure("sth_behind")
    if proof_size == current["tree_size"]:
        if proof.get("root_hash") != current["root_hash"]:
            raise ReputationFailure("split_view")
    else:
        path = presentation.get("consistency_path")
        if not path:
            raise ReputationFailure("consistency_failed")
        try:
            nodes = _decode_path(path)
        except ReputationFailure:
            raise ReputationFailure("consistency_failed") from None
        if not mkl.verify_consistency(
            proof_size,
            current["tree_size"],
            mkl.b64url_decode(proof["root_hash"]),
            mkl.b64url_decode(current["root_hash"]),
            nodes,
        ):
            raise ReputationFailure("consistency_failed")
    history = _verify_history(history_proof, current, presentation.get("disclosures") or [], earlier_indices)
    matched = [item for item in history if item["index"] == int(proof["index"])]
    if len(matched) != 1:
        raise ReputationFailure("inclusion_failed")
    entry = matched[0]["entry"]
    if entry.get("jti") != proof.get("entry_jti") or entry.get("sub") != payload.get("sub"):
        raise ReputationFailure("inclusion_failed")
    if entry.get("iss") != payload.get("iss") or entry.get("entry_type") != "attestation":
        raise ReputationFailure("inclusion_failed")
    _verify_leaf(
        int(proof["index"]),
        proof_size,
        entry,
        proof["inclusion_path"],
        proof["root_hash"],
        "inclusion_failed",
    )
    status = _status_of(history, int(proof["index"]), entry["jti"])
    jti = payload["jti"]
    if jti in jti_cache:
        raise ReputationFailure("replayed_jti")
    jti_cache.add(jti)
    return {
        "jti_recorded": True,
        "score": _score(history, presentation.get("disclosures") or []),
        "status": status,
    }


def _monitor_log(doc: dict, jwt_mod) -> None:
    """Full-log monitor checks. Not used to grade presentations."""
    entries = doc["entries"]
    encoded = [mkl.canon(e) for e in entries]
    subs_seen: list[str] = []
    for label, sth in doc["sth"].items():
        payload = sth["payload"]
        if sth["document"]["tree_size"] != payload["tree_size"]:
            raise ValueError(f"sth {label} document tree_size")
        if int(payload["iat"]) != int(payload["timestamp"]):
            raise ValueError("sth iat")
        if "subject_map_size" not in payload:
            raise ValueError("subject_map_size missing")
        size = int(payload["tree_size"])
        if size == 0:
            if payload["root_hash"] != EMPTY_ROOT or payload["subject_map_root"] != EMPTY_ROOT:
                raise ValueError("empty root")
            if int(payload["subject_map_size"]) != 0:
                raise ValueError("empty map size")
            continue
        if payload["root_hash"] != mkl.b64url(mkl.mth(encoded[:size])):
            raise ValueError(f"monitor root {label}")
        leaves = sth["subject_map"]
        if int(payload["subject_map_size"]) != len(leaves):
            raise ValueError("map size")
        subs = [leaf["sub"] for leaf in leaves]
        if subs != sorted(subs, key=lambda s: s.encode("utf-8")) or len(subs) != len(set(subs)):
            raise ValueError("subject tree order")
        for i in range(1, len(subs)):
            if not _sub_lt(subs[i - 1], subs[i]):
                raise ValueError("subject subs not increasing")
        leaf_bytes = [mkl.canon(leaf) for leaf in leaves]
        if payload["subject_map_root"] != mkl.b64url(mkl.mth(leaf_bytes)):
            raise ValueError("monitor subject root")
        subs_seen = subs
    # Append-only histories for subs present in both size-4 and size-6 maps.
    early = {leaf["sub"]: leaf for leaf in doc["sth"]["4"]["subject_map"]}
    late = {leaf["sub"]: leaf for leaf in doc["sth"]["6"]["subject_map"]}
    for sub, leaf in early.items():
        if sub not in late:
            raise ValueError(f"subject disappeared {sub}")
        early_idx = entries[leaf["index_entry"]]["indices"]
        late_idx = entries[late[sub]["index_entry"]]["indices"]
        if late_idx[: len(early_idx)] != early_idx:
            raise ValueError(f"history shrank {sub}")
    by_index = {item["index"]: item for item in doc["disclosures"]}
    for item in doc["disclosures"]:
        salt = mkl.b64url_decode(item["salt"])
        if mkl.detail_commit(salt, item["detail"]) != entries[item["index"]]["detail_commit"]:
            raise ValueError("monitor detail")
    for item in doc["monitor_settlements"]:
        salt = mkl.b64url_decode(item["settlement_salt"])
        got = mkl.settlement_hash(salt, item["evidence"])
        if got != entries[item["index"]]["settlement_hash"]:
            raise ValueError("monitor settlement")
        if "settlement_evidence" in by_index.get(item["index"], {}):
            raise ValueError("settlement evidence leaked into disclosures")
    del subs_seen


def _earlier_from_prior(prior: dict, held: list[str], disclosures: list[dict], jwks: dict, now: int, jwt_mod) -> list[int]:
    """Verify a previously accepted history proof against a held STH. No full-log oracle."""
    decoded = []
    last = ReputationFailure("consistency_failed")
    for token in held:
        try:
            decoded.append(_decode_sth(token, jwks, now, jwt_mod))
        except ReputationFailure as exc:
            last = exc
    for sth in decoded:
        try:
            hist = _verify_history(prior, sth, disclosures, None)
            return [item["index"] for item in hist]
        except ReputationFailure as exc:
            last = exc
    raise last


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
    except Exception as exc:
        return [f"reputation: merkle self-check failed: {exc}"]

    doc = json.loads(path.read_text())
    if int(doc["sth_max_age_seconds"]) != 86400 or int(doc["mmd_seconds"]) != 3600:
        failures.append("reputation: STH_MAX_AGE must be 86400 and MMD 3600")
    try:
        log_schema = json.loads((SCHEMA_DIR / "log-entry.schema.json").read_text())
        sth_schema = json.loads((SCHEMA_DIR / "sth-payload.schema.json").read_text())
        map_schema = json.loads((SCHEMA_DIR / "subject-checkpoint.schema.json").read_text())
        pres_schema = json.loads((SCHEMA_DIR / "reputation-presentation.schema.json").read_text())
        detail_schema = json.loads((SCHEMA_DIR / "detail.schema.json").read_text())
    except Exception as exc:
        return [f"reputation: schema load failed: {exc}"]
    log_v = Draft202012Validator(log_schema)
    sth_v = Draft202012Validator(sth_schema)
    map_v = Draft202012Validator(map_schema)
    pres_v = Draft202012Validator(pres_schema)
    detail_v = Draft202012Validator(detail_schema)
    for i, entry in enumerate(doc["entries"]):
        errors = sorted(log_v.iter_errors(entry), key=lambda err: list(err.path))
        if errors:
            return [f"reputation: entry {i} failed log-entry schema: {errors[0].message}"]
    for label, sth in doc["sth"].items():
        errors = sorted(sth_v.iter_errors(sth["payload"]), key=lambda err: list(err.path))
        if errors:
            return [f"reputation: STH {label} failed sth-payload schema: {errors[0].message}"]
        for leaf in sth["subject_map"]:
            errors = sorted(map_v.iter_errors(leaf), key=lambda err: list(err.path))
            if errors:
                return [f"reputation: subject checkpoint failed schema: {errors[0].message}"]
    for item in doc["disclosures"]:
        errors = sorted(detail_v.iter_errors(item["detail"]), key=lambda err: list(err.path))
        if errors:
            return [f"reputation: detail schema: {errors[0].message}"]
    jwks = json.loads((VECTORS_DIR / "jwks-es256.json").read_text())
    now = int(doc["fixed_clock_unix"])
    try:
        _monitor_log(doc, jwt_mod)
    except Exception as exc:
        failures.append(f"reputation: monitor precondition failed: {exc}")
        print(f"    FAIL monitor: {exc}")
        return failures

    print(f"[6] verifying {len(doc['vectors'])} reputation vector(s) in {path.relative_to(REPO)}")
    for vector in doc["vectors"]:
        vid = vector["id"]
        cache: set[str] = set()
        try:
            if vector.get("base_expect") == "pass":
                base_cache: set[str] = set()
                verify_attestation_vector(
                    vector["presentation"]["attestation"],
                    jwks,
                    issuer=doc["issuer"],
                    now=now,
                    max_skew=SKEW_SECONDS,
                    jti_cache=base_cache,
                    jwt_mod=jwt_mod,
                )
            if vector["expect"] in ("pass", "revoked"):
                errors = sorted(
                    pres_v.iter_errors(vector["presentation"]),
                    key=lambda err: list(err.path),
                )
                if errors:
                    raise ReputationFailure(f"schema:{errors[0].message}")
            earlier = vector.get("earlier_indices")
            if vector.get("prior_history_proof"):
                earlier = _earlier_from_prior(
                    vector["prior_history_proof"],
                    vector.get("held_sths") or [],
                    vector["presentation"].get("disclosures") or [],
                    jwks,
                    now,
                    jwt_mod,
                )
            result = evaluate_presentation(
                vector["presentation"],
                vector.get("held_sths") or [],
                jwks,
                now,
                jwt_mod,
                cache,
                earlier,
                _issuer_proofs_for_vector(doc["entries"], vector),
            )
            if vector["expect"] == "reject":
                outcome = "error:accepted"
            elif vector["expect"] == "revoked":
                outcome = "revoked" if result["status"] == "revoked" else f"error:{result['status']}"
                if outcome == "revoked" and vector.get("score") and result.get("score") != vector["score"]:
                    outcome = f"error:score {result.get('score')}"
            else:
                outcome = "pass"
                if vector.get("status") and result["status"] != vector["status"]:
                    outcome = f"error:status {result['status']}"
                if vector.get("score") and result.get("score") != vector["score"]:
                    outcome = f"error:score {result.get('score')}"
            if vector["expect"] == "reject":
                pass
            elif not result["jti_recorded"] and vector["expect"] != "pass":
                outcome = "error:jti"
            elif vector["expect"] == "pass" and result["status"] == "absent":
                pass
            elif vector["expect"] in ("pass", "revoked") and not result["jti_recorded"] and result["status"] != "absent":
                outcome = "error:jti not recorded"
        except ReputationFailure as exc:
            outcome = "reject" if vector["expect"] == "reject" and exc.code == vector.get("reason") else f"error:{exc.code}"
            if vector["expect"] == "reject" and exc.code == vector.get("reason"):
                if cache:
                    outcome = "error:jti consumed on reject"
                else:
                    outcome = "reject"
        except Exception as exc:
            outcome = f"error:{exc}"

        if outcome == vector["expect"]:
            print(f"    ok   {vid}: {outcome}" + (f" ({vector.get('reason')})" if outcome == "reject" else ""))
        else:
            failures.append(
                f"reputation:{vid}: expected {vector['expect']}"
                + (f"/{vector.get('reason')}" if vector["expect"] == "reject" else "")
                + f" but got {outcome}"
            )
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
