variable "region" {
  # Valid alternative Scaleway zones: nl-ams-1 (Amsterdam Zone 1), pl-waw-1 (Warsaw Zone 1).
  description = "Scaleway zone selector for the instance."
  type        = string
  default     = "fr-par-1"
}

variable "scaleway_project_id" {
  description = "Scaleway project ID"
  type        = string
}

variable "scaleway_access_key" {
  description = "Scaleway API access key"
  type        = string
}

variable "scaleway_secret_key" {
  description = "Scaleway API secret key"
  type        = string
  sensitive   = true
}
