# Linux compatibility packages

This directory is not a catalog of supported production downloads. Use the
current GitHub Release or a verified in-app update.

The small version-bound `tuxdrive_VERSION_all.deb` compatibility copy has exactly
the same bytes and Debian package identity as the corresponding modern-named
Release asset. Its commit-pinned raw URL keeps pre-0.26.3 clients upgradable.
Do not overwrite an existing version's bytes.

The original 0.19.1 file is temporarily retained because already-issued legacy
metadata remains valid through 9 November 2026. It is not a new-install target.
Other obsolete package copies remain recoverable in the immutable 0.26.79
source history at commit `dd626c281342ed448f326d3e2b83924203fac673`; they are no
longer advertised by the current tree. No Git history has been rewritten.
