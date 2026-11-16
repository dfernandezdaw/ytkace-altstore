import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import scripts.sync as sync_module
from scripts.sync import SyncError, extract_metadata, release_upload_url, replace_release_asset, source_document, validate_release, validate_variant_minimum_os, variant_for

class SyncTests(unittest.TestCase):
    def test_variant_classification(self):
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_iOS16_21.33.6.ipa"), "ios16")
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_21.46.3.ipa"), None)
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_iOS17_21.46.3.ipa"), "ios17")

    def test_validate_upstream_assets(self):
        assets = [{"name": "x_iOS16.ipa"}, {"name": "x_iOS17.ipa"}]
        self.assertEqual(set(validate_release({"assets": assets})), {"ios16", "ios17"})
        with self.assertRaises(SyncError): validate_release({"assets": assets[:1]})
        with self.assertRaises(SyncError): validate_release({"assets": assets + [{"name": "y_iOS16.ipa"}]})

    def test_extract_fixture_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.ipa"
            info = {"CFBundleIdentifier": "example.ytkace", "CFBundleExecutable": "Test", "CFBundleShortVersionString": "1.2", "CFBundleVersion": "3", "MinimumOSVersion": "16.0", "NSCameraUsageDescription": "Camera"}
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("Payload/Test.app/Info.plist", plistlib.dumps(info))
                zf.writestr("Payload/Test.app/Test", b"fixture")
            xml = plistlib.dumps({"aps-environment": "production"}).decode()
            result = type("Result", (), {"returncode": 0, "stdout": xml, "stderr": ""})()
            with patch("scripts.sync.subprocess.run", return_value=result):
                found = extract_metadata(path)
            self.assertEqual(found["bundleIdentifier"], "example.ytkace")
            self.assertEqual(found["minOSVersion"], "16.0")
            self.assertEqual(found["privacy"], {"NSCameraUsageDescription": "Camera"})
            self.assertEqual(found["entitlements"], ["aps-environment"])

    def test_main_app_privacy_description_wins_over_extension_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fixture.ipa"
            main = {"CFBundleIdentifier": "example.ytkace", "CFBundleExecutable": "Test", "CFBundleShortVersionString": "1", "CFBundleVersion": "1", "MinimumOSVersion": "16.0", "NSCameraUsageDescription": "Main app camera description"}
            extension = {"CFBundleExecutable": "CameraExtension", "NSCameraUsageDescription": "Extension camera description"}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Payload/Test.app/Info.plist", plistlib.dumps(main))
                archive.writestr("Payload/Test.app/Test", b"fixture")
                archive.writestr("Payload/Test.app/PlugIns/Camera.appex/Info.plist", plistlib.dumps(extension))
                archive.writestr("Payload/Test.app/PlugIns/Camera.appex/CameraExtension", b"fixture")
            runner = """import json, plistlib, sys\nfrom types import SimpleNamespace\nfrom unittest.mock import patch\nfrom scripts.sync import extract_metadata\nxml = plistlib.dumps({'aps-environment': 'production'}).decode()\nresult = SimpleNamespace(returncode=0, stdout=xml, stderr='')\nwith patch('scripts.sync.subprocess.run', return_value=result):\n    print(json.dumps(extract_metadata(sys.argv[1])['privacy']))\n"""
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = "0"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            result = subprocess.run([sys.executable, "-c", runner, str(path)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["NSCameraUsageDescription"], "Main app camera description")

    def test_minimum_os_matches_filename_selected_variant(self):
        validate_variant_minimum_os("ios16", "16.0")
        validate_variant_minimum_os("ios17", "17.0")
        validate_variant_minimum_os("ios17", "17.2")
        validate_variant_minimum_os("ios17", "18.0")
        with self.assertRaises(SyncError): validate_variant_minimum_os("ios16", "17.0")
        with self.assertRaises(SyncError): validate_variant_minimum_os("ios17", "16.0")
        with self.assertRaises(SyncError): validate_variant_minimum_os("ios16", None)

    def test_source_has_two_stable_variants_and_schema_keys(self):
        base = {"bundleIdentifier": "example.ytkace", "version": "1", "buildVersion": "1", "size": 7, "minOSVersion": "16.0", "entitlements": ["aps-environment"], "privacy": {"NSCameraUsageDescription": "Camera"}}
        doc = source_document({"published_at": "2025-01-01T00:00:00Z"}, {"ios16": base, "ios17": {**base, "minOSVersion": "18.0"}})
        self.assertEqual(set(doc), {"name", "apps", "news"})
        versions = doc["apps"][0]["versions"]
        self.assertEqual(len(versions), 2)
        self.assertEqual({v["downloadURL"].rsplit("/", 1)[-1] for v in versions}, {"YTKACE-iOS16.ipa", "YTKACE-iOS17.ipa"})
        self.assertTrue(versions[0]["downloadURL"].endswith("YTKACE-iOS17.ipa"))
        self.assertEqual(versions[0]["minOSVersion"], "18.0")
        self.assertTrue(versions[1]["downloadURL"].endswith("YTKACE-iOS16.ipa"))
        self.assertTrue(all(v["size"] == 7 for v in versions))
        self.assertEqual(doc["apps"][0]["appPermissions"], {"entitlements": ["aps-environment"], "privacy": {"NSCameraUsageDescription": "Camera"}})
        self.assertTrue(doc["apps"][0]["iconURL"].startswith("https://"))

    def test_workflow_uses_supported_macos_runner(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml").read_text()
        self.assertIn("    runs-on: macos-15\n", workflow)
        self.assertNotIn("runs-on: macos-14", workflow)

    def test_release_upload_url_uses_name_query_parameter(self):
        self.assertEqual(
            release_upload_url("https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}", "YTKACE iOS16.ipa.uploading"),
            "https://uploads.github.com/repos/o/r/releases/1/assets?name=YTKACE+iOS16.ipa.uploading",
        )

    def test_failed_upload_preserves_existing_release_asset(self):
        old = {"id": 1, "name": "YTKACE-iOS16.ipa"}
        with patch("scripts.sync.upload_release_asset", side_effect=OSError("upload failed")) as upload:
            with patch("scripts.sync.rename_release_asset") as rename:
                with patch("scripts.sync.delete_release_asset") as delete:
                    with self.assertRaisesRegex(OSError, "upload failed"):
                        replace_release_asset("https://api.github.com/repos/o/r", "https://uploads.github.com/assets{?name,label}", [old], old["name"], Path("new.ipa"), "token")
        upload.assert_called_once()
        rename.assert_not_called()
        delete.assert_not_called()

    def test_ambiguous_promotion_error_reconciles_remote_asset(self):
        repo_api = "https://api.github.com/repos/o/r"
        old = {"id": 1, "name": "YTKACE-iOS16.ipa"}
        staged = {"id": 2, "name": "YTKACE-iOS16.ipa.uploading"}
        backup = {"id": 1, "name": ".YTKACE-iOS16.ipa.previous"}
        promoted = {"id": 2, "name": "YTKACE-iOS16.ipa"}
        with patch("scripts.sync.upload_release_asset", return_value=staged):
            with patch("scripts.sync.rename_release_asset", side_effect=[backup, OSError("connection reset")]):
                with patch("scripts.sync.get_release_asset", return_value=promoted) as get_asset:
                    with patch("scripts.sync.delete_release_asset") as delete:
                        result = replace_release_asset(repo_api, "https://uploads.github.com/assets{?name,label}", [old], old["name"], Path("new.ipa"), "token")
        self.assertEqual(result, promoted)
        get_asset.assert_called_once_with(repo_api, staged, "token")
        delete.assert_called_once_with(repo_api, backup, "token")

    def test_failed_promotion_restores_old_stable_asset(self):
        old = {"id": 1, "name": "YTKACE-iOS16.ipa"}
        staged = {"id": 2, "name": ".YTKACE-iOS16.ipa.test.uploading"}
        backup = {"id": 1, "name": ".YTKACE-iOS16.ipa.previous"}
        with patch("scripts.sync.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "test"
            with patch("scripts.sync.upload_release_asset", return_value=staged):
                with patch("scripts.sync.rename_release_asset", side_effect=[backup, OSError("promotion failed"), old]) as rename:
                    with patch("scripts.sync.get_release_asset", return_value=staged):
                        with patch("scripts.sync.delete_release_asset") as delete:
                            with self.assertRaisesRegex(OSError, "promotion failed"):
                                replace_release_asset("https://api.github.com/repos/o/r", "https://uploads.github.com/assets{?name,label}", [old], old["name"], Path("new.ipa"), "token")
        self.assertEqual(rename.call_args_list[-1].args[1:3], (backup, old["name"]))
        delete.assert_called_once_with("https://api.github.com/repos/o/r", staged, "token")

    def test_pair_replacement_rolls_back_first_when_second_fails(self):
        repo_api = "https://api.github.com/repos/o/r"
        upload_url = "https://uploads.github.com/assets{?name,label}"
        token = "token"
        old16 = {"id": 1, "name": "YTKACE-iOS16.ipa"}
        old17 = {"id": 3, "name": "YTKACE-iOS17.ipa"}
        staged16 = {"id": 2, "name": ".YTKACE-iOS16.ipa.16.uploading"}
        staged17 = {"id": 4, "name": ".YTKACE-iOS17.ipa.17.uploading"}
        backup16 = {"id": 1, "name": ".YTKACE-iOS16.ipa.16.previous"}
        backup17 = {"id": 3, "name": ".YTKACE-iOS17.ipa.17.previous"}
        promoted16 = {"id": 2, "name": "YTKACE-iOS16.ipa"}
        renames = [backup16, promoted16, backup17, OSError("second promotion failed"), old17, None, old16]
        with patch("scripts.sync.uuid.uuid4", side_effect=[SimpleNamespace(hex="16"), SimpleNamespace(hex="17"), SimpleNamespace(hex="rollback")]):
            with patch("scripts.sync.upload_release_asset", side_effect=[staged16, staged17]) as upload:
                with patch("scripts.sync.rename_release_asset", side_effect=renames) as rename:
                    with patch("scripts.sync.get_release_asset", return_value=staged17) as get_asset:
                        with patch("scripts.sync.delete_release_asset") as delete:
                            with self.assertRaisesRegex(OSError, "second promotion failed"):
                                sync_module.replace_release_assets(
                                    repo_api, upload_url, [old16, old17],
                                    [("YTKACE-iOS16.ipa", Path("ios16.ipa")), ("YTKACE-iOS17.ipa", Path("ios17.ipa"))],
                                    token,
                                )
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(get_asset.call_args, call(repo_api, staged17, token))
        self.assertEqual(rename.call_count, 7)
        self.assertEqual(rename.call_args_list[1], call(repo_api, staged16, "YTKACE-iOS16.ipa", token))
        self.assertEqual(rename.call_args_list[4], call(repo_api, backup17, "YTKACE-iOS17.ipa", token))
        first_rollback = rename.call_args_list[5].args
        self.assertEqual(first_rollback[:2], (repo_api, promoted16))
        self.assertTrue(first_rollback[2].startswith(".YTKACE-iOS16.ipa."))
        self.assertTrue(first_rollback[2].endswith(".rollback"))
        self.assertEqual(rename.call_args_list[6], call(repo_api, backup16, "YTKACE-iOS16.ipa", token))
        self.assertEqual(delete.call_args_list, [call(repo_api, staged17, token), call(repo_api, promoted16, token)])

    def test_asset_replacement_promotes_new_before_removing_backup(self):
        old = {"id": 1, "name": "YTKACE-iOS16.ipa"}
        staged = {"id": 2, "name": "YTKACE-iOS16.ipa.uploading"}
        backup = {"id": 1, "name": "YTKACE-iOS16.ipa.previous"}
        promoted = {"id": 2, "name": "YTKACE-iOS16.ipa"}
        with patch("scripts.sync.upload_release_asset", return_value=staged) as upload:
            with patch("scripts.sync.rename_release_asset", side_effect=[backup, promoted]) as rename:
                with patch("scripts.sync.delete_release_asset") as delete:
                    result = replace_release_asset("https://api.github.com/repos/o/r", "https://uploads.github.com/assets{?name,label}", [old], old["name"], Path("new.ipa"), "token")
        self.assertEqual(result, promoted)
        upload.assert_called_once()
        calls = rename.call_args_list
        self.assertEqual(calls[0].args[1], old)
        self.assertTrue(calls[0].args[2].startswith(".YTKACE-iOS16.ipa."))
        self.assertTrue(calls[0].args[2].endswith(".previous"))
        self.assertEqual(calls[1].args, ("https://api.github.com/repos/o/r", staged, "YTKACE-iOS16.ipa", "token"))
        delete.assert_called_once_with("https://api.github.com/repos/o/r", backup, "token")

if __name__ == "__main__": unittest.main()
