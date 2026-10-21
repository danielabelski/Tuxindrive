#!/bin/sh
# Called only after the archive digest and reviewed baseline were verified.
set -eu
export PATH=/ucrt64/bin:/usr/bin
mkdir -p build-inputs
pacman -Q > build-inputs/windows-msys2-packages.txt
python -m pip download --no-deps --require-hashes --only-binary=:all: \
  -r requirements-build-tools.lock --dest build/verified-build-tools
wheelhouse=$(cygpath -m "$PWD/build/verified-build-tools")
PIP_NO_INDEX=1 PIP_FIND_LINKS="$wheelhouse" \
  python -m pip install --break-system-packages --no-deps .
python -m pip list --format=json > build-inputs/windows-python-packages.json
python -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk, Gio, GLib; import cryptography, defusedxml; import keyring.backends.Windows; assert Gtk.get_major_version() == 3'
sh scripts/build-windows-msys2.sh
