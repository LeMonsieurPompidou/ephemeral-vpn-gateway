locals {
  vpn_name = "ephemeral-vpn-lightsail"
  cloud_init = templatefile("${path.module}/../terraform-common/cloud-init.yaml.tftpl", {
    wireguard_port     = var.wireguard_port
    server_private_key = var.server_private_key
    client_public_key  = var.client_public_key
  })
}
resource "aws_lightsail_instance" "vpn" {
  name              = local.vpn_name
  availability_zone = "${var.region}${var.availability_zone_suffix}"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = var.instance_type
  key_pair_name     = var.ssh_key_pair_name
  user_data         = local.cloud_init
  tags              = { Purpose = "ephemeral-wireguard" }
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
  value       = { instance_name = aws_lightsail_instance.vpn.name, static_ip_name = aws_lightsail_static_ip.vpn.name }
}
output "readiness_hint" {
  description = "Remote readiness marker created by cloud-init"
  value       = "/var/lib/cloud/instance/wireguard-ready"
}
