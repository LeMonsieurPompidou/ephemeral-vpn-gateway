locals {
  vpn_name               = "ephemeral-vpn-do-${substr(var.deployment_id, 0, 8)}"
  digitalocean_tags      = ["wireguard", "ephemeral-vpn", "deployment:${var.deployment_id}"]
  bootstrap_source       = file("${path.module}/../terraform-common/bootstrap.sh.tftpl")
  effective_client_peers = length(var.client_peers) > 0 ? var.client_peers : (var.client_public_key == null ? [] : [{ id = "client-1", public_key = var.client_public_key, tunnel_ipv4 = "10.8.0.2" }])
  client_peer_config     = join("\n\n", [for peer in local.effective_client_peers : "[Peer]\nPublicKey = ${peer.public_key}\nAllowedIPs = ${peer.tunnel_ipv4}/32"])
  bootstrap_script = replace(replace(replace(replace(
    local.bootstrap_source,
    "@@WIREGUARD_PORT@@", tostring(var.wireguard_port)),
    "@@SERVER_PRIVATE_KEY@@", var.server_private_key),
    "@@CLIENT_PEERS@@", local.client_peer_config),
  "@@BOOTSTRAP_FINGERPRINT@@", substr(sha256(local.bootstrap_source), 0, 12))
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
  tags      = local.digitalocean_tags
  lifecycle {
    precondition {
      condition     = var.ssh_public_key != null || var.ssh_key_name != null
      error_message = "Provide ssh_public_key or ssh_key_name."
    }
    precondition {
      condition     = length(local.effective_client_peers) >= 1 && length(local.effective_client_peers) <= 10
      error_message = "Provide between 1 and 10 WireGuard client peers."
    }
    precondition {
      condition     = alltrue([for tag in local.digitalocean_tags : length(tag) <= 255 && can(regex("^[a-z0-9:_-]+$", tag))])
      error_message = "DigitalOcean tags must contain only lowercase letters, digits, colons, dashes, or underscores and be at most 255 characters."
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
