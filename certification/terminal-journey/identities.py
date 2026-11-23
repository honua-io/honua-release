"""Bounded identities a candidate returns: opaque tokens and server actor ids."""
from __future__ import annotations

import re

from transport import ExecutionError

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
# A proposal's requestedBy/resolvedBy is a server actor id, not an opaque token. honua-server emits
# exactly these shapes, every free component Uri.EscapeDataString-encoded (unreserved
# [A-Za-z0-9._~-] kept, everything else as uppercase %XX):
#   CanonicalSecurityActor.Resolve (approve route, MCP audit):
#     `<scheme>:subject:<issuer|->:<subject>`, `<scheme>:api-key:<guid>`, `<scheme>:name:<name>`
#   StudioAuthorizationService.ResolveCallerId (the Studio publication proposer, which the
#   operation gateway records as requestedBy for honua_studio_propose_publication):
#     `subject:<issuer>:<subject>@tenant:<tenant>` for an issuer-bearing token, else the bare
#     api_key_id / api_key_name / identity name (a legacy TOKEN-shaped id).
# e2e-cloud-aws run 40947860102 refused the `%` in the signed bearer proposer; #1075 then admitted
# `%XX` but not the `@tenant:` suffix, so run 40981429785 (aws-eks/redis-on, proposer
# `subject:https%3A%2F%2Fterminal-journey.invalid:journey-author@tenant:public`) still failed
# 8.execution with "omitted a bounded canonical identity (requestedBy)".
_ACTOR_ESCAPED = r"(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})"
_ACTOR_SCHEME = r"[a-z0-9][a-z0-9._-]{0,63}"
PRINCIPAL_MAX = 512
PRINCIPAL = re.compile(
    rf"(?:{_ACTOR_SCHEME}:subject:{_ACTOR_ESCAPED}+:{_ACTOR_ESCAPED}+"
    rf"|{_ACTOR_SCHEME}:api-key:[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}"
    rf"|{_ACTOR_SCHEME}:name:{_ACTOR_ESCAPED}+"
    rf"|subject:{_ACTOR_ESCAPED}+:{_ACTOR_ESCAPED}+@tenant:{_ACTOR_ESCAPED}*"
    r"|[A-Za-z0-9][A-Za-z0-9_.:-]{0,199})\Z")


def _token(value):
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None


def identity(value, label):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ExecutionError(label, "candidate omitted a bounded canonical identity")
    return value


def principal_shape(value):
    """A value-free shape summary of a refused actor id: type, length and character classes."""
    if not isinstance(value, str):
        return "absent" if value is None else f"type={type(value).__name__}"
    classes = []
    for name, test in (("lower", str.islower), ("upper", str.isupper), ("digit", str.isdigit)):
        if any(c.isascii() and test(c) for c in value):
            classes.append(name)
    classes.extend(sorted({repr(c) for c in value if c.isascii() and c.isprintable() and not c.isalnum()
                           and not c.isspace()}))
    for name, test in (("whitespace", str.isspace), ("control", lambda c: not c.isprintable() and not c.isspace()),
                       ("non-ascii", lambda c: not c.isascii())):
        if any(test(c) for c in value):
            classes.append(name)
    if re.search(r"%(?![0-9A-F]{2})", value):
        classes.append("malformed-or-lowercase-escape")
    return f"length={len(value)} classes={','.join(classes) or 'none'} segments={value.count(':') + 1}"


def principal_identity(value, label, field):
    """A bounded server actor id (PRINCIPAL); the failure names the field and its value-free shape."""
    if not isinstance(value, str) or len(value) > PRINCIPAL_MAX or not PRINCIPAL.fullmatch(value):
        raise ExecutionError(label, f"candidate omitted a bounded canonical identity ({field}; "
                                    f"{principal_shape(value)})")
    return value
