# Ephemeral VPN Gateway

Ephemeral VPN Gateway provisions short-lived WireGuard gateways through Terraform and a Python/pywebview desktop UI. Terraform adapters are included for DigitalOcean, Scaleway, and AWS Lightsail. Cloud IP addresses are datacenter addresses; selecting a country does not guarantee access to a particular streaming service.

## Normal workflow

The primary UI is deliberately small:

```text
Provider -> Country -> Location -> Lifetime -> Deploy -> Connect
```

Routing mode, custom `AllowedIPs`, DNS, WireGuard port, and an optional manual SSH source `/32` are under **Advanced settings**. MTU 1420 and persistent keepalive 25 are automatic tested defaults. Full-tunnel mode always produces `0.0.0.0/0`.

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
    |-- client.privatekey
    |-- client.conf                    (after apply)
    |-- ssh.privatekey
    |-- ssh.publickey
    |-- known_hosts
    `-- deployment.log
```

Each Terraform root declares the local backend. A fresh deployment copies only Terraform configuration, the provider lockfile, optional legacy tfvars, and shared templates into its runtime working directory; provider-root state is never copied. Deployment `init` runs there with `-input=false -reconfigure`, configures the backend with the absolute runtime `terraform.tfstate` path, and sets a deployment-specific `TF_DATA_DIR`. Recovery initialization first verifies the persisted local-backend metadata and uses the same path without `-reconfigure`. Plan, saved-plan apply, output, and destroy therefore use one verified backend. Runtime guards reject unsafe paths, verify the staged configuration manifest and backend metadata, and fingerprint provider-root state before and after every Terraform operation.

Provider operations also use an OS/filesystem lock. Registry replacement is atomic and registry reads/writes use a cross-process lock.

## Legacy provider-root state recovery

Older versions could write ignored `terraform.tfstate` files into `vpn-aws-lightsail`, `vpn-digitalocean`, or `vpn-scaleway`. Startup inspects primary and backup files without changing them and classifies them as empty, active, malformed, ambiguous, or migrated.

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

### Other providers

- DigitalOcean: `DIGITALOCEAN_TOKEN`
- Scaleway: `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, and `SCW_DEFAULT_PROJECT_ID`

Ignored legacy `terraform.tfvars` credentials remain accepted for migration compatibility, although environment variables are preferred.

## SSH readiness and source address

Immediately before planning, the application requests its public IPv4 from `https://checkip.amazonaws.com/`, validates that it is globally routable, and uses the exact `/32` in the provider firewall. Detection has a short timeout, never falls back to `0.0.0.0/0`, and does not log the address. A manual public IPv4 `/32` override is available under Advanced settings.

If SSH readiness fails and automatic redetection returns a different address, the application performs one controlled Terraform plan/apply to update the SSH rule, then retries. It does not loop indefinitely.

Each deployment also receives a locally generated Ed25519 SSH key:

- AWS imports its public key as a Lightsail key pair and connects as `ubuntu`.
- DigitalOcean registers its public key as a deployment SSH-key resource and connects as `root`.
- Scaleway registers its public key as a deployment account SSH-key resource and connects as `root`.

The SSH command always specifies the runtime private key with `-i`, uses `IdentitiesOnly=yes`, disables password/keyboard-interactive authentication, and keeps a deployment-local `known_hosts`. The private key is never passed to Terraform.

Readiness checks wait for cloud-init and verify the marker, `wg-quick@wg0`, the `wg0` link, forwarding, NAT, and the configured UDP listener. Cancellation interrupts Terraform and SSH retry waits.

## Lifecycle, cancellation, and recovery

Durable registry metadata records plan/apply timestamps, state presence, whether cloud resources may exist, cleanup status, expiry, and legacy migration provenance.

- Before apply starts, cancellation means cloud resources cannot have been created. The UI offers **Remove local deployment**, which removes the runtime and registry record.
- From immediately before apply onward, the application conservatively assumes resources may exist. State and recovery material are retained and the UI offers **Destroy cloud resources**.
- Destroy always receives a new cancellation token; it never reuses a cancelled deployment token.
- Operations are mapped directly to their deployment IDs, so polling, cancellation, logs, and cleanup cannot select the newest unrelated record.
- On restart, interrupted pre-apply records become locally removable. Interrupted apply/readiness/destroy records are marked as requiring reconciliation.

Do not delete state for a partial apply. If state is missing after apply may have started, use the provider console and recovery metadata to reconcile resources before removing local records.

## Keys, logs, and sensitive cleanup

WireGuard server and client keypairs are generated locally with Python `cryptography` X25519. Users do not run `wg genkey`. Terraform receives the server private/public keys and client public key; it never receives the client private key.

Saved plans, tfvars, and state are sensitive because they contain server provisioning material. Runtime files use restrictive modes where supported and inherit the user's protected application-data ACL on Windows.

Per-deployment logs are redacted, ANSI-stripped, capped at 512 KiB, and never intentionally contain private keys, generated client configuration, API tokens, or temporary AWS credentials.

After Terraform confirms successful destruction, the application removes:

- generated tfvars and plans
- Terraform state and backups
- `.terraform`
- WireGuard client secrets/configuration (unless explicitly preserved through the compatibility API)
- the temporary SSH keypair and deployment `known_hosts`

Only sanitized tombstone metadata and the bounded redacted log remain. Failed destruction preserves all recovery-required material.

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
