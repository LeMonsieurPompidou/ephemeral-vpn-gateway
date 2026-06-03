# Ephemeral VPN Gateway

![Terraform](https://img.shields.io/badge/IaC-Terraform-5C4EE5?logo=terraform&logoColor=white)
![Scaleway](https://img.shields.io/badge/Cloud-Scaleway-4F0599)
![DigitalOcean](https://img.shields.io/badge/Cloud-DigitalOcean-0080FF)
![WireGuard](https://img.shields.io/badge/VPN-WireGuard-1C1C1C?logo=wireguard&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

Ephemeral VPN Gateway is a multi-cloud, short-lived WireGuard deployment toolkit built around Terraform and a native Python desktop app. It can provision VPN gateways on Scaleway or DigitalOcean, generate client configs and QR codes, and tear everything down as soon as you are done.

The project ships with a real desktop GUI that connects directly to the Terraform orchestration layer. You can launch the app, choose a cloud stack and location, and watch the interface update with the live public IP and the actual WireGuard QR code generated from the deployed infrastructure.

## Key Features

- Native desktop GUI for a simple point-and-click deployment flow.
- Multi-cloud support for Scaleway and DigitalOcean.
- Real Terraform orchestration through the Python bridge layer.
- Live status updates, including deployment progress, public IP, and connected state.
- Native WireGuard QR code rendering directly inside the app.
- Desktop-delivered client configuration files for laptop import.
- Ephemeral infrastructure designed to be deployed only when needed and destroyed immediately after use.
- Advanced terminal workflows for users who prefer automation scripts or headless environments.

## Project Architecture

```text
ephemeral-vpn-gateway/
├── vpn-scaleway/
│   ├── main.tf
│   ├── providers.tf
│   └── variables.tf
├── vpn-digitalocean/
│   ├── main.tf
│   ├── providers.tf
│   └── variables.tf
└── vpn-gui-app/
    ├── app.py
    ├── bridge.py
    └── ui/
        ├── index.html
        ├── style.css
        ├── script.js
        └── assets/
            ├── Hérès_VPN_logo.png
            └── Hérès_VPN_logo.ico
```

Each Terraform directory is a self-contained root module. The Scaleway module targets European zones, while the DigitalOcean module targets global regions such as New York, Amsterdam, Frankfurt, London, Singapore, and more.

## Prerequisites

- Terraform 1.5 or newer.
- Git.
- A Scaleway account with API credentials if you want to use the European deployment.
- A DigitalOcean account with an API token if you want to use the global deployment.
- A local SSH key pair, typically `~/.ssh/id_ed25519` and `~/.ssh/id_ed25519.pub`.
- Windows PowerShell for the optional automation shortcuts.

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

## Deployment & Operation Workflows

### 1. Standalone Desktop GUI Workflow (Recommended)

This is the preferred workflow for end-users who want a seamless point-and-click experience.

**Prerequisites and setup**

- Install or run `Hérès_VPN.exe`.
- Confirm that the Scaleway or DigitalOcean credentials are already configured in the matching Terraform module.
- Ensure the local SSH key pair exists if your selected provider requires it.

**Deploy (ON)**

1. Start `Hérès_VPN.exe`.
2. Select either `Scaleway Stack` or `DigitalOcean Stack`.
3. Choose a region or zone from the provider-specific dropdown.
4. Click `Deploy VPN`.
5. Watch the status card animate to `Connecting...` and then `Connected`.
6. Review the live public IP that appears in the interface.
7. Scan the native WireGuard QR code rendered directly on screen.

**Destroy (OFF)**

Press `Destroy VPN` inside the interface to fully tear down the cloud resources.

**Technical note: packaging the GUI**

Rebuild the standalone executable from the repository root with PyInstaller using these exact commands:

```bash
pip install --upgrade pyinstaller pyinstaller-hooks-contrib pywebview
pyinstaller --onefile --noconsole --name "Hérès_VPN" --icon "vpn-gui-app\ui\assets\Hérès_VPN_logo.ico" --add-data "vpn-gui-app\ui;ui" --add-data "vpn-scaleway;vpn-scaleway" --add-data "vpn-digitalocean;vpn-digitalocean" "vpn-gui-app\app.py"
```

The compiled artifact is `Hérès_VPN.exe`.

### 2. Automated PowerShell Workflows (Windows Terminal)

This workflow is ideal for local terminal users who prefer profile automation shortcuts.

**Prerequisites and setup**

Open your PowerShell profile with:

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
# ======================================================================

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
    Write-Host "Deploying Scaleway VPN in zone: [$Region]..." -ForegroundColor Cyan
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

**Deploy (ON)**

For example, to launch a DigitalOcean gateway in San Francisco:

```powershell
vpn-do-on sfo3
```

**Destroy (OFF)**

When you are done, shut it down with:

```powershell
vpn-do-off
```

The same pattern works for Scaleway in Europe:

```powershell
vpn-sw-on fr-par-1
vpn-sw-off
```

### 3. Standard Infrastructure-as-Code CLI Workflow (Cross-Platform)

This workflow is ideal for headless servers, Linux/macOS operators, or CI/CD runners.

**Prerequisites and setup**

- Clone the repository and enter the workspace.
- Make sure the matching `terraform.tfvars` file exists in the provider directory.
- Confirm your cloud credentials and local SSH key are available.

```bash
git clone <repo_url>
cd ephemeral-vpn-gateway
```

**Deploy (ON)**

DigitalOcean example:

```bash
cd vpn-digitalocean
terraform init
terraform apply -var="region=lon1"
```

Scaleway example:

```bash
cd vpn-scaleway
terraform init
terraform apply -var="region=fr-par-1"
```

**Destroy (OFF)**

To clean up manually, run:

```bash
terraform destroy -auto-approve
```

Use that command in the same module directory you deployed from, whether it is `vpn-digitalocean` or `vpn-scaleway`.

## Cost Optimization

This project is designed for short-lived sessions, not permanent uptime. Destroying the gateway immediately after use keeps the total cost low and makes the setup suitable for temporary privacy, travel, testing, and one-off regional access.

## Security Notes

- Treat API tokens and generated WireGuard configs as secrets.
- Do not commit real credentials to version control.
- Use the shortest possible lifetime for each gateway.
- Destroy unused infrastructure as soon as your session ends.

## Troubleshooting

- If Terraform reports missing variables, confirm that the correct `terraform.tfvars` file exists in the target module directory.
- If a client cannot resolve DNS, ensure the generated client file uses the intended public DNS resolver.
- If a deployment fails immediately after boot, wait for cloud-init to finish and try again.
- If SSH host key warnings appear, clear the old entry from `known_hosts` because the infrastructure is intentionally ephemeral.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
