#!/usr/bin/env python3
"""Build AltStore source metadata with direct links to official upstream YTKACE IPAs."""
from __future__ import annotations
import hashlib
import json
import plistlib
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM = "https://api.github.com/repos/itzzace/ytkace/releases/latest"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "source.json"

class SyncError(RuntimeError): pass

def request(url):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ytkace-altstore-sync"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read(), response.headers.get("Content-Type", "")

def json_request(url):
    raw, _ = request(url)
    return json.loads(raw)

def variant_for(name):
    lowered = name.lower()
    if not lowered.endswith(".ipa"): return None
    if re.search(r"(?:ios\s*16|ios16|_ios16)(?:\D|$)", lowered): return "ios16"
    if re.search(r"(?:ios\s*17|ios17|17\+)(?:\D|$)", lowered): return "ios17"
    return None

def extract_metadata(ipa_path):
    with zipfile.ZipFile(ipa_path) as archive:
        candidates = [n for n in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
        if len(candidates) != 1: raise SyncError(f"Expected one application Info.plist in {ipa_path}")
        app_dir = candidates[0].rsplit("/", 1)[0]
        info = plistlib.loads(archive.read(candidates[0]))
        privacy, entitlements = {}, set()
        extension_dirs = sorted(n.rsplit("/Info.plist", 1)[0] for n in archive.namelist()
                               if n.endswith(".appex/Info.plist") and n.startswith(app_dir + "/PlugIns/"))
        app_dirs = [app_dir] + extension_dirs
        with tempfile.TemporaryDirectory(prefix="ytkace-extract-") as extracted:
            archive.extractall(extracted)
            for directory in app_dirs:
                bundle_info = plistlib.loads((Path(extracted) / directory / "Info.plist").read_bytes())
                for key, value in bundle_info.items():
                    if isinstance(key, str) and "UsageDescription" in key and isinstance(value, str):
                        privacy.setdefault(key, value)
                executable = bundle_info.get("CFBundleExecutable")
                if not executable:
                    raise SyncError(f"Missing CFBundleExecutable in {directory}")
                binary = Path(extracted) / directory / executable
                try:
                    result = subprocess.run(["codesign", "-d", "--entitlements", ":-", str(binary)],
                                            check=False, capture_output=True, text=True)
                except FileNotFoundError as exc:
                    raise SyncError("codesign is required to extract signed entitlements (run on macOS)") from exc
                xml = result.stdout if "<plist" in result.stdout else result.stderr
                if result.returncode != 0 and "<plist" not in xml:
                    raise SyncError(f"Could not extract signed entitlements from {directory}: {result.stderr.strip()}")
                if "<plist" in xml:
                    start, end = xml.find("<?xml"), xml.find("</plist>")
                    if start < 0: start = xml.find("<plist")
                    if end < 0: raise SyncError(f"Malformed signed entitlements in {directory}")
                    values = plistlib.loads(xml[start:end + len("</plist>")].encode())
                    if not isinstance(values, dict): raise SyncError(f"Unexpected entitlements in {directory}")
                    entitlements.update(values)
        required = {"bundleIdentifier": info.get("CFBundleIdentifier"), "version": info.get("CFBundleShortVersionString"), "buildVersion": info.get("CFBundleVersion")}
        if not all(required.values()): raise SyncError(f"Missing required bundle/version metadata in {ipa_path}")
        return {**required, "minOSVersion": info.get("MinimumOSVersion"), "entitlements": sorted(entitlements), "privacy": privacy}

def parse_minimum_os(minimum_os):
    value = str(minimum_os or "")
    if not re.fullmatch(r"\d+(?:\.\d+)*", value):
        raise SyncError(f"Missing or invalid MinimumOSVersion: {minimum_os!r}")
    return tuple(int(part) for part in value.split("."))

def variant_from_minimum_os(minimum_os):
    major = parse_minimum_os(minimum_os)[0]
    if major == 16:
        return "ios16"
    if major >= 17:
        return "ios17"
    raise SyncError(f"MinimumOSVersion {minimum_os!r} is below supported iOS 16 baseline")

def validate_variant_minimum_os(variant, minimum_os):
    limits = {"ios16": 16, "ios17": 17}
    if variant not in limits:
        raise SyncError(f"Unknown IPA variant {variant!r}")
    try:
        actual_major = parse_minimum_os(minimum_os)[0]
    except SyncError as exc:
        raise SyncError(f"Missing or invalid MinimumOSVersion for {variant}: {minimum_os!r}") from exc
    mismatch = actual_major != limits[variant] if variant == "ios16" else actual_major < limits[variant]
    if mismatch:
        expected = f"iOS {limits[variant]}" if variant == "ios16" else f"iOS {limits[variant]} or newer"
        raise SyncError(f"{variant} IPA reports iOS {minimum_os}, incompatible with its advertised {expected} baseline")

def validate_official_download_url(url):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path.startswith("/itzzace/ytkace/releases/download/"):
        raise SyncError(f"IPA asset URL is not an official upstream release URL: {url!r}")

def validate_release(release):
    assets = [asset for asset in release.get("assets", [])
              if isinstance(asset.get("name"), str) and asset["name"].lower().endswith(".ipa")]
    if not 1 <= len(assets) <= 2:
        raise SyncError("Latest upstream release must provide one or two IPA assets")
    names = [asset["name"] for asset in assets]
    if len(set(names)) != len(names):
        raise SyncError("Latest upstream release contains duplicate IPA asset names")
    variants = [variant_for(name) for name in names]
    for variant in ("ios16", "ios17"):
        if variants.count(variant) > 1:
            raise SyncError(f"Multiple IPA assets match {variant}")
    for asset in assets:
        validate_official_download_url(asset.get("browser_download_url"))
        if not isinstance(asset.get("size"), int) or asset["size"] < 1:
            raise SyncError(f"Invalid IPA asset size for {asset['name']!r}")
    return assets

def download(url, path):
    raw, _ = request(url)
    Path(path).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)

def source_document(release, records):
    if not 1 <= len(records) <= 2:
        raise SyncError("Source generation requires one or two IPA records")
    bundle_ids = {record.get("bundleIdentifier") for record in records}
    if len(bundle_ids) != 1 or None in bundle_ids:
        raise SyncError("IPA records do not share a valid bundle identifier")
    for record in records:
        parse_minimum_os(record.get("minOSVersion"))
        validate_official_download_url(record.get("downloadURL"))

    def sort_key(record):
        return (parse_minimum_os(record["minOSVersion"]), str(record["version"]),
                str(record["buildVersion"]), record["downloadURL"])

    ordered = sorted(records, key=sort_key, reverse=True)
    entitlements = sorted({item for record in records for item in record.get("entitlements", [])})
    privacy = {}
    for record in reversed(ordered):
        privacy.update(record.get("privacy", {}))
    app = {"bundleIdentifier": ordered[0]["bundleIdentifier"], "name": "YTKACE", "developerName": "itzzace", "localizedDescription": "YTKACE para AltStore.", "iconURL": "https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/14/ce/45/14ce45d8-5ca6-1dfa-c4a5-4660061cf213/logo_youtube_2024_q4_color-0-0-1x_U007emarketing-0-0-0-7-0-0-0-85-220.png/512x512bb.jpg", "versions": [], "appPermissions": {"entitlements": entitlements, "privacy": privacy}}
    for meta in ordered:
        app["versions"].append({"version": meta["version"], "buildVersion": meta["buildVersion"], "date": release.get("published_at") or datetime.now(timezone.utc).isoformat(), "downloadURL": meta["downloadURL"], "size": meta["size"], "minOSVersion": meta["minOSVersion"]})
    return {"name": "YTKACE", "apps": [app], "news": []}

def sync():
    release = json_request(UPSTREAM)
    assets = validate_release(release)
    records, checksums, seen_variants = [], {}, set()
    with tempfile.TemporaryDirectory(prefix="ytkace-sync-") as tmp:
        for index, asset in enumerate(assets):
            path = Path(tmp) / f"{index}-{Path(asset['name']).name}"
            digest, size = download(asset["browser_download_url"], path)
            if size != asset["size"]:
                raise SyncError(f"Downloaded size mismatch for {asset['name']}")
            metadata = extract_metadata(path)
            variant = variant_for(asset["name"])
            if variant:
                validate_variant_minimum_os(variant, metadata.get("minOSVersion"))
            else:
                variant = variant_from_minimum_os(metadata.get("minOSVersion"))
            if variant in seen_variants:
                raise SyncError(f"Multiple IPA assets match {variant} after MinimumOSVersion classification")
            seen_variants.add(variant)
            metadata["size"] = size
            metadata["downloadURL"] = asset["browser_download_url"]
            records.append(metadata)
            checksums[asset["name"]] = digest
        if len({record["bundleIdentifier"] for record in records}) != 1:
            raise SyncError("IPA variants do not share a bundle identifier")
        doc = source_document(release, records)
    SOURCE_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"Synchronized {release['tag_name']} ({records[0]['bundleIdentifier']}); SHA-256: {checksums}")

if __name__ == "__main__":
    try: sync()
    except Exception as exc: raise SystemExit(f"sync failed: {exc}")
