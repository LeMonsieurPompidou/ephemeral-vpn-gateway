locals {
  vpn_name = "ephemeral-vpn-do"
  cloud_init = templatefile("${path.module}/../terraform-common/cloud-init.yaml.tftpl", {
    wireguard_port     = var.wireguard_port
    server_private_key = var.server_private_key
    client_public_key  = var.client_public_key
  })
}

data "digitalocean_ssh_key" "main" { name = var.ssh_key_name }

resource "digitalocean_droplet" "vpn" {
  name      = local.vpn_name
  size      = var.instance_type
  image     = "ubuntu-24-04-x64"
  region    = var.region
  ssh_keys  = [data.digitalocean_ssh_key.main.id]
  user_data = local.cloud_init
  tags      = ["wireguard", "ephemeral-vpn"]
}

resource "digitalocean_firewall" "vpn" {
  name        = "${local.vpn_name}-firewall"
  droplet_ids = [digitalocean_droplet.vpn.id]
  inbound_rule {
    protocol         = "udp"
    port_range       = tostring(var.wireguard_port)
    source_addresses = ["0.0.0.0/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.ssh_allowed_cidr]
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "all"
    destination_addresses = ["0.0.0.0/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "all"
    destination_addresses = ["0.0.0.0/0"]
  }
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0"]
  }
}

output "vpn_public_ip" {
  description = "Public IPv4 address"
  value       = digitalocean_droplet.vpn.ipv4_address
}
output "server_public_key" {
  description = "WireGuard server public key"
  value       = var.server_public_key
}
output "resource_ids" {
  description = "Non-secret resource identifiers"
  value       = { droplet_id = tostring(digitalocean_droplet.vpn.id), firewall_id = digitalocean_firewall.vpn.id }
}
output "readiness_hint" {
  description = "Remote readiness marker created by cloud-init"
  value       = "/var/lib/cloud/instance/wireguard-ready"
}
