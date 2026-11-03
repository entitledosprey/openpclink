"""PCLink's work area: named pages, each holding views placed from a layout file."""
from __future__ import annotations

from typing import Callable, Protocol

from PySide6 import QtCore, QtGui, QtWidgets

from .gauges import GAUGE_TYPES, Gauge, GaugeSettings, Reading, ValueList
from .layoutfile import Layout, LayoutPage, LayoutView, new_guid

GRID = 10  # PCLink's views snap to this.
CAPTION_HEIGHT = 20
GRIP = 12
LIVE_VIEW_TYPES = ("Runtime Value List", *GAUGE_TYPES)
DEFAULT_SIZES = {
    "Digital Gauge": (120, 120), "Bar Gauge": (80, 250), "Curve Gauge": (240, 240), "Dial Gauge": (240, 250),
    "Target Gauge": (540, 90), "Runtime Value List": (300, 250), "Help Browser": (350, 370), "Auto Settings": (600, 360),
}


class WorkspaceHost(Protocol):
    def reading(self, data_id: int) -> Reading | None: ...
    def caption_of_view(self, view: LayoutView) -> str: ...
    def build_view(self, view: LayoutView) -> QtWidgets.QWidget | None: ...
    def view_context_menu(self, frame: "ViewFrame", menu: QtWidgets.QMenu) -> None: ...
    def current_table(self) -> int | None: ...
    def layout_changed(self) -> None: ...


class LiveView(QtWidgets.QStackedWidget):
    """A gauge or value list following the ids its layout entry names."""

    def __init__(self, view: LayoutView, host: WorkspaceHost) -> None:
        super().__init__()
        self.view, self.host = view, host
        if view.type == "Runtime Value List":
            self.body: Gauge | ValueList = ValueList(int(view.attributes.get("ColumnsCount", "1") or 1), view.attributes.get("BoldFont") == "true")
        else:
            self.body = GAUGE_TYPES[view.type](GaugeSettings.from_attributes(view.attributes))
        self.addWidget(self.body)

    def refresh(self) -> None:
        readings = [self.host.reading(data_id) for data_id in self.view.ids_for(self.host.current_table())]
        shown = [reading for reading in readings if reading is not None]
        if isinstance(self.body, Gauge) and self.view.type != "Digital Gauge":
            shown = shown[: max(self.body.readings_wanted, 1)]
        self.body.set_readings(shown)


class ViewFrame(QtWidgets.QFrame):
    """A view with its caption bar. Unlocked views move and resize with the mouse."""

    closed = QtCore.Signal(object)

    def __init__(self, view: LayoutView, body: QtWidgets.QWidget, caption: str, host: WorkspaceHost) -> None:
        super().__init__()
        self.view, self.body, self.host = view, body, host
        self.setFrameShape(QtWidgets.QFrame.Shape.Box)
        self.title = QtWidgets.QLabel(caption)
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName("viewCaption")
        close = QtWidgets.QToolButton()
        close.setText("×")
        close.setAutoRaise(True)
        close.clicked.connect(lambda: self.closed.emit(self))
        self.bar = QtWidgets.QWidget()
        self.bar.setFixedHeight(CAPTION_HEIGHT)
        self.bar.setObjectName("viewBar")
        row = QtWidgets.QHBoxLayout(self.bar)
        row.setContentsMargins(4, 0, 2, 0)
        row.addWidget(self.title, 1)
        row.addWidget(close)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(1, 1, 1, 1)
        column.setSpacing(0)
        column.addWidget(self.bar)
        column.addWidget(body, 1)
        self.bar.installEventFilter(self)
        self._drag: QtCore.QPoint | None = None
        self._resizing = False
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setMouseTracking(True)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = QtWidgets.QMenu(self)
        lock = menu.addAction("Lock Position")
        lock.setCheckable(True)
        lock.setChecked(self.view.locked)
        lock.toggled.connect(self.set_locked)
        self.host.view_context_menu(self, menu)
        menu.exec(event.globalPos())

    def set_locked(self, locked: bool) -> None:
        self.view.locked = locked
        self.host.layout_changed()

    def eventFilter(self, _watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        kind = event.type()
        if kind == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton and not self.view.locked:
            self._drag = event.globalPosition().toPoint() - self.pos()
            self.raise_()
        elif kind == QtCore.QEvent.Type.MouseMove and self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)
        elif kind == QtCore.QEvent.Type.MouseButtonRelease and self._drag is not None:
            self._drag = None
            self.parent().frame_moved(self)
        return False

    def _in_grip(self, point: QtCore.QPoint) -> bool:
        return point.x() >= self.width() - GRIP and point.y() >= self.height() - GRIP

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self.view.locked and self._in_grip(event.position().toPoint()):
            self._resizing = True

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._resizing:
            self.resize(max(int(event.position().x()), 60), max(int(event.position().y()), 40))

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        if self._resizing:
            self._resizing = False
            self.parent().frame_moved(self)


class PageWidget(QtWidgets.QWidget):
    """Places views in proportion, so a layout made for one screen fits on another."""

    def __init__(self, page: LayoutPage, host: WorkspaceHost) -> None:
        super().__init__()
        self.page, self.host = page, host
        self.frames: list[ViewFrame] = []
        self.basis = page.extent()
        for view in page.views:
            self._add_frame(view)

    def _add_frame(self, view: LayoutView) -> ViewFrame | None:
        body = LiveView(view, self.host) if view.type in LIVE_VIEW_TYPES else self.host.build_view(view)
        if body is None:
            return None
        frame = ViewFrame(view, body, self.host.caption_of_view(view), self.host)
        frame.setParent(self)
        frame.closed.connect(self.remove_frame)
        self.frames.append(frame)
        self._place(frame)
        frame.show()
        return frame

    def add_view(self, view: LayoutView) -> ViewFrame | None:
        if not view.guid:
            view.guid = new_guid()
        self.page.views.append(view)
        self.basis = (max(self.basis[0], view.left + view.width), max(self.basis[1], view.top + view.height))
        frame = self._add_frame(view)
        if frame:
            frame.raise_()
        self.host.layout_changed()
        return frame

    def remove_frame(self, frame: ViewFrame) -> None:
        self.frames.remove(frame)
        self.page.views.remove(frame.view)
        frame.hide()
        frame.deleteLater()
        self.host.layout_changed()

    def _scale(self) -> tuple[float, float]:
        return max(self.width(), 1) / max(self.basis[0], 1), max(self.height(), 1) / max(self.basis[1], 1)

    def _place(self, frame: ViewFrame) -> None:
        x, y = self._scale()
        view = frame.view
        frame.setGeometry(round(view.left * x), round(view.top * y), round(view.width * x), round(view.height * y))

    def frame_moved(self, frame: ViewFrame) -> None:
        x, y = self._scale()
        view = frame.view
        view.left = max(round(frame.x() / x / GRID) * GRID, 0)
        view.top = max(round(frame.y() / y / GRID) * GRID, 0)
        view.width = max(round(frame.width() / x / GRID) * GRID, GRID)
        view.height = max(round(frame.height() / y / GRID) * GRID, GRID)
        self._place(frame)
        self.host.layout_changed()

    def resizeEvent(self, _event: QtGui.QResizeEvent) -> None:
        for frame in self.frames:
            self._place(frame)

    def refresh_live(self) -> None:
        for frame in self.frames:
            if isinstance(frame.body, LiveView):
                frame.body.refresh()

    def bodies(self, kind: type | tuple[type, ...]) -> list[QtWidgets.QWidget]:
        return [frame.body for frame in self.frames if isinstance(frame.body, kind)]


class Workspace(QtWidgets.QWidget):
    """PCLink's page tabs over the current page."""

    page_changed = QtCore.Signal()

    def __init__(self, host: WorkspaceHost) -> None:
        super().__init__()
        self.host = host
        self.layout_: Layout = Layout()
        self.tabs = QtWidgets.QTabBar()
        self.tabs.setMovable(False)
        self.tabs.setExpanding(False)
        self.tabs.setDocumentMode(True)
        self.stack = QtWidgets.QStackedWidget()
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.tabs)
        column.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self._select)

    def load(self, layout: Layout) -> None:
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        while self.stack.count():
            page = self.stack.widget(0)
            self.stack.removeWidget(page)
            page.deleteLater()
        self.layout_ = layout
        for page in layout.pages:
            self.stack.addWidget(PageWidget(page, self.host))
            self.tabs.addTab(page.name)
        names = [page.name for page in layout.pages]
        at = names.index(layout.selected) if layout.selected in names else 0
        self.tabs.setCurrentIndex(at)
        self.stack.setCurrentIndex(at)
        self.tabs.blockSignals(False)
        self.page_changed.emit()

    def _select(self, index: int) -> None:
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        self.layout_.selected = self.layout_.pages[index].name
        self.page_changed.emit()

    def current(self) -> PageWidget | None:
        return self.stack.currentWidget()

    def add_page(self, name: str) -> None:
        page = LayoutPage(name)
        self.layout_.pages.append(page)
        self.stack.addWidget(PageWidget(page, self.host))
        self.tabs.addTab(name)
        self.tabs.setCurrentIndex(self.tabs.count() - 1)

    def delete_page(self) -> None:
        index = self.tabs.currentIndex()
        if index < 0 or self.tabs.count() <= 1:
            return
        widget = self.stack.widget(index)
        self.stack.removeWidget(widget)
        widget.deleteLater()
        del self.layout_.pages[index]
        self.tabs.removeTab(index)
        self.host.layout_changed()

    def rename_page(self, name: str) -> None:
        index = self.tabs.currentIndex()
        if index >= 0:
            self.layout_.pages[index].name = name
            self.layout_.pages[index].name_string = ""
            self.layout_.selected = name
            self.tabs.setTabText(index, name)
            self.host.layout_changed()

    def show_page(self, name: str) -> bool:
        for index, page in enumerate(self.layout_.pages):
            if page.name.lower() == name.lower():
                self.tabs.setCurrentIndex(index)
                return True
        return False

    def add_view(self, view_type: str, on_view: Callable[[LayoutView], None] | None = None) -> ViewFrame | None:
        """Add a new view at a free corner of the current page."""
        page = self.current()
        if page is None:
            return None
        width, height = DEFAULT_SIZES.get(view_type, (320, 240))
        view = LayoutView(view_type, left=GRID * 2, top=GRID * 2, width=width, height=height)
        if on_view:
            on_view(view)
        return page.add_view(view)

    def refresh_live(self) -> None:
        page = self.current()
        if page is not None:
            page.refresh_live()
