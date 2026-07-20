# /mod-modernization — modernize ONE feature at a time (`--feature`)

A large app doesn't have to be deconstructed all at once. Name a **feature** in
plain English — the page/screen/flow name clients actually use — and the chain
modernizes just that slice.

```text
/mod-modernization . --run-id run-1 --feature "Vet Search"
```

- The orchestrator resolves the plain-English name to its owning bounded context
  (in-session judgment over a deterministic candidate ranking), then scopes the
  synthesis to that context.
- The emitted spec is **titled by the feature name** ("Vet Search — Modernization
  Specification") while the filename / `spec_id` stays the context slug
  (`spec-vets.spec.md`).
- The slice's dependencies come along as **Boundary Contracts** (documented
  interface contracts, not re-specced); the `ROADMAP` waves only the slice + its
  prerequisites.

Repeat `--feature` for several features, or use `--context <slug>` directly if you
already know the bounded-context name. Composes with `--target` and the twin-repo
flags.
