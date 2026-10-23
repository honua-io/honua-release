# Deployment entry point. The shared application owns input types and validation.
variable "region" {
  type    = any
  default = "us-west-2"
}
variable "name_prefix" {
  type = any
}
variable "owner" {
  type = any
}
variable "expires_at" {
  type = any
}
variable "candidate" {
  type = any
}
variable "server_domain" {
  type = any
}
variable "allowed_operator_cidrs" {
  type = any
}
variable "operation_certificate_secret_arn" {
  type = any
}
variable "audit_chain_secret_arn" {
  type = any
}
variable "console_agent_secret_arn" {
  type    = any
  default = null
}
variable "console_agent_key_id" {
  type    = any
  default = null
}
variable "console_agent_secret_version_id" {
  type    = any
  default = null
}
variable "secret_kms_key_arns" {
  type    = any
  default = []
}
variable "operation_certificate_kms_key_arn" {
  type    = any
  default = ""
}
variable "audit_chain_kms_key_arn" {
  type    = any
  default = ""
}
variable "identity_redirects" {
  type = any
}
variable "identity_secrets_kms_key_arn" {
  type    = any
  default = ""
}
variable "edge_proxy_image" {
  type    = any
  default = "quay.io/oauth2-proxy/oauth2-proxy@sha256:8e4bec4f9fe4de10de63070209204075e4407503170aa19893d6e8d99f083d1b"
}
variable "route53_zone_id" {
  type = any
}
variable "deletion_protection" {
  type    = any
  default = true
}
variable "permissions_boundary_arn" {
  type = any
}
variable "destroy_artifact_data" {
  type    = any
  default = false
}
variable "human_enrollment" {
  type    = any
  default = null
}
variable "campaign_context" {
  type    = any
  default = null
}
