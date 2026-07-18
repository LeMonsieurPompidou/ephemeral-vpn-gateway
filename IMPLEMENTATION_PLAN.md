# Ephemeral VPN Gateway implementation plan

## Current architecture and issues

- `vpn-gui-app/bridge.py` directly maps two provider names to Terraform folders and
  executes blocking Terraform commands. There is no deployment identity, recovery,
  cancellation, timeout, locking, readiness check, or safe destruction boundary.
- Provider/location data is duplicated in JavaScript. Several displayed Scaleway IDs
  (`paris-1`, for example) are not valid Terraform zone IDs.
- Both Terraform roots generate client private keys in Terraform state and write
  client files to the user's Desktop. SSH is unrestricted and the modules duplicate
  cloud-init almost entirely.
- Terraform applies without validating or consuming an explicit plan. Exceptions and
  command output are not redacted. The dependency lock file is ignored.
- The UI models deployment as connected/disconnected booleans and can only destroy by
  provider, which is unsafe when an earlier or partial deployment exists.

## Phased checklist

- [x] Inspect every Terraform, Python, JavaScript, HTML, CSS, packaging, and docs file.
- [x] Add typed models, catalog validation, provider protocol/registry, and adapters.
- [x] Add a persistent runtime deployment registry and explicit state transitions.
- [x] Add a cancellable, timed, redacting Terraform runner using saved plans and JSON.
- [x] Generate client keys/configuration locally with restrictive permissions.
- [x] Add bounded readiness checks and safe deployment-scoped destruction.
- [x] Refactor shared cloud-init and harden DigitalOcean and Scaleway networking.
- [x] Add an AWS Lightsail root module and catalog entries.
- [x] Replace hardcoded UI provider data and expose progress, logs, recovery, options,
      expiration, copy/save/QR, and guarded destruction/close behavior.
- [x] Add unit tests, Terraform checks, and credential-free CI.
- [x] Update packaging, examples, lock-file policy, and complete documentation.
- [x] Run all locally available formatting, linting, type, and unit checks.

## Compatibility assumptions

- Existing Terraform root directories and their `region` variable remain usable from
  the CLI. Legacy `deploy(provider, region)` and `destroy(provider)` bridge calls are
  retained as wrappers while the UI moves to deployment UUIDs.
- Existing cloud resources are not modified or destroyed during development or tests.
  Cloud and Terraform calls are mocked in the automated test suite.
