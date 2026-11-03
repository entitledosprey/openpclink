"""PCLink's colour themes. Hex values are as PCLink stores them, blue in the high byte."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtGui

from .gauges import tcolor

ITEMS = (
    ("ViewBackground", "View Background"), ("View Title Bar", "View Titlebar Background"), ("ViewText", "View Text"),
    ("DataAreaBackground", "Data Area Background"), ("DataAreaBorder", "Data Area Border"), ("GridLines", "Grid Lines"),
    ("Sliders", "Sliders"), ("Cursors", "Cursors"), ("LogCrosshairs", "Logging Crosshair"), ("Markers", "Markers"),
    ("OK", "OK"), ("WarningLow", "Soft Warning"), ("WarningHigh", "Hard Warning"), ("ValueChanged", "Value Changed"),
    ("OfflineValueChanged", "Offline Value Changed"), ("Active Cell", "Active Cell"), ("Active Control", "Active Control"),
    ("GradientLow", "Gradient Low"), ("GradientMiddle", "Gradient Middle"), ("GradientHigh", "Gradient High"),
    ("Background Centre", "Background Centre"), ("Background Edge", "Background Edge"),
)
_TABLE = {
    "Blue": "34230f 1e140d ffffff 34230f 808080 808080 ffffff 00ffff b0b0ff 0000ff 50ff50 0f50ff 3232ff ffff00 000080 50ff50 ff9630 faaaff aaffff aafffa 8b720e 7c4b0c",
    "White": "ffffff dcdcdc 231400 ffffff 320000 c0c0c0 505050 00dcdc b0b0ff 0000ff 008000 00a5ff 3232ff ffff00 000080 50ff50 ff9630 b0b0ff b0ffff b0ffb0 fafafa fafafa",
    "Black": "000000 3d3d3d f5f5f5 000000 c0c0c0 646464 ffffff 00ffff b0b0ff 0000ff 50ff50 0064ff 0000ff a0a000 000080 008000 ff9630 b0b0ff b0ffff b0ffb0 000000 000000",
    "Gray": "262525 463f3f ffffff 282828 808080 808080 696969 00ffff b0b0ff 0000ff 50ff50 20a0ff 3232ff ffff00 000080 008000 ff9933 b0b0ff b0ffff b0ffb0 302d2d 302d2d",
}
_OVERLAYS_A = "68a600 a57801 a600a2 0400a6 0060a6"
_OVERLAYS_B = "b0b0ff b0fdff b5ffb0 fff9b0 ffb0d6"
_OVERLAYS = {"Blue": _OVERLAYS_A, "White": _OVERLAYS_A, "Black": _OVERLAYS_B, "Gray": _OVERLAYS_B}
_PARAMETERS_BLUE = "ffffff ff9630 40ff40 ffff2a 20a0ff ff6eb4 00f0f0 dfacd1 5741d8 008000 b7b7b7 f0fbff"
_PARAMETERS = {
    "Blue": _PARAMETERS_BLUE, "Gray": _PARAMETERS_BLUE,
    "White": "231400 00ff00 ff0000 ff6eb4 e020ff eed41b ff9630 0080ff 008080 ff80ff b7b7b7 e22178",
    "Black": "f5f5f5 00ff00 ff9630 00f0f0 e020ff efefef ffff00 0080ff 008080 ff80ff b7b7b7 e22178",
}
THEME_NAMES = ("Blue", "White", "Black", "Gray")


def _numbers(text: str) -> list[int]:
    return [int(item, 16) for item in text.split()]


@dataclass
class Theme:
    name: str
    items: dict[str, int]
    overlays: list[int]
    parameters: list[int]

    @classmethod
    def built_in(cls, name: str) -> "Theme":
        return cls(name, dict(zip((key for key, _ in ITEMS), _numbers(_TABLE[name]))), _numbers(_OVERLAYS[name]), _numbers(_PARAMETERS[name]))

    @classmethod
    def from_saved(cls, saved: dict) -> "Theme":
        base = cls.built_in(saved.get("base", "Black"))
        base.name = saved.get("name", base.name)
        base.items.update(saved.get("items", {}))
        base.overlays = saved.get("overlays", base.overlays)
        base.parameters = saved.get("parameters", base.parameters)
        return base

    def saved(self) -> dict:
        return {"name": self.name, "items": self.items, "overlays": self.overlays, "parameters": self.parameters}

    def colour(self, item: str) -> QtGui.QColor:
        return tcolor(self.items[item])

    def parameter_colour(self, index: int) -> QtGui.QColor:
        return tcolor(self.parameters[index % len(self.parameters)])

    def overlay_colour(self, index: int) -> QtGui.QColor:
        return tcolor(self.overlays[index % len(self.overlays)])

    def palette(self) -> QtGui.QPalette:
        palette = QtGui.QPalette()
        role = QtGui.QPalette.ColorRole
        for name, item in (
            (role.Window, "ViewBackground"), (role.Base, "DataAreaBackground"), (role.AlternateBase, "View Title Bar"),
            (role.WindowText, "ViewText"), (role.Text, "ViewText"), (role.ButtonText, "ViewText"),
            (role.Button, "View Title Bar"), (role.Highlight, "Active Control"),
        ):
            palette.setColor(name, self.colour(item))
        return palette

    def style_sheet(self) -> str:
        title = self.colour("View Title Bar").name()
        text = self.colour("ViewText").name()
        back = self.colour("ViewBackground").name()
        active = self.colour("Active Control").name()
        data = self.colour("DataAreaBackground").name()
        border = self.colour("DataAreaBorder").name()
        return (
            f"Workspace, PageWidget, ViewFrame {{ background:{back}; color:{text}; }}"
            f"PageWidget QWidget {{ background:{data}; color:{text}; }}"
            f"PageWidget QLineEdit, PageWidget QAbstractSpinBox, PageWidget QComboBox {{ border:1px solid {border}; }}"
            f"PageWidget QHeaderView::section {{ background:{title}; color:{text}; }}"
            f"PageWidget QWidget#viewBar {{ background:{title}; }} PageWidget QLabel#viewCaption {{ background:transparent; color:{text}; font-size:13pt; }}"
            f"QTabBar::tab {{ background:{back}; color:{text}; padding:3px 14px; }}"
            f"QTabBar::tab:selected {{ background:{active}; color:#ffffff; border-radius:3px; }}"
            f"PageWidget ViewFrame QToolButton {{ background:transparent; color:{text}; }}"
        )


def theme_named(options) -> Theme:
    saved = options.themes.get(options.theme) if options.themes else None
    if saved:
        return Theme.from_saved(saved)
    return Theme.built_in(options.theme if options.theme in THEME_NAMES else "Black")
