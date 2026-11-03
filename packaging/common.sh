# Shared by the platform builds. Bakes the user's own PCLink data into this build.
bake_and_freeze() {  # $1=installer or PCLink folder, $2=extra pyinstaller args...
    source_path="$1"; shift
    mkdir -p src/openpclink/data
    python - "$source_path" <<'PY'
import sys
from pathlib import Path
from openpclink.importer import import_pclink
print("baked", import_pclink(Path(sys.argv[1]), Path("src/openpclink/data/pclink-data.json.gz")))
PY
    pyinstaller --noconfirm --clean --name OpenPCLink \
        --paths src \
        --add-data "src/openpclink/data:openpclink/data" \
        --hidden-import openpclink.cli --hidden-import openpclink.gui --hidden-import openpclink.link_helper \
        --hidden-import serial.tools.list_ports \
        --collect-submodules pyftdi --collect-submodules usb \
        --exclude-module unicorn --exclude-module pefile --exclude-module openpclink.rules --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets \
        --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.Qt3DCore \
        --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtPdf --exclude-module tkinter \
        "$@" packaging/entry.py
}
