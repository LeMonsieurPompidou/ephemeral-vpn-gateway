# Ephemeral VPN Gateway

Ephemeral VPN Gateway provisions short-lived WireGuard gateways with Terraform and a
Python/pywebview desktop interface. Supported cloud adapters are DigitalOcean,
Scaleway, and AWS Lightsail. A separate, non-provisioning residential adapter reserves
a future interface for trusted user-owned nodes; it does not integrate proxy services.

Cloud IP addresses are datacenter addresses. A selected country does **not** guarantee
access to any streaming catalog or service. Locations remain `unverified` unless a
test result is explicitly recorded in the version-controlled catalog.

## Architecture

```text
UI (HTML/CSS/JS; catalog-driven)
  -> BridgeService (threaded operations, compatibility API)
  -> Orchestrator (typed lifecycle, validation, health, recovery)
  -> ProviderRegistry -> provider adapters
  -> TerraformRunner -> provider root + deployment-scoped state

vpn-gui-app/provider_catalog.json       provider/location metadata
terraform-common/cloud-init.yaml.tftpl shared WireGuard provisioning
vpn-{digitalocean,scaleway,aws-lightsail}/ provider resources
runtime deployment registry            local, uncommitted, sensitive
```

The public orchestration concepts are `list_providers`, `list_locations`,
`validate_credentials`, `initialize`, `plan`, `deploy`, `destroy`, `get_status`, and
`get_client_config`. Provider selection uses a registry rather than conditional chains.
Legacy `deploy(provider, region)` and unambiguous provider-based destroy calls remain
available through the bridge as a migration path.

## Locations and credentials

Locations are listed in [provider_catalog.json](vpn-gui-app/provider_catalog.json),
including the initially supported Lightsail regions. Credentials use each provider's
normal environment/shared-file resolution:

- DigitalOcean: `DIGITALOCEAN_TOKEN` and `DIGITALOCEAN_SSH_KEY_NAME` (or the SSH key
  name in an ignored legacy `terraform.tfvars`).
- Scaleway: `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, and `SCW_DEFAULT_PROJECT_ID`.
- AWS: the normal AWS SDK chain, including environment variables, `AWS_PROFILE`, shared
  credential/config files, web identity, and instance credentials.

Existing DigitalOcean/Scaleway `terraform.tfvars` credential names remain accepted for
migration, but environment variables are preferred. Copy the matching
`terraform.tfvars.example` for non-secret CLI settings. Never commit the populated file.
Set `ssh_allowed_cidr` to your public `/32`. The safe loopback default intentionally
blocks remote SSH until it is configured.

## Development and build

Requirements: Python 3.10+, Terraform 1.5+, OpenSSH, an existing provider SSH key, and
the WireGuard client.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r vpn-gui-app\requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python vpn-gui-app\app.py
```

Build the packaged app from the repository root:

```powershell
.\.venv\Scripts\pyinstaller "Hérès_VPN.spec"
```

The spec includes the UI, catalog, shared template, and all Terraform roots and handles
PyInstaller's resource directory explicitly.

## Deployment lifecycle

The state machine is: `idle`, `validating_credentials`, `initializing`, `planning`,
`provisioning`, `waiting_for_cloud_init`, `checking_wireguard`, `verifying_egress`,
`ready`, `destroying`, `destroyed`, `failed`, or `cancelled`. Terraform runs off the UI
thread, streams redacted logs, uses argument arrays, validates configuration, creates an
explicit saved plan, applies that exact plan, and parses `terraform output -json`.

Every operation receives a UUID and private runtime directory containing its plan,
state, generated variables, and client configuration. A JSON registry persists provider,
location, paths, timestamps, state, public IP, non-secret resource IDs, last error, and
expiration. Startup shows unfinished deployments for recovery or destruction. Closing
the app warns when a selected deployment may still own resources.

Automatic expiration is opt-in. The selected expiry is recorded and displayed. While
the desktop process is running, a background monitor destroys an expired deployment
only when the user enabled automatic expiration. If the application is closed at the
deadline, the deployment remains in recovery for explicit destruction at next startup.

## Health checks and networking

After apply, the app requires a public IP and valid client configuration, then retries
SSH for up to five minutes. It waits for cloud-init and checks the readiness marker,
`wg-quick@wg0`, the `wg0` interface, IPv4 forwarding, NAT masquerading, and the expected
UDP listener. Egress and DNS verification are optional because they require traffic
through a connected client; they are disabled by default.

The UI validates custom AllowedIPs, DNS IPs, port, MTU, keepalive, SSH CIDR, and expiry.
IPv4 full tunnel is the safe default. IPv6 is disabled unless explicitly selected; the
current cloud modules do not configure a complete routed IPv6 path, so IPv6 should be
treated as experimental and may fail readiness/use checks.

## Security model

- Client X25519 keys are generated locally with the standard cryptography library; the
  client private key is never a Terraform variable or output.
- Runtime secret files use restrictive permissions where the operating system supports
  them. Client files are deleted after successful destroy unless preservation is
  explicitly requested.
- Terraform variables carrying server keys are sensitive and outputs contain no private
  key. Provider credentials are never interpolated into command strings.
- Logs and actionable exceptions redact tokens, common credential forms, AWS access key
  IDs, and WireGuard private-key lines.
- Destroy requires a recorded UUID, an existing state file, and paths contained in the
  known resource/runtime roots. Operations are locked per Terraform directory.
- Terraform state necessarily contains sensitive server provisioning material. Treat
  the entire runtime directory and all state/plan files as secrets; do not sync or share
  them.

## Extending providers and locations

To add a cloud provider, add a self-contained Terraform root that consumes `region`,
`wireguard_port`, `ssh_allowed_cidr`, `server_private_key`, `server_public_key`, and
`client_public_key`; render the shared cloud-init template; return the four operational
outputs used by existing roots; add catalog entries; and register an adapter in
`providers.py`. Add mocked tests and a CI matrix entry.

To add a location, edit the catalog with a unique lowercase ID, ISO country code/name,
city, real provider region/zone, server type, capabilities, and streaming status. App
startup rejects malformed or duplicate data. Only record `tested` when a deliberate test
has been performed; never infer it from geography.

## Tests and CI

Normal tests mock commands and create no cloud resources:

```powershell
.\.venv\Scripts\python -m ruff format --check vpn-gui-app tests
.\.venv\Scripts\python -m ruff check vpn-gui-app tests
.\.venv\Scripts\python -m mypy
.\.venv\Scripts\python -m pytest
terraform fmt -check -recursive
terraform -chdir=vpn-digitalocean init -backend=false
terraform -chdir=vpn-digitalocean validate
```

Repeat the last two commands for Scaleway and Lightsail. GitHub Actions runs these checks
without credentials and never plans or applies infrastructure. Each Terraform root has
its own committed dependency lockfile.

## Troubleshooting and cleanup

- Credential validation failures: set the provider environment variables in the process
  that launches the GUI, or migrate the legacy ignored `terraform.tfvars`.
- SSH readiness timeout: verify your key, SSH CIDR, provider username, security group,
  cloud-init logs, and that local `ssh` is on `PATH`.
- Failed/cancelled apply: keep the runtime registry/state and use the recovery view to
  destroy. Never delete state before confirming the cloud resources are gone.
- WireGuard connects without traffic: inspect forwarding, NAT, AllowedIPs, DNS, and MTU.
- Old host key: remove only the exact ephemeral IP entry from `known_hosts`.

For a complete uninstall: destroy every recovery/active deployment first; confirm the
three cloud consoles contain no matching instances, static IPs, or firewalls; close the
app; remove the local runtime directory shown by `EPHEMERAL_VPN_RUNTIME_DIR` or the
platform default (`%LOCALAPPDATA%\EphemeralVpnGateway` on Windows); then remove the app
and repository. Runtime deletion is irreversible and should happen only after cloud
cleanup is verified.

Licensed under the MIT License. See [LICENSE](LICENSE).
