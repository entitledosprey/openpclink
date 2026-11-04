# Shared by the platform builds. Bakes the user's own PCLink data into this build.
# The baked folder holds the parameter database, strings, the CAN and firmware keys, the manual and its
# pictures, the rules table, and PCLink's own Layouts, CAN setups, Firmware, runtime and tuning view
# layouts, DefaultParameterConfig.rtcr and LoggingOptions.xml. The build stops if any is missing.
check_baked_data() {
    python - <<'PY'
import sys
from pathlib import Path
data = Path("src/openpclink/data")
missing = [name for name in ("pclink-data.json.gz", "pclink-help.json.gz", "pclink-rules.json.gz") if not (data / name).exists()]
assets = data / "pclink-assets"
for name in ("RuntimeViewLayout", "TSViewlayout", "TSViewlayoutM", "DefaultParameterConfig.rtcr", "LoggingOptions.xml"):
    if not (assets / name).is_file():
        missing.append(f"pclink-assets/{name}")
for folder, pattern in (("Layouts", "*.llf5"), ("CAN", "*"), ("Firmware", "*.lfp")):
    if not any((assets / folder).glob(pattern)):
        missing.append(f"pclink-assets/{folder}")
if not any((data / "pclink-help-images").glob("*")):
    missing.append("pclink-help-images (needs 7z: brew install p7zip, or apt install p7zip-full)")
from openpclink.database import Database
db = Database.load(data / "pclink-data.json.gz")
for name in ("stream_key", "file_key", "firmware_key"):
    if not getattr(db, name):
        missing.append(name)
if missing:
    sys.exit("baked data is incomplete: " + ", ".join(missing))
print("baked data complete")
PY
}

# $1 = the built program. It must run, find its data, and not contain the emulator.
verify_build() {
    "$1" check --bundled
    if "$1" check --bundled | grep -q "^Emulator:  present"; then
        echo "the build contains emulator code" >&2; return 1
    fi
}

bake_and_freeze() {  # $1=installer or PCLink folder, $2=extra pyinstaller args...
    source_path="$1"; shift
    mkdir -p src/openpclink/data
    python - "$source_path" <<'PY'
import sys
from pathlib import Path
from openpclink.importer import import_pclink
print("baked", import_pclink(Path(sys.argv[1]), Path("src/openpclink/data/pclink-data.json.gz")))
from openpclink.database import Database
from openpclink.native_rules import compile_table
db = Database.load(Path("src/openpclink/data/pclink-data.json.gz"))
source = Path("packaging/pclink-rules.source.json.gz")
if not source.exists():
    sys.exit("missing packaging/pclink-rules.source.json.gz; generate it with reverse-engineering/extract_rules.py")
compile_table(source, Path("src/openpclink/data/pclink-rules.json.gz"), lambda i: db.known(i) and db.parameter(i).path[:1] == ("Table Allocation",))
PY
    rm -f src/openpclink/data/pclink-rules.dll.gz
    check_baked_data
    pyinstaller --noconfirm --clean --name OpenPCLink \
        --paths src \
        --add-data "src/openpclink/data:openpclink/data" \
        --hidden-import openpclink.cli --hidden-import openpclink.gui --hidden-import openpclink.link_helper \
        --hidden-import serial.tools.list_ports \
        --collect-submodules pyftdi --collect-submodules usb \
        --exclude-module unicorn --exclude-module pefile --exclude-module openpclink.rules --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets \
        --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick --exclude-module PySide6.Qt3DCore \
        --hidden-import PySide6.QtMultimedia --exclude-module PySide6.QtPdf --exclude-module tkinter \
        "$@" packaging/entry.py
}
