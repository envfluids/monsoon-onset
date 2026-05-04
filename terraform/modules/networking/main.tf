# -----------------------------------------------------------------------------
# Networking Module
# Creates VPC, subnets, Cloud NAT, and firewall rules
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# VPC
# -----------------------------------------------------------------------------

resource "google_compute_network" "main" {
  name                    = "${var.name_prefix}-${var.environment}-vpc"
  project                 = var.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

# -----------------------------------------------------------------------------
# Subnet
# -----------------------------------------------------------------------------

resource "google_compute_subnetwork" "main" {
  name          = "${var.name_prefix}-${var.environment}-subnet"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.main.id
  ip_cidr_range = var.subnet_cidr

  # Enable private Google access for Cloud Run, GCS, etc.
  private_ip_google_access = true

  # Secondary ranges for GKE if ever needed
  # secondary_ip_range {
  #   range_name    = "pods"
  #   ip_cidr_range = "10.1.0.0/16"
  # }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# -----------------------------------------------------------------------------
# Cloud NAT
# Enables outbound internet access for resources without public IPs
# -----------------------------------------------------------------------------

resource "google_compute_router" "main" {
  name    = "${var.name_prefix}-${var.environment}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  name    = "${var.name_prefix}-${var.environment}-nat"
  project = var.project_id
  region  = var.region
  router  = google_compute_router.main.name

  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# -----------------------------------------------------------------------------
# Firewall Rules
# -----------------------------------------------------------------------------

# Allow internal communication within VPC
resource "google_compute_firewall" "allow_internal" {
  name    = "${var.name_prefix}-${var.environment}-allow-internal"
  project = var.project_id
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "udp"
    ports    = ["0-65535"]
  }

  allow {
    protocol = "icmp"
  }

  source_ranges = [var.subnet_cidr]
}

# Allow SSH from IAP (Identity-Aware Proxy) for secure access
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "${var.name_prefix}-${var.environment}-allow-iap-ssh"
  project = var.project_id
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # IAP's IP range
  source_ranges = ["35.235.240.0/20"]

  target_tags = ["allow-iap-ssh"]
}

# Allow health checks from Google's health check ranges
resource "google_compute_firewall" "allow_health_checks" {
  name    = "${var.name_prefix}-${var.environment}-allow-health-checks"
  project = var.project_id
  network = google_compute_network.main.name

  allow {
    protocol = "tcp"
    ports    = ["80", "443", "8080"]
  }

  source_ranges = [
    "130.211.0.0/22", # Google health check
    "35.191.0.0/16",  # Google health check
  ]

  target_tags = ["allow-health-check"]
}

