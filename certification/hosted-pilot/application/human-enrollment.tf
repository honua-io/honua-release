# Real identities are opt-in and encrypted before entering reviewed source.
# The existing saved-plan workflow owns every user/membership mutation.
variable "human_enrollment" {
  description = "Sealed operator-nominated identities for an already observed pilot pool. Null creates no users. Plaintext emails and credentials must never enter checked-in configuration."
  type = object({
    pool_id = string
    key_arn = string
    users   = map(object({ role = string, payload = string }))
  })
  default = null
  validation {
    condition = var.human_enrollment == null ? true : (
      can(regex("^${var.region}_[A-Za-z0-9]+$", var.human_enrollment.pool_id)) &&
      can(regex("^arn:aws:kms:${var.region}:[0-9]{12}:key/[a-f0-9-]{36}$", var.human_enrollment.key_arn)) &&
      length(var.human_enrollment.users) >= 2 && length(var.human_enrollment.users) <= 10 &&
      toset([for person in var.human_enrollment.users : person.role]) == toset(["authors", "approvers"]) &&
      alltrue([for alias, person in var.human_enrollment.users :
        can(regex("^[a-z][a-z0-9_]{2,31}$", alias)) &&
        length(person.payload) >= 44 && length(person.payload) <= 8192 && length(person.payload) % 4 == 0 &&
        can(regex("^[A-Za-z0-9+/]+={0,2}$", person.payload))
      ])
    )
    error_message = "Use a regional pool/key and two to ten opaque aliases with sealed input and separate author/approver roles."
  }
}

locals {
  enrollment_users = var.human_enrollment == null ? {} : var.human_enrollment.users
  enrollment_plaintext = { for alias, value in data.aws_kms_secrets.humans :
    alias => try(jsondecode(value.plaintext["identity"]), {})
  }
}

data "aws_kms_secrets" "humans" {
  for_each = local.enrollment_users
  secret {
    name                 = "identity"
    payload              = each.value.payload
    key_id               = var.human_enrollment.key_arn
    encryption_algorithm = "SYMMETRIC_DEFAULT"
    context = {
      Purpose = "honua.pilot-human-enrollment/v1"
      Pilot   = var.name_prefix
      PoolId  = var.human_enrollment.pool_id
      Alias   = each.key
      Role    = each.value.role
    }
  }
}

resource "terraform_data" "human_enrollment" {
  count = var.human_enrollment == null ? 0 : 1
  input = sha256(jsonencode(var.human_enrollment))
  lifecycle {
    precondition {
      condition     = var.human_enrollment.pool_id == aws_cognito_user_pool.pilot.id
      error_message = "Enrollment must name this actual pilot pool."
    }
    precondition {
      condition = alltrue([for person in local.enrollment_plaintext : try(
        toset(keys(person)) == toset(["schema", "email", "temporaryPassword", "emailOwnershipConfirmed"]) &&
        person.schema == "honua.pilot-human-enrollment/v1" && person.emailOwnershipConfirmed == true &&
        length(person.email) <= 128 && person.email == lower(trimspace(person.email)) &&
        can(regex("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$", person.email)) &&
        length(person.temporaryPassword) >= 16 && length(person.temporaryPassword) <= 128 &&
        alltrue([for pattern in ["[a-z]", "[A-Z]", "[0-9]", "[!@#%+_-]"] : can(regex(pattern, person.temporaryPassword))]), false)
      ])
      error_message = "Decrypted enrollment must contain operator-confirmed human emails and strong temporary credentials."
    }
    precondition {
      condition     = length(distinct([for person in local.enrollment_plaintext : try(person.email, "")])) == length(local.enrollment_users)
      error_message = "Separate aliases must represent distinct human emails."
    }
  }
}

resource "aws_cognito_user" "humans" {
  for_each             = local.enrollment_users
  depends_on           = [terraform_data.human_enrollment]
  user_pool_id         = aws_cognito_user_pool.pilot.id
  username             = local.enrollment_plaintext[each.key].email
  temporary_password   = local.enrollment_plaintext[each.key].temporaryPassword
  message_action       = "SUPPRESS"
  force_alias_creation = false
  enabled              = true
  attributes = {
    email = local.enrollment_plaintext[each.key].email
    # Explicit operator confirmation is checked above; this is not evidence
    # that a real human completed hosted login or approved an agent action.
    email_verified = "true"
  }
}

resource "aws_cognito_user_in_group" "humans" {
  for_each     = local.enrollment_users
  user_pool_id = aws_cognito_user_pool.pilot.id
  username     = aws_cognito_user.humans[each.key].username
  group_name   = aws_cognito_user_group.operators[each.value.role].name
}

output "human_enrollment" {
  description = "Observed Cognito subject references only; not a login, approval or human-session qualification receipt."
  value = { for alias, person in aws_cognito_user.humans : alias => {
    subject_id = person.sub
    role       = local.enrollment_users[alias].role
    status     = person.status
  } }
}
