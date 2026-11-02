variable "azurite_account_key" {
  type        = string
  sensitive   = true
  description = "Emulator-only key supplied from the ignored .env by the local helper."
}

resource "docker_volume" "azurite" {
  name = "tomi-platform_azurite-data"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [labels]
  }
}

resource "docker_network" "azurite" {
  name   = "tomi-platform_default"
  driver = "bridge"

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [labels, ipam_config]
  }
}

resource "docker_container" "azurite" {
  name  = "tomi-platform-azurite-1"
  image = "mcr.microsoft.com/azure-storage/azurite:3.37.0"

  command        = ["azurite-blob", "--blobHost", "0.0.0.0", "--location", "/data", "--disableTelemetry"]
  env            = ["AZURITE_ACCOUNTS=retailflow:${var.azurite_account_key}"]
  network_mode   = docker_network.azurite.name
  security_opts  = ["no-new-privileges:true"]
  restart        = "no"
  must_run       = true
  remove_volumes = false

  capabilities {
    drop = ["ALL"]
  }

  ports {
    internal = 10000
    external = 10000
    ip       = "127.0.0.1"
    protocol = "tcp"
  }

  mounts {
    target = "/data"
    source = docker_volume.azurite.name
    type   = "volume"
  }

  lifecycle {
    # Preserve legacy Compose labels without putting machine-specific paths into source.
    ignore_changes = [labels]
  }
}
