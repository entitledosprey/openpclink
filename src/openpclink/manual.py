"""The manual window: a topic tree beside the page, as PCLink's help viewer lays it out."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from .database import HelpManual

ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
_TOPIC = re.compile(r'(?is)<OBJECT type="text/sitemap">(.*?)</OBJECT>|<UL>|</UL>')
_PARAM = re.compile(r'(?is)<param name="(\w+)" value="([^"]*)"')


def parse_contents(text: str) -> list[dict]:
    """The nested topics of a CHM table of contents (.hhc) as [{"name", "page", "children"}]."""
    root: list[dict] = []
    stack = [root]
    last = None
    for match in _TOPIC.finditer(text):
        token = match.group(0).upper()
        if token == "<UL>":
            if last is not None:
                stack.append(last["children"])
        elif token == "</UL>":
            if len(stack) > 1:
                stack.pop()
        else:
            params = {key.lower(): value for key, value in _PARAM.findall(match.group(1))}
            last = {"name": params.get("name", ""), "page": Path(params.get("local", "")).stem.lower(), "children": []}
            stack[-1].append(last)
    return root


def find_contents(manual: HelpManual) -> list[dict]:
    stored = manual.pages.get("__contents__")
    if stored:
        return stored
    for folder in (Path(__file__).resolve().parents[2] / "work" / "help",):
        path = folder / "Help_File_ENG.hhc"
        if path.exists():
            return parse_contents(path.read_text(encoding="utf-8", errors="replace"))
    return [{"name": name, "page": name, "children": []} for name in sorted(manual.pages) if not name.startswith("__")]


class ManualWindow(QtWidgets.QWidget):
    """Select Topic on the left, the page on the right."""

    def __init__(self, manual: HelpManual) -> None:
        super().__init__()
        self.manual = manual
        self.setWindowTitle("Link ECU Online Manual")
        self.resize(980, 680)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabel("Select Topic")
        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setSearchPaths(manual.image_folders())
        self.browser.anchorClicked.connect(self._link)
        self.items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._fill(self.tree, find_contents(manual))
        self.tree.currentItemChanged.connect(lambda item, _old: item and self._open(item.data(0, ID_ROLE), select=False))
        split = QtWidgets.QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.browser)
        split.setSizes([260, 720])
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(split)

    def _fill(self, parent, topics: list[dict]) -> None:
        for topic in topics:
            item = QtWidgets.QTreeWidgetItem(parent, [topic["name"]])
            item.setData(0, ID_ROLE, topic["page"])
            self.items.setdefault(topic["page"], item)
            self._fill(item, topic["children"])

    def _link(self, url: QtCore.QUrl) -> None:
        name = Path(url.path()).stem.lower() or url.toString()
        if self.manual.page_for(name):
            self._open(name)

    def _open(self, name: str, select: bool = True) -> None:
        html = self.manual.page_for(name) if name else None
        self.browser.setHtml(html or f"<p>The manual has no page called {name}.</p>")
        item = self.items.get(name)
        if select and item is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
            self.tree.blockSignals(False)

    def show_topic(self, name: str) -> None:
        self._open(name.partition("#")[0].lower())
        self.show()
        self.raise_()
