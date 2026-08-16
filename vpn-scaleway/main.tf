locals {
  vpn_name = "ephemeral-vpn-gateway-${substr(var.deployment_id, 0, 8)}"
  cloud_init = templatefile("${path.module}/../terraform-common/cloud-init.yaml.tftpl", {
    wireguard_port     = var.wireguard_port
    server_private_key = var.server_private_key
    client_public_key  = var.client_public_key
  })
}
resource "scaleway_account_ssh_key" "main" {
  name       = "${local.vpn_name}-key"
  public_key = var.ssh_public_key == null ? file(pathexpand(var.ssh_public_key_path)) : var.ssh_public_key
}
resource "scaleway_instance_ip" "vpn" {}
resource "scaleway_instance_security_group" "vpn" {
  name                    = "${local.vpn_name}-sg"
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"
  inbound_rule {
    action   = "accept"
    port     = 22
    protocol = "TCP"
    ip_range = var.ssh_allowed_cidr
  }
  inbound_rule {
    action   = "accept"
    port     = var.wireguard_port
    protocol = "UDP"
    ip_range = "0.0.0.0/0"
  }
}
resource "scaleway_instance_server" "vpn" {
  name              = local.vpn_name
  type              = var.instance_type
  image             = "ubuntu_noble"
  zone              = var.region
  ip_id             = scaleway_instance_ip.vpn.id
  security_group_id = scaleway_instance_security_group.vpn.id
  root_volume { delete_on_termination = true }
  user_data  = { cloud-init = local.cloud_init }
  tags       = compact(["vpn", "wireguard", "ephemeral", "deployment:${var.deployment_id}", var.expires_at == null ? "" : "expires-at:${var.expires_at}"])
  depends_on = [scaleway_account_ssh_key.main]
}
output "vpn_public_ip" {
  description = "Public IPv4 address"
  value       = scaleway_instance_ip.vpn.address
}
output "server_public_key" {
  description = "WireGuard server public key"
  value       = var.server_public_key
}
output "resource_ids" {
  description = "Non-secret resource identifiers"
  value = {
    server_id  = scaleway_instance_server.vpn.id
    ip_id      = scaleway_instance_ip.vpn.id
    ssh_key_id = scaleway_account_ssh_key.main.id
  }
}
output "readiness_hint" {
  description = "Remote readiness marker created by cloud-init"
  value       = "/var/lib/cloud/instance/wireguard-ready"
}
