# Ephemeral VPN Gateway

![Terraform](https://img.shields.io/badge/IaC-Terraform-5C4EE5?logo=terraform&logoColor=white)
![Scaleway](https://img.shields.io/badge/Cloud-Scaleway-4F0599)
![DigitalOcean](https://img.shields.io/badge/Cloud-DigitalOcean-0080FF)
![WireGuard](https://img.shields.io/badge/VPN-WireGuard-1C1C1C?logo=wireguard&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

Ephemeral VPN Gateway is a fully automated, multi-cloud Terraform project for deploying short-lived WireGuard VPN gateways on demand. It supports both Scaleway in Europe and DigitalOcean globally, giving you a fast, repeatable way to spin up a private egress tunnel, browse securely, and tear everything down when you are done.

The design is intentionally ephemeral: create the gateway only when you need it, pay for the brief runtime you actually use, and destroy it immediately after the session ends. Terraform generates the WireGuard key material, provisions the cloud instance, and delivers ready-to-import client configurations and mobile QR codes directly to your Windows Desktop.

## Key Features

- Multi-cloud support for Scaleway and DigitalOcean.
- Ephemeral, pay-per-minute infrastructure that is meant to be created and destroyed on demand.
- Secure WireGuard tunneling with server and client key generation handled by Terraform.
- Automatic desktop delivery of client configuration files for laptop usage.
- Dynamic HTML and JavaScript QR code generation for mobile WireGuard clients.
- Clean separation between European and global deployment targets.
- PowerShell-friendly automation for quick deploy and destroy workflows.

## Project Architecture

```text
ephemeral-vpn-gateway/
├── vpn-scaleway/
│   ├── main.tf
│   ├── providers.tf
│   └── variables.tf
└── vpn-digitalocean/
    ├── main.tf
    ├── providers.tf
    └── variables.tf
```

Each folder is a self-contained Terraform root module. The Scaleway module targets European zones, while the DigitalOcean module targets global regions such as New York, Amsterdam, Frankfurt, London, and others.

## Prerequisites

- Terraform 1.5 or newer.
- Git.
- A Scaleway account with API credentials if you want to use the European deployment.
- A DigitalOcean account with an API token if you want to use the global deployment.
- A local SSH key pair, typically `~/.ssh/id_ed25519` and `~/.ssh/id_ed25519.pub`.
- Windows PowerShell for the automation shortcuts and day-to-day operations.

## Configuration

Each root module requires provider credentials before Terraform can deploy or destroy infrastructure.

For Scaleway, create a `terraform.tfvars` file inside `vpn-scaleway/` with the following values:

```hcl
scaleway_project_id = "YOUR_SCALEWAY_PROJECT_ID"
scaleway_access_key = "YOUR_SCALEWAY_ACCESS_KEY"
scaleway_secret_key = "YOUR_SCALEWAY_SECRET_KEY"
```

For DigitalOcean, create a `terraform.tfvars` file inside `vpn-digitalocean/` with the following values:

```hcl
do_token     = "YOUR_DIGITALOCEAN_TOKEN"
ssh_key_name = "YOUR_SSH_KEY_NAME"
```

## Advanced Automation (PowerShell Shortcuts)

On Windows, the fastest workflow is to load a few helper functions into your PowerShell profile. This lets you deploy and destroy either cloud environment with a short command instead of repeatedly typing long Terraform invocations.

Open your profile with:

```powershell
notepad $PROFILE
```

If PowerShell blocks profile loading, set the execution policy for your user account:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Add the following shortcuts to your profile:

```powershell
# ==============================================================================
# UNIVERSAL EPHEMERAL VPN GATEWAY SHORTCUTS
# ==============================================================================

# --- DIGITALOCEAN SHORTCUTS ---
# Available regions: nyc1, nyc3 (US East), sfo2, sfo3 (US West), ams3 (NL), fra1 (DE), lon1 (UK), sgp1 (SG), blr1 (IN), tor1 (CA)
function vpn-do-on {
    param ([string]$Region = "nyc3")
    Write-Host "Deploying DigitalOcean VPN in region: [$Region]..." -ForegroundColor Cyan
    Set-Location -Path "~\Documents\GitHub\ephemeral-vpn-gateway\vpn-digitalocean"
    terraform plan -var="region=$Region" -out=tfplan
    terraform apply tfplan
}
function vpn-do-off {
    Write-Host "Destroying DigitalOcean VPN infrastructure..." -ForegroundColor DarkRed
    Set-Location -Path "~\Documents\GitHub\ephemeral-vpn-gateway\vpn-digitalocean"
    terraform destroy -auto-approve
}

# --- SCALEWAY SHORTCUTS ---
# Available zones: fr-par-1 (FR), fr-par-2 (FR), fr-par-3 (FR), nl-ams-1 (NL), nl-ams-2 (NL), nl-ams-3 (NL), pl-waw-1 (PL), pl-waw-2 (PL), pl-waw-3 (PL), it-mil-1 (IT)
function vpn-sw-on {
    param ([string]$Region = "fr-par-1")
    Write-Host "Deploying Scaleway VPN in region: [$Region]..." -ForegroundColor Cyan
    Set-Location -Path "~\Documents\GitHub\ephemeral-vpn-gateway\vpn-scaleway"
    terraform plan -var="region=$Region" -out=tfplan
    terraform apply tfplan
}
function vpn-sw-off {
    Write-Host "Destroying Scaleway VPN infrastructure..." -ForegroundColor DarkRed
    Set-Location -Path "~\Documents\GitHub\ephemeral-vpn-gateway\vpn-scaleway"
    terraform destroy -auto-approve
}
```

## How to Use

The intended workflow is simple:

1. Load your PowerShell profile and open a new shell.
2. Deploy the cloud you want with the matching shortcut.
3. Import the generated desktop configuration into WireGuard.
4. Scan the generated QR code on your phone if you want mobile access.
5. Browse securely through the ephemeral tunnel.
6. Destroy the infrastructure when the session is over.

### Standard CLI Method

If you prefer not to use the PowerShell shortcuts, use the standard Terraform workflow directly from the module directory.

First, clone the repository and enter the workspace:

```bash
git clone <repo_url>
cd ephemeral-vpn-gateway
```

Then run the Terraform commands from the module you want to deploy.

**Terraform commands only:**

```bash
# Standard Terraform workflow
cd vpn-digitalocean
terraform init
terraform apply -var="region=lon1"
# To destroy manually:
terraform destroy -var="region=lon1"
```

**PowerShell shortcuts:**

For example, to launch a DigitalOcean gateway in San Francisco:

```powershell
vpn-do-on sfo3
```

Then open the generated desktop client file, import it into WireGuard, and scan the HTML QR code on your phone if you want a mobile client. When you are done, shut the gateway down with:

```powershell
vpn-do-off
```

The same pattern works for Scaleway in Europe, using zone names only:

```powershell
vpn-sw-on fr-par-1
vpn-sw-off
```

## How to Destroy (Important)

This infrastructure is intentionally ephemeral. If you leave a droplet or instance running, the cloud provider will continue billing you for every minute it stays alive.

Always destroy the environment when you are finished:

```powershell
vpn-do-off
vpn-sw-off
```

If you are using the standard CLI workflow, run `terraform destroy` in the matching module directory and confirm the prompt. Destroying the gateway is not optional for cost control; it is the mechanism that stops billing.

## Cost Optimization

This project is built for short-lived sessions, not permanent uptime. The `*-off` commands destroy the gateway immediately, which stops billing for the compute instance and keeps total cost to fractions of a cent per session in practice. That makes the setup ideal for temporary privacy, travel, testing, and one-off regional access.

## What Gets Generated

During deployment, Terraform writes the client artifacts directly to your Windows Desktop:

- Laptop WireGuard configuration file.
- Phone QR code HTML file with embedded QRCode.js.

These artifacts are regenerated on every apply so the laptop and phone always receive fresh keys and a current endpoint.

## Security Notes

- Treat API tokens and generated WireGuard configs as secrets.
- Do not commit real credentials to version control.
- Use the shortest possible lifetime for each gateway.
- Destroy unused infrastructure as soon as your session ends.

## Troubleshooting

- If Terraform reports missing variables, confirm that the correct `terraform.tfvars` file exists in the target module directory.
- If a client cannot resolve DNS, ensure the generated client file uses the intended public DNS resolver.
- If a deployment fails immediately after boot, wait for cloud-init to finish and re-run the health checks from the module-specific notes.
- If SSH host key warnings appear, clear the old entry from `known_hosts` because the infrastructure is intentionally ephemeral.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.