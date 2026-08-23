# Ephemeral VPN Gateway

Ephemeral VPN Gateway provisions short-lived WireGuard gateways through Terraform and a Python/pywebview desktop UI. Terraform adapters are included for DigitalOcean, Scaleway, and AWS Lightsail. Cloud IP addresses are datacenter addresses; selecting a country does not guarantee access to a particular streaming service.

## Normal workflow

The primary UI is deliberately small:

```text
Provider -> Country -> Location -> Lifetime -> VPN clients -> Deploy -> Connect
```

The normal GUI intentionally uses the validated defaults: IPv4 full tunnel (`AllowedIPs = 0.0.0.0/0`), DNS `1.1.1.1` and `1.0.0.1`, WireGuard UDP port 51820, MTU 1420, persistent keepalive 25, and automatic SSH source-address detection. These remain backend options for tests and future expert workflows, but infrastructure controls are not shown in the normal product flow.

Select one VPN client for every phone, computer, or other device (1 to 10). Every client receives a unique WireGuard keypair and tunnel address, with its own QR code and Desktop-exportable `.conf`. **Do not reuse the same WireGuard client configuration on multiple devices. Create/select one client per device.**

`Lifetime` is local, best-effort cleanup. It is persisted and retried after the application restarts, but it cannot destroy resources while the computer is off, the application is not running, or provider credentials are unavailable. There is currently no provider-side TTL service.

## Architecture and runtime state

```text
UI -> BridgeService -> Orchestrator -> ProviderRegistry -> TerraformRunner
```

Every operation is bound to one deployment UUID. The default Windows runtime root is `%LOCALAPPDATA%\EphemeralVpnGateway`; set `EPHEMERAL_VPN_RUNTIME_DIR` to override it.

```text
EphemeralVpnGateway\
|-- deployments.json
|-- deployments.lock
|-- locks\<provider>.lock
`-- <deployment-id>\
    |-- .terraform\
    |-- terraform-work\                 (state-free configuration copy)
    |-- terraform-work-manifest.json
    |-- terraform.tfstate
    |-- terraform.tfstate.backup       (when Terraform creates one)
    |-- deployment.tfplan
    |-- deployment.auto.tfvars.json
    |-- clients\
    |   |-- client-1\
    |   |   |-- client.privatekey
    |   |   `-- client.conf            (after apply)
    |   `-- client-N\ ...
    |-- ssh.privatekey
    |-- ssh.publickey
    |-- known_hosts
    `-- deployment.log
```

Each Terraform root declares the local backend. A fresh deployment copies only Terraform configuration, the provider lockfile, optional legacy tfvars, and shared templates into its runtime working directory; provider-root state is never copied. Deployment `init` runs there with `-input=false -reconfigure`, configures the backend with the absolute runtime `terraform.tfstate` path, and sets a deployment-specific `TF_DATA_DIR`. Recovery initialization first verifies the persisted local-backend metadata and uses the same path without `-reconfigure`. Plan, saved-plan apply, output, and destroy therefore use one verified backend. Runtime guards reject unsafe paths, verify the staged configuration manifest and backend metadata, and fingerprint provider-root state before and after every Terraform operation.

Provider operations also use an OS/filesystem lock. Registry replacement is atomic and registry reads/writes use a cross-process lock.

## Legacy provider-root state recovery

Older versions could write ignored `terraform.tfstate` files into `vpn-aws-lightsail`, `vpn-digitalocean`, or `vpn-scaleway`. Startup inspects primary and backup files without changing them and classifies them as empty, active, malformed, ambiguous, or migrated.

The Gateway form never shows the large technical legacy-state card. Empty, validly migrated, and validly reconciled-stale states are invisible. A genuinely blocking state produces only a compact **Deployment recovery is required** warning; the hashes, lineage, serial, migration, and stale-reconciliation controls are available only after opening **Recovery**.

- Active, malformed, and ambiguous state blocks a new deployment for that provider.
- The original state is never deleted, overwritten, moved, merged, or destroyed automatically.
- Automatic migration is offered only when the primary state's provider resources and server public key match exactly one existing deployment registry record.
- Migration creates a timestamped runtime backup, copies the state into that matched deployment's runtime, verifies hashes, and records the source and backup in registry metadata.
- If no runtime deployment matches, **Mark stale — cloud absence confirmed** is available for a parseable active state. It requires the operator to type an explicit confirmation after independently verifying that every summarized cloud resource is absent. The action does not contact the provider or run Terraform.
- Stale reconciliation copies the primary state and any backup to `%LOCALAPPDATA%\EphemeralVpnGateway\legacy-quarantine\<provider>\<timestamp>-<short-sha>\`, verifies the copies, and atomically stores a receipt under `legacy-reconciliations`. The provider is unblocked only while the source path, SHA-256, lineage, serial, resource/output summaries, and quarantine copies still match that receipt.
- A migrated classification is valid only while the recorded deployment runtime still contains a parseable, provider-matching state with the same SHA-256 as the legacy source.
- Empty or ambiguous backups require explicit operator reconciliation; the application does not guess which snapshot represents cloud reality.

## Credentials

### AWS IAM Identity Center / SSO

Configure and log in with AWS CLI v2, then launch the GUI from the same environment:

```powershell
aws configure sso --profile heres-vpn
aws sso login --profile heres-vpn
$env:AWS_PROFILE = "heres-vpn"
.\.venv\Scripts\python.exe vpn-gui-app\app.py
```

Before planning, the application locates AWS CLI v2 and runs a bounded, non-interactive:

```text
aws sts get-caller-identity --profile heres-vpn --no-cli-pager
```

Account details are not logged. An expired session produces an instruction to rerun `aws sso login --profile heres-vpn`. The UI's **Check credentials** action can be retried without restarting.

### DigitalOcean

The existing `main`-branch credential contract remains supported: an ignored
`vpn-digitalocean/terraform.tfvars` may supply `do_token` (and the historical
`ssh_key_name`). The deployment-scoped working copy receives a protected byte-for-byte
copy, while the provider-root file remains unchanged.

Environment authentication is also supported. Use the canonical provider variable and
launch Hérès from that PowerShell session:

```powershell
$env:DIGITALOCEAN_TOKEN = "<token>"
.\.venv\Scripts\python.exe vpn-gui-app\app.py
```

`DIGITALOCEAN_ACCESS_TOKEN` is accepted as the Terraform provider's supported fallback,
but Hérès consistently recommends `DIGITALOCEAN_TOKEN`. With environment credentials,
**Check credentials** performs a bounded, read-only account request and distinguishes
missing credentials, rejected/insufficient credentials, and network/API failure without
logging the token. With `terraform.tfvars`, it confirms that the known-good variable is
configured and reports that Terraform will perform the provider validation.

### Scaleway

The existing `main`-branch contract remains supported: an ignored
`vpn-scaleway/terraform.tfvars` may supply `scaleway_access_key`,
`scaleway_secret_key`, and `scaleway_project_id`. Alternatively, set the provider
environment variables and launch Hérès from the same PowerShell session:

```powershell
$env:SCW_ACCESS_KEY = "<access-key>"
$env:SCW_SECRET_KEY = "<secret-key>"
$env:SCW_DEFAULT_PROJECT_ID = "<project-id>"
.\.venv\Scripts\python.exe vpn-gui-app\app.py
```

The selected catalog location supplies the Scaleway zone and region explicitly, so `SCW_DEFAULT_ZONE` and `SCW_DEFAULT_REGION` are not required. **Check credentials** makes a bounded, read-only request for the configured Project. Missing-variable guidance names variables only; it never displays existing values.

### Environment lifetime and packaged builds

PowerShell `$env:...` assignments exist only in that process and its child processes. A source run or packaged executable started from the same terminal inherits them; an executable opened later by double-click from Explorer does not inherit an unrelated terminal's temporary environment. Either start the packaged executable from the configured terminal or deliberately configure Windows user environment variables outside Hérès, understanding that Windows then persists them for other processes owned by that user.

Hérès never writes provider credentials to the registry or logs. Environment credentials
remain in the inherited process environment. A user-created provider `terraform.tfvars`
is git-ignored, copied only into the protected deployment runtime for Terraform, and
removed with other sensitive runtime artifacts after confirmed destroy. Never commit a
populated tfvars file.


## SSH readiness and source address

Immediately before planning, the application requests its public IPv4 from `https://checkip.amazonaws.com/`, validates that it is globally routable, and uses the exact `/32` in the provider firewall. Detection has a short timeout, never falls back to `0.0.0.0/0`, and does not log the address. A manual override remains available through the backend options for diagnostics, not the normal GUI.

If SSH readiness fails and automatic redetection returns a different address, the application performs one controlled Terraform plan/apply to update the SSH rule, then retries. It does not loop indefinitely.

Each deployment also receives a locally generated Ed25519 SSH key:

- AWS imports its public key as a Lightsail key pair and connects as `ubuntu`.
- DigitalOcean registers its public key as a deployment SSH-key resource and connects as `root`.
- Scaleway registers its public key as a deployment account SSH-key resource and connects as `root`.

The SSH command always specifies the runtime private key with `-i`, uses `IdentitiesOnly=yes`, disables password/keyboard-interactive authentication, and keeps a deployment-local `known_hosts`. The private key is never passed to Terraform.

Readiness checks wait for cloud-init and verify the marker, `wg-quick@wg0`, the `wg0` link, forwarding, NAT, and the configured UDP listener. Cancellation interrupts Terraform and SSH retry waits.

## Lifecycle, cancellation, and recovery

Durable registry metadata records plan/apply timestamps, state presence, whether cloud resources may exist, cleanup status, expiry, non-secret client identities/addresses, the catalog hourly-price snapshot, and legacy migration provenance. Client private keys are never stored in the registry.

- Before apply starts, cancellation means cloud resources cannot have been created. The UI offers **Remove local deployment**, which removes the runtime and registry record.
- From immediately before apply onward, the application conservatively assumes resources may exist. State and recovery material are retained and the UI offers **Destroy cloud resources**.
- Destroy always receives a new cancellation token; it never reuses a cancelled deployment token.
- Operations are mapped directly to their deployment IDs, so polling, cancellation, logs, and cleanup cannot select the newest unrelated record.
- On restart, interrupted pre-apply records become locally removable. Interrupted apply/readiness/destroy records are marked as requiring reconciliation.

Do not delete state for a partial apply. If state is missing after apply may have started, use the provider console and recovery metadata to reconcile resources before removing local records.

## Keys, logs, and sensitive cleanup

WireGuard server and client keypairs are generated locally with Python `cryptography` X25519. Users do not run `wg genkey`. Terraform receives the server private/public keys and a typed list containing each client's public key and `/32` tunnel address. It never receives a client private key. The server configuration contains one peer block per client.

At Ready, select a client to display only that client's QR code and Desktop export. Export names are deliberately simple WireGuard tunnel names: `HeresVPN1.conf` through `HeresVPN10.conf`, independent of provider, location, or deployment. Windows resolves the real Desktop known folder, including redirected/OneDrive Desktops, before opening native Save As. The protected runtime copy remains authoritative.

The status card estimates session cost from the catalog's numeric hourly estimate and elapsed time since `apply_started_at`. The timer freezes at confirmed `destroyed_at`; requesting destroy does not stop it. This is an estimate only: provider rounding, minimum charges, taxes, and other resources can make actual billing differ. Locations without verified pricing display **Unavailable**. No billing API is queried.

Saved plans, tfvars, and state are sensitive because they contain server provisioning material. Runtime files use restrictive modes where supported and inherit the user's protected application-data ACL on Windows.

Per-deployment logs are redacted, ANSI-stripped, capped at 512 KiB, and never intentionally contain private keys, generated client configuration, API tokens, or temporary AWS credentials.

After Terraform confirms successful destruction, the application removes:

- generated tfvars and plans
- Terraform state and backups
- `.terraform`
- WireGuard client secrets/configuration (unless explicitly preserved through the compatibility API)
- the temporary SSH keypair and deployment `known_hosts`
- every exact user export tracked for that deployment whose regular-file type and SHA-256 still match its authoritative client configuration

The app never globs Desktop configuration files. A missing tracked export is considered already clean; a symlink, changed file, malformed provenance record, or mismatched hash is refused and shown as a separate manual local-cleanup warning without changing the successful cloud-destroy state. Failed destruction leaves exports and all recovery-required material untouched. Only sanitized tombstone/provenance metadata and the bounded redacted log remain.

The provider selector contains only implemented Terraform providers: AWS Lightsail, DigitalOcean, and Scaleway. A user-owned residential exit remains a possible roadmap direction but is not offered as a deployable provider.

## Development and validation

Requirements are Python 3.10+, Terraform 1.5+, OpenSSH, AWS CLI v2 for Lightsail, and a WireGuard client.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r vpn-gui-app\requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe vpn-gui-app\app.py
```

Build from the repository root:

```powershell
.\.venv\Scripts\pyinstaller "Hérès_VPN.spec"
```

Local tests mock Terraform and cloud interactions; they never apply infrastructure:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy .
terraform fmt -check -recursive
```

For provider schema validation, use `terraform init -backend=false` followed by `terraform validate`. Never run plan/apply against an unreconciled provider-root state.

Licensed under the MIT License. See [LICENSE](LICENSE).
