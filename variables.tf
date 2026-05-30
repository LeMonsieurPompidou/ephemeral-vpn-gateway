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
