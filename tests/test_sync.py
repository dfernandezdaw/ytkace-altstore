import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import scripts.sync as sync_module
from scripts.sync import SyncError, extract_metadata, source_document, validate_release, validate_variant_minimum_os, variant_for

class SyncTests(unittest.TestCase):
    def test_variant_classification(self):
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_iOS16_21.33.6.ipa"), "ios16")
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_21.46.3.ipa"), None)
        self.assertEqual(variant_for("YTKACE_1.1.8_YouTube_iOS17_21.46.3.ipa"), "ios17")

    def test_validate_upstream_assets_accepts_one_or_two_ipas(self):
        assets = [
            {"name": "x_iOS16.ipa", "size": 10, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/x_iOS16.ipa"},
            {"name": "x_21.46.3.ipa", "size": 11, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/x_21.46.3.ipa"},
        ]
        self.assertEqual(validate_release({"assets": assets}), assets)
        self.assertEqual(validate_release({"assets": assets[:1]}), assets[:1])
        with self.assertRaises(SyncError):
            validate_release({"assets": []})
        extra = {"name": "extra.ipa", "size": 12, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/extra.ipa"}
        with self.assertRaises(SyncError):
            validate_release({"assets": assets + [extra]})
        forged = {**assets[0], "browser_download_url": "https://example.com/ipa.ipa"}
        with self.assertRaisesRegex(SyncError, "not an official upstream"):
            validate_release({"assets": [forged]})

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

    def test_minimum_os_classifies_unlabelled_variants(self):
        self.assertEqual(sync_module.variant_from_minimum_os("16.0"), "ios16")
        self.assertEqual(sync_module.variant_from_minimum_os("17.0"), "ios17")
        self.assertEqual(sync_module.variant_from_minimum_os("18.0"), "ios17")
        with self.assertRaisesRegex(SyncError, "below supported"):
            sync_module.variant_from_minimum_os("15.0")

    def test_sync_rejects_duplicate_unlabelled_variants_without_source_write(self):
        assets = [
            {"name": "first.ipa", "size": 100, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/first.ipa"},
            {"name": "second.ipa", "size": 101, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/second.ipa"},
        ]
        metadata = [
            {"bundleIdentifier": "com.google.ios.youtube", "version": "1", "buildVersion": "1", "minOSVersion": "16.0", "entitlements": [], "privacy": {}},
            {"bundleIdentifier": "com.google.ios.youtube", "version": "2", "buildVersion": "2", "minOSVersion": "16.0", "entitlements": [], "privacy": {}},
        ]
        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.json"
            source_path.write_text("existing source\n")
            with patch.object(sync_module, "SOURCE_PATH", source_path):
                with patch.object(sync_module, "json_request", return_value={"tag_name": "v1", "assets": assets}):
                    with patch.object(sync_module, "download", side_effect=[("a", 100), ("b", 101)]):
                        with patch.object(sync_module, "extract_metadata", side_effect=metadata):
                            with self.assertRaisesRegex(SyncError, "Multiple IPA assets match ios16"):
                                sync_module.sync()
            self.assertEqual(source_path.read_text(), "existing source\n")

    def test_sync_rejects_unsupported_unlabelled_baseline_without_source_write(self):
        asset = {"name": "unlabelled.ipa", "size": 100, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1/unlabelled.ipa"}
        metadata = {"bundleIdentifier": "com.google.ios.youtube", "version": "1", "buildVersion": "1", "minOSVersion": "15.0", "entitlements": [], "privacy": {}}
        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.json"
            source_path.write_text("existing source\n")
            with patch.object(sync_module, "SOURCE_PATH", source_path):
                with patch.object(sync_module, "json_request", return_value={"tag_name": "v1", "assets": [asset]}):
                    with patch.object(sync_module, "download", return_value=("a", 100)):
                        with patch.object(sync_module, "extract_metadata", return_value=metadata):
                            with self.assertRaisesRegex(SyncError, "below supported"):
                                sync_module.sync()
            self.assertEqual(source_path.read_text(), "existing source\n")

    def test_source_uses_official_urls_and_supports_one_or_two_versions(self):
        base = {"bundleIdentifier": "example.ytkace", "version": "1", "buildVersion": "1", "size": 7, "minOSVersion": "16.0", "entitlements": ["aps-environment"], "privacy": {"NSCameraUsageDescription": "Camera"}, "downloadURL": "https://github.com/itzzace/ytkace/releases/download/v1/YTKACE-iOS16.ipa"}
        newer = {**base, "minOSVersion": "18.0", "downloadURL": "https://github.com/itzzace/ytkace/releases/download/v1/YTKACE-iOS17.ipa"}
        release = {"published_at": "2025-01-01T00:00:00Z"}
        doc = source_document(release, [base, newer])
        self.assertEqual(set(doc), {"name", "apps", "news"})
        versions = doc["apps"][0]["versions"]
        self.assertEqual(len(versions), 2)
        self.assertEqual([v["downloadURL"] for v in versions], [newer["downloadURL"], base["downloadURL"]])
        self.assertEqual(versions[0]["minOSVersion"], "18.0")
        self.assertTrue(all(v["size"] == 7 for v in versions))
        self.assertEqual(doc["apps"][0]["appPermissions"], {"entitlements": ["aps-environment"], "privacy": {"NSCameraUsageDescription": "Camera"}})
        self.assertTrue(doc["apps"][0]["iconURL"].startswith("https://"))
        single = source_document(release, [base])["apps"][0]["versions"]
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0]["downloadURL"], base["downloadURL"])

    def test_invalid_single_ipa_preserves_existing_source(self):
        asset = {"name": "YTKACE_1.1.8_YouTube_21.46.3.ipa", "size": 100, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1.1.8/YTKACE_1.1.8_YouTube_21.46.3.ipa"}
        invalid = {"bundleIdentifier": "com.google.ios.youtube", "version": "21.46.3", "buildVersion": "21.46.3", "minOSVersion": None, "entitlements": [], "privacy": {}}
        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.json"
            source_path.write_text("existing source\n")
            with patch.object(sync_module, "SOURCE_PATH", source_path):
                with patch.object(sync_module, "json_request", return_value={"tag_name": "v1.1.8", "assets": [asset]}) as api:
                    with patch.object(sync_module, "download", return_value=("digest", 100)) as download:
                        with patch.object(sync_module, "extract_metadata", return_value=invalid):
                            with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
                                with self.assertRaisesRegex(SyncError, "MinimumOSVersion"):
                                    sync_module.sync()
            self.assertEqual(source_path.read_text(), "existing source\n")
        api.assert_called_once_with(sync_module.UPSTREAM)
        download.assert_called_once_with(asset["browser_download_url"], unittest.mock.ANY)

    def test_checked_in_source_uses_official_upstream_downloads(self):
        source_path = Path(__file__).resolve().parents[1] / "source.json"
        doc = json.loads(source_path.read_text())
        versions = doc["apps"][0]["versions"]
        self.assertEqual(len(versions), 2)
        self.assertTrue(all(v["downloadURL"].startswith("https://github.com/itzzace/ytkace/releases/download/") for v in versions))
        self.assertFalse(any("dfernandezdaw/ytkace-altstore/releases/download" in v["downloadURL"] for v in versions))

    def test_sync_uses_upstream_urls_without_release_api_or_token(self):
        ios16 = {"name": "YTKACE_1.1.8_YouTube_iOS16_21.33.6.ipa", "size": 100, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1.1.8/YTKACE_1.1.8_YouTube_iOS16_21.33.6.ipa"}
        unlabelled16 = {"name": "YTKACE_1.1.8_YouTube_21.33.6.ipa", "size": 100, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1.1.8/YTKACE_1.1.8_YouTube_21.33.6.ipa"}
        ios17 = {"name": "YTKACE_1.1.8_YouTube_21.46.3.ipa", "size": 200, "browser_download_url": "https://github.com/itzzace/ytkace/releases/download/v1.1.8/YTKACE_1.1.8_YouTube_21.46.3.ipa"}
        metadata = [
            {"bundleIdentifier": "com.google.ios.youtube", "version": "21.33.6", "buildVersion": "21.33.6", "minOSVersion": "16.0", "entitlements": [], "privacy": {}},
            {"bundleIdentifier": "com.google.ios.youtube", "version": "21.46.3", "buildVersion": "21.46.3", "minOSVersion": "17.0", "entitlements": [], "privacy": {}},
        ]
        cases = (
            ([ios17], [metadata[1]]),
            ([unlabelled16], [metadata[0]]),
            ([unlabelled16, ios17], metadata),
            ([ios16, ios17], metadata),
        )
        for assets, records in cases:
            with self.subTest(asset_count=len(assets)), tempfile.TemporaryDirectory() as td:
                source_path = Path(td) / "source.json"
                with patch.object(sync_module, "SOURCE_PATH", source_path):
                    with patch.object(sync_module, "json_request", return_value={"tag_name": "v1.1.8", "published_at": "2026-11-15T20:23:33Z", "assets": assets}) as api:
                        with patch.object(sync_module, "download", side_effect=[("digest", a["size"]) for a in assets]) as download:
                            with patch.object(sync_module, "extract_metadata", side_effect=records):
                                with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
                                    with redirect_stdout(io.StringIO()) as output:
                                        sync_module.sync()
                doc = json.loads(source_path.read_text())
                self.assertEqual(api.call_args_list, [call(sync_module.UPSTREAM)])
                self.assertEqual([item.args[0] for item in download.call_args_list], [a["browser_download_url"] for a in assets])
                self.assertEqual({v["downloadURL"] for v in doc["apps"][0]["versions"]}, {a["browser_download_url"] for a in assets})
                self.assertEqual(len(doc["apps"][0]["versions"]), len(assets))
                self.assertIn("Synchronized v1.1.8", output.getvalue())

    def test_workflow_uses_supported_macos_runner(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml").read_text()
        self.assertIn("    runs-on: macos-15\n", workflow)
        self.assertNotIn("runs-on: macos-14", workflow)

    def test_sync_step_does_not_receive_github_token(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml").read_text()
        sync_step = workflow.split("- name: Synchronize upstream release", 1)[1].split("- name: Publish source.json", 1)[0]
        self.assertNotIn("GITHUB_TOKEN:", sync_step)

    def test_workflow_uses_node24_compatible_action_releases(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/sync.yml").read_text()
        self.assertIn("actions/checkout@v7.0.1", workflow)
        self.assertIn("actions/setup-python@v7.1.0", workflow)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)

if __name__ == "__main__": unittest.main()
