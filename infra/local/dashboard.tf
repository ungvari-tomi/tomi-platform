variable "dashboard_enabled" {
  type        = bool
  default     = false
  description = "Opt-in sales reporting publication and loopback-only Streamlit dashboard."
}

resource "docker_volume" "reporting" {
  count = var.dashboard_enabled ? 1 : 0
  name  = "retailflow-reporting-snapshots"
  lifecycle { prevent_destroy = true }
}

resource "docker_network" "dashboard" {
  count  = var.dashboard_enabled ? 1 : 0
  name   = "retailflow-dashboard"
  driver = "bridge"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "dashboard" {
  count        = var.dashboard_enabled ? 1 : 0
  name         = "retailflow-dashboard:local"
  keep_locally = true
  build {
    context    = local.application_context
    dockerfile = "infra/dashboard/Dockerfile"
  }
  triggers = {
    inputs       = sha256(join("", [for name in ["Dockerfile", "requirements.txt", "app.py", "catalog_page.py", "analyst_page.py"] : filesha256("${path.module}/../dashboard/${name}")]))
    reader       = filesha256("${path.module}/../dbt/reporting.py")
    catalog      = filesha256("${path.module}/../dbt/catalog_reporting.py")
    dockerignore = filesha256("${path.module}/../dashboard/Dockerfile.dockerignore")
  }
}

resource "docker_container" "reporting_permissions" {
  count          = var.dashboard_enabled ? 1 : 0
  name           = "retailflow-reporting-permissions"
  image          = docker_image.dashboard[0].image_id
  command        = ["python", "-c", "import os; os.chmod('/reports', 0o2770)"]
  user           = "10001:10001"
  network_mode   = "none"
  read_only      = true
  attach         = true
  must_run       = false
  remove_volumes = false
  memory         = 64
  memory_swap    = 128
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    target = "/reports"
    source = docker_volume.reporting[0].name
    type   = "volume"
  }
}

resource "docker_container" "dashboard" {
  count          = var.dashboard_enabled ? 1 : 0
  name           = "retailflow-dashboard"
  image          = docker_image.dashboard[0].image_id
  network_mode   = var.analyst_enabled ? null : docker_network.dashboard[0].name
  user           = "10001:10001"
  read_only      = true
  must_run       = true
  restart        = "on-failure"
  memory         = 512
  memory_swap    = 1024
  cpus           = "0.5"
  remove_volumes = false
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  env = var.analyst_enabled ? ["RETAILFLOW_AI_ENABLED=true"] : []
  dynamic "networks_advanced" {
    for_each = var.analyst_enabled ? [docker_network.dashboard[0].name, docker_network.analyst[0].name] : []
    content { name = networks_advanced.value }
  }
  mounts {
    target    = "/reports"
    source    = docker_volume.reporting[0].name
    type      = "volume"
    read_only = true
  }
  tmpfs = { "/tmp" = "rw,noexec,nosuid,size=128m,uid=10001,gid=10001" }
  ports {
    internal = 8501
    external = 8501
    ip       = "127.0.0.1"
  }
  healthcheck {
    test         = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).close()"]
    interval     = "30s"
    timeout      = "5s"
    retries      = 3
    start_period = "20s"
  }
  depends_on = [docker_container.reporting_permissions]
}
