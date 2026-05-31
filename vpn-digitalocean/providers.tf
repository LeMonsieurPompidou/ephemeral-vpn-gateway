terraform {
  required_version = ">= 1.5.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = ">= 2.0.0"
    }

    wireguard = {
      source  = "OJFord/wireguard"
      version = ">= 0.4.0"
    }

    local = {
      source  = "hashicorp/local"
      version = ">= 2.0.0"
    }
  }
}

provider "digitalocean" {
  token = var.do_token
}