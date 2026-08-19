locals {
  vpn_name = "ephemeral-vpn-do-${substr(var.deployment_id, 0, 8)}"
  bootstrap_script = replace(replace(replace(
    file("${path.module}/../terraform-common/bootstrap.sh.tftpl"),
    "@@WIREGUARD_PORT@@", tostring(var.wireguard_port)),
    "@@SERVER_PRIVATE_KEY@@", var.server_private_key),
  "@@CLIENT_PUBLIC_KEY@@", var.client_public_key)
  cloud_init = replace(
    file("${path.module}/../terraform-common/cloud-init.yaml.tftpl"),
    "@@BOOTSTRAP_SCRIPT@@",
    indent(6, chomp(local.bootstrap_script)),
  )
  user_data = var.user_data_payload != null ? var.user_data_payload : local.cloud_init
}

data "digitalocean_ssh_key" "existing" {
  count = var.ssh_public_key == null ? 1 : 0
  name  = coalesce(var.ssh_key_name, "missing-key-name")
}

resource "digitalocean_ssh_key" "vpn" {
  count      = var.ssh_public_key == null ? 0 : 1
  name       = "${local.vpn_name}-ssh"
  public_key = var.ssh_public_key
}

resource "digitalocean_droplet" "vpn" {
  name      = local.vpn_name
  size      = var.instance_type
  image     = "ubuntu-24-04-x64"
  region    = var.region
  ssh_keys  = var.ssh_public_key == null ? [data.digitalocean_ssh_key.existing[0].id] : [digitalocean_ssh_key.vpn[0].id]
  user_data = local.user_data
  tags      = compact(["wireguard", "ephemeral-vpn", "deployment:${var.deployment_id}", var.expires_at == null ? "" : "expires-at:${var.expires_at}"])
  lifecycle {
    precondition {
      condition     = var.ssh_public_key != null || var.ssh_key_name != null
      error_message = "Provide ssh_public_key or ssh_key_name."
    }
  }
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
  value = {
    droplet_id  = tostring(digitalocean_droplet.vpn.id)
    firewall_id = digitalocean_firewall.vpn.id
    ssh_key_id  = var.ssh_public_key == null ? tostring(data.digitalocean_ssh_key.existing[0].id) : tostring(digitalocean_ssh_key.vpn[0].id)
  }
}
output "readiness_hint" {
  description = "Remote readiness marker created by cloud-init"
  value       = "/var/lib/ephemeral-vpn/ready"
}
