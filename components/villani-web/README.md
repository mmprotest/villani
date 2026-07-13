# Villani Web

Villani Web consumes the shared monochrome terminal design tokens from
`components/villani-ui`. Release packaging rebuilds the application and validates every local
`index.html` asset reference.

Villani Web renders control-plane run detail through `@villani/run-model`. Explicit API fields are
authoritative and canonical events are a backward-compatible fallback. Overview, candidates,
costs, files, policy, and timeline therefore share canonical attempt IDs, aggregate accounting,
verification authority, selection reasoning, and redaction state.

Development requires Node 20. Run `npm ci`, `npm test`, `npm run typecheck`, `npm run build`,
`npm run format:check`, and `npm run e2e`.
