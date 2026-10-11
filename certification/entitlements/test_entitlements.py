"""Entitlement certification: exact positive/negative assertions, fail-closed coverage, fixture hygiene."""
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import fixtures as fx  # noqa: E402
import run  # noqa: E402

PROBES = json.loads((HERE / "probes.v1.json").read_text(encoding="utf-8"))
FIXTURES = json.loads((HERE / "fixtures.v1.json").read_text(encoding="utf-8"))
GATED = PROBES["catalog"]["gated"]

# A stand-in corpus file in the shape of LicenseTestSupport.cs. The real file is fetched from the
# candidate's exact honua-server commit at run time; this repository never holds a signing seed.
FAKE_SEED = bytes(range(1, 33))
FAKE_CORPUS = ("private static readonly byte[] SigningSeed =\n    [\n        "
               + ", ".join(f"0x{b:02X}" for b in FAKE_SEED) + "\n    ];\n\n    public const string KeyId = \"test-key\";\n")


def fake_fixtures():
    anchor = dict(FIXTURES["trust_anchor"], public_key_fingerprint=fx.fingerprint(fx.trusted_public_key(FAKE_SEED)))
    return dict(FIXTURES, trust_anchor=anchor)


def problem(key, edition, status=402, title="Payment Required"):
    body = {"type": "about:blank", "title": title, "status": status,
            "detail": f"X requires an active {edition} entitlement. Current edition is Enterprise; install a license "
                      f"that includes '{key}'. entitlement: {key}"}
    return run.Response(status, "application/problem+json", json.dumps(body).encode())


def geoservices(key, edition, code=402):
    body = {"error": {"code": code, "message": "Payment Required",
                      "details": [f"X requires an active {edition} entitlement.", f"entitlement: {key}", "CorrelationId: 1"]}}
    return run.Response(200, "application/json; charset=utf-8", json.dumps(body).encode())


# ── the committed table is complete and well-formed ───────────────────────────────────────

def test_every_gated_catalog_key_has_exactly_one_row():
    assert run.coverage_problems(PROBES) == []
    rows = [p["key"] for p in PROBES["probes"]] + [r["key"] for r in PROBES["no_http_surface"]]
    assert sorted(rows) == sorted(GATED)


def test_a_new_gated_key_without_a_row_fails_coverage():
    probes = json.loads(json.dumps(PROBES))
    probes["catalog"]["gated"]["geocoding.forward"] = "Pro"
    assert any("geocoding.forward has no probe" in p for p in run.coverage_problems(probes))


def test_geocoding_is_certified_with_a_real_batch_and_the_geoservices_denial():
    batch = next(p for p in PROBES["probes"] if p["key"] == "geocoding.batch")
    assert "geocodeAddresses" in batch["request"]["path"] and batch["denial"] == "geoservices"
    assert "positive" in batch and "positive_blocked" not in batch
    assert {r["key"] for r in PROBES["no_http_surface"]} >= {"geocoding.failover"}


def test_each_edition_is_denied_by_a_fixture_that_lacks_only_that_tier():
    assert PROBES["negative_fixture"] == {"Pro": "keyless", "Enterprise": "pro-tier"}
    catalog = PROBES
    grants = {f["id"]: set(fx.granted_keys(f, catalog)) for f in FIXTURES["fixtures"]}
    pro = {k for k, e in GATED.items() if e == "Pro"}
    enterprise = {k for k, e in GATED.items() if e == "Enterprise"}
    assert grants["entitled"] >= pro | enterprise
    assert pro <= grants["pro-tier"] and not enterprise & grants["pro-tier"]
    # Only declared prerequisites stay active on the keyless stack, and none is a key denied there.
    assert (pro | enterprise) & grants["keyless"] == fx.negative_prerequisites("keyless", catalog) == {"ai.agent-operations", "ai.approval-workflows"}
    assert not {p["key"] for p in PROBES["probes"] if GATED[p["key"]] == "Pro"} & grants["keyless"]
    # The keyless fixture is Enterprise-labelled: a denial there proves the key, not the label, gates.
    assert next(f for f in FIXTURES["fixtures"] if f["id"] == "keyless")["edition"] == "Enterprise"


def test_blocked_positives_are_explicit_and_few():
    blocked = sorted(p["key"] for p in PROBES["probes"] if "positive_blocked" in p)
    assert blocked == ["analytics.h3", "analytics.line-of-sight", "analytics.slice", "analytics.sun-shadow",
                       "analytics.viewshed", "import.geoserver", "import.geoservices"]


# ── negative: exactly the documented denial, nothing else ────────────────────────────────

@pytest.mark.parametrize("response", [
    run.Response(404, "application/problem+json", b'{"status":404,"title":"Not Found","detail":"entitlement: identity.saml"}'),
    run.Response(500, "application/problem+json", b'{"status":500}'),
    run.Response(200, "application/json", b'{"success":true}'),
    run.Response(403, "application/problem+json", b'{"status":403,"title":"Forbidden"}'),
    run.Response(402, "text/plain", b"payment required"),
])
def test_a_non_402_or_unshaped_answer_is_not_a_denial(response):
    assert run.evaluate_denial("problem", "identity.saml", "Enterprise", response)


def test_the_problem_denial_must_name_the_probed_key_and_its_edition():
    assert run.evaluate_denial("problem", "identity.saml", "Enterprise", problem("identity.saml", "Enterprise")) == []
    assert run.evaluate_denial("problem", "identity.saml", "Enterprise", problem("identity.scim", "Enterprise"))
    assert run.evaluate_denial("problem", "identity.saml", "Enterprise", problem("identity.saml", "Pro"))
    assert run.evaluate_denial("problem", "identity.saml", "Enterprise", problem("identity.saml", "Enterprise", title="Nope"))


def test_the_geoservices_denial_is_the_200_envelope_with_code_402_naming_the_key():
    key = "editing.featureserver-edits"
    assert run.evaluate_denial("geoservices", key, "Pro", geoservices(key, "Pro")) == []
    assert run.evaluate_denial("geoservices", key, "Pro", geoservices(key, "Pro", code=400))
    assert run.evaluate_denial("geoservices", key, "Pro", geoservices("analytics.density", "Pro"))
    literal_402 = run.Response(402, "application/json", geoservices(key, "Pro").body)
    assert run.evaluate_denial("geoservices", key, "Pro", literal_402)


# ── positive: the documented success, not merely "not 402" ───────────────────────────────

def positive(key):
    return next(p for p in PROBES["probes"] if p["key"] == key)["positive"]


@pytest.mark.parametrize("response", [
    run.Response(404, "application/problem+json", b'{"status":404}'),
    run.Response(500, "application/problem+json", b'{"status":500}'),
    run.Response(400, "application/json", b'{"success":false}'),
    run.Response(200, "text/html", b"<html></html>"),
    run.Response(200, "application/json", b'{"success":false,"data":[]}'),
    run.Response(200, "application/json", b'{"success":true,"data":{}}'),
])
def test_anything_but_the_documented_success_fails_the_positive(response):
    assert run.evaluate_positive(positive("admin.multi-tenancy"), response)


def test_the_documented_success_passes_the_positive():
    ok = run.Response(200, "application/json; charset=utf-8", b'{"success":true,"data":[],"message":null}')
    assert run.evaluate_positive(positive("admin.multi-tenancy"), ok) == []


def test_geoservices_errors_inside_a_200_fail_the_positive():
    envelope = run.Response(200, "application/json", b'{"error":{"code":400,"message":"Bad Request"}}')
    assert run.evaluate_positive(positive("editing.featureserver-edits"), envelope)
    done = run.Response(200, "application/json", b'{"addResults":[],"updateResults":[],"deleteResults":[],"success":true}')
    assert run.evaluate_positive(positive("editing.featureserver-edits"), done) == []


def test_binary_and_stream_successes_are_checked_by_content():
    png = positive("staticmap.high-dpi")
    assert run.evaluate_positive(png, run.Response(200, "image/png", b"\x89PNG\r\n\x1a\nrest")) == []
    assert run.evaluate_positive(png, run.Response(200, "image/png", b"GIF89a"))
    sse = positive("streaming.feature-subscriptions")
    connected = b'event: status\ndata: {"type":"status","status":"connected"}\n\n'
    assert run.evaluate_positive(sse, run.Response(200, "text/event-stream", connected)) == []
    assert run.evaluate_positive(sse, run.Response(200, "text/event-stream", b"event: error\n\n"))


def test_json_paths_resolve_indices_and_absence():
    document = {"results": [{"value": {"url": "/temp/a.pdf"}}]}
    assert run.check_json(document, {"path": "results[0].value.url", "endswith": ".pdf"}) is None
    assert run.check_json(document, {"path": "results[1].value.url", "type": "string"}) == "results[1].value.url is missing"
    assert run.check_json(document, {"path": "error", "absent": True}) is None
    assert run.check_json({"count": True}, {"path": "count", "type": "number"})


# ── observe + verdict over a fake candidate ──────────────────────────────────────────────

SEEDED = {"layer:maui-inspections": 7, "layer:maui-zoning": 2, "layer:maui-place-names": 6, "scim_token": "t"}
CONTEXT = {**SEEDED, "zoneId": 7, "coverageId": 7}


class FakeCandidate:
    """Answers every probe the way a correctly gated candidate would for one fixture."""

    def __init__(self, fixture_id, overrides=None):
        self.fixture = next(f for f in FIXTURES["fixtures"] if f["id"] == fixture_id)
        self.granted = set(fx.granted_keys(self.fixture, PROBES))
        self.overrides = overrides or {}
        self.requests = []
        self.by_request = {}
        for probe in PROBES["probes"]:
            self.by_request.setdefault((probe["request"]["method"], probe["request"]["path"].split("?")[0]), []).append(probe)

    def __call__(self, method, url, headers, body, markers=()):
        path = url.split("localhost", 1)[1]
        self.requests.append((method, path, headers))
        bare = path.split("?")[0]
        if bare == "/api/v1/admin/license/status":
            entitlements = [{"key": k, "isActive": k in self.granted} for k in sorted(set(GATED) | set(PROBES["catalog"]["community"]))]
            data = {"mode": "enabled", "validationState": "Valid", "licenseId": self.fixture["license_id"],
                    "edition": self.fixture["edition"], "entitlements": entitlements}
            return run.Response(200, "application/json", json.dumps({"success": True, "data": data}).encode())
        if bare == "/api/v1/admin/license/entitlements":
            keys = sorted(set(GATED) | set(PROBES["catalog"]["community"]))
            return run.Response(200, "application/json", json.dumps({"data": [{"key": k} for k in keys]}).encode())
        for probe in PROBES["probes"]:
            for step in probe.get("setup", []):
                setup = run.substitute(step, CONTEXT)
                if method == setup["method"] and bare == setup["path"] and json.loads(body) == setup["json"]:
                    captured = {"data": {"zoneId": 7}, "id": 7} if step.get("capture") else {}
                    return run.Response(step["expect_status"], "application/json", json.dumps(captured).encode())
        key = self.match(method, path, body)
        if key in self.overrides:
            return self.overrides[key]
        probe = next(p for p in PROBES["probes"] if p["key"] == key)
        if key in self.granted:
            return synthesize_success(probe)
        return problem(key, GATED[key]) if probe["denial"] == "problem" else geoservices(key, GATED[key])

    def match(self, method, path, body):
        for probe in PROBES["probes"]:
            request = run.substitute(probe["request"], CONTEXT)
            if request["method"] == method and request["path"] == path:
                if "json" in request and json.loads(body) != request["json"]:
                    continue
                return probe["key"]
        raise AssertionError(f"unexpected request {method} {path}")


def synthesize_success(probe):
    expect = probe.get("positive")
    if expect is None:
        return run.Response(404, "application/problem+json", b'{"status":404}')
    if "magic" in expect:
        return run.Response(expect["status"], expect["content_type"], bytes.fromhex(expect["magic"]) + b"..")
    if "text_contains" in expect:
        return run.Response(expect["status"], expect["content_type"], "\n".join(expect["text_contains"]).encode())
    if "checks" not in expect:
        return run.Response(expect["status"], expect["content_type"], b"\x1a\x02tile")
    document = {}
    for check in expect["checks"]:
        if check.get("absent"):
            continue
        value = check.get("equals", {"array": [{"attributes": {"count": 1}}], "number": 1, "string": "x/tileset.json.pdf.png"}
                          .get(check.get("type"), "x"))
        if "contains" in check:
            value = [check["contains"]]
        if "endswith" in check:
            value = "x" + check["endswith"]
        put(document, check["path"], value)
    return run.Response(expect["status"], expect["content_type"] + "; charset=utf-8", json.dumps(document).encode())


def put(document, path, value):
    parts = re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)
    current = document
    for position, (name, index) in enumerate(parts):
        last = position == len(parts) - 1
        nxt = ([] if parts[position + 1][1] else {}) if not last else value
        if index:
            while len(current) <= int(index):
                current.append(None)
            if last or current[int(index)] is None:
                current[int(index)] = nxt
            current = current[int(index)]
        else:
            if last or name not in current:
                current[name] = nxt
            current = current[name]


def observe(fixture_id, **overrides):
    fixture = next(f for f in FIXTURES["fixtures"] if f["id"] == fixture_id)
    transport = FakeCandidate(fixture_id, overrides)
    stack = run.Stack("http://localhost", "admin-key", transport, dict(SEEDED))
    return run.observe(stack, PROBES, fixture, set(fx.granted_keys(fixture, PROBES)), "sha256:" + "0" * 64), transport


def all_records(**overrides):
    return {fid: observe(fid, **overrides.get(fid, {}))[0] for fid in ("entitled", "pro-tier", "keyless")}


def test_a_correctly_gated_candidate_is_blocked_only_by_the_declared_infrastructure_gaps():
    report = run.verdict(PROBES, FIXTURES, all_records())
    assert report["problems"] == []
    assert report["status"] == "blocked"
    assert report["summary"] == {"pass": 38, "fail": 0, "blocked": 7, "no-http-surface": 17}
    negatives = {row["key"]: row["negative"]["status"] for row in report["capabilities"] if "negative" in row}
    assert set(negatives.values()) == {"pass"}  # every gated key is denied, blocked positives included


def test_an_entitled_404_is_a_failure_not_an_allowed_answer():
    lost = run.Response(404, "application/problem+json", b'{"status":404,"title":"Not Found"}')
    report = run.verdict(PROBES, FIXTURES, all_records(entitled={"identity.saml": lost}))
    assert report["status"] == "fail"
    saml = next(row for row in report["capabilities"] if row["key"] == "identity.saml")
    assert saml["positive"]["status"] == "fail" and "HTTP 404, expected 200" in saml["positive"]["reasons"]


def test_an_unentitled_500_is_a_failure_not_a_denial():
    crash = run.Response(500, "application/problem+json", b'{"status":500}')
    report = run.verdict(PROBES, FIXTURES, all_records(**{"pro-tier": {"geocoding.batch": crash}}))
    batch = next(row for row in report["capabilities"] if row["key"] == "geocoding.batch")
    assert report["status"] == "fail" and batch["negative"]["status"] == "fail"


def test_an_enterprise_key_denied_by_its_pro_prerequisite_fails():
    masked = problem("identity.oidc", "Pro")
    report = run.verdict(PROBES, FIXTURES, all_records(**{"pro-tier": {"identity.oidc-multi-provider": masked}}))
    row = next(r for r in report["capabilities"] if r["key"] == "identity.oidc-multi-provider")
    assert row["negative"]["status"] == "fail"


def test_catalog_drift_and_license_mismatch_fail_the_lane():
    records = all_records()
    records["entitled"]["live_catalog"] = sorted(records["entitled"]["live_catalog"] + ["geocoding.forward"])
    records["keyless"]["license_check"] = ["license licenseId is 'other', expected 'x'"]
    report = run.verdict(PROBES, FIXTURES, records)
    assert report["status"] == "fail"
    assert any("unlisted ['geocoding.forward']" in p for p in report["problems"])
    assert any(p.startswith("keyless: license licenseId") for p in report["problems"])


def test_a_missing_fixture_stack_is_blocked_never_pass():
    records = all_records()
    del records["pro-tier"]
    report = run.verdict(PROBES, FIXTURES, records)
    assert report["status"] == "blocked"
    assert "no observations for the pro-tier fixture stack" in report["why"]


def test_a_broken_setup_or_transport_fails_that_probe_without_aborting_the_stack():
    fixture = next(f for f in FIXTURES["fixtures"] if f["id"] == "entitled")
    healthy = FakeCandidate("entitled")

    def flaky(method, url, headers, body, markers=()):
        if url.endswith("/api/v1/admin/alerts/zones"):
            return run.Response(200, "text/html", b"<html>proxy</html>")
        if url.endswith("/api/v1/admin/tenants"):
            raise ConnectionResetError("reset by peer")
        return healthy(method, url, headers, body, markers)

    stack = run.Stack("http://localhost", "admin-key", flaky, dict(SEEDED))
    record = run.observe(stack, PROBES, fixture, set(fx.granted_keys(fixture, PROBES)), "sha256:" + "0" * 64)
    by_key = {o["key"]: o for o in record["observations"]}
    assert by_key["alerts.enter-exit"]["status"] == "fail"
    assert "setup 'geofence zone' did not return data.zoneId" in by_key["alerts.enter-exit"]["reasons"]
    assert by_key["admin.multi-tenancy"]["status"] == "fail"
    assert by_key["admin.multi-tenancy"]["reasons"] == ["request failed: reset by peer"]
    assert by_key["identity.saml"]["status"] == "pass"


def test_every_request_carries_the_admin_credential_and_substitutes_placeholders():
    _, transport = observe("entitled")
    assert all(headers["X-API-Key"] == "admin-key" for _, _, headers in transport.requests)
    assert not any("{" in path for _, path, _ in transport.requests)


def test_the_report_carries_fingerprints_and_never_license_material():
    minted = fx.mint_all(FAKE_CORPUS, fake_fixtures(), PROBES)
    records = all_records()
    for fid, fixture in minted["fixtures"].items():
        records[fid]["license_fingerprint"] = fixture["license_fingerprint"]
    text = json.dumps(run.verdict(PROBES, FIXTURES, records))
    for fixture in minted["fixtures"].values():
        envelope = json.loads(fixture["envelope"])
        assert envelope["signature"] not in text and envelope["payload"] not in text
        assert fixture["license_fingerprint"] in text
    assert minted["trusted_public_key"] not in text


# ── fixtures ─────────────────────────────────────────────────────────────────────────────

def test_fixture_licenses_are_deterministic_signed_envelopes():
    first, second = (fx.mint_all(FAKE_CORPUS, fake_fixtures(), PROBES) for _ in range(2))
    assert first == second
    envelope = json.loads(first["fixtures"]["pro-tier"]["envelope"])
    assert envelope["version"] == 1 and envelope["keyId"] == "test-key"
    payload = json.loads(fx.base64.urlsafe_b64decode(envelope["payload"] + "=="))
    assert payload["schema"] == "honua.license/v1" and payload["edition"] == "Pro"
    assert "expiresAt" not in payload
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    public = Ed25519PrivateKey.from_private_bytes(FAKE_SEED).public_key()
    public.verify(fx.base64.urlsafe_b64decode(envelope["signature"] + "=="),
                  fx.base64.urlsafe_b64decode(envelope["payload"] + "=="))


def test_a_corpus_key_that_is_not_the_committed_trust_anchor_is_refused():
    with pytest.raises(fx.FixtureError, match="trust-anchor fingerprint"):
        fx.mint_all(FAKE_CORPUS, FIXTURES, PROBES)
    with pytest.raises(fx.FixtureError, match="no longer declares SigningSeed"):
        fx.mint_all("public const string KeyId = \"test-key\";", fake_fixtures(), PROBES)


def test_no_license_material_or_signing_seed_is_committed():
    for path in HERE.iterdir():
        if path.suffix in (".json", ".yml", ".py", ".sh", ".md") and path.name != Path(__file__).name:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r'"signature"\s*:\s*"[A-Za-z0-9_-]{40,}', text), path
            assert not re.search(r"0x[0-9A-Fa-f]{2}\s*,\s*0x[0-9A-Fa-f]{2}", text), path


def test_generated_uploads_are_well_formed():
    cloud = run.las_grid()
    assert cloud[:4] == b"LASF" and len(cloud) == 227 + 16 * 34
    assert b"bldg:Building" in run.citygml_building() and b"EPSG::4326" in run.citygml_building()


# ── wiring ───────────────────────────────────────────────────────────────────────────────

def test_the_overlay_changes_only_licensing_and_route_opt_ins():
    overlay = yaml.safe_load((HERE / "compose.licensed.yml").read_text(encoding="utf-8"))["services"]["server"]
    env = overlay["environment"]
    assert env["Licensing__Mode"] == "Enabled"
    assert env["Licensing__LicenseContent"].startswith("${HONUA_ENTITLEMENT_LICENSE:?")
    assert not any(k in env for k in ("Licensing__DevGrantEdition", "HONUA_DEV_AUTH", "ConnectionStrings__DefaultConnection"))
    assert set(overlay) == {"environment", "tmpfs"}


def test_the_certification_gate_runs_and_reports_the_entitlement_lane():
    workflow = yaml.safe_load((ROOT / ".github/workflows/certification.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["conformance-entitlements"]
    assert "conformance-entitlements" in workflow["jobs"]["report"]["needs"]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "certification/entitlements/lane.sh" in script
    assert "LicenseTestSupport.cs" in script
    fragment = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/gate-fragment")
    assert fragment["with"]["gate"] == "conformance-entitlements"
