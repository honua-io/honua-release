# Mock plan only: no cloud identity, reads, provisioning or inference.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/offline-test" }
  }
}
mock_provider "random" {}
mock_provider "null" {}

override_module {
  target = module.server
  outputs = {
    vpc_id             = "vpc-12345678"
    private_subnet_ids = ["subnet-12345678"]
    ecs_cluster_name   = "offline-test"
    alb_dns_name       = "offline.invalid"
  }
}

variables {
  name_prefix = "honua-pilot-offline"
  owner       = "offline-test-fixture"
  expires_at  = "2026-10-30T00:00:00Z"
  candidate = {
    manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    server_image     = "registry.invalid/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_image    = "registry.invalid/console@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
    sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
  }
  server_domain                    = "pilot.offline.invalid"
  certificate_arn                  = "arn:aws:acm:us-west-2:123456789012:certificate/offline-test"
  allowed_operator_cidrs           = ["192.0.2.1/32"]
  operation_certificate_secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-operations"
  audit_chain_secret_arn           = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-audit"
  console_agent_secret_arn         = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-agent"
}

run "private_preparation" {
  command = plan
  assert {
    condition     = aws_ecs_service.console.desired_count == 1 && !aws_ecs_service.console.network_configuration[0].assign_public_ip
    error_message = "Console must remain one private task."
  }
  assert {
    condition     = length(aws_security_group.console.ingress) == 0
    error_message = "Unbound identity must not expose Console."
  }
  assert {
    condition     = output.preparation.public_console_origin == null && output.preparation.public_audience_origin == null
    error_message = "Preparation cannot advertise public origins."
  }
  assert {
    condition = alltrue([for setting in jsondecode(aws_ecs_task_definition.console.container_definitions)[0].environment :
      setting.name != "HONUA_NATIVE_MODEL_ENABLED" || setting.value == "false"
    ]) && jsondecode(aws_ecs_task_definition.console.container_definitions)[0].secrets == [{ name = "HONUA_AGENT_API_KEY", valueFrom = var.console_agent_secret_arn }]
    error_message = "Native inference must remain disabled and only the dedicated agent secret reference injected."
  }
}

run "refuse_public_demo" {
  command = plan
  variables { server_domain = "demo.honua.io" }
  expect_failures = [var.server_domain]
}

run "refuse_mutable_console_image" {
  command = plan
  variables {
    candidate = {
      manifest_sha256  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      server_image     = "registry.invalid/server@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      console_image    = "registry.invalid/console:latest"
      server_revision  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      console_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      studio_revision  = "cccccccccccccccccccccccccccccccccccccccc"
      sdk_revision     = "dddddddddddddddddddddddddddddddddddddddd"
    }
  }
  expect_failures = [var.candidate]
}

run "refuse_world_open_server" {
  command = plan
  variables { allowed_operator_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_noncanonical_world_open_server" {
  command = plan
  variables { allowed_operator_cidrs = ["1.2.3.4/0"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_malformed_operator_cidr" {
  command = plan
  variables { allowed_operator_cidrs = ["not-a-cidr"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_ipv6_operator_cidr" {
  command = plan
  variables { allowed_operator_cidrs = ["2001:db8::/64"] }
  expect_failures = [var.allowed_operator_cidrs]
}

run "refuse_literal_secret" {
  command = plan
  variables { console_agent_secret_arn = "not-a-secret-reference" }
  expect_failures = [var.console_agent_secret_arn]
}

run "refuse_shared_security_secrets" {
  command = plan
  variables {
    console_agent_secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:offline-audit"
  }
  expect_failures = [var.console_agent_secret_arn]
}
