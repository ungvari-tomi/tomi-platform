variable "extraction_enabled" {
  type        = bool
  default     = false
  description = "Opt-in one-shot extraction. Review landing backup and saved plan before enabling."
  validation {
    condition     = !var.extraction_enabled || var.enabled
    error_message = "Extraction requires the catalog to be enabled."
  }
}

variable "extraction_run_id" {
  type        = string
  default     = "manual-1"
  description = "Stable retry identity; change only for an explicitly requested new snapshot."
  validation {
    condition     = length(var.extraction_run_id) > 0 && length(var.extraction_run_id) <= 256
    error_message = "Provide a nonempty bounded extraction run identity."
  }
}

locals {
  extraction_context = abspath("${path.module}/../../..")
  extraction_inputs = [
    "infra/mongodb/extraction/Dockerfile", "infra/mongodb/extraction/Dockerfile.dockerignore",
    "infra/mongodb/extraction/requirements.txt", "infra/mongodb/extract.py",
    "infra/mongodb/catalog.py", "retailflow/__init__.py",
    "retailflow/batch_landing.py", "retailflow/blob_storage.py",
  ]
}

resource "docker_image" "extraction" {
  count        = var.extraction_enabled ? 1 : 0
  name         = "retailflow-catalog-extraction:local"
  keep_locally = true
  build {
    context    = local.extraction_context
    dockerfile = "infra/mongodb/extraction/Dockerfile"
  }
  triggers = {
    sources = sha256(join("", [for name in local.extraction_inputs : filesha256("${local.extraction_context}/${name}")]))
  }
}

resource "docker_volume" "extraction_state" {
  count = var.extraction_enabled ? 1 : 0
  name  = "retailflow-catalog-extraction-state"
  lifecycle { prevent_destroy = true }
}

resource "docker_container" "extraction" {
  count          = var.extraction_enabled ? 1 : 0
  name           = "retailflow-catalog-extraction"
  image          = docker_image.extraction[0].image_id
  user           = "10001:10001"
  read_only      = true
  attach         = true
  must_run       = false
  remove_volumes = false
  restart        = "no"
  memory         = 256
  memory_swap    = 384
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  networks_advanced { name = docker_network.catalog[0].name }
  networks_advanced { name = "tomi-platform_default" }
  env = ["RETAILFLOW_BLOB_ENDPOINT=http://tomi-platform-azurite-1:10000/retailflow"]
  command = [
    "--run-id", var.extraction_run_id,
    "--blob-key-file", "/run/secrets/azurite-key",
  ]
  mounts {
    target    = "/run/secrets/reader.json"
    source    = "${local.secrets_mount}/reader.json"
    type      = "bind"
    read_only = true
  }
  mounts {
    target    = "/run/secrets/azurite-key"
    source    = "${local.secrets_mount}/azurite-key"
    type      = "bind"
    read_only = true
  }
  mounts {
    target = "/state"
    source = docker_volume.extraction_state[0].name
    type   = "volume"
  }
  depends_on = [docker_container.verify]
}
