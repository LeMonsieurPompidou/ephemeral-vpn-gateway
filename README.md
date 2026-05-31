# Ephemeral VPN Gateway (Scaleway + Terraform)

![Terraform](https://img.shields.io/badge/IaC-Terraform-5C4EE5?logo=terraform&logoColor=white)
![Provider](https://img.shields.io/badge/Cloud-Scaleway-4F0599)
![VPN](https://img.shields.io/badge/VPN-WireGuard-1C1C1C?logo=wireguard&logoColor=white)
![Region](https://img.shields.io/badge/Region-Paris%20(fr--par)-0055A4)
![License](https://img.shields.io/badge/License-MIT-green)

This project is an Infrastructure as Code (IaC) setup that uses Terraform to deploy an ephemeral, private WireGuard VPN gateway on Scaleway (Paris region).

The gateway is designed for short-lived usage: create it when needed, use a French egress IP to securely bypass streaming geo-restrictions, and destroy it afterward to minimize cost.

Terraform now also generates the WireGuard key material it needs for the server and both clients, then writes client files to your Windows Desktop during `terraform apply`.

## Architecture

```text
Local Client (Phone/Laptop)
        |
        | WireGuard Tunnel (UDP 51820)
        v
Scaleway Instance (fr-par / fr-par-1)
        |
        | NAT / Egress
        v
Open Internet (French Exit IP)
```

## Prerequisites

- A Scaleway account
- A Scaleway Project ID, Access Key, and Secret Key
- Terraform >= 1.5.0
- Windows package manager (either Winget or Chocolatey)
- SSH key pair on your local machine (`~/.ssh/id_ed25519` and `~/.ssh/id_ed25519.pub`)

### Install Terraform on Windows

Using Winget:

```powershell
winget install HashiCorp.Terraform
```

Using Chocolatey:

```powershell
choco install terraform -y
```

Verify installation:

```bash
terraform version
```

## Configuration

This project expects Scaleway credentials through Terraform variables.

You can provide them in `terraform.tfvars`:

```hcl
scaleway_project_id = "<your-project-id>"
scaleway_access_key = "<your-access-key>"
scaleway_secret_key = "<your-secret-key>"
```

The deployment also registers your local public SSH key in Scaleway via:

- `scaleway_account_ssh_key.main` with `~/.ssh/id_ed25519.pub`

It also creates the WireGuard client artifacts on your Windows Desktop:

- `scaleway-vpn.conf` for the laptop client
- `phone-vpn-qrcode.html` for the phone client QR code

If you do not already have the key pair:

```bash
ssh-keygen -t ed25519 -C "vpn"
```

## How to Deploy

From the project root, run:

```bash
terraform init
terraform apply
```

When prompted, type `yes` to confirm resource creation.

After apply completes, Terraform outputs the public IP as `vpn_public_ip` and writes the client files directly to your Desktop.

## Multi-Region Deployment

This project is not limited to Paris (`fr-par`). To change the deployment location, open `providers.tf` and update the `region` and `zone` values in the `provider "scaleway"` block. Example mappings:

- Amsterdam:

```hcl
region = "nl-ams"
zone   = "nl-ams-1"
```

- Warsaw:

```hcl
region = "pl-waw"
zone   = "pl-waw-1"
```

After editing `providers.tf`, run `terraform init` (if switching providers) and `terraform apply` to redeploy in the new region.

## How to Connect

### 1) SSH into the instance

```bash
ssh root@<vpn_public_ip>
```

Replace `<vpn_public_ip>` with the `vpn_public_ip` output value.

### 2) Use the generated laptop configuration

After `terraform apply` completes, open `C:\Users\ADMIN\Desktop\scaleway-vpn.conf` and import it into your WireGuard desktop client.

The file already contains the correct laptop private key, the server public key, the public endpoint, DNS, and the tunnel IP settings.

### 3) Scan the phone QR code

Open `C:\Users\ADMIN\Desktop\phone-vpn-qrcode.html` in a browser.

The page renders the phone WireGuard configuration as a QR code and also shows the underlying config text on the page, so you can scan it directly with the WireGuard mobile app.

## How to Destroy (Important)

To avoid unnecessary costs, destroy the infrastructure when finished:

```bash
terraform destroy
```

Type `yes` when prompted.

## Day-to-Day Operations & Troubleshooting

### Scenario 1: You closed your terminal but the server is still running

If you lost your previous shell session, retrieve the current VPN server IP again with:

```bash
terraform output
```

Look for `vpn_public_ip` in the output and reuse it for SSH and client checks.

Important: your phone can remain connected to WireGuard even if your PC is turned off, as long as the Scaleway instance is still running.

### Scenario 2: SSH troubleshooting (`Connection refused`)

If SSH returns `Connection refused` immediately after deployment, this is usually a boot timing issue.

Wait 30-60 seconds for cloud-init and system services to finish startup, then try again:

```bash
ssh root@<vpn_public_ip>
```

### Scenario 3: SSH troubleshooting (`REMOTE HOST IDENTIFICATION HAS CHANGED`)

This project is ephemeral by design. If Scaleway reuses an IP from a previous instance, SSH host key verification will fail with this warning.

Clean the old host signature from your local `known_hosts` file:

```bash
ssh-keygen -R <vpn_public_ip>
```

Then reconnect with SSH.

### Scenario 4: Re-deploying on another day

Use this quick workflow:

```bash
terraform apply
ssh-keygen -R <vpn_public_ip>
ssh root@<vpn_public_ip>
```

Then reopen both Desktop artifacts so they match the new deployment:

- `C:\Users\ADMIN\Desktop\scaleway-vpn.conf`
- `C:\Users\ADMIN\Desktop\phone-vpn-qrcode.html`

## Security Notes

- Treat API keys and VPN configs as sensitive secrets.
- Prefer not to commit real credentials into version control.
- Restrict SSH access (`22/TCP`) to trusted source IPs when possible.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
