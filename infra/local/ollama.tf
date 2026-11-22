variable "ollama_enabled" {
  type        = bool
  default     = false
  description = "Opt-in protected local model storage/runtime; retain true after creation."
}

resource "docker_volume" "ollama_models" {
  count = var.ollama_enabled ? 1 : 0
  name  = "retailflow-ollama-models"
  lifecycle { prevent_destroy = true }
}

# Only the exited seeder joins this download bridge. Serving has no egress route.
resource "docker_network" "ollama_download" {
  count  = var.ollama_enabled ? 1 : 0
  name   = "retailflow-ollama-download"
  driver = "bridge"
}

resource "docker_image" "ollama" {
  count        = var.ollama_enabled ? 1 : 0
  name         = "retailflow-ollama:local"
  keep_locally = true
  build { context = abspath("${path.module}/../ollama") }
  triggers = {
    inputs = sha256(join("", [for name in ["Dockerfile", ".dockerignore", "seed.sh"] : filesha256("${path.module}/../ollama/${name}")]))
  }
}

resource "docker_container" "ollama_seed" {
  count          = var.ollama_enabled ? 1 : 0
  name           = "retailflow-ollama-seed"
  image          = docker_image.ollama[0].image_id
  entrypoint     = ["/bin/sh", "/app/seed.sh"]
  command        = []
  user           = "10001:10001"
  network_mode   = docker_network.ollama_download[0].name
  attach         = true
  must_run       = false
  read_only      = true
  remove_volumes = false
  memory         = 512
  memory_swap    = 512
  cpus           = "1"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    source = docker_volume.ollama_models[0].name
    target = "/models"
    type   = "volume"
  }
  tmpfs = { "/tmp" = "rw,noexec,nosuid,size=64m,uid=10001,gid=10001" }
}

resource "docker_container" "ollama" {
  count          = var.ollama_enabled ? 1 : 0
  name           = "retailflow-ollama"
  image          = docker_image.ollama[0].image_id
  user           = "10001:10001"
  read_only      = true
  must_run       = true
  restart        = "on-failure"
  remove_volumes = false
  memory         = 3072
  memory_swap    = 3072
  cpus           = "2"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  networks_advanced { name = docker_network.analyst_model[0].name }
  mounts {
    source    = docker_volume.ollama_models[0].name
    target    = "/models"
    type      = "volume"
    read_only = true
  }
  tmpfs = { "/tmp" = "rw,noexec,nosuid,size=64m,uid=10001,gid=10001" }
  healthcheck {
    test         = ["CMD", "ollama", "list"]
    interval     = "30s"
    timeout      = "5s"
    retries      = 3
    start_period = "15s"
  }
  lifecycle {
    precondition {
      condition     = var.analyst_model == "qwen3:1.7b"
      error_message = "This reviewed CPU runtime seeds only the pinned qwen3:1.7b manifest."
    }
  }
  depends_on = [docker_container.ollama_seed]
}
