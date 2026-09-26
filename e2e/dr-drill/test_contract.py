from pathlib import Path

HERE = Path(__file__).parent


def test_drill_is_destructive_and_content_gated():
    text = (HERE / "run.sh").read_text()
    for required in ("pg_dump", "down -v", "pg_restore", "BEFORE", "AFTER", "/rest/services?f=json"):
        assert required in text
    assert "contentEqual':True" in text
    assert "originalDatabaseDestroyed':True" in text


def test_required_domains_are_seeded_and_verified():
    seed = (HERE / "seed.sql").read_text()
    snapshot = (HERE / "snapshot.sql").read_text()
    for domain in ("tenant_alpha", "tenant_beta", "layers", "operate_fixture_execution_jobs",
                   "alert_events", "audit_log", "feature_change_outbox", "fieldcollection_sync_cursors"):
        assert domain in seed
        assert domain in snapshot
    for checksum in ("'jobLog'", "'audit'"):
        assert checksum in snapshot


def test_tenant_isolation_uses_tenant_roles_and_denies_cross_schema_reads():
    seed = (HERE / "seed.sql").read_text()
    run = (HERE / "run.sh").read_text()
    for role in ("dr_tenant_alpha", "dr_tenant_beta"):
        assert f"CREATE ROLE {role} NOLOGIN" in seed
        assert role in run
    assert 'SET ROLE $role; SELECT name FROM $own_schema.customer_assets;' in run
    assert 'SET ROLE $role; SELECT name FROM $other_schema.customer_assets;' in run
    assert "assert_all_tenant_isolation" in run


def test_receipt_is_signed_and_candidate_bound():
    text = (HERE / "run.sh").read_text()
    for required in ("ED25519", "pkeyutl -sign", "pkeyutl -verify", "releaseLock",
                     "IMAGE_DIGEST", "BACKUP_SHA", "rpoMs", "rtoMs"):
        assert required in text
    assert "sha256sum receipt.json receipt.json.sig receipt.pub.pem > SHA256SUMS" in text
    assert 'sha256sum "$OUT/receipt.json"' not in text


def test_full_platform_drill_covers_every_declared_substrate():
    """The drill must fail closed on an enabled substrate it cannot exercise.

    A producer that silently drops a substrate would still satisfy the validator's "exactly the
    enabled set" check only by accident of the manifest; the guard here is that the drill refuses
    to emit a receipt it cannot back with an observation.
    """
    text = (HERE / "full_platform.py").read_text()
    assert "no backup path is implemented for enabled substrate" in text
    assert "no drill surface is implemented for substrate" in text
    for substrate in ("postgresql", "redis", "object-storage", "job-queue",
                      "transactional-outbox", "workflow-cursors"):
        assert f'"{substrate}"' in text


def test_full_platform_drill_destroys_and_restores_into_clean_stores():
    text = (HERE / "full_platform.py").read_text()
    for required in ("down", "-v", "docker\", \"volume\", \"create\"",
                     "primary state survived destruction",
                     "was not created as a clean store",
                     "the recreated database was not a clean store",
                     "the recreated Redis was not a clean store"):
        assert required in text
    assert '"primaryStateDestroyed": True' in text
    assert '"restoredIntoCleanStore": True' in text


def test_full_platform_measurements_come_from_observed_timestamps():
    text = (HERE / "full_platform.py").read_text()
    # RTO must be derived from the receipt's own recorded window, not from a constant or a
    # process-ready signal; the validator recomputes exactly this quantity.
    assert "rto_ms = (recovered - stopped).total_seconds() * 1000.0" in text
    assert "rpo_ms = (backup_completed - last_write).total_seconds() * 1000.0" in text
    assert "the runtime instance identity did not change across recovery" in text


def test_full_platform_receipt_is_v2_signed_and_candidate_bound():
    text = (HERE / "full_platform.py").read_text()
    for required in ("honua.dr-drill-receipt/v2", '"scope": "full-platform"', "candidateLockDigest",
                     "ED25519", "pkeyutl -sign".replace(" ", "\", \""), "validate_dr_receipt.py"):
        assert required in text


def test_full_platform_topology_is_the_install_not_the_seam():
    compose = (HERE / "compose.full-platform.yml").read_text()
    for service in ("db:", "redis:", "server:", "storage-init:"):
        assert service in compose
    assert "ConnectionStrings__Redis" in compose
    assert "FileStorage__Provider" in compose
    # Named volumes are what the drill destroys; anonymous volumes would make the destruction
    # step unverifiable.
    for volume in ("db_data", "redis_data", "honua_storage"):
        assert volume in compose
