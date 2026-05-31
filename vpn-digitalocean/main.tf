locals {
  vpn_name             = "ephemeral-vpn-do"
  wireguard_server_ip  = "10.8.0.1/24"
  wireguard_laptop_ip  = "10.8.0.2/32"
  wireguard_phone_ip   = "10.8.0.3/32"
  wireguard_port       = 51820
  wireguard_client_dns = "1.1.1.1"
}

data "digitalocean_ssh_key" "main" {
  name = var.ssh_key_name
}

resource "wireguard_asymmetric_key" "server" {}

resource "wireguard_asymmetric_key" "laptop" {}

resource "wireguard_asymmetric_key" "phone" {}

resource "digitalocean_droplet" "vpn" {
  name   = local.vpn_name
  size   = "s-1vcpu-1gb"
  image  = "ubuntu-24-04-x64"
  region = var.region

  ssh_keys = [data.digitalocean_ssh_key.main.id]

user_data = <<-CLOUDINIT
    #cloud-config
    write_files:
      - path: /etc/wireguard/wg0.conf
        permissions: "0600"
        content: |
          [Interface]
          Address = ${local.wireguard_server_ip}
          ListenPort = ${local.wireguard_port}
          PrivateKey = ${wireguard_asymmetric_key.server.private_key}
          PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
          PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

          [Peer]
          PublicKey = ${wireguard_asymmetric_key.laptop.public_key}
          AllowedIPs = ${local.wireguard_laptop_ip}

          [Peer]
          PublicKey = ${wireguard_asymmetric_key.phone.public_key}
          AllowedIPs = ${local.wireguard_phone_ip}
    runcmd:
      - |
        bash -lc '
          set -euo pipefail

          while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/lib/dpkg/lock >/dev/null 2>&1; do
            echo "Waiting for apt lock..."
            sleep 3
          done

          export DEBIAN_FRONTEND=noninteractive
          apt-get update
          apt-get install -y wireguard wireguard-tools iptables

          printf "net.ipv4.ip_forward=1\n" > /etc/sysctl.d/99-wireguard.conf
          sysctl -w net.ipv4.ip_forward=1

          systemctl enable --now wg-quick@wg0
        '
  CLOUDINIT
}

resource "local_file" "laptop_wg_config" {
  filename = pathexpand("~/Desktop/digitalocean-vpn.conf")

  content = <<-EOT
    [Interface]
    PrivateKey = ${wireguard_asymmetric_key.laptop.private_key}
    Address = ${local.wireguard_laptop_ip}
    DNS = ${local.wireguard_client_dns}

    [Peer]
    PublicKey = ${wireguard_asymmetric_key.server.public_key}
    Endpoint = ${digitalocean_droplet.vpn.ipv4_address}:${local.wireguard_port}
    AllowedIPs = 0.0.0.0/0, ::/0
    PersistentKeepalive = 25
  EOT
}

resource "local_file" "phone_wg_qrcode" {
  filename = pathexpand("~/Desktop/phone-digitalocean-vpn-qrcode.html")

  content = <<-EOT
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Phone WireGuard QR Code</title>
      <style>
        :root {
          color-scheme: light;
          font-family: Segoe UI, Arial, sans-serif;
          --bg: #f6f8fc;
          --card: #ffffff;
          --text: #0f172a;
          --muted: #475569;
          --border: #dbe3ef;
          --accent: #2563eb;
        }

        body {
          margin: 0;
          min-height: 100vh;
          display: grid;
          place-items: center;
          background: radial-gradient(circle at top, #ffffff 0, var(--bg) 45%, #e8eef7 100%);
          color: var(--text);
        }

        .card {
          width: min(92vw, 720px);
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 24px;
          box-shadow: 0 24px 80px rgba(15, 23, 42, 0.12);
          padding: 28px;
        }

        h1 {
          margin: 0 0 8px;
          font-size: 1.6rem;
        }

        p {
          margin: 0 0 20px;
          color: var(--muted);
          line-height: 1.5;
        }

        .layout {
          display: grid;
          grid-template-columns: 280px 1fr;
          gap: 24px;
          align-items: start;
        }

        #qrcode {
          width: 256px;
          height: 256px;
          padding: 12px;
          border: 1px solid var(--border);
          border-radius: 20px;
          display: grid;
          place-items: center;
          background: #fff;
        }

        pre {
          margin: 0;
          padding: 16px;
          border-radius: 16px;
          border: 1px solid var(--border);
          background: #0f172a;
          color: #e2e8f0;
          overflow: auto;
          white-space: pre-wrap;
          word-break: break-word;
          min-height: 256px;
        }

        .hint {
          margin-top: 14px;
          font-size: 0.95rem;
          color: var(--muted);
        }

        .hint strong {
          color: var(--accent);
        }

        @media (max-width: 760px) {
          .layout {
            grid-template-columns: 1fr;
          }

          #qrcode {
            margin: 0 auto;
          }
        }
      </style>
      <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    </head>
    <body>
      <main class="card">
        <h1>Phone WireGuard QR Code</h1>
        <p>Open this file locally, then scan the QR code with the WireGuard mobile app.</p>

        <div class="layout">
          <div>
            <div id="qrcode"></div>
            <div class="hint"><strong>Tip:</strong> keep the screen bright for easier scanning.</div>
          </div>

          <div>
            <pre id="config"></pre>
          </div>
        </div>
      </main>

      <script>
        const wireguardConfig = ${jsonencode(<<-CFG
          [Interface]
          PrivateKey = ${wireguard_asymmetric_key.phone.private_key}
          Address = ${local.wireguard_phone_ip}
          DNS = ${local.wireguard_client_dns}

          [Peer]
          PublicKey = ${wireguard_asymmetric_key.server.public_key}
          Endpoint = "${digitalocean_droplet.vpn.ipv4_address}:51820"
          AllowedIPs = 0.0.0.0/0, ::/0
          PersistentKeepalive = 25
        CFG
        )};

        document.getElementById("config").textContent = wireguardConfig;
        new QRCode(document.getElementById("qrcode"), {
          text: wireguardConfig,
          width: 256,
          height: 256,
          correctLevel: QRCode.CorrectLevel.M
        });
      </script>
    </body>
    </html>
  EOT
}

output "vpn_public_ip" {
  description = "Public IP of the VPN gateway"
  value       = digitalocean_droplet.vpn.ipv4_address
}

output "ssh_connect" {
  description = "SSH command to connect to the VPN gateway"
  value       = "ssh root@${digitalocean_droplet.vpn.ipv4_address}"
}