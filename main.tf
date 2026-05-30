locals {
  vpn_name = "ephemeral-vpn-gateway"
}

resource "scaleway_account_ssh_key" "main" {
  name       = "${local.vpn_name}-key"
  public_key = file("~/.ssh/id_ed25519.pub")
}

resource "scaleway_instance_ip" "vpn_ip" {
}

resource "scaleway_instance_security_group" "vpn_sg" {
  name                    = "${local.vpn_name}-sg"
  description             = "Security group for personal WireGuard VPN"
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"

  inbound_rule {
    action   = "accept"
    port     = 22
    protocol = "TCP"
    ip_range = "0.0.0.0/0"
  }

  inbound_rule {
    action   = "accept"
    port     = 51820
    protocol = "UDP"
    ip_range = "0.0.0.0/0"
  }
}

resource "scaleway_instance_server" "vpn" {
  name              = local.vpn_name
  type              = "PLAY2-MICRO" # Alternative: STARDUST1-A
  image             = "ubuntu_noble"
  ip_id             = scaleway_instance_ip.vpn_ip.id
  security_group_id = scaleway_instance_security_group.vpn_sg.id

  user_data = {
    cloud-init = <<-CLOUDINIT
      #cloud-config
      package_update: true
      package_upgrade: true
      runcmd:
        - [ bash, -lc, "set -euo pipefail" ]
        - [ bash, -lc, "apt-get update" ]
        - [ bash, -lc, "apt-get install -y ca-certificates curl gnupg lsb-release qrencode" ]
        - [ bash, -lc, "curl -fsSL https://get.docker.com | sh" ]
        - [ bash, -lc, "systemctl enable --now docker" ]
        - [ bash, -lc, "echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-wireguard.conf" ]
        - [ bash, -lc, "sysctl --system" ]
        - [ bash, -lc, "mkdir -p /opt/wireguard/config" ]
        - [ bash, -lc, "chmod 700 /opt/wireguard/config" ]
        - [ bash, -lc, "docker run -d --name=wireguard --cap-add=NET_ADMIN --cap-add=SYS_MODULE -e TZ=Europe/Paris -e SERVERURL=auto -e SERVERPORT=51820 -e PEERS=\"phone,laptop\" -e PEERDNS=1.1.1.1 -e LOG_CONFS=true -p 51820:51820/udp -v /opt/wireguard/config:/config -v /lib/modules:/lib/modules:ro --sysctl net.ipv4.conf.all.src_valid_mark=1 --restart unless-stopped lscr.io/linuxserver/wireguard:latest" ]
    CLOUDINIT
  }

  tags = ["vpn", "wireguard", "ephemeral", "france"]
}

output "vpn_public_ip" {
  description = "Public IP of the VPN gateway"
  value       = scaleway_instance_ip.vpn_ip.address
}

output "ssh_connect" {
  description = "SSH command to connect to the VPN gateway"
  value       = "ssh root@${scaleway_instance_ip.vpn_ip.address}"
}
