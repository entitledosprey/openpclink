#!/bin/bash
# Usage: packaging/build_macos.sh <PCLink installer .exe or extracted folder>
# Produces dist/OpenPCLink-<version>-macos-<arch>.dmg for THIS Mac's architecture.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 1 ] || { echo "usage: $0 <PCLink installer or folder>"; exit 2; }
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip -q install -e '.[dev]'
libusb="$(brew --prefix libusb)/lib/libusb-1.0.0.dylib"
[ -f "$libusb" ] || { echo "run: brew install libusb"; exit 1; }
source packaging/common.sh
bake_and_freeze "$1" --windowed --osx-bundle-identifier org.openpclink.app --add-binary "$libusb:."
verify_build dist/OpenPCLink.app/Contents/MacOS/OpenPCLink
version=$(python -c 'import openpclink;print(openpclink.__version__)')
arch=$(uname -m)
staging=$(mktemp -d)
cp -R dist/OpenPCLink.app "$staging/"
ln -s /Applications "$staging/Applications"
rm -f "dist/OpenPCLink-$version-macos-$arch.dmg"
hdiutil create -quiet -volname OpenPCLink -srcfolder "$staging" -ov -format UDZO "dist/OpenPCLink-$version-macos-$arch.dmg"
rm -rf "$staging"
echo "Built dist/OpenPCLink-$version-macos-$arch.dmg"
