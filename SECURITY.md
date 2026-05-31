# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Known limitations (v0.1)

- **OAuth 2.0 / OIDC not supported.** Only bearer-token authentication is
  implemented. Many production MCP deployments require OAuth. Do not connect
  to untrusted servers without proper auth configured.
- **`requestState` in sentinel checkpoint state.** Do not store secrets or
  session tokens in `requestState` — they may appear in checkpoint snapshots.
  See TD-010 in `docs/governance/TECH_DEBT.md`.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately to: **shawnknight@the-master-plan.com**

Include a description, steps to reproduce, and potential impact.
You will receive a response within 72 hours.
