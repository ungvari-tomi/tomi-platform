resource "docker_volume" "dbt_artifacts" {
  name = "retailflow-dbt-artifacts"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "dbt" {
  name         = "retailflow-dbt:local"
  keep_locally = true
  build {
    context = abspath("${path.module}/../dbt")
  }
  triggers = {
    context_sha256 = sha256(join("", [
      for name in sort(tolist(fileset("${path.module}/../dbt", "**"))) :
      filesha256("${path.module}/../dbt/${name}")
      if !startswith(name, ".venv/") && !strcontains(name, "__pycache__")
    ]))
  }
}

resource "docker_container" "dbt" {
  name           = "retailflow-dbt"
  image          = docker_image.dbt.image_id
  command        = ["check"]
  network_mode   = "none"
  user           = "10001:10001"
  read_only      = true
  attach         = true
  must_run       = false
  remove_volumes = false
  memory         = 512
  memory_swap    = 1024
  cpus           = "1.0"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  env = [
    "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb",
    "RETAILFLOW_DBT_ARTIFACT_DIR=/artifacts",
    "DBT_SEND_ANONYMOUS_USAGE_STATS=false",
    "RETAILFLOW_DATASET_VERSION=${var.orders_dataset_version}",
    "RETAILFLOW_SEED=${var.orders_seed}",
    "RETAILFLOW_INTERVAL_SECONDS=${var.orders_interval_seconds}",
  ]
  mounts {
    target = "/warehouse"
    source = docker_volume.duckdb.name
    type   = "volume"
  }
  mounts {
    target = "/artifacts"
    source = docker_volume.dbt_artifacts.name
    type   = "volume"
  }
  tmpfs = {
    "/tmp" = "rw,noexec,nosuid,size=128m,uid=10001,gid=10001"
  }
  depends_on = [docker_container.duckdb]
}
