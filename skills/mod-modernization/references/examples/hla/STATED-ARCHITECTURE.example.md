# Stated Target Architecture — Orders Platform

<!--
  This is an EXAMPLE of an HLA (High-Level Architecture) document. Drop one or
  more `*.md` files like this under `<project>/.foundry/hla/` and the chain reads
  them (load_hla) to ground an "Architecture Conformance" section — it compares
  the discovered system against your STATED intent.

  Freeform Markdown: describe the target layers, bounded-context boundaries,
  chosen tech, and the rules the modernization must honor. No fixed schema — the
  chain captures the raw text and cites it. Absent any .foundry/hla/ file, the
  Architecture Conformance section is simply omitted (byte-identical).
-->

## Layers

- **Edge / UI** — React SPA, server-rendered nothing; all data via REST.
- **API** — Spring Boot services, one per bounded context, no shared mutable state.
- **Data** — PostgreSQL; each service owns its schema; no cross-service SQL.

## Bounded contexts (target)

| Context | Owns | Must NOT touch |
|---------|------|----------------|
| Orders | orders, order_lines | customer PII, inventory writes |
| Catalog | products, categories | orders, pricing overrides |
| Customer | customers, addresses | orders, catalog |

## Architecture rules (conformance is checked against these)

1. No context reads another context's tables directly — only via its REST API.
2. All money is `NUMERIC`, never floating point.
3. Every write path is idempotent (safe under retry on a container platform).
4. No business logic in the database (no triggers/procs carrying rules).
