resource "docker_network" "mailpit" {
  name   = "retailflow-mailpit"
  driver = "bridge"
  lifecycle { prevent_destroy = true }
}

resource "docker_volume" "mailpit" {
  name = "retailflow-mailpit-data"
  lifecycle { prevent_destroy = true }
}

resource "docker_image" "mailpit" {
  name         = "retailflow-mailpit:local"
  keep_locally = true
  build { context = abspath("${path.module}/../mailpit") }
  triggers = {
    dockerfile   = filesha256("${path.module}/../mailpit/Dockerfile")
    dockerignore = filesha256("${path.module}/../mailpit/.dockerignore")
  }
}

resource "docker_container" "mailpit" {
  name          = "retailflow-mailpit"
  image         = docker_image.mailpit.image_id
  network_mode  = docker_network.mailpit.name
  user          = "10001:10001"
  read_only     = true
  security_opts = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  cpus           = "0.25"
  memory         = 128
  memory_swap    = 256
  must_run       = true
  restart        = "no"
  remove_volumes = false
  ports {
    internal = 1025
    external = 1025
    ip       = "127.0.0.1"
  }
  ports {
    internal = 8025
    external = 8025
    ip       = "127.0.0.1"
  }
  mounts {
    target = "/data"
    source = docker_volume.mailpit.name
    type   = "volume"
  }
  healthcheck {
    test           = ["CMD", "/mailpit", "readyz"]
    interval       = "15s"
    timeout        = "5s"
    retries        = 3
    start_period   = "10s"
    start_interval = "1s"
  }
}
