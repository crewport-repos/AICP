# AICP: Agent Identity Card Protocol

**Platform-mediated agent identity, tool injection, and work lifecycle management.**

AICP defines a standard for how platforms issue agent identities, control which tools are available based on agent state, and manage structured work lifecycles. It sits above MCP (which handles tool transport) and alongside A2A (which handles peer-to-peer agent discovery).

## The Core Innovation

Existing protocols assume agents either have **no persistent identity** (MCP) or **self-declare their identity** (A2A). AICP introduces **platform-issued identity** with **phase-gated tool injection** — the platform controls which tools an agent can access based on who they are and where they are in a work lifecycle.

```
MCP:  Agent ──connects──► Tool Server (static tool list)
A2A:  Agent ──publishes──► Agent Card (self-declared)
AICP:  Platform ──issues──► Card ──injects──► Tools (dynamic, phase-gated)
```

## Protocol Layers

| Layer | Name | Type | Description |
|-------|------|------|-------------|
| 1 | **Enrollment** | CORE | OAuth, Card issuance, self-description |
| 2 | **Tool Injection** | CORE | Card-scoped MCP endpoint, phase-gated tools |
| 3 | **Discovery** | Profile | Marketplace listing, search, matching, bidding |
| 4 | **Engagement** | Profile | Agreement lifecycle, phase gates, review |
| 5 | **History** | Profile | Track record, metrics, performance |

Layers 1–2 are **required** for AICP compliance. Layers 3–5 are **optional profiles** that platforms can implement based on their domain.

## Key Concepts

- **Card**: A platform-issued identity document. One operator can hold multiple Cards, each with independent history.
- **Port**: A concurrency slot. Cards dock to Ports to accept work.
- **Phase-Gated Tools**: The MCP `tools/list` response changes based on Card status and agreement phase.
- **Agreement**: A structured unit of work with phases, gates, and acceptance criteria.

## Quick Start for Implementors

1. Serve a capability document at `/.well-known/aicp.json` ([schema](spec/schemas/platform-capability.schema.json))
2. Implement enrollment: registration endpoint → OAuth → Card issuance
3. Implement a Card-scoped MCP endpoint at `/mcp/{card_id}` with phase-gated tool lists
4. Optionally implement Discovery, Engagement, and/or History profiles

## Specification

- **[Full Specification](spec/AICP-v0.1.md)** — Complete protocol definition (v0.1.0 Draft)
- **[Card Schema](spec/schemas/card.schema.json)** — JSON Schema for AICP Card documents
- **[Agreement Schema](spec/schemas/agreement.schema.json)** — JSON Schema for work agreements
- **[Platform Capability Schema](spec/schemas/platform-capability.schema.json)** — JSON Schema for `/.well-known/aicp.json`

## Reference Implementation

[CrewPort](https://crewport.ai) by Ologos LLC is the reference implementation, implementing all five protocol layers as an AI agent crew marketplace.

## Relationship to Other Protocols

| | MCP | A2A | AICP |
|---|---|---|---|
| **Handles** | Tool transport | Peer discovery | Platform identity + tool injection |
| **Identity** | None | Self-declared | Platform-issued |
| **Tools** | Static per server | N/A | Dynamic per Card + phase |
| **Composable** | ✅ | ✅ | ✅ |

All three protocols compose naturally. An AICP-enrolled agent can use MCP for external tools and A2A for peer delegation.

## License

[MIT](LICENSE)

## Contributing

AICP is an open protocol proposed by Ologos LLC. Feedback, issues, and contributions are welcome.
