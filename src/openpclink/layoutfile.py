"""PCLink's layout files (.llf5, .llfx): pages of views placed by position."""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


class LayoutError(Exception):
    pass


GROUP_ALL = 0  # The ids a view shows unless a table with its own group has focus.


@dataclass
class LayoutView:
    type: str
    left: int = 0
    top: int = 0
    width: int = 300
    height: int = 200
    locked: bool = False
    font_size: int = 0
    guid: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    groups: dict[int, list[int]] = field(default_factory=dict)
    log_parameters: list[tuple[int, int]] = field(default_factory=list)
    extra: list[ET.Element] = field(default_factory=list)  # Children kept unchanged so a save does not lose them.

    _FIXED = ("Type", "Left", "Top", "Width", "Height", "Locked", "FontSize", "Guid")

    @classmethod
    def from_element(cls, node: ET.Element) -> "LayoutView":
        def number(name: str, default: int = 0) -> int:
            try:
                return int(float(node.get(name, default)))
            except ValueError:
                return default

        view = cls(
            type=node.get("Type", ""), left=number("Left"), top=number("Top"), width=number("Width", 300),
            height=number("Height", 200), locked=node.get("Locked") == "true", font_size=number("FontSize"),
            guid=node.get("Guid", ""),
            attributes={k: v for k, v in node.attrib.items() if k not in cls._FIXED},
        )
        for child in node:
            if child.tag == "DataIDGroups":
                for group in child:
                    ids = [int(i) for i in group.get("DataIDs", "").split(",") if i.strip().isdigit()]
                    view.groups[int(group.get("GroupID", "0"))] = ids
            elif child.tag == "LogParameterViewInfos":
                view.log_parameters = [(int(i.get("DataID", "0")), int(i.get("DisplayGroup", "0"))) for i in child]
            else:
                view.extra.append(child)
        return view

    def to_element(self) -> ET.Element:
        node = ET.Element("View", {
            "Type": self.type, "Left": str(self.left), "Top": str(self.top), "Width": str(self.width),
            "Height": str(self.height), "Locked": "true" if self.locked else "false",
            "FontSize": str(self.font_size), "Guid": self.guid or new_guid(), **self.attributes,
        })
        if self.groups:
            groups = ET.SubElement(node, "DataIDGroups")
            for group_id, ids in self.groups.items():
                ET.SubElement(groups, "RuntimeViewDataIDGroup", {"GroupID": str(group_id), "DataIDs": ",".join(map(str, ids))})
        if self.log_parameters:
            infos = ET.SubElement(node, "LogParameterViewInfos")
            for data_id, group in self.log_parameters:
                ET.SubElement(infos, "LogParameterViewInfo", {"DataID": str(data_id), "DisplayGroup": str(group)})
        node.extend(self.extra)
        return node

    def ids_for(self, table_id: int | None = None) -> list[int]:
        if table_id in self.groups:
            return self.groups[table_id]
        return self.groups.get(GROUP_ALL, [])

    def part(self, tag: str) -> ET.Element | None:
        return next((node for node in self.extra if node.tag == tag), None)


@dataclass
class LayoutPage:
    name: str
    views: list[LayoutView] = field(default_factory=list)
    name_string: str = ""
    current_table: int = 0
    current_settings_view: str = ""

    def extent(self) -> tuple[int, int]:
        """The size the views were placed in."""
        if not self.views:
            return 1, 1
        return max(v.left + v.width for v in self.views), max(v.top + v.height for v in self.views)


@dataclass
class Layout:
    pages: list[LayoutPage] = field(default_factory=list)
    selected: str = ""

    @classmethod
    def parse(cls, text: str | bytes) -> "Layout":
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise LayoutError(f"not a layout file: {exc}") from exc
        node = root.find("ViewLayout/Pages")
        if node is None:
            raise LayoutError("not a layout file: it has no pages")
        layout = cls(selected=node.get("SelectedPage", ""))
        for page_node in node.findall("Page"):
            views = page_node.find("Views")
            layout.pages.append(LayoutPage(
                name=page_node.get("Name", "Page"),
                name_string=page_node.get("NameStrId", ""),
                current_table=int(page_node.get("CurrentTableDataID", "0") or 0),
                current_settings_view=page_node.get("CurrentSettingsView", ""),
                views=[LayoutView.from_element(view) for view in (views if views is not None else [])],
            ))
        return layout

    @classmethod
    def load(cls, path: Path) -> "Layout":
        try:
            return cls.parse(Path(path).read_bytes())
        except OSError as exc:
            raise LayoutError(str(exc)) from exc

    def to_xml(self) -> bytes:
        root = ET.Element("LinkStorage", {"Version": "100", "Type": ".llf"})
        pages = ET.SubElement(ET.SubElement(root, "ViewLayout"), "Pages", {"SelectedPage": self.selected})
        for page in self.pages:
            attributes = {"Name": page.name, "CurrentTableDataID": str(page.current_table), "CurrentSettingsView": page.current_settings_view}
            if page.name_string:
                attributes["NameStrId"] = page.name_string
            page_node = ET.SubElement(pages, "Page", attributes)
            views = ET.SubElement(page_node, "Views")
            views.extend(view.to_element() for view in page.views)
        ET.indent(root, "\t")
        return b'<?xml version="1.0"?>\r\n' + ET.tostring(root, encoding="utf-8").replace(b"\n", b"\r\n") + b"\r\n"

    def save(self, path: Path) -> None:
        Path(path).write_bytes(self.to_xml())

    def page(self, name: str) -> LayoutPage | None:
        return next((page for page in self.pages if page.name == name), None)


def new_guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"
