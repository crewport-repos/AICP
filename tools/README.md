# tools/

`check-spec.py` is the only automated check in this repository. It answers one
question: **does the spec agree with its own schemas?**

```sh
python3 -m venv .venv
.venv/bin/pip install --no-deps -r tools/requirements.txt
.venv/bin/python tools/check-spec.py
```

## What it checks

1. **parse** — every `spec/schemas/*.schema.json` is well-formed JSON.
2. **meta** — every schema declares draft 2020-12 and validates against that
   meta-schema.
3. **examples** — every fenced ` ```json ` block in `spec/AICP-v0.1.md` is
   validated against the schema it declares it illustrates.
4. **drift** — a schema reproduced inside the prose is compared against the
   schema file it copies.
5. **vectors** — `spec/test-vectors/vectors.json` attestation JWTs (§6.3.4.1).
6. **reputation** — `spec/test-vectors/reputation-vectors.json`: RFC 9162
   inclusion, consistency, per-subject completeness, and revocation (§6.4).
   Log entries in that file are also checked against `log-entry.schema.json`.

## How an example is bound to a schema

A fenced code block in markdown does not say which schema it illustrates, and
**guessing from the surrounding prose is not acceptable** — a heuristic that
happens to be right today produces a green check carrying no signal tomorrow.

So the binding is written on the fence itself, where it sits beside the example,
moves with it, and shows up in the diff when either one changes:

````
```json aicp:instance=platform-capability
{ "app_version": "0.1.0", ... }
```
````

Three forms are allowed:

| Binding | Meaning |
|---|---|
| `aicp:instance=<name>` | This block is a document that MUST validate against `spec/schemas/<name>.schema.json`. |
| `aicp:schema=<name>` | This block **is** a JSON Schema, reproduced in the prose. It must be a valid 2020-12 schema and must not disagree with `spec/schemas/<name>.schema.json` about what is valid. |
| `aicp:none` | This block illustrates something AICP defines no schema for — an HTTP envelope, a JWKS, a registry listing. |

A ` ```json ` block with **no** `aicp:` binding is a hard failure. `aicp:none`
is a deliberate, reviewable claim that lands in code review; leaving a block
unannotated is not. The checker prints how many blocks it checked and asserts
that number equals how many fenced json blocks exist, so a block can never be
silently skipped — in a green run, an unchecked example and a passing example
must not look the same.

The binding was put on the fence rather than in a separate manifest because a
manifest has to identify blocks by position ("the 7th json block"), and every
insertion into the prose silently repoints every entry after it. The fence
annotation cannot drift from the example it describes.

## Adding an example

Annotate the fence. If it illustrates no AICP schema, say `aicp:none`. If you
add a new schema to `spec/schemas/`, it is picked up automatically.

## Why `description` is ignored by the drift check

Drift compares what a schema *means*, not how it reads: `description` and `$id`
are dropped before comparing, so only differences that change which documents
are valid — `required`, `type`, `enum`, the set of properties — can fail the
check. The stripping is context-aware: a property *named* `description` is
data, not an annotation, and is compared like any other property.
