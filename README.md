# YTKACE AltStore Source

This repository publishes a public AltStore source for the latest YTKACE release. It links directly to the official upstream IPA downloads, so this repository does not mirror the binaries.

## Add the source to AltStore

Add this URL in AltStore:

**https://raw.githubusercontent.com/dfernandezdaw/ytkace-altstore/main/source.json**

AltStore receives one or two versions, depending on the IPA assets in the latest upstream release. Each version keeps the official download URL and the minimum iOS version extracted from its package.

## How synchronization works

GitHub Actions checks the latest published release in [itzzace/ytkace](https://github.com/itzzace/ytkace) every six hours. Maintainers can also start the workflow manually from **Actions → Sync YTKACE AltStore source → Run workflow**.

The synchronizer:

1. Accepts one or two `.ipa` assets with valid official upstream download URLs.
2. Downloads each IPA to temporary runner storage to inspect its package metadata; it does not upload or retain the binaries in this repository.
3. Builds `source.json` with the available versions and their original upstream download URLs.
4. Commits `source.json` to `main` only when its contents change.

The workflow uses the automatic `GITHUB_TOKEN` with `contents: write` to publish source metadata. The synchronization step itself does not need a token to download public upstream assets.

## Package metadata and validation

Version, build, minimum iOS version, package size, signed entitlements, and privacy usage descriptions come from the IPA files. Entitlements are read with `codesign` on macOS from the app and its extensions. The app name, developer name, icon URL, source structure, and localized app description are repository configuration; the AltStore description remains Spanish (`YTKACE para AltStore.`).

The sync fails before replacing `source.json` if the release has no IPA or more than two, an official URL or asset size is invalid, package metadata is missing, a filename-derived variant conflicts with `MinimumOSVersion`, two assets resolve to the same iOS 16 or iOS 17+ variant, the minimum version is below iOS 16, or the packages do not share a bundle identifier. If a filename does not identify a variant, the extracted minimum iOS version is used: iOS 16 maps to the iOS 16 variant, and iOS 17 or later maps to the iOS 17+ variant.

## Troubleshooting

For a failed sync, open **Actions**, select the run, and inspect the **Synchronize upstream release** step. A failed validation leaves the previously published `source.json` unchanged. For source publication errors, check the workflow's `contents: write` permission.
