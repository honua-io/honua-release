# The campaign owns its existing network and the private SQL controller. The
# application keeps the original campaign Server/database identities while
# Cognito and Console use the foundation's exact pilot ownership namespace.
variable "campaign_context" {
  type = object({
    network = object({
      vpc_id             = string, vpc_cidr = string, public_subnet_ids = list(string),
      private_subnet_ids = list(string), nat_public_ips = list(string)
    })
    controller_security_group_id = string
    artifact_bucket_name         = string
  })
  default = null
  validation {
    condition = var.campaign_context == null ? true : (
      can(regex("^vpc-([a-f0-9]{8}|[a-f0-9]{17})$", var.campaign_context.network.vpc_id)) &&
      can(cidrnetmask(var.campaign_context.network.vpc_cidr)) &&
      can(regex("^sg-([a-f0-9]{8}|[a-f0-9]{17})$", var.campaign_context.controller_security_group_id)) &&
      length(var.campaign_context.network.public_subnet_ids) >= 2 &&
      length(var.campaign_context.network.private_subnet_ids) >= 2 &&
      length(setintersection(toset(var.campaign_context.network.public_subnet_ids), toset(var.campaign_context.network.private_subnet_ids))) == 0 &&
      alltrue([for id in concat(var.campaign_context.network.public_subnet_ids, var.campaign_context.network.private_subnet_ids) : can(regex("^subnet-([a-f0-9]{8}|[a-f0-9]{17})$", id))]) &&
      length(var.campaign_context.network.nat_public_ips) > 0 &&
      alltrue([for ip in var.campaign_context.network.nat_public_ips : can(cidrnetmask("${ip}/32"))]) &&
      var.campaign_context.artifact_bucket_name == "${var.name_prefix}-agent-target-files-${split(":", var.permissions_boundary_arn)[4]}"
    )
    error_message = "Reuse the exact campaign VPC, separate public/private subnets, NAT addresses, controller security group and retained target-files bucket."
  }
}

locals {
  application_prefix   = var.campaign_context == null ? var.name_prefix : "${var.name_prefix}-agent"
  application_database = var.campaign_context == null ? "honua" : "honua_agent_${substr(var.candidate.manifest_sha256, 0, 24)}"
  application_db_role  = var.campaign_context == null ? "honua" : "honua_agent_app"
  application_network = var.campaign_context == null ? {
    vpc_id            = module.network[0].vpc_id, vpc_cidr = module.network[0].vpc_cidr_block,
    public_subnet_ids = module.network[0].public_subnets, private_subnet_ids = module.network[0].private_subnets,
    nat_public_ips    = module.network[0].nat_public_ips
  } : var.campaign_context.network
  application_agent_environment = merge({
    AgentWorkspace__DeploymentId = local.application_prefix
    AgentWorkspace__ReleaseId    = "sha256:${var.candidate.manifest_sha256}"
    }, var.console_agent_key_id == null ? {} : {
    AgentWorkspace__LocalAgentPrincipalId = "apikey:api-key:${var.console_agent_key_id}"
  })
}

# Read the actual supplied network rather than accepting a coherent set of
# strings that points Console, Server or the SQL controller at another VPC.
data "aws_vpc" "campaign" {
  count = var.campaign_context == null ? 0 : 1
  id    = var.campaign_context.network.vpc_id
}
data "aws_subnet" "campaign" {
  for_each = var.campaign_context == null ? {} : {
    for index, id in concat(var.campaign_context.network.public_subnet_ids, var.campaign_context.network.private_subnet_ids) : tostring(index) => id
  }
  id = each.value
}
data "aws_security_group" "campaign_controller" {
  count = var.campaign_context == null ? 0 : 1
  id    = var.campaign_context.controller_security_group_id
}
resource "terraform_data" "campaign_network" {
  count = var.campaign_context == null ? 0 : 1
  input = var.campaign_context.network.vpc_id
  lifecycle {
    precondition {
      condition = (data.aws_vpc.campaign[0].cidr_block == var.campaign_context.network.vpc_cidr &&
        alltrue([for subnet in data.aws_subnet.campaign : subnet.vpc_id == var.campaign_context.network.vpc_id]) &&
      data.aws_security_group.campaign_controller[0].vpc_id == var.campaign_context.network.vpc_id)
      error_message = "Campaign subnets, controller security group and CIDR must belong to the actual target VPC."
    }
  }
}

output "ecs_cluster_name" { value = module.server.ecs_cluster_name }
output "ecs_service_name" { value = module.server.ecs_service_name }
output "db_endpoint" {
  value     = module.server.db_endpoint
  sensitive = true
}
output "db_connection_secret_arn" { value = module.server.db_connection_secret_arn }
output "admin_password_secret_arn" { value = module.server.admin_password_secret_arn }
output "runtime_bindings" {
  description = "Configured references for independent installed verification; not a readiness or qualification receipt."
  value = {
    server_origin                = local.server_origin, database = local.application_database, app_role = local.application_db_role,
    vpc_id                       = local.application_network.vpc_id, server_prefix = local.application_prefix,
    controller_security_group_id = try(var.campaign_context.controller_security_group_id, null),
    agent_actor_id               = try(local.application_agent_environment.AgentWorkspace__LocalAgentPrincipalId, null),
    agent_secret_version_id      = var.console_agent_secret_version_id,
    console_origin               = var.identity_redirects.console_origin, issuer = local.server_generic_oidc.authority
  }
}
