# Hérès VPN

<p align="center">
  <img src="vpn-gui-app/ui/assets/Hérès_VPN_logo.png" alt="Hérès VPN logo" width="160">
</p>

Hérès VPN is a Windows desktop application that provisions temporary WireGuard VPN gateways in cloud accounts controlled by the user.

Choose a provider, location, lifetime, and number of devices; Hérès creates the gateway, presents a separate QR code and WireGuard configuration for each device, tracks the running time and estimated cost, and destroys the infrastructure from the same application. End-to-end deployment and VPN connectivity have been manually validated on AWS Lightsail, DigitalOcean, and Scaleway.

## Features

- **Multi-cloud gateways:** deploy to AWS Lightsail, DigitalOcean, or Scaleway, with country and region selection from a shared provider catalog.
- **Ephemeral lifecycle:** follow credential validation, Terraform initialization, planning, provisioning, cloud initialization, readiness, and explicit destruction from one desktop workflow.
- **Best-effort lifetime:** optionally schedule local cleanup after 30 minutes to 8 hours. Cleanup cannot run while Hérès is closed, the computer is off, or credentials are unavailable; this is not a provider-side TTL guarantee.
- **Multi-client WireGuard:** create 1–10 independent peers with unique keypairs and tunnel addresses. One client should be used per device.
- **Mobile and desktop delivery:** scan a client-specific QR code or export `HeresVPN1.conf` through `HeresVPN10.conf` with a native Windows Save As dialog.
- **Estimated cost visibility:** display the catalog's hourly estimate, live session duration, and real-time **Estimated cost** where a trusted numeric rate is available.
- **Recovery-first safety:** deployment-scoped Terraform state, durable lifecycle records, interrupted-operation recovery, provider-scoped legacy-state reconciliation, and conservative destroy rules.
- **Secret-aware cleanup:** after confirmed cloud destruction, remove runtime secrets and provenance-matching configuration files exported by Hérès.

## How it works

```text
User
  |
  v
Hérès desktop GUI (pywebview)
  |
  v
Python bridge and lifecycle orchestrator
  |
  v
Deployment-scoped Terraform working directory and state
  |
  +----------+----------------+----------------+
  |          |                |                |
  v          v                v                v
AWS       DigitalOcean     Scaleway       Recovery registry
  \          |                /
   \         |               /
    +--------+--------------+
             |
             v
      WireGuard gateway
             |
      +------+------+ ...
      |             |
      v             v
   Client 1      Client 2
```

The normal deployment flow is:

1. Select a provider.
2. Select a country and location.
3. Choose a lifetime or leave automatic expiration disabled.
4. Choose one VPN client for each device.
5. Run **Check credentials**.
6. Click **Deploy VPN** and wait for **Ready**.
7. Connect each device with its own QR code or `.conf` file.
8. Click **Destroy cloud resources** when finished.

## Multi-client WireGuard

Hérès uses the `10.8.0.0/24` tunnel network:

```text
Gateway     10.8.0.1
Client 1    10.8.0.2
Client 2    10.8.0.3
...
Client 10   10.8.0.11
```

Every client receives:

- an independent WireGuard private/public keypair;
- a unique tunnel IPv4 address;
- one `[Peer]` entry on the gateway with a `/32` route;
- one full-tunnel client configuration; and
- one QR configuration and one exportable `.conf`.

Client private keys remain local and do not enter Terraform variables, Terraform state, cloud user-data, provider APIs, the deployment registry, or logs. Terraform and the gateway receive only each client's public key and tunnel address. The server WireGuard key is generated locally but must be delivered to the gateway, so the deployment plan, state, and generated provisioning data are sensitive.

Do not reuse one Hérès client configuration on multiple devices. WireGuard endpoint roaming can make devices using the same peer identity displace one another.

## Requirements

Hérès has been validated end-to-end on Windows. The code contains POSIX permission handling, but macOS and Linux desktop workflows and packaging have not been validated as releases.

Required to run from source:

- Windows with Python 3.10 or newer;
- Terraform 1.5 or newer;
- OpenSSH client available as `ssh`;
- a supported cloud account and credentials;
- a WireGuard client on each device that will connect; and
- AWS CLI v2 when using AWS Lightsail with IAM Identity Center/SSO.

Runtime Python dependencies are declared in [`vpn-gui-app/requirements.txt`](vpn-gui-app/requirements.txt). Development tools are declared in [`requirements-dev.txt`](requirements-dev.txt). Node.js is only needed for the JavaScript syntax checks used during development.

## Run from source

From Windows PowerShell:

```powershell
git clone https://github.com/LeMonsieurPompidou/terraform-ephemeral-vpn.git
cd terraform-ephemeral-vpn

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\vpn-gui-app\requirements.txt -r .\requirements-dev.txt

.\.venv\Scripts\python.exe .\vpn-gui-app\app.py
```

Hérès stores deployment runtimes under `%LOCALAPPDATA%\EphemeralVpnGateway` by default. `EPHEMERAL_VPN_RUNTIME_DIR` can override that location for development, but changing it also changes which durable registry and recovery records the process sees.

## Provider credentials

Use placeholders in local configuration and never commit real credentials. **Check credentials** follows the same credential source that the provider will use during deployment.

### Packaged Windows application

Select a provider and use **Configure credentials**. DigitalOcean and Scaleway secrets are stored as per-user generic credentials in Windows Credential Manager; they are not written to `settings.json`, the deployment registry, Terraform source, or the executable. Scaleway's project ID and the selected AWS profile name are non-secret preferences stored in `%LOCALAPPDATA%\EphemeralVpnGateway\settings.json`.

Packaged credential precedence is: inherited provider environment variables, then Windows Credential Manager/non-secret Hérès settings, then missing. This lets the executable work when opened directly from Explorer while retaining environment overrides for advanced use.

### AWS Lightsail

AWS authentication remains owned by AWS CLI v2 and IAM Identity Center. Save a profile name in **Configure credentials**, then use **Login with AWS** and **Check credentials**. Hérès never stores AWS SSO tokens or passwords. The equivalent advanced/source setup is:

```powershell
aws configure sso --profile heres-vpn
aws sso login --profile heres-vpn
$env:AWS_PROFILE = "heres-vpn"

.\.venv\Scripts\python.exe .\vpn-gui-app\app.py
```

The credential check runs a bounded, non-interactive `aws sts get-caller-identity` for the selected profile. Identity output and account details are not logged. `AWS_PROFILE` overrides the saved profile for that process. If the SSO session expires, use **Login with AWS** again.

### DigitalOcean

For the packaged application, enter the API token through **Configure credentials**. A bounded read-only account check must succeed before the token is saved to Windows Credential Manager.

Source/development mode retains the known-good, Git-ignored provider tfvars contract. Create `vpn-digitalocean/terraform.tfvars`:

```hcl
do_token     = "<digitalocean-token>"
ssh_key_name = "<existing-key-name>"
```

`do_token` is the credential input. `ssh_key_name` is retained for historical/manual compatibility; desktop deployments normally register their generated per-deployment SSH public key.

Environment authentication is also supported and takes precedence:

```powershell
$env:DIGITALOCEAN_TOKEN = "<digitalocean-token>"
.\.venv\Scripts\python.exe .\vpn-gui-app\app.py
```

`DIGITALOCEAN_ACCESS_TOKEN` is accepted as the provider-compatible fallback, while `DIGITALOCEAN_TOKEN` is the recommended variable. Environment-based credential checking performs a bounded read-only account request. With tfvars, Hérès verifies only that the expected variable is configured and leaves value validation to Terraform without exposing it.

### Scaleway

For the packaged application, **Configure credentials** stores the access and secret keys in Windows Credential Manager and the non-secret project ID in Hérès settings. A bounded read-only project check must succeed before they are persisted.

Source/development mode retains the known-good, Git-ignored tfvars contract:

```hcl
scaleway_access_key = "<access-key>"
scaleway_secret_key = "<secret-key>"
scaleway_project_id = "<project-id>"
```

Place these values in `vpn-scaleway/terraform.tfvars`, or use environment variables, which take precedence:

```powershell
$env:SCW_ACCESS_KEY = "<access-key>"
$env:SCW_SECRET_KEY = "<secret-key>"
$env:SCW_DEFAULT_PROJECT_ID = "<project-id>"

.\.venv\Scripts\python.exe .\vpn-gui-app\app.py
```

The selected catalog location supplies the Scaleway zone and region, so `SCW_DEFAULT_ZONE` and `SCW_DEFAULT_REGION` are not required. Environment-based checking performs a bounded, read-only project request; tfvars values are validated by Terraform.

### Credential handling notes

- Provider `terraform.tfvars` files are source-mode compatibility inputs. They are ignored by Git and copied into the protected deployment runtime only when that exact source is selected.
- Environment variables override saved credentials. PowerShell `$env:...` assignments are session-local and inherited only by child processes.
- A packaged `.exe` opened from Explorer can use saved Windows credentials without a PowerShell session.
- Hérès does not intentionally store provider secrets in `settings.json`, `deployments.json`, application logs, Live non-sensitive logs, browser storage, or generated auto tfvars.
- **Remove saved credentials** removes only Hérès entries. If an active gateway may exist, the UI warns that credentials may be needed for Destroy.
- Do not commit credentials, paste them into issues, or include them in screenshots.

## Usage

The Gateway form contains the normal product controls:

```text
Provider -> Country -> City / region -> Lifetime -> VPN clients
```

During deployment, the UI reconciles its progress with the durable backend lifecycle:

```text
Validating credentials
  -> Initializing
  -> Planning
  -> Provisioning
  -> Waiting for cloud init
  -> Checking WireGuard
  -> Ready
```

At **Ready**:

- select a client tab;
- scan its QR code for a mobile WireGuard app; or
- click **Save configuration**, accept the Desktop default, and import the corresponding `HeresVPN1.conf`–`HeresVPN10.conf` file into WireGuard for Windows.

Each selected client has different key material and a different tunnel address. QR/configuration contents are never written to logs.

When finished, use **Destroy cloud resources**. After Terraform confirms destruction, Hérès removes sensitive runtime material and attempts to delete every tracked exported `.conf` whose path, regular-file type, and content fingerprint still match the file Hérès created.

## Recovery

Recovery appears when a deployment was interrupted, an apply or destroy failed, resources may still exist, or historical provider-root Terraform state requires attention.

Hérès prefers to block rather than silently forget infrastructure that might still exist:

- **Destroy cloud resources** is the correct action once apply may have started or runtime state contains managed resources.
- **Remove local deployment** is available only when the lifecycle and state prove that cloud resources were not created and are no longer possible.
- Legacy state is scoped to its provider, so a DigitalOcean blocker does not prevent an unrelated AWS or Scaleway deployment.
- Ambiguous DigitalOcean or Scaleway legacy state can be checked with exact read-only resource queries. **Mark stale and reconcile** is enabled only after cloud absence is verified for the exact state fingerprint.
- Reconciliation preserves the original state, creates verified quarantine copies, and binds a durable receipt to the provider, source path, lineage, serial, resource summary, and SHA-256 fingerprints.

Do not manually delete or edit `terraform.tfstate`, discard a recovery record simply because it is old, or use local removal when cloud resources may exist.

## Security model

### Local keys and configurations

- WireGuard and deployment SSH keypairs are generated locally.
- Each client private key stays in its protected deployment runtime and is omitted from Terraform/cloud inputs, registry metadata, and logs.
- Private runtime files use verified Windows ACLs restricted to the current user; POSIX systems use mode `0600`.
- The SSH client uses the deployment identity explicitly, a deployment-local `known_hosts`, batch authentication, and no password fallback.
- QR codes and exported configurations contain a WireGuard client private key and must be treated as credentials.

### Provider credentials and logs

- Provider credentials remain in their original ignored tfvars file or the launching process environment.
- Required tfvars are staged into the protected runtime and removed after confirmed destruction.
- Logs are secret-redacted, ANSI-stripped, UTF-8 decoded, bounded in size, and designed for non-sensitive lifecycle diagnostics.

### Terraform and recovery isolation

- Every deployment has its own Terraform working copy, `TF_DATA_DIR`, backend metadata, saved plan, and local state path.
- Fresh initialization uses a non-interactive explicit backend configuration and does not migrate provider-root historical state.
- Recovery destroy verifies that backend metadata and state still belong to the same deployment before Terraform runs.
- Provider-root state is fingerprinted around Terraform operations; legacy reconciliation uses provider-scoped locks, immutable quarantine copies, and fingerprint-bound receipts.
- A single durable active-operation rule prevents two deployments from being created concurrently.

### Exported configurations

Hérès records the exact path and SHA-256 of every successful export. After confirmed destroy it deletes only matching regular files, never globs a Desktop and never follows symlinks. Changed, replaced, or unsafe paths are left untouched with a local cleanup warning.

If a configuration is copied, renamed, uploaded, or shared outside a tracked export path, Hérès cannot find or clean that copy.

## Estimated cost

The provider catalog currently contains these estimates:

| Provider | Catalog estimate |
|---|---:|
| AWS Lightsail | approximately `$0.005/hour` |
| DigitalOcean | approximately `$0.009/hour` |
| Scaleway | `Unavailable` |

The live calculation is:

```text
Estimated cost = elapsed seconds / 3600 * catalog hourly estimate
```

Elapsed time starts at `apply_started_at`, not when the application opens. It continues while resources may exist, including during destroy, and freezes at confirmed `destroyed_at` (with the destroyed record's update time as a backward-compatible fallback).

This is an estimate only. Hérès does not query provider billing APIs. Provider rounding, minimum charges, taxes, data transfer, and other billable resources can make the actual invoice differ.

## Limitations

- **Datacenter IPs:** all current providers supply cloud/datacenter addresses. Websites and streaming services may recognize or block them. A public IP in a selected country does not guarantee a particular content catalog.
- **Local expiration:** lifetime cleanup is best-effort and cannot run while the app or computer is unavailable.
- **Windows validation:** the complete desktop workflow has been validated on Windows; other desktop platforms are not currently release-validated.
- **No billing integration:** cost is calculated from static catalog metadata rather than provider invoices.
- **No stable downloadable release claimed:** source execution is the current reproducible path. Windows packaging is prepared with PyInstaller, but each release build still requires end-to-end validation.

## Project structure

```text
ephemeral-vpn-gateway/
|-- vpn-gui-app/          Python orchestration, provider adapters, security, and UI
|-- vpn-aws-lightsail/    AWS Lightsail Terraform adapter
|-- vpn-digitalocean/     DigitalOcean Terraform adapter
|-- vpn-scaleway/         Scaleway Terraform adapter
|-- terraform-common/     Shared WireGuard/bootstrap templates
|-- tests/                Lifecycle, security, provider, recovery, and UI regressions
|-- .github/workflows/    GitHub Actions validation
`-- Hérès_VPN.spec        PyInstaller build specification
```

## Technical highlights

- Python typed models and provider adapters behind a pywebview desktop bridge.
- Terraform lifecycle orchestration with deployment-scoped backends and state.
- Shared provider-safe bootstrap generation for Bash and cloud-config transports.
- Multi-peer WireGuard key, address, server configuration, QR, and export management.
- Progress-aware cloud-init and SSH readiness monitoring with typed transient/fatal failures.
- Windows private-file ACL hardening and POSIX permission checks.
- Atomic registry writes, cross-process locks, single-active-operation semantics, and deterministic recovery.
- Secret-redacted bounded logs and provenance-checked post-destroy cleanup.

## Testing and quality

The regression suite covers backend initialization, bootstrap rendering, provider credentials, multi-client WireGuard, SSH/readiness behavior, lifecycle recovery, legacy reconciliation, configuration export/cleanup, cost estimation, and GUI state synchronization.

Run the main checks from PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp .pytest-run-last
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy .
terraform fmt -check -recursive
Get-ChildItem .\vpn-gui-app\ui -Filter *.js | ForEach-Object { node --check $_.FullName }
git diff --check
```

Validate provider schemas without enabling a backend:

```powershell
terraform -chdir=vpn-aws-lightsail init -backend=false -input=false -lockfile=readonly
terraform -chdir=vpn-aws-lightsail validate

terraform -chdir=vpn-digitalocean init -backend=false -input=false -lockfile=readonly
terraform -chdir=vpn-digitalocean validate

terraform -chdir=vpn-scaleway init -backend=false -input=false -lockfile=readonly
terraform -chdir=vpn-scaleway validate
```

GitHub Actions runs Python formatting, linting, typing, tests, Terraform formatting, and backend-disabled validation. Automated tests mock cloud mutations and do not deploy infrastructure.

## Building the Windows application

The repository contains a PyInstaller specification that bundles the GUI, provider Terraform roots, shared bootstrap templates, and application icon:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller .\Hérès_VPN.spec
```

The output is created under `dist/`. Packaging is prepared, but this repository does not claim that a current downloadable binary release has been published. Validate the built executable's credential inheritance, Desktop export, recovery paths, and complete deploy/destroy workflow before distributing it.

The PyInstaller spec uses an audited file-by-file data allowlist. It deliberately includes the committed Terraform source and dependency lockfile for each provider, shared bootstrap templates, the provider catalog, GUI assets, and the locally vendored QR renderer. It rejects tfvars, Terraform state, plans, generated keys/configurations, tests, and `.terraform` directories before packaging. Run the manifest audit directly with:

```powershell
.\.venv\Scripts\python.exe .\release_bundle.py
```

Build from a clean working tree and inspect the resulting archive as normal release hygiene. DigitalOcean and Scaleway tfvars remain supported when running from source, but are never bundled. Packaged users configure credentials through Hérès and Windows Credential Manager; environment variables remain optional overrides.

## Development

Keep changes focused and run the relevant regression tests plus the validation commands above. Never commit:

- provider credentials or populated tfvars;
- Terraform state, plans, or `.terraform` directories;
- generated WireGuard/SSH keys or client configurations;
- runtime logs or deployment directories; or
- locally built executables.

The repository's `.gitignore` excludes these common artifacts, but review every diff before publishing.

## License

Hérès VPN is licensed under the [MIT License](LICENSE).
