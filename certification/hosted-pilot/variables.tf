variable "region" {
  type    = string
  default = "us-west-2"
}

variable "name_prefix" {
  description = "Dedicated new pilot prefix; public demo resource names are refused."
  type        = string
  validation {
    condition     = can(regex("^honua-pilot-[a-z0-9-]{4,16}$", var.name_prefix))
    error_message = "Use an isolated honua-pilot-<suffix> prefix (4-16 lowercase characters)."
  }
}

variable "owner" {
  type = string
  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "A real resource owner is required."
  }
}

variable "expires_at" {
  description = "RFC3339 cleanup deadline tag; does not itself schedule teardown."
  type        = string
  validation {
    condition     = can(formatdate("YYYY-MM-DD", var.expires_at))
    error_message = "expires_at must be RFC3339."
  }
}

variable "candidate" {
  description = "Reviewed immutable artifact bindings; claims must be compared with installed observations."
  type = object({
    manifest_sha256  = string
    server_image     = string
    console_image    = string
    server_revision  = string
    console_revision = string
    studio_revision  = string
    sdk_revision     = string
  })
  validation {
    condition = can(regex("^[a-f0-9]{64}$", var.candidate.manifest_sha256)) && alltrue([
      for image in [var.candidate.server_image, var.candidate.console_image] :
      can(regex("^[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]+)?(/[A-Za-z0-9._-]+)+@sha256:[0-9a-f]{64}$", image))
      ]) && alltrue([
      for revision in [var.candidate.server_revision, var.candidate.console_revision, var.candidate.studio_revision, var.candidate.sdk_revision] :
      can(regex("^[a-f0-9]{40}$", revision))
    ])
    error_message = "Bind a SHA256 manifest, digest-only images and full 40-character source revisions."
  }
}

variable "server_domain" {
  description = "Dedicated pilot Server hostname, never the public demo."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9-]+(\\.[a-z0-9-]+)+$", var.server_domain)) && !contains(["demo.honua.io", "honua.io", "www.honua.io"], var.server_domain)
    error_message = "Use a dedicated pilot hostname, not demo.honua.io or a public site hostname."
  }
}

variable "certificate_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:acm:[a-z0-9-]+:[0-9]{12}:certificate/.+$", var.certificate_arn))
    error_message = "An existing HTTPS certificate ARN is required."
  }
}

variable "allowed_operator_cidrs" {
  description = "Explicit Server HTTPS ingress while operator identity is unbound; no world-open ingress."
  type        = list(string)
  validation {
    condition     = length(var.allowed_operator_cidrs) > 0 && alltrue([for cidr in var.allowed_operator_cidrs : try(cidrnetmask(cidr) != "0.0.0.0", false)])
    error_message = "Supply explicit non-world-open IPv4 operator CIDRs."
  }
}

variable "operation_certificate_secret_arn" {
  description = "Existing independent operation-key-ring PKCS12 secret ARN; no secret value enters inputs."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.operation_certificate_secret_arn))
    error_message = "Supply a Secrets Manager secret ARN."
  }
}

variable "audit_chain_secret_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.audit_chain_secret_arn))
    error_message = "Supply an independent audit-chain Secrets Manager ARN."
  }
}

variable "console_agent_secret_arn" {
  description = "Existing dedicated agent API-key secret ARN. Console remains private and native inference disabled."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.console_agent_secret_arn))
    error_message = "Supply the independently scoped agent's Secrets Manager ARN."
  }
  validation {
    condition     = length(distinct([var.console_agent_secret_arn, var.operation_certificate_secret_arn, var.audit_chain_secret_arn])) == 3
    error_message = "Agent, operation certificate and audit-chain secrets must be independent references."
  }
}

variable "secret_kms_key_arns" {
  description = "Customer-managed secret encryption keys only, if required."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.secret_kms_key_arns : can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/.+$", arn))])
    error_message = "Only explicit KMS key ARNs are accepted."
  }
}

variable "operation_certificate_kms_key_arn" {
  type    = string
  default = ""
}

variable "audit_chain_kms_key_arn" {
  type    = string
  default = ""
}

variable "identity_redirects" {
  description = "Exact reviewed HTTPS Console-edge redirects. Server BFF callback is on the Console origin, not the upstream Server origin. No wildcard, query, fragment, userinfo, port or loopback URLs."
  type = object({
    console_origin          = string
    edge_callback_url       = string
    edge_logout_url         = string
    server_bff_callback_url = string
    server_bff_logout_url   = string
  })
  validation {
    condition = can(regex("^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", var.identity_redirects.console_origin)) && !contains([
      "https://demo.honua.io", "https://honua.io", "https://www.honua.io", "https://127.0.0.1"
    ], var.identity_redirects.console_origin) && !can(cidrnetmask("${trimprefix(var.identity_redirects.console_origin, "https://")}/32"))
    error_message = "Bind a dedicated HTTPS DNS Console origin without a path, port or reserved public-demo hostname."
  }
  validation {
    condition = (
      var.identity_redirects.edge_callback_url == "${var.identity_redirects.console_origin}/oauth2/callback" &&
      var.identity_redirects.server_bff_callback_url == "${var.identity_redirects.console_origin}/admin/auth/callback" &&
      var.identity_redirects.edge_logout_url == "${var.identity_redirects.console_origin}/auth/login" &&
      var.identity_redirects.server_bff_logout_url == "${var.identity_redirects.console_origin}/admin"
    )
    error_message = "Use exact same-origin edge /oauth2/callback, Server BFF /admin/auth/callback, edge logout /auth/login and Server logout /admin URLs."
  }
}

variable "identity_secrets_kms_key_arn" {
  description = "Optional reviewed KMS key for generated Cognito client secrets; empty uses the Secrets Manager managed key."
  type        = string
  default     = ""
  validation {
    condition     = var.identity_secrets_kms_key_arn == "" || can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/.+$", var.identity_secrets_kms_key_arn))
    error_message = "Use an explicit KMS key ARN or the managed-key default."
  }
}
