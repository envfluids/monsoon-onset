# -----------------------------------------------------------------------------
# Networking Module Outputs
# -----------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC network ID"
  value       = google_compute_network.main.id
}

output "vpc_name" {
  description = "VPC network name"
  value       = google_compute_network.main.name
}

output "subnetwork_id" {
  description = "Subnetwork ID"
  value       = google_compute_subnetwork.main.id
}

output "subnetwork_name" {
  description = "Subnetwork name"
  value       = google_compute_subnetwork.main.name
}

output "router_name" {
  description = "Cloud Router name"
  value       = google_compute_router.main.name
}

output "nat_name" {
  description = "Cloud NAT name"
  value       = google_compute_router_nat.main.name
}
