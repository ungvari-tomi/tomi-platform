resource "docker_volume" "duckdb" {
  name = "retailflow-duckdb-data"

  lifecycle {
    prevent_destroy = true
  }
}

resource "docker_volume" "duckdb_backups" {
  name = "retailflow-duckdb-backups"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "duckdb" {
  name         = "retailflow-duckdb:local"
  keep_locally = true

  build {
    context = abspath("${path.module}/../duckdb")
  }

  triggers = {
    context_sha256 = sha256(join("", [
      for name in ["Dockerfile", ".dockerignore", "requirements.txt", "runner.py", "recovery.py"] :
      filesha256("${path.module}/../duckdb/${name}")
    ]))
  }
}

resource "docker_container" "duckdb" {
  name           = "retailflow-duckdb"
  image          = docker_image.duckdb.image_id
  network_mode   = docker_network.azurite.name
  user           = "10001:10001"
  read_only      = true
  security_opts  = ["no-new-privileges:true"]
  cpus           = "1.0"
  memory         = 512
  memory_swap    = 1024
  must_run       = true
  restart        = "no"
  remove_volumes = false

  env = [
    "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb",
    "RETAILFLOW_DUCKDB_THREADS=1",
    "RETAILFLOW_DUCKDB_MEMORY_LIMIT=256MB",
    # Local backup mount path, not a credential.
    # pragma: allowlist nextline secret
    "RETAILFLOW_DUCKDB_BACKUP_DIR=/backups",
    "RETAILFLOW_DUCKDB_BACKUP_SECONDS=86400",
  ]

  capabilities {
    drop = ["ALL"]
  }

  mounts {
    target = "/warehouse"
    source = docker_volume.duckdb.name
    type   = "volume"
  }

  tmpfs = {
    "/tmp" = "rw,noexec,nosuid,size=64m,uid=10001,gid=10001"
  }

  mounts {
    target = "/backups"
    source = docker_volume.duckdb_backups.name
    type   = "volume"
  }

  healthcheck {
    test         = ["CMD", "python", "/app/runner.py", "check"]
    interval     = "30s"
    timeout      = "5s"
    retries      = 3
    start_period = "10s"
  }
  depends_on = [docker_container.warehouse_permissions]
}

resource "docker_container" "warehouse_permissions" {
  name           = "retailflow-warehouse-permissions"
  image          = docker_image.duckdb.image_id
  user           = "10001:10001"
  command        = ["python", "/app/runner.py", "permissions"]
  network_mode   = "none"
  read_only      = true
  attach         = true
  must_run       = false
  remove_volumes = false
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    target = "/warehouse"
    source = docker_volume.duckdb.name
    type   = "volume"
  }
}
