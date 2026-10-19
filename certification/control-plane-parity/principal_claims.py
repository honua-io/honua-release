"""Expected identity assertions for genuine owned IdP principals."""


def check_principal(user: str, claims: list[dict], normal_status: int, override_status: int) -> None:
    def values(kind: str) -> set[str]:
        return {item["value"] for item in claims if item["type"] == kind}

    assert values("tenant_id") == {"tenant-b" if user == "admin-b" else "tenant-a"}, "Unexpected authenticated tenant"
    assert values("preferred_username") == {user}, "Unexpected authenticated username"
    assert len(values("sub")) == 1 and "" not in values("sub"), "Missing authenticated subject"
    roles = values("roles") | values("role") | values("http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
    assert "user" in roles and ("admin" in roles) == (user != "viewer-a"), "Unexpected authenticated role"
    if user == "admin-read-a":
        scopes = {scope for value in values("scp") | values("scope") for scope in value.split()}
        assert scopes == {"honua.mcp.read"}, "Expected genuine read-only OAuth ceiling"
    assert normal_status == (403 if user == "viewer-a" else 200), "Unexpected ordinary request permission"
    assert override_status == 403, "Authenticated tenant override was not refused"
