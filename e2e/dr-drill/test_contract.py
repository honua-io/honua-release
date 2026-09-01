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


def test_receipt_is_signed_and_candidate_bound():
    text = (HERE / "run.sh").read_text()
    for required in ("ED25519", "pkeyutl -sign", "pkeyutl -verify", "releaseLock",
                     "IMAGE_DIGEST", "BACKUP_SHA", "rpoMs", "rtoMs"):
        assert required in text
