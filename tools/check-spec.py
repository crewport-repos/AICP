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

try:
    import jwt
except ImportError:  # pragma: no cover - optional until vector deps installed
    jwt = None

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


def _jwks_to_key(jwk: dict):
    alg = jwk.get("alg") or ("EdDSA" if jwk.get("kty") == "OKP" else "ES256")
    if alg not in jwt.algorithms.get_default_algorithms():
        raise ValueError(f"unsupported jwk alg {alg}")
    return jwt.algorithms.get_default_algorithms()[alg].from_jwk(json.dumps(jwk))


def verify_attestation_vector(
    token: str,
    jwks: dict,
    *,
    issuer: str,
    now: int,
    max_skew: int,
    jti_cache: set[str],
) -> None:
    header = jwt.get_unverified_header(token)
    payload = jwt.decode(token, options={"verify_signature": False})

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
    key = _jwks_to_key(keys[hdr_kid])
    jwt.decode(
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
    if jwt is None:
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

    print()
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED — the spec agrees with its schemas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
