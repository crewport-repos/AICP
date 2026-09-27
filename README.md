# AICP: Agent Identity Card Protocol

**MCP identity profile and portable agent identity format.**

AICP standardizes (1) an **MCP profile** for platform-issued agent identity—OAuth enrollment, Card-bound MCP endpoints, read/write/commit scopes, and lifecycle-aware `tools/list` projection—and (2) a **portable identity format**—Card documents, `/.well-known/aicp.json`, and cross-platform JWS attestations.

```
MCP (generic):     Agent ──connects──► Tool server (`tools/list`; catalog may change)
AICP MCP Profile:  Platform ──issues──► Card ──projects──► Tools via standard MCP
AICP Identity:     Card + attestations ──verify──► Peer platforms (JWKS + JWS)
```

## Normative core

| Part | Conformance class | What it specifies |
|------|-------------------|-------------------|
| **A** | **AICP MCP Profile** | Enrollment, OAuth 2.1 authorization server, Card-scoped MCP, scope groups, tool projection, MCP `2025-06-18` interop |
| **B** | **AICP Identity Format** | Card schema, platform capability document, federation (JWKS, attestation signing, verification) |

## Informative profiles (optional, non-normative)

Marketplace discovery, phased fulfillment lifecycles, and work history are documented as **informative appendices** in the specification. Example tool names in those sections are not required for conformance.

## Key concepts

- **Card**: Platform-issued identity for one agent or agent group; multiple Cards per operator.
- **Tool projection**: The MCP `tools/list` result filtered by Card status (and optional lifecycle state)—still standard MCP, no custom methods.
- **Attestation**: JWS-signed reputation payload verifiable via issuer JWKS.

## Quick start for implementors

1. Publish `/.well-known/aicp.json` ([schema](spec/schemas/platform-capability.schema.json)) with `app_version` `0.2.0-draft` or later.
2. Implement enrollment and OAuth per **AICP MCP Profile** (§5).
3. Expose Card-scoped MCP with read/write/commit scopes and RFC 9728 metadata.
4. For **AICP Identity Format**, serve Cards matching [card.schema.json](spec/schemas/card.schema.json) and publish JWKS + attestations (§6.3).

## Specification

- **[Full specification](spec/AICP-v0.1.md)** — v0.2.0-draft
- **[Card schema](spec/schemas/card.schema.json)**
- **[Platform capability schema](spec/schemas/platform-capability.schema.json)** — `/.well-known/aicp.json`
- **[Attestation schema](spec/schemas/attestation.schema.json)**
- **[Agreement schema](spec/schemas/agreement.schema.json)** — informative lifecycle profile
- **[Audit event schema](spec/schemas/audit-event.schema.json)**

## Conformance

Claim **AICP MCP Profile**, **AICP Identity Format**, or both. See §3 of the specification for required sections. **AICP 1.0** will require two independent interoperable implementations per normative class.

## Implementations

| Implementation | AICP MCP Profile | AICP Identity Format |
|----------------|------------------|----------------------|
| [CrewPort](https://crewport.ai) | Partial | Partial |
| [Diskuss](https://diskuss.tech) (dev: [diskuss.dev](https://diskuss.dev)) | Partial | Partial |

## Relationship to MCP

AICP adds identity and policy on top of standard MCP—no custom JSON-RPC methods. Optional hints use `_meta["ai.crewport.aicp/*"]` or `experimental` under the same prefix. See §4 of the specification.

## License

[MIT](LICENSE)

## Contributing

AICP is an open protocol maintained by [CrewPort](https://github.com/crewport-repos/AICP). Feedback and contributions are welcome.
