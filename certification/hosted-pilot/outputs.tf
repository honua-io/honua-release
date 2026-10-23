output "deployment" {
  value = module.application.deployment
}
output "identity_preparation" {
  value = module.application.identity_preparation
}
output "human_enrollment" {
  value = module.application.human_enrollment
}
output "ecs_cluster_name" {
  value = module.application.ecs_cluster_name
}
output "ecs_service_name" {
  value = module.application.ecs_service_name
}
output "db_endpoint" {
  value     = module.application.db_endpoint
  sensitive = true
}
output "db_connection_secret_arn" {
  value = module.application.db_connection_secret_arn
}
output "admin_password_secret_arn" {
  value = module.application.admin_password_secret_arn
}
output "runtime_bindings" {
  value = module.application.runtime_bindings
}
output "native_membership_credentials" {
  value = module.application.native_membership_credentials
}
