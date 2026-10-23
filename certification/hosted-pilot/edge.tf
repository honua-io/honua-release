locals {
  # This is oauth2-proxy's structured configuration, pinned by the reviewed image
  # digest. Runtime expansion is performed by oauth2-proxy, never by Terraform.
  # Header values=[] strips inbound tokens rather than supplying Cognito access
  # tokens to Server, whose separate BFF issues the actual operator bearer.
  edge_config = jsonencode({
    server = { bindAddress = "0.0.0.0:8080" }
    upstreamConfig = { upstreams = [{
      id             = "console", path = "/", uri = "http://127.0.0.1:8081",
      passHostHeader = true, proxyWebSockets = true,
      timeout        = "5m", flushInterval = "100ms"
    }] }
    injectRequestHeaders = [
      { name = "X-Forwarded-User", preserveRequestValue = false, values = [{ claimSource = { claim = "user" } }] },
      { name = "X-Forwarded-Email", preserveRequestValue = false, values = [{ claimSource = { claim = "email" } }] },
      { name = "X-Honua-Edge-Auth", preserveRequestValue = false, values = [{ secretSource = { fromEnv = "HONUA_EDGE_SHARED_SECRET" } }] },
      { name = "X-Forwarded-Access-Token", preserveRequestValue = false, values = [] },
      { name = "X-Forwarded-Proto", preserveRequestValue = false, values = [{ secretSource = { value = base64encode("https") } }] },
      { name = "X-Forwarded-Host", preserveRequestValue = false, values = [{ secretSource = { value = base64encode(trimprefix(var.identity_redirects.console_origin, "https://")) } }] },
      { name = "Authorization", preserveRequestValue = false, values = [] },
      { name = "X-API-Key", preserveRequestValue = false, values = [] },
    ]
    providers = [{
      id            = "pilot-cognito", provider = "oidc", name = "Pilot operators",
      clientID      = aws_cognito_user_pool_client.operators["console_edge"].id,
      clientSecret  = "$${OAUTH2_PROXY_CLIENT_SECRET}",
      scope         = "openid profile email", code_challenge_method = "S256",
      allowedGroups = values(local.identity_groups),
      oidcConfig = {
        issuerURL                      = "https://${aws_cognito_user_pool.pilot.endpoint}",
        emailClaim                     = "email", userIDClaim = "sub", groupsClaim = "cognito:groups",
        audienceClaims                 = ["aud"], insecureSkipNonce = false,
        insecureSkipIssuerVerification = false, insecureAllowUnverifiedEmail = false
      }
    }]
  })
}

resource "random_password" "edge_shared" {
  length  = 64
  special = false
}
resource "random_password" "edge_cookie" {
  length  = 32
  special = false
}
resource "aws_secretsmanager_secret" "edge" {
  name                    = "${var.name_prefix}/edge/session"
  kms_key_id              = var.identity_secrets_kms_key_arn == "" ? null : var.identity_secrets_kms_key_arn
  recovery_window_in_days = 7
}
resource "aws_secretsmanager_secret_version" "edge" {
  secret_id = aws_secretsmanager_secret.edge.id
  secret_string = jsonencode({
    shared = random_password.edge_shared.result
    cookie = random_password.edge_cookie.result
  })
}

resource "aws_lb_target_group" "console" {
  name_prefix = "pilot-"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.server.vpc_id
  health_check {
    path    = "/ping"
    matcher = "200"
  }
}
resource "aws_lb_listener_rule" "console" {
  listener_arn = module.server.https_listener_arn
  priority     = 100
  condition {
    host_header {
      values = [trimprefix(var.identity_redirects.console_origin, "https://")]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.console.arn
  }
}
# Prevent a host-header mismatch from reaching the default Server target.
resource "aws_lb_listener_rule" "refuse_unknown_host" {
  listener_arn = module.server.https_listener_arn
  priority     = 50000
  condition {
    host_header { values = ["*"] }
  }
  action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      status_code  = "404"
      message_body = "Unknown pilot host"
    }
  }
}
resource "aws_lb_listener_rule" "server" {
  listener_arn = module.server.https_listener_arn
  priority     = 101
  condition {
    host_header { values = [var.server_domain] }
  }
  action {
    # The Server module owns the default action/target; an explicit redirect to
    # it would loop. Its target ARN is read from the existing listener below.
    type             = "forward"
    target_group_arn = data.aws_lb_listener.server.default_action[0].target_group_arn
  }
}
data "aws_lb_listener" "server" {
  arn = module.server.https_listener_arn
}
