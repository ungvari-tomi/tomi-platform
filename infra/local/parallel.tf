variable "parallel_ingestion_enabled" {
  type        = bool
  default     = false
  description = "Opt-in Kafka inventory streaming and shipment webhooks; requires reviewed deployment."
}

variable "event_interval_seconds" {
  type    = number
  default = 600
  validation {
    condition     = var.event_interval_seconds >= 5 && var.event_interval_seconds <= 3600 && floor(var.event_interval_seconds) == var.event_interval_seconds
    error_message = "Use 600 seconds normally, or 5 seconds for disposable demonstrations."
  }
}

locals {
  parallel_roles = var.parallel_ingestion_enabled ? toset([
    "inventory-producer", "inventory-consumer", "shipment-simulator", "shipment-webhook"
  ]) : toset([])
  parallel_sources = [
    "__init__.py", "api.py", "currencies.py", "customers.py", "blob_storage.py",
    "event_contracts.py", "event_ingestion.py", "event_runtime.py", "shipment_webhook.py"
  ]
}

resource "docker_network" "inventory_stream" {
  count    = var.parallel_ingestion_enabled ? 1 : 0
  name     = "retailflow-inventory-stream"
  internal = true
  lifecycle { prevent_destroy = true }
}

resource "docker_network" "shipment_events" {
  count    = var.parallel_ingestion_enabled ? 1 : 0
  name     = "retailflow-shipment-events"
  internal = true
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "kafka" {
  count = var.parallel_ingestion_enabled ? 1 : 0
  name  = "retailflow-inventory-kafka"
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "event_state" {
  for_each = var.parallel_ingestion_enabled ? toset(["inventory-producer", "shipment-simulator"]) : toset([])
  name     = "retailflow-${each.key}-state"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "kafka" {
  count = var.parallel_ingestion_enabled ? 1 : 0
  # Verified official Apache Kafka 4.2.0 Linux amd64 manifest, not a credential.
  name         = "apache/kafka:4.2.0@sha256:b5556544da1dccd04f03690107a233c37dbfd01d66e28ffdfc4fafba9b6e5039"
  keep_locally = true
}

resource "docker_container" "kafka" {
  count          = var.parallel_ingestion_enabled ? 1 : 0
  name           = "retailflow-kafka"
  image          = docker_image.kafka[0].image_id
  network_mode   = docker_network.inventory_stream[0].name
  user           = "1000:1000"
  memory         = 1024
  memory_swap    = 1536
  cpus           = "1.0"
  must_run       = true
  remove_volumes = false
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  # Official startup configures writable image paths; durable records use the protected volume.
  env = [
    # Public, stable local KRaft cluster identity; never rotate behind an existing volume.
    # pragma: allowlist nextline secret
    "CLUSTER_ID=MkU3OEVBNTcwNTJENDM2Qk",
    "KAFKA_NODE_ID=1", "KAFKA_PROCESS_ROLES=broker,controller",
    "KAFKA_CONTROLLER_QUORUM_VOTERS=1@retailflow-kafka:9093",
    "KAFKA_LISTENERS=INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:19092",
    "KAFKA_ADVERTISED_LISTENERS=INTERNAL://retailflow-kafka:9092,EXTERNAL://127.0.0.1:19092",
    "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT",
    "KAFKA_INTER_BROKER_LISTENER_NAME=INTERNAL", "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
    "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1", "KAFKA_DEFAULT_REPLICATION_FACTOR=1",
    "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1", "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1",
    "KAFKA_AUTO_CREATE_TOPICS_ENABLE=false", "KAFKA_LOG_DIRS=/var/lib/kafka/data",
    "KAFKA_LOG_RETENTION_HOURS=168", "KAFKA_LOG_SEGMENT_BYTES=16777216",
    "KAFKA_HEAP_OPTS=-Xms256m -Xmx512m"
  ]
  mounts {
    target = "/var/lib/kafka/data"
    source = docker_volume.kafka[0].name
    type   = "volume"
  }
  ports {
    internal = 19092
    external = 19092
    ip       = "127.0.0.1"
  }
  healthcheck {
    test         = ["CMD", "/opt/kafka/bin/kafka-broker-api-versions.sh", "--bootstrap-server", "retailflow-kafka:9092"]
    interval     = "15s"
    timeout      = "10s"
    retries      = 12
    start_period = "30s"
  }
  wait         = true
  wait_timeout = 180
}

resource "docker_image" "parallel" {
  count        = var.parallel_ingestion_enabled ? 1 : 0
  name         = "retailflow-parallel:local"
  keep_locally = true
  build {
    context    = local.application_context
    dockerfile = "infra/parallel/Dockerfile"
  }
  triggers = {
    application  = sha256(join("", [for name in local.parallel_sources : filesha256("${path.module}/../../retailflow/${name}")]))
    dockerfile   = filesha256("${path.module}/../parallel/Dockerfile")
    requirements = filesha256("${path.module}/../parallel/requirements.txt")
    dockerignore = filesha256("${path.module}/../../.dockerignore")
  }
}

resource "docker_container" "inventory_topic" {
  count         = var.parallel_ingestion_enabled ? 1 : 0
  name          = "retailflow-inventory-topic-init"
  image         = docker_image.parallel[0].image_id
  user          = "10001:10001"
  read_only     = true
  network_mode  = docker_network.inventory_stream[0].name
  command       = ["python", "-m", "retailflow.event_runtime", "init-topics"]
  env           = ["RETAILFLOW_KAFKA_BOOTSTRAP=retailflow-kafka:9092"]
  must_run      = false
  start         = true
  memory        = 192
  security_opts = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  depends_on = [docker_container.kafka]
}

resource "docker_container" "parallel" {
  for_each       = local.parallel_roles
  name           = "retailflow-${each.key}"
  image          = docker_image.parallel[0].image_id
  user           = "10001:10001"
  read_only      = true
  must_run       = true
  restart        = "on-failure"
  remove_volumes = false
  memory         = 256
  memory_swap    = 512
  cpus           = "0.5"
  security_opts  = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  networks_advanced {
    name = startswith(each.key, "inventory-") ? docker_network.inventory_stream[0].name : docker_network.shipment_events[0].name
  }
  dynamic "networks_advanced" {
    for_each = contains(["inventory-consumer", "shipment-webhook"], each.key) ? [1] : []
    content { name = docker_network.azurite.name }
  }
  command = each.key == "shipment-webhook" ? [
    "python", "-m", "uvicorn", "retailflow.shipment_webhook:app", "--host", "0.0.0.0", "--port", "8090"
  ] : ["python", "-m", "retailflow.event_runtime", each.key]
  env = concat([
    "RETAILFLOW_SEED=${var.orders_seed}", "RETAILFLOW_INTERVAL_SECONDS=${var.orders_interval_seconds}",
    "RETAILFLOW_EVENT_INTERVAL_SECONDS=${var.event_interval_seconds}",
    "RETAILFLOW_EVENT_STATE_DIR=/state", "RETAILFLOW_KAFKA_BOOTSTRAP=retailflow-kafka:9092"
    ], contains(["inventory-consumer", "shipment-webhook"], each.key) ? [
    "AZURITE_ACCOUNT_KEY=${var.azurite_account_key}",
    "RETAILFLOW_BLOB_ENDPOINT=http://tomi-platform-azurite-1:10000/retailflow",
    "RETAILFLOW_DUCKDB_PATH=/warehouse/retailflow.duckdb"
    ] : [], startswith(each.key, "shipment-") ? [
    "RETAILFLOW_SHIPMENT_WEBHOOK_URL=http://retailflow-shipment-webhook:8090",
    "RETAILFLOW_SHIPMENT_KEY_FILE=/run/retailflow-secrets/shipment-webhook.key"
  ] : [])
  dynamic "mounts" {
    for_each = contains(["inventory-producer", "shipment-simulator"], each.key) ? [1] : []
    content {
      target = "/state"
      source = docker_volume.event_state[each.key].name
      type   = "volume"
    }
  }
  dynamic "mounts" {
    for_each = contains(["inventory-consumer", "shipment-webhook"], each.key) ? [1] : []
    content {
      target = "/warehouse"
      source = docker_volume.duckdb.name
      type   = "volume"
    }
  }
  dynamic "mounts" {
    for_each = startswith(each.key, "shipment-") ? [1] : []
    content {
      target    = "/run/retailflow-secrets/shipment-webhook.key"
      source    = abspath("${path.module}/../../.local-secrets/shipment-webhook.key")
      type      = "bind"
      read_only = true
    }
  }
  dynamic "ports" {
    for_each = each.key == "shipment-webhook" ? [1] : []
    content {
      internal = 8090
      external = 18090
      ip       = "127.0.0.1"
    }
  }
  depends_on = [docker_container.inventory_topic, docker_container.azurite, docker_container.duckdb]
}
