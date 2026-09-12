output "url" {
  description = "URL for admin-ui once DNS/hosts is configured."
  value       = "https://${var.ingress_hostname}"
}
