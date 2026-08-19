locals {
  vpn_name = "ephemeral-vpn-lightsail-${substr(var.deployment_id, 0, 8)}"
  bootstrap_script = replace(replace(replace(
    file("${path.module}/../terraform-common/bootstrap.sh.tftpl"),
    "@@WIREGUARD_PORT@@", tostring(var.wireguard_port)),
    "@@SERVER_PRIVATE_KEY@@", var.server_private_key),
  "@@CLIENT_PUBLIC_KEY@@", var.client_public_key)
  user_data = var.user_data_payload != null ? var.user_data_payload : local.bootstrap_script
}
resource "aws_lightsail_key_pair" "vpn" {
  count      = var.ssh_public_key == null ? 0 : 1
  name       = "${local.vpn_name}-ssh"
  public_key = var.ssh_public_key
  tags       = { Purpose = "ephemeral-wireguard", Deployment = var.deployment_id, ExpiresAt = coalesce(var.expires_at, "disabled") }
}
resource "aws_lightsail_instance" "vpn" {
  name              = local.vpn_name
  availability_zone = "${var.region}${var.availability_zone_suffix}"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = var.instance_type
  key_pair_name     = var.ssh_public_key == null ? var.ssh_key_pair_name : aws_lightsail_key_pair.vpn[0].name
  user_data         = local.user_data
  tags              = { Purpose = "ephemeral-wireguard", Deployment = var.deployment_id, ExpiresAt = coalesce(var.expires_at, "disabled") }
}
resource "aws_lightsail_static_ip" "vpn" {
  name = "${local.vpn_name}-ip"
}
resource "aws_lightsail_static_ip_attachment" "vpn" {
  static_ip_name = aws_lightsail_static_ip.vpn.name
  instance_name  = aws_lightsail_instance.vpn.name
}
resource "aws_lightsail_instance_public_ports" "vpn" {
  instance_name = aws_lightsail_instance.vpn.name
  port_info {
    protocol  = "udp"
    from_port = var.wireguard_port
    to_port   = var.wireguard_port
    cidrs     = ["0.0.0.0/0"]
  }
  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = [var.ssh_allowed_cidr]
  }
}
output "vpn_public_ip" {
  description = "Static public IPv4 address"
  value       = aws_lightsail_static_ip.vpn.ip_address
}
output "server_public_key" {
  description = "WireGuard server public key"
  value       = var.server_public_key
}
output "resource_ids" {
  description = "Non-secret resource identifiers"
  value = {
    instance_name  = aws_lightsail_instance.vpn.name
    static_ip_name = aws_lightsail_static_ip.vpn.name
    ssh_key_name   = var.ssh_public_key == null ? coalesce(var.ssh_key_pair_name, "provider-default") : aws_lightsail_key_pair.vpn[0].name
  }
}
output "readiness_hint" {
  description = "Remote readiness marker created by cloud-init"
  value       = "/var/lib/ephemeral-vpn/ready"
}
