variable "airflow_db_password" {
  type      = string
  sensitive = true
}

variable "airflow_password" {
  type      = string
  sensitive = true
}

variable "airflow_postgres_admin_password" {
  type      = string
  sensitive = true
}

variable "airflow_fernet_key" {
  type      = string
  sensitive = true
}

variable "airflow_jwt_secret" {
  type      = string
  sensitive = true
}

variable "orders_interval_seconds" {
  type    = number
  default = 600
  validation {
    condition     = var.orders_interval_seconds >= 1 && var.orders_interval_seconds <= 3600 && floor(var.orders_interval_seconds) == var.orders_interval_seconds
    error_message = "Order generation interval must be an integer from 1 to 3600."
  }
}

variable "orders_dataset_version" {
  type    = string
  default = "orders-v3"
  validation {
    condition     = contains(["orders-v2", "orders-v3"], var.orders_dataset_version)
    error_message = "Unsupported orders dataset version."
  }
}

variable "orders_seed" {
  type    = number
  default = 42
}

locals {
  application_context = abspath("${path.module}/../..")
  application_hash = sha256(join("", [
    for name in sort(tolist(fileset("${path.module}/../../retailflow", "*.py"))) :
    filesha256("${path.module}/../../retailflow/${name}")
  ]))
  airflow_env = [
    "AIRFLOW__CORE__EXECUTOR=LocalExecutor",
    "AIRFLOW__CORE__LOAD_EXAMPLES=False",
    "AIRFLOW__CORE__PARALLELISM=2",
    "AIRFLOW__CORE__FERNET_KEY=${var.airflow_fernet_key}",
    "AIRFLOW__CORE__EXECUTION_API_SERVER_URL=http://retailflow-airflow-api-server:8080/execution/",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql+psycopg2://airflow:${var.airflow_db_password}@retailflow-airflow-postgres:5432/airflow",
    "AIRFLOW__API_AUTH__JWT_SECRET=${var.airflow_jwt_secret}",
    "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin",
    "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE=/opt/airflow/auth/passwords.json",
    "AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=True",
    "RETAILFLOW_AIRFLOW_PASSWORD=${var.airflow_password}",
    "AZURITE_ACCOUNT_KEY=${var.azurite_account_key}",
    "RETAILFLOW_BLOB_ENDPOINT=http://tomi-platform-azurite-1:10000/retailflow",
    "RETAILFLOW_ORDERS_URL=http://retailflow-orders-api:8000",
    # Local path, not a base64 credential.
    # pragma: allowlist nextline secret
    "RETAILFLOW_STATE_DIR=/opt/airflow/state",
    "RETAILFLOW_DATASET_VERSION=${var.orders_dataset_version}",
  ]
}

resource "docker_image" "orders_api" {
  name         = "retailflow-orders-api:local"
  keep_locally = true
  build {
    context    = local.application_context
    dockerfile = "infra/orchestration/api.Dockerfile"
  }
  triggers = {
    application  = local.application_hash
    dockerfile   = filesha256("${path.module}/../orchestration/api.Dockerfile")
    requirements = filesha256("${path.module}/../orchestration/api-requirements.txt")
    dockerignore = filesha256("${path.module}/../../.dockerignore")
  }
}

resource "docker_container" "orders_api" {
  name          = "retailflow-orders-api"
  image         = docker_image.orders_api.image_id
  network_mode  = docker_network.azurite.name
  user          = "10001:10001"
  read_only     = true
  must_run      = true
  memory        = 256
  memory_swap   = 512
  cpus          = "0.5"
  security_opts = ["no-new-privileges:true"]
  env = [
    "RETAILFLOW_INTERVAL_SECONDS=${var.orders_interval_seconds}",
    "RETAILFLOW_SEED=${var.orders_seed}",
    "RETAILFLOW_DATASET_VERSION=${var.orders_dataset_version}",
  ]
  capabilities { drop = ["ALL"] }
  ports {
    internal = 8000
    external = 18000
    ip       = "127.0.0.1"
  }
  healthcheck {
    test         = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
    interval     = "10s"
    timeout      = "5s"
    retries      = 5
    start_period = "10s"
  }
}

resource "docker_image" "airflow" {
  name         = "retailflow-airflow:local"
  keep_locally = true
  build {
    context    = local.application_context
    dockerfile = "infra/orchestration/airflow.Dockerfile"
  }
  triggers = {
    application  = local.application_hash
    dags         = filesha256("${path.module}/../../dags/retailflow_extraction.py")
    dockerfile   = filesha256("${path.module}/../orchestration/airflow.Dockerfile")
    constraints  = filesha256("${path.module}/../orchestration/constraints.txt")
    startup      = filesha256("${path.module}/../orchestration/start.py")
    duckdb       = filesha256("${path.module}/../orchestration/duckdb-requirements.txt")
    dbt_runtime  = filesha256("${path.module}/../dbt/run.py")
    dbt_packages = filesha256("${path.module}/../dbt/requirements.txt")
    dbt_project = sha256(join("", [
      for name in sort(tolist(setunion(
        fileset("${path.module}/../dbt/project", "**/*.sql"),
        fileset("${path.module}/../dbt/project", "**/*.yml")
      ))) : filesha256("${path.module}/../dbt/project/${name}")
    ]))
    dockerignore = filesha256("${path.module}/../../.dockerignore")
  }
}

resource "docker_image" "airflow_postgres" {
  name         = "postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5"
  keep_locally = true
}

resource "docker_volume" "airflow_postgres" {
  name = "retailflow-airflow-postgres-data"
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "airflow_logs" {
  name = "retailflow-airflow-logs"
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "airflow_state" {
  name = "retailflow-airflow-state"
  lifecycle { prevent_destroy = true }
}

resource "docker_container" "airflow_postgres" {
  name           = "retailflow-airflow-postgres"
  image          = docker_image.airflow_postgres.image_id
  network_mode   = docker_network.azurite.name
  must_run       = true
  wait           = true
  wait_timeout   = 120
  memory         = 512
  memory_swap    = 1024
  cpus           = "1.0"
  remove_volumes = false
  security_opts  = ["no-new-privileges:true"]
  env            = ["POSTGRES_USER=postgres", "POSTGRES_DB=airflow", "POSTGRES_PASSWORD=${var.airflow_postgres_admin_password}"]
  mounts {
    target = "/var/lib/postgresql/data"
    source = docker_volume.airflow_postgres.name
    type   = "volume"
  }
  healthcheck {
    test         = ["CMD", "pg_isready", "-U", "postgres", "-d", "airflow"]
    interval     = "5s"
    timeout      = "5s"
    retries      = 10
    start_period = "10s"
  }
}

resource "docker_container" "airflow_init" {
  name          = "retailflow-airflow-init"
  image         = docker_image.airflow.image_id
  network_mode  = docker_network.azurite.name
  user          = "50000:0"
  env           = concat(local.airflow_env, ["RETAILFLOW_POSTGRES_ADMIN_PASSWORD=${var.airflow_postgres_admin_password}"])
  command       = ["python", "/opt/airflow/start.py", "init"]
  attach        = true
  must_run      = false
  memory        = 1024
  memory_swap   = 2048
  security_opts = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  depends_on = [docker_container.airflow_postgres]
}

resource "docker_container" "airflow" {
  for_each       = toset(["api-server", "scheduler", "dag-processor"])
  name           = "retailflow-airflow-${each.key}"
  image          = docker_image.airflow.image_id
  network_mode   = docker_network.azurite.name
  user           = "50000:0"
  group_add      = each.key == "scheduler" ? ["10001"] : []
  env            = concat(local.airflow_env, each.key == "scheduler" ? ["RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb"] : [])
  command        = ["python", "/opt/airflow/start.py", each.key]
  memory         = each.key == "scheduler" ? 1536 : 1024
  memory_swap    = each.key == "scheduler" ? 3072 : 2048
  cpus           = "1.0"
  must_run       = true
  remove_volumes = false
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  mounts {
    target = "/opt/airflow/logs"
    source = docker_volume.airflow_logs.name
    type   = "volume"
  }
  mounts {
    target = "/opt/airflow/state"
    source = docker_volume.airflow_state.name
    type   = "volume"
  }
  dynamic "ports" {
    for_each = each.key == "api-server" ? [1] : []
    content {
      internal = 8080
      external = 8080
      ip       = "127.0.0.1"
    }
  }
  dynamic "mounts" {
    for_each = each.key == "scheduler" ? [1] : []
    content {
      target = "/warehouse"
      source = docker_volume.duckdb.name
      type   = "volume"
    }
  }
  depends_on = [docker_container.airflow_init, docker_container.orders_api, docker_container.azurite, docker_container.duckdb]
}
