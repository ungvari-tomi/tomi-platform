variable "analyst_enabled" {
  type        = bool
  default     = false
  description = "Opt-in private snapshot-only business analyst; requires dashboard."
}

variable "analyst_provider" {
  type    = string
  default = "mock"
  validation {
    condition     = contains(["mock", "ollama", "openai"], var.analyst_provider)
    error_message = "Use mock, ollama or openai."
  }
}

variable "analyst_model" {
  type    = string
  default = ""
}

variable "analyst_allow_hosted" {
  type    = bool
  default = false
}

variable "analyst_key_file" {
  type        = string
  default     = ""
  description = "Ignored private hosted-provider key path only; contents never enter Terraform."
}

locals {
  analyst_key_path         = replace(abspath(var.analyst_key_file), "\\", "/")
  analyst_key_mount_source = can(regex("^[A-Za-z]:/", local.analyst_key_path)) ? "/run/desktop/mnt/host/${lower(substr(local.analyst_key_path, 0, 1))}/${substr(local.analyst_key_path, 3, length(local.analyst_key_path) - 3)}" : local.analyst_key_path
}

resource "docker_network" "analyst" {
  count    = var.analyst_enabled ? 1 : 0
  name     = "retailflow-analyst-private"
  driver   = "bridge"
  internal = true
}

# A model adapter needs outbound connectivity. HTTP allowlists are application
# controls, not a per-destination network firewall. Mock mode has no egress bridge.
resource "docker_network" "analyst_model" {
  count  = var.analyst_enabled && var.analyst_provider != "mock" ? 1 : 0
  name   = "retailflow-analyst-model-access"
  driver = "bridge"
}

resource "docker_image" "analyst" {
  count        = var.analyst_enabled ? 1 : 0
  name         = "retailflow-analyst:local"
  keep_locally = true
  build {
    context    = local.application_context
    dockerfile = "infra/analyst/Dockerfile"
  }
  triggers = {
    runtime = sha256(join("", [for name in sort(tolist(fileset("${path.module}/../../retailflow_ai", "*.py"))) : filesha256("${path.module}/../../retailflow_ai/${name}")]))
    build   = sha256(join("", [for name in ["Dockerfile", "Dockerfile.dockerignore", "requirements.txt"] : filesha256("${path.module}/../analyst/${name}")]))
    readers = sha256(join("", [for name in ["reporting.py", "catalog_reporting.py"] : filesha256("${path.module}/../dbt/${name}")]))
  }
}

resource "docker_container" "analyst" {
  count          = var.analyst_enabled ? 1 : 0
  name           = "retailflow-analyst"
  image          = docker_image.analyst[0].image_id
  user           = "10001:10001"
  read_only      = true
  restart        = "on-failure"
  must_run       = true
  remove_volumes = false
  memory         = 512
  memory_swap    = 768
  cpus           = "0.5"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  networks_advanced { name = docker_network.analyst[0].name }
  dynamic "networks_advanced" {
    for_each = docker_network.analyst_model
    content { name = networks_advanced.value.name }
  }
  env = concat([
    "RETAILFLOW_AI_PROVIDER=${var.analyst_provider}",
    "RETAILFLOW_AI_MODEL=${var.analyst_model}",
    "RETAILFLOW_AI_ALLOW_HOSTED=${var.analyst_allow_hosted}",
  ], var.analyst_provider == "ollama" ? ["RETAILFLOW_AI_OLLAMA_ENDPOINT=http://host.docker.internal:11434/api/chat"] : [])
  mounts {
    target    = "/reports"
    source    = docker_volume.reporting[0].name
    type      = "volume"
    read_only = true
  }
  dynamic "mounts" {
    for_each = var.analyst_provider == "openai" ? [var.analyst_key_file] : []
    content {
      target    = "/run/secrets/model-key"
      source    = local.analyst_key_mount_source
      type      = "bind"
      read_only = true
    }
  }
  tmpfs = { "/tmp" = "rw,noexec,nosuid,size=64m,uid=10001,gid=10001" }
  healthcheck {
    test         = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3).close()"]
    interval     = "30s"
    timeout      = "5s"
    retries      = 3
    start_period = "10s"
  }
  lifecycle {
    precondition {
      condition     = var.dashboard_enabled
      error_message = "The analyst requires the existing reporting dashboard volume."
    }
    precondition {
      condition     = var.analyst_provider == "mock" || (length(var.analyst_model) > 0 && length(var.analyst_model) <= 100)
      error_message = "Choose an installed/supported model explicitly. Terraform never pulls a model."
    }
    precondition {
      condition     = var.analyst_provider != "openai" || (var.analyst_allow_hosted && var.analyst_key_file != "")
      error_message = "Hosted calls require cost/data-sharing consent and an existing private key file."
    }
  }
  depends_on = [docker_container.reporting_permissions]
}
