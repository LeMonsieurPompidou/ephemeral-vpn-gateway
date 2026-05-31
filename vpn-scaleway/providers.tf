terraform {
  required_version = ">= 1.5.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0.0"
    }

    wireguard = {
      source  = "OJFord/wireguard"
      version = ">= 0.2.0"
    }

    scaleway = {
      source  = "scaleway/scaleway"
      version = ">= 2.0.0"
    }
  }
}

provider "scaleway" {
  project_id = var.scaleway_project_id
  access_key = var.scaleway_access_key
  secret_key = var.scaleway_secret_key

  region = "fr-par"
  zone   = "fr-par-1"
}
