variable "human_operator_credential" {
  description = "Immutable private reference from an actual authenticated human session. This root never mints an OperatorBearer or places its value in Terraform. Only the trusted controller receives it; model worker and SQL bootstrap tasks do not."
  type        = object({ secretArn = string, versionId = string })
  default     = null
  validation {
    condition = var.human_operator_credential == null ? true : (
      can(regex("^arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:${var.name_prefix}/human-session/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$", var.human_operator_credential.secretArn)) &&
      can(regex("^[A-Za-z0-9-]{32,64}$", var.human_operator_credential.versionId))
    )
    error_message = "Use the actual separate human session's immutable private secret reference in this pilot namespace."
  }
}
