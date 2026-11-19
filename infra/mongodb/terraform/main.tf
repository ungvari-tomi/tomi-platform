terraform {
  required_version = "= 1.17.1"
  required_providers {
    docker = { source = "kreuzwerker/docker", version = "4.6.0" }
  }
}

provider "docker" {
  host = var.docker_host
}

variable "docker_host" {
  type    = string
  default = "npipe:////./pipe/dockerDesktopLinuxEngine"
  validation {
    condition     = contains(["npipe:////./pipe/dockerDesktopLinuxEngine", "unix:///var/run/docker.sock"], var.docker_host)
    error_message = "Use a local Docker Desktop pipe or local Unix socket only."
  }
}

variable "secrets_directory" {
  type        = string
  default     = null
  description = "Public host directory path only; never supply credential values to Terraform."
}

variable "enabled" {
  type    = bool
  default = false
}

locals {
  context      = abspath("${path.module}/..")
  secrets_path = var.secrets_directory == null ? abspath("${path.module}/../../../.local-secrets/mongodb") : abspath(var.secrets_directory)
  # Docker Desktop reports canonical Linux VM paths for Windows bind mounts.
  secrets_mount = can(regex("^[A-Za-z]:/", local.secrets_path)) ? "/run/desktop/mnt/host/${lower(substr(local.secrets_path, 0, 1))}/${substr(local.secrets_path, 3, length(local.secrets_path) - 3)}" : local.secrets_path
}

resource "docker_network" "catalog" {
  count    = var.enabled ? 1 : 0
  name     = "retailflow-product-catalog"
  internal = true
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "data" {
  count = var.enabled ? 1 : 0
  name  = "retailflow-mongodb-data"
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "backups" {
  count = var.enabled ? 1 : 0
  name  = "retailflow-mongodb-backups"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "server" {
  count        = var.enabled ? 1 : 0
  name         = "retailflow-mongodb:local"
  keep_locally = true
  build {
    context    = local.context
    dockerfile = "server.Dockerfile"
  }
  triggers = {
    sources = sha256(join("", [for name in ["server.Dockerfile", ".dockerignore", "contract.json", "init.js", "health.js", "backup.js"] : filesha256("${local.context}/${name}")]))
  }
}

resource "docker_image" "client" {
  count        = var.enabled ? 1 : 0
  name         = "retailflow-catalog:local"
  keep_locally = true
  build {
    context    = local.context
    dockerfile = "client.Dockerfile"
  }
  triggers = {
    sources = sha256(join("", [for name in ["client.Dockerfile", ".dockerignore", "catalog.py", "requirements.txt"] : filesha256("${local.context}/${name}")]))
  }
}

resource "docker_container" "mongodb" {
  count          = var.enabled ? 1 : 0
  name           = "retailflow-mongodb"
  image          = docker_image.server[0].image_id
  network_mode   = docker_network.catalog[0].name
  user           = "999:999"
  read_only      = true
  must_run       = true
  restart        = "unless-stopped"
  remove_volumes = false
  stop_timeout   = 60
  memory         = 1024
  memory_swap    = 1536
  cpus           = "1.0"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  # File paths only: no passwords/file contents enter Terraform values or state.
  # pragma: allowlist nextline secret
  env = ["MONGO_INITDB_ROOT_USERNAME_FILE=/run/secrets/admin-user", "MONGO_INITDB_ROOT_PASSWORD_FILE=/run/secrets/admin-password"]
  mounts {
    target = "/data/db"
    source = docker_volume.data[0].name
    type   = "volume"
  }
  mounts {
    target = "/backups"
    source = docker_volume.backups[0].name
    type   = "volume"
  }
  dynamic "mounts" {
    for_each = toset(["admin-user", "admin-password", "reader.json", "writer.json"])
    content {
      target    = "/run/secrets/${mounts.value}"
      source    = "${local.secrets_mount}/${mounts.value}"
      type      = "bind"
      read_only = true
    }
  }
  tmpfs = {
    "/tmp"           = "rw,noexec,nosuid,size=64m,uid=999,gid=999"
    "/data/configdb" = "rw,noexec,nosuid,size=16m,uid=999,gid=999"
  }
  healthcheck {
    test         = ["CMD", "mongosh", "--quiet", "--nodb", "--file", "/opt/catalog/health.js"]
    interval     = "30s"
    timeout      = "10s"
    retries      = 10
    start_period = "30s"
  }
  wait         = true
  wait_timeout = 180
  lifecycle {
    precondition {
      condition     = alltrue([for name in ["admin-user", "admin-password", "reader.json", "writer.json"] : fileexists("${local.secrets_path}/${name}")])
      error_message = "Generate and protect the ignored catalog credential files before an enabled plan."
    }
  }
}

resource "docker_container" "seed" {
  count          = var.enabled ? 1 : 0
  name           = "retailflow-catalog-seed"
  image          = docker_image.client[0].image_id
  network_mode   = docker_network.catalog[0].name
  user           = "10001:10001"
  read_only      = true
  command        = ["seed"]
  attach         = true
  must_run       = false
  remove_volumes = false
  memory         = 128
  memory_swap    = 256
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    target    = "/run/secrets/catalog.json"
    source    = "${local.secrets_mount}/writer.json"
    type      = "bind"
    read_only = true
  }
  depends_on = [docker_container.mongodb]
}

resource "docker_container" "verify" {
  count          = var.enabled ? 1 : 0
  name           = "retailflow-catalog-verify"
  image          = docker_image.client[0].image_id
  network_mode   = docker_network.catalog[0].name
  user           = "10001:10001"
  read_only      = true
  command        = ["verify"]
  attach         = true
  must_run       = false
  remove_volumes = false
  memory         = 128
  memory_swap    = 256
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    target    = "/run/secrets/catalog.json"
    source    = "${local.secrets_mount}/reader.json"
    type      = "bind"
    read_only = true
  }
  depends_on = [docker_container.seed]
}
