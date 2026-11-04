#!/bin/bash
# Usage: packaging/build_linux.sh <PCLink folder inside the repo> [amd64|arm64]
# Builds a .deb and a portable tarball in Docker, so it works from macOS as well.
set -euo pipefail
cd "$(dirname "$0")/.."
source_path="$1"; arch="${2:-amd64}"
docker build --platform "linux/$arch" -q -t "openpclink-build-$arch" -f packaging/Dockerfile.linux packaging >/dev/null
docker run --rm --platform "linux/$arch" -v "$PWD:/repo:ro" -v "$PWD/dist:/out" "openpclink-build-$arch" bash -euc "
    cp -r /repo/src /repo/packaging /repo/pyproject.toml /tmp/ && mkdir -p /tmp/w && cd /tmp
    python -m venv /tmp/venv && source /tmp/venv/bin/activate
    pip -q install -e '.[dev]' 2>&1 | grep -v 'WARNING' || true
    source packaging/common.sh
    bake_and_freeze '/repo/$source_path' --add-binary \"\$(ls /usr/lib/*/libusb-1.0.so.0):.\"
    verify_build dist/OpenPCLink/OpenPCLink
    version=\$(python -c 'import openpclink;print(openpclink.__version__)')
    root=/tmp/deb; mkdir -p \$root/DEBIAN \$root/opt \$root/usr/bin \$root/usr/share/applications \$root/usr/lib/udev/rules.d
    cp -r dist/OpenPCLink \$root/opt/openpclink
    ln -s /opt/openpclink/OpenPCLink \$root/usr/bin/openpclink
    cp packaging/openpclink.desktop \$root/usr/share/applications/
    cp packaging/70-link-ecu.rules \$root/usr/lib/udev/rules.d/
    printf 'Package: openpclink\nVersion: %s\nArchitecture: $arch\nMaintainer: local build\nDepends: libegl1, libgl1, libxkbcommon0, libdbus-1-3, libfontconfig1, libglib2.0-0\nDescription: Unofficial tuning client for Link ECUs\n' \"\$version\" > \$root/DEBIAN/control
    printf '#!/bin/sh\nudevadm control --reload-rules 2>/dev/null || true\nudevadm trigger 2>/dev/null || true\n' > \$root/DEBIAN/postinst
    chmod 755 \$root/DEBIAN/postinst
    dpkg-deb --root-owner-group -b \$root /out/openpclink_\${version}_$arch.deb >/dev/null
    tar -C dist -czf /out/OpenPCLink-\$version-linux-$arch.tar.gz OpenPCLink
    echo Built openpclink_\${version}_$arch.deb
"
