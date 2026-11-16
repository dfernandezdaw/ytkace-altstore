#!/usr/bin/env python3
"""Mirror the latest official YTKACE IPA variants into the stable AltStore release."""
from __future__ import annotations
import hashlib, json, os, plistlib, re, subprocess, tempfile, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path

OWNER, REPO = "dfernandezdaw", "ytkace-altstore"
UPSTREAM = "https://api.github.com/repos/itzzace/ytkace/releases/latest"
API = "https://api.github.com"
TAG = "ytkace-current"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "source.json"
ASSET_NAMES = {"ios16": "YTKACE-iOS16.ipa", "ios17": "YTKACE-iOS17.ipa"}

class SyncError(RuntimeError): pass

def request(url, token=None, data=None, method=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ytkace-altstore-sync"}
    if token: headers["Authorization"] = f"Bearer {token}"
    if data is not None: headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response: return response.read(), response.headers.get("Content-Type", "")

def json_request(url, token=None, data=None, method=None):
    raw, _ = request(url, token, json.dumps(data).encode() if data is not None else None, method)
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

def validate_variant_minimum_os(variant, minimum_os):
    limits = {"ios16": 16, "ios17": 17}
    if variant not in limits:
        raise SyncError(f"Unknown IPA variant {variant!r}")
    match = re.fullmatch(r"(\d+)(?:\.\d+)*", str(minimum_os or ""))
    if not match:
        raise SyncError(f"Missing or invalid MinimumOSVersion for {variant}: {minimum_os!r}")
    actual_major = int(match.group(1))
    mismatch = actual_major != limits[variant] if variant == "ios16" else actual_major < limits[variant]
    if mismatch:
        expected = f"iOS {limits[variant]}" if variant == "ios16" else f"iOS {limits[variant]} or newer"
        raise SyncError(f"{variant} IPA reports iOS {minimum_os}, incompatible with its advertised {expected} baseline")

def validate_release(release):
    found = {}
    unclassified = []
    for asset in release.get("assets", []):
        variant = variant_for(asset["name"])
        if variant:
            if variant in found: raise SyncError(f"Multiple IPA assets match {variant}")
            found[variant] = asset
        elif asset["name"].lower().endswith(".ipa"):
            unclassified.append(asset)
    # Current upstream names the iOS 17+ IPA without an OS marker. Infer it only
    # when there is exactly one unclassified IPA alongside a single iOS 16 IPA.
    if "ios16" in found and "ios17" not in found and len(unclassified) == 1:
        found["ios17"] = unclassified[0]
    if set(found) != {"ios16", "ios17"}: raise SyncError("Latest upstream release must provide one iOS 16 IPA and one unambiguous second (iOS 17+) IPA")
    return found

def download(url, path):
    raw, _ = request(url)
    Path(path).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)

def remote_asset_matches(asset, digest):
    if asset.get("digest"):
        return asset["digest"] == "sha256:" + digest
    # Older API responses may omit digest; compare bytes rather than re-upload blindly.
    with tempfile.TemporaryDirectory(prefix="ytkace-remote-") as tmp:
        path = Path(tmp) / "asset.ipa"
        actual, _ = download(asset["browser_download_url"], path)
        return actual == digest

def release_upload_url(upload_url, name):
    base = upload_url.split("{", 1)[0]
    return base + "?" + urllib.parse.urlencode({"name": name})

def upload_release_asset(upload_url, name, path, token):
    endpoint = release_upload_url(upload_url, name)
    req = urllib.request.Request(endpoint, data=Path(path).read_bytes(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream", "Accept": "application/vnd.github+json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read())

def rename_release_asset(repo_api, asset, name, token):
    return json_request(f"{repo_api}/releases/assets/{asset['id']}", token, {"name": name}, "PATCH")

def get_release_asset(repo_api, asset, token):
    return json_request(f"{repo_api}/releases/assets/{asset['id']}", token)

def delete_release_asset(repo_api, asset, token):
    request(f"{repo_api}/releases/assets/{asset['id']}", token, method="DELETE")

def replace_release_asset(repo_api, upload_url, existing_assets, name, path, token, preserve_backup=False):
    previous = next((asset for asset in existing_assets if asset["name"] == name), None)
    suffix = uuid.uuid4().hex
    staged_name = f".{name}.{suffix}.uploading"
    backup_name = f".{name}.{suffix}.previous"
    uploaded = upload_release_asset(upload_url, staged_name, path, token)
    backup = None
    if previous:
        try:
            backup = rename_release_asset(repo_api, previous, backup_name, token)
        except Exception as backup_error:
            try:
                observed = get_release_asset(repo_api, previous, token)
            except Exception as status_error:
                raise SyncError("Previous asset rename outcome is unknown; retaining staged and previous assets") from backup_error
            if observed.get("name") == backup_name:
                backup = observed
            elif observed.get("name") == name:
                delete_release_asset(repo_api, uploaded, token)
                raise backup_error
            else:
                raise SyncError("Previous asset has an unexpected name; retaining all release assets") from backup_error
    try:
        promoted = rename_release_asset(repo_api, uploaded, name, token)
    except Exception as promotion_error:
        try:
            observed_upload = get_release_asset(repo_api, uploaded, token)
        except Exception as status_error:
            raise SyncError("Promotion outcome is unknown; retaining staged and backup assets for reconciliation") from promotion_error
        if observed_upload.get("name") == name:
            # The server applied the rename but its response was lost; treat the
            # observed stable asset as promoted rather than deleting it by ID.
            promoted = observed_upload
        elif observed_upload.get("name") == staged_name:
            if backup:
                try:
                    rename_release_asset(repo_api, backup, name, token)
                except Exception as rollback_error:
                    try:
                        observed_backup = get_release_asset(repo_api, backup, token)
                    except Exception as status_error:
                        raise SyncError("Rollback outcome is unknown; retaining staged and backup assets") from promotion_error
                    if observed_backup.get("name") != name:
                        raise SyncError("Rollback did not restore the stable name; retaining staged and backup assets") from promotion_error
            delete_release_asset(repo_api, uploaded, token)
            raise promotion_error
        else:
            raise SyncError("Staged asset has an unexpected name; retaining all release assets") from promotion_error
    if preserve_backup:
        return promoted, backup
    if backup:
        delete_release_asset(repo_api, backup, token)
    return promoted

def replace_release_assets(repo_api, upload_url, existing_assets, replacements, token):
    completed = []
    try:
        for name, path in replacements:
            promoted, backup = replace_release_asset(
                repo_api, upload_url, existing_assets, name, path, token, preserve_backup=True
            )
            completed.append((name, promoted, backup))
    except Exception as promotion_error:
        for name, promoted, backup in reversed(completed):
            if backup is None:
                delete_release_asset(repo_api, promoted, token)
                continue
            rollback_name = f".{name}.{uuid.uuid4().hex}.rollback"
            try:
                rename_release_asset(repo_api, promoted, rollback_name, token)
            except Exception:
                observed = get_release_asset(repo_api, promoted, token)
                if observed.get("name") != rollback_name:
                    raise SyncError("Pair rollback could not move the promoted asset; retaining its backup") from promotion_error
            try:
                rename_release_asset(repo_api, backup, name, token)
            except Exception:
                observed = get_release_asset(repo_api, backup, token)
                if observed.get("name") != name:
                    raise SyncError("Pair rollback did not restore the previous stable asset; retaining backups") from promotion_error
            try:
                delete_release_asset(repo_api, promoted, token)
            except Exception:
                pass  # The previous stable asset is restored; a later sync cleans the rollback asset.
        raise
    for _, _, backup in completed:
        if backup:
            delete_release_asset(repo_api, backup, token)
    return [promoted for _, promoted, _ in completed]

def source_document(release, records):
    entitlements = sorted(set(records["ios16"]["entitlements"]) | set(records["ios17"]["entitlements"]))
    privacy = {**records["ios16"]["privacy"], **records["ios17"]["privacy"]}
    app = {"bundleIdentifier": records["ios16"]["bundleIdentifier"], "name": "YTKACE", "developerName": "itzzace", "localizedDescription": "YTKACE para AltStore.", "iconURL": "https://raw.githubusercontent.com/itzzace/ytkace/main/screenshots/framed/settings.png", "versions": [], "appPermissions": {"entitlements": entitlements, "privacy": privacy}}
    # List the newest/highest-minimum-OS build first. iOS 16 devices skip it;
    # iOS 17+ devices select it instead of the compatible fallback build.
    for key in ("ios17", "ios16"):
        meta = records[key]
        version = {"version": meta["version"], "buildVersion": meta["buildVersion"], "date": release.get("published_at") or datetime.now(timezone.utc).isoformat(), "downloadURL": f"https://github.com/{OWNER}/{REPO}/releases/download/{TAG}/{ASSET_NAMES[key]}", "size": meta["size"]}
        if meta.get("minOSVersion"): version["minOSVersion"] = meta["minOSVersion"]
        app["versions"].append(version)
    return {"name": "YTKACE", "apps": [app], "news": []}

def sync():
    token = os.environ.get("GITHUB_TOKEN")
    if not token: raise SyncError("GITHUB_TOKEN is required")
    release = json_request(UPSTREAM)
    assets = validate_release(release)
    records, checksums = {}, {}
    with tempfile.TemporaryDirectory(prefix="ytkace-sync-") as tmp:
        paths = {}
        for variant, asset in assets.items():
            path = Path(tmp) / asset["name"]
            digest, size = download(asset["browser_download_url"], path)
            if size != asset["size"]: raise SyncError(f"Downloaded size mismatch for {asset['name']}")
            records[variant] = extract_metadata(path)
            validate_variant_minimum_os(variant, records[variant].get("minOSVersion"))
            records[variant]["size"] = size
            checksums[variant] = digest
            paths[variant] = path
        ids = {r["bundleIdentifier"] for r in records.values()}
        if len(ids) != 1: raise SyncError("iOS variants do not share a bundle identifier")
        doc = source_document(release, records)
        SOURCE_PATH.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        repo_api = f"{API}/repos/{OWNER}/{REPO}"
        try: stable = json_request(f"{repo_api}/releases/tags/{TAG}", token)
        except urllib.error.HTTPError as exc:
            if exc.code != 404: raise
            stable = json_request(f"{repo_api}/releases", token, {"tag_name": TAG, "name": "YTKACE current", "body": "Automatically synchronized current official IPA variants.", "draft": False, "prerelease": False}, "POST")
        existing = {a["name"]: a for a in stable.get("assets", [])}
        replacements = []
        for key, name in ASSET_NAMES.items():
            if name in existing and existing[name].get("size") == records[key]["size"]:
                if remote_asset_matches(existing[name], checksums[key]): continue
            replacements.append((name, paths[key]))
        if replacements:
            replace_release_assets(repo_api, stable["upload_url"], stable.get("assets", []), replacements, token)
            stable = json_request(f"{repo_api}/releases/tags/{TAG}", token)
            existing = {a["name"]: a for a in stable.get("assets", [])}
        # Remove any stale or extraneous assets; stable release has exactly these two IPA assets.
        stable = json_request(f"{repo_api}/releases/tags/{TAG}", token)
        for old in stable.get("assets", []):
            if old["name"] not in ASSET_NAMES.values(): delete_release_asset(repo_api, old, token)
    print(f"Synchronized {release['tag_name']} ({records['ios16']['bundleIdentifier']}); SHA-256: {checksums}")

if __name__ == "__main__":
    try: sync()
    except Exception as exc: raise SystemExit(f"sync failed: {exc}")
