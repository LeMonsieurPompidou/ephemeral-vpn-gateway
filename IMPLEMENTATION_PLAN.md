# Ephemeral VPN Gateway implementation status

## Reliability model

- Every deployment UUID owns its local-backend state, saved plan, `TF_DATA_DIR`, generated variables, WireGuard client material, temporary SSH identity, and bounded log.
- Provider source directories are configuration-only. Existing source-root state is fingerprinted and preserved for guarded recovery.
- Apply-risk metadata distinguishes a locally removable pre-apply operation from a deployment that may own cloud resources.
- Operation IDs map directly to deployment IDs. Thread locks, filesystem locks, and atomic registry replacement protect concurrent local operations.
- Terraform output JSON is validated against one typed provider contract.

## Implemented recovery behavior

- Startup classifies primary and backup legacy states as empty, active, malformed, ambiguous, or migrated.
- Unsafe legacy states block new provider deployments.
- A state is copied into a runtime only when its provider resources and server public key match exactly one registry record. The source remains unchanged and a timestamped backup plus hashes are recorded.
- Interrupted pre-apply work is locally removable. Interrupted apply, readiness, and destroy work is conservatively recoverable and cannot be discarded locally.

## Security and UX

- Public SSH source IPv4 `/32` is detected automatically with no open-SSH fallback.
- Every provider imports a per-deployment Ed25519 public key; SSH always uses its matching runtime private key explicitly.
- AWS IAM Identity Center profiles receive a real `sts get-caller-identity` preflight and actionable SSO renewal errors.
- Lifetime is primary UI. Networking controls are advanced; MTU and keepalive are automatic.
- Lifetime remains a local best-effort mechanism, not a provider-side TTL guarantee.
- Confirmed destroy removes sensitive recovery artifacts; failed destroy preserves them.
- Deployment logs are persisted, redacted, ANSI-free, and size-bounded.

## Future work

- Optional provider-side tagged-resource reapers can build on `deployment_id`, `expires_at`, and ownership tags.
- Closing SSH port 22 immediately after readiness should be verified in real provider tests before enabling automatically.
- Provider account/API behavior, key propagation timing, and public-IP-change reconciliation require real non-production end-to-end verification.
