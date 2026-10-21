"""Prepared729 controls; not executed or candidate proof."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import mcp_managed_identity_fixture as fixture
from mcp_managed_identity_fixture_controls import OfflineNormalApi, PRIVATE_ADMIN, SALT
from mcp_render_validation_controls import runtime

class WorkspaceProfileControls(unittest.TestCase):
    def test_exact_live_bootstrap_service_required_without_legacy_shape_changes(self):
        self.assertEqual(fixture.profile_permissions("admin"), ["admin"])
        self.assertEqual(fixture.profile_permissions("limited-read"), ["read:*"])
        self.assertEqual(fixture.profile_permissions("workspace-reader", "owned-fixture"), ["read:*", "execute:owned-fixture/geometry.area"])
        for profile, service in [("admin", "owned"), ("limited-read", "owned"), ("workspace-reader", None), ("workspace-reader", "*"), ("workspace-reader", "owned/other"), ("workspace-reader", " owned"), ("unknown", "owned")]:
            with self.subTest(profile=profile, service=service), self.assertRaises(ValueError):
                fixture.profile_permissions(profile, service)

    def test_normal_create_revoke_and_original_receipt_bind_exact_execution_service(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OfflineNormalApi()
            managed = fixture.ManagedIdentityFixture(admin_credential=PRIVATE_ADMIN,
                private_path=Path(directory)/"private.json", receipt_path=Path(directory)/"receipt.json",
                inspect_owned=runtime, redis=api.redis, credential_salt=SALT,
                profile="workspace-reader", service_id="owned-fixture")
            with patch.object(fixture, "build_opener", return_value=api):
                managed.create(); managed.revoke()
            value = fixture.verify_receipt(managed.receipt, runtime(), "workspace-reader", service_id="owned-fixture")
            self.assertEqual(value["permissions"], ["read:*", "execute:owned-fixture/geometry.area"])
            for service in (None, "different-fixture"):
                with self.subTest(service=service), self.assertRaises(ValueError):
                    fixture.verify_receipt(managed.receipt, runtime(), "workspace-reader", service_id=service)
            for permissions in (["admin"], ["read:*"], ["read:*", "execute:owned-fixture/*"]):
                altered=copy.deepcopy(managed.receipt); altered["permissions"]=permissions
                with self.subTest(permissions=permissions), self.assertRaises(ValueError):
                    fixture.verify_receipt(altered, runtime(), "workspace-reader", service_id="owned-fixture")

if __name__ == "__main__": unittest.main()
