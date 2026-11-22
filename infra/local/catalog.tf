variable "catalog_pipeline_enabled" {
  type        = bool
  default     = false
  description = "Opt-in paused catalog workflow; requires deployed MongoDB and reviewed secret mounts."
}

locals {
  catalog_secrets_path  = abspath("${path.module}/../../.local-secrets/mongodb")
  catalog_secrets_mount = can(regex("^[A-Za-z]:/", local.catalog_secrets_path)) ? "/run/desktop/mnt/host/${lower(substr(local.catalog_secrets_path, 0, 1))}/${substr(local.catalog_secrets_path, 3, length(local.catalog_secrets_path) - 3)}" : local.catalog_secrets_path
}
