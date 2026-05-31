variable "do_token" {
  description = "DigitalOcean API token"
  type        = string
  sensitive   = true
}

variable "ssh_key_name" {
  description = "Name of the SSH key already uploaded to DigitalOcean"
  type        = string
}

variable "region" {
  description = "DigitalOcean region slug for the droplet. Use this to switch between data centers such as nyc1, nyc3, sfo3, ams3, fra1, and lon1."
  type        = string
  default     = "nyc3"
}