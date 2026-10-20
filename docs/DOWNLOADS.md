# Official downloads

Download TuxInDrive only from the immutable
[GitHub Releases page](https://github.com/tpluharik/Tuxindrive/releases). Each
release provides platform installers and `SHA256SUMS.txt` for integrity checks.

Windows installers are currently **not Authenticode-signed**. SignPath.io and
SignPath Foundation integration is planned but is not active in this release.
The expiring Ed25519-signed in-app manifests independently bind the download
version, URL and SHA-256. A checksum by itself is not publisher authentication.
See the [code signing policy](CODE_SIGNING_POLICY.md).

Package-manager listings must reference the same versioned GitHub Release files
and checksums. Mirrors and third-party repackaging are not authoritative update
sources.

The optional [TuxInDrive Network Lab](NETWORK_LAB.md) is published as a separate
`network-lab-vVERSION.REVISION` GitHub release and `.deb`. It is a local fictional
scenario tool, not an update for the desktop client or server. Do not install a
Network Lab package as a replacement for `tuxindrive` or `tuxindrive-server`.
The current lab package is
`tuxindrive-network-lab_0.26.31+lab5_all.deb`, published only under the
`network-lab-v0.26.31.5` tag. Its release must include a checksum beside the
asset; an Actions artifact is temporary build evidence, not an update source.
