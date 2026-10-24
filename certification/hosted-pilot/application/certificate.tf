# The delegated zone is pre-existing; both hostnames and their transport
# certificate are configured by this root, including DNS validation records.
resource "aws_acm_certificate" "pilot" {
  domain_name               = var.server_domain
  subject_alternative_names = [trimprefix(var.identity_redirects.console_origin, "https://")]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "certificate" {
  for_each = toset([var.server_domain, trimprefix(var.identity_redirects.console_origin, "https://")])
  zone_id  = var.route53_zone_id
  name     = one([for item in aws_acm_certificate.pilot.domain_validation_options : item.resource_record_name if item.domain_name == each.key])
  type     = one([for item in aws_acm_certificate.pilot.domain_validation_options : item.resource_record_type if item.domain_name == each.key])
  records  = [one([for item in aws_acm_certificate.pilot.domain_validation_options : item.resource_record_value if item.domain_name == each.key])]
  ttl      = 60
}
resource "aws_acm_certificate_validation" "pilot" {
  certificate_arn         = aws_acm_certificate.pilot.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate : record.fqdn]
}
