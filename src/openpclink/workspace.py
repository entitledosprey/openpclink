"""PCLink's work area: named pages, each holding views placed from a layout file.

PCLink places views at absolute pixel positions and scrolls when the window is smaller. Here a page is scaled to
fit the window instead, so a layout made for another screen size always shows whole. Positions are still stored
as PCLink's pixels.
"""
from __future__ import annotations

import collections
import copy
import time
from typing import Callable, Protocol

from PySide6 import QtCore, QtGui, QtWidgets

from . import gauges
from .gauges import GAUGE_TYPES, Gauge, GaugeSettings, Reading, ValueList
from .layoutfile import GROUP_ALL, Layout, LayoutPage, LayoutView, new_guid

GAP = 2  # PCLink's shipped layouts leave 2 pixels between views.
SNAP = 8  # How close an edge must come to a neighbour's to jump to it, in layout pixels.
CAPTION_HEIGHT = 22
GRIP = 14
LIVE_VIEW_TYPES = ("Runtime Value List", *GAUGE_TYPES)
LIVE_CONVERT = ("Runtime Value List", "Digital Gauge", "Bar Gauge", "Curve Gauge", "Dial Gauge", "Target Gauge")
LOG_CONVERT = (("Time Plot", "Time Plot"), ("Parameter List", "Parameter List"), ("Logged Values List", "Value List"))
LOG_CONVERT_TYPES = tuple(kind for _title, kind in LOG_CONVERT)
CANONICAL = {"Curve Gauge": "Curved Bar"}
# Sizes PCLink gives a view added from the menu (FUN_005dd378); the other types are not read there.
DEFAULT_SIZES = {
    "Digital Gauge": (120, 180), "Bar Gauge": (80, 250), "Curve Gauge": (100, 200), "Curved Bar": (100, 200),
    "Dial Gauge": (150, 150), "Target Gauge": (470, 90), "Runtime Value List": (218, 248), "Help Browser": (350, 370),
    "Auto Settings": (600, 360),
}
WHATS_THIS = {
    "Runtime Value List": "Shows live ECU values in a list. Values turn red or orange when they pass their warning limits.",
    "Digital Gauge": "Shows live ECU values as large numbers. The background changes when a value passes a warning limit.",
    "Bar Gauge": "Shows a live ECU value as a bar with a scale. Ticks take the colour of the warning band they are in.",
    "Dial Gauge": "Shows a live ECU value on a dial with a needle.",
    "Curve Gauge": "Shows a live ECU value as a curved bar made of blocks.",
    "Curved Bar": "Shows a live ECU value as a curved bar made of blocks.",
    "Target Gauge": "Shows a target and an actual value on one scale; the bar between the pointers is the error.",
}


class WorkspaceHost(Protocol):
    def reading(self, data_id: int) -> Reading | None: ...
    def caption_of_view(self, view: LayoutView) -> str: ...
    def build_view(self, view: LayoutView) -> QtWidgets.QWidget | None: ...
    def view_context_menu(self, frame: "ViewFrame", menu: QtWidgets.QMenu) -> None: ...
    def current_table(self) -> int | None: ...
    def layout_changed(self, workspace: "Workspace | None" = None) -> None: ...


def rect_of(view: LayoutView) -> QtCore.QRect:
    return QtCore.QRect(view.left, view.top, view.width, view.height)


# ---- drawn icons ----------------------------------------------------------------------------------------------

def _arrow_head(painter: QtGui.QPainter, tip: QtCore.QPointF, direction: QtCore.QPointF, size: float) -> None:
    side = QtCore.QPointF(-direction.y(), direction.x())
    painter.drawPolygon(QtGui.QPolygonF([tip, tip - direction * size + side * size * 0.6, tip - direction * size - side * size * 0.6]))


def draw_expand_icon(painter: QtGui.QPainter, box: QtCore.QRectF, colour: QtGui.QColor) -> None:
    """PCLink's close button: an X whose four arms end in arrow heads."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(colour, 2))
    painter.setBrush(colour)
    centre, reach = box.center(), min(box.width(), box.height()) / 2 - 1
    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        direction = QtCore.QPointF(dx, dy) * 0.7071
        painter.drawLine(centre + direction * reach * 0.25, centre + direction * (reach - 2))
        _arrow_head(painter, centre + direction * reach, direction, reach * 0.55)
    painter.restore()


def draw_move_icon(painter: QtGui.QPainter, box: QtCore.QRectF, colour: QtGui.QColor) -> None:
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtGui.QPen(colour, 1.5))
    painter.setBrush(colour)
    centre, reach = box.center(), min(box.width(), box.height()) / 2 - 1
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        direction = QtCore.QPointF(dx, dy)
        painter.drawLine(centre, centre + direction * (reach - 2))
        _arrow_head(painter, centre + direction * reach, direction, reach * 0.45)
    painter.restore()


def draw_lock_icon(painter: QtGui.QPainter, box: QtCore.QRectF, colour: QtGui.QColor, closed: bool) -> None:
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    body = QtCore.QRectF(box.left() + 2, box.center().y() - 1, box.width() - 4, box.height() / 2)
    painter.setPen(QtGui.QPen(colour, 1.5))
    painter.setBrush(colour)
    painter.drawRect(body)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    shackle = QtCore.QRectF(body.left() + 2, body.top() - body.height() * 0.9, body.width() - 4, body.height() * 1.1)
    painter.drawArc(shackle, 0 if closed else 40 * 16, 180 * 16)
    painter.restore()


def draw_triangle(painter: QtGui.QPainter, box: QtCore.QRectF, colour: QtGui.QColor, forward: bool) -> None:
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(colour)
    a, b = (box.right() - 3, box.left() + 3) if forward else (box.left() + 3, box.right() - 3)
    painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(a, box.center().y()), QtCore.QPointF(b, box.top() + 2), QtCore.QPointF(b, box.bottom() - 2)]))
    painter.restore()


# ---- views ----------------------------------------------------------------------------------------------------

class LiveView(QtWidgets.QStackedWidget):
    """A gauge or value list following the ids its layout entry names."""

    def __init__(self, view: LayoutView, host: WorkspaceHost) -> None:
        super().__init__()
        self.view, self.host = view, host
        self.body: Gauge | ValueList = self._make_body()
        self.addWidget(self.body)

    def _make_body(self) -> Gauge | ValueList:
        view = self.view
        if view.type == "Runtime Value List":
            attributes = view.attributes
            return ValueList(
                int(float(attributes.get("ColumnsCount", "1") or 1)), attributes.get("BoldFont") == "true",
                int(float(attributes.get("SortMode", "0") or 0)), int(float(view.font_size or 0)),
            )
        return GAUGE_TYPES[view.type](GaugeSettings.from_attributes(view.attributes))

    def rebuild(self) -> None:
        """Draw again from the layout entry, after its properties changed."""
        old = self.body
        self.body = self._make_body()
        self.addWidget(self.body)
        self.setCurrentWidget(self.body)
        self.removeWidget(old)
        old.deleteLater()

    def refresh(self) -> None:
        readings = [self.host.reading(data_id) for data_id in self.view.ids_for(self.host.current_table())]
        shown = [reading for reading in readings if reading is not None]
        if isinstance(self.body, Gauge) and self.view.type != "Digital Gauge":
            shown = shown[: max(self.body.readings_wanted, 1)]
        self.body.set_readings(shown)


class CaptionBar(QtWidgets.QWidget):
    """The strip above a view: its caption, and PCLink's icons. Locked views show no move icon and a dimmed close."""

    ICON = 18

    def __init__(self, frame: "ViewFrame", caption: str) -> None:
        super().__init__()
        self.frame, self.caption = frame, caption
        self.setFixedHeight(CAPTION_HEIGHT)
        self.setObjectName("viewBar")
        self.setMouseTracking(True)

    def table_bar(self) -> bool:
        return self.frame.view.type == "Auto Settings"

    def icons(self) -> dict[str, QtCore.QRectF]:
        top = (CAPTION_HEIGHT - self.ICON) / 2
        right = self.width() - self.ICON - 3
        spots = {"close": QtCore.QRectF(right, top, self.ICON, self.ICON)}
        if not self.frame.view.locked:
            right -= self.ICON + 2
            spots["move"] = QtCore.QRectF(right, top, self.ICON, self.ICON)
        if self.table_bar():
            left = 3.0
            for name in ("lock", "previous", "next"):
                spots[name] = QtCore.QRectF(left, top, self.ICON, self.ICON)
                left += self.ICON + 1
        return spots

    def set_caption(self, text: str) -> None:
        self.caption = text
        self.update()

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), gauges.theme_colour("lane"))
        text = gauges.theme_colour("text")
        font = QtGui.QFont(painter.font())
        font.setPixelSize(17)
        painter.setFont(font)
        painter.setPen(text)
        left = 62 if self.table_bar() else 4
        box = self.rect().adjusted(left, 0, -(self.ICON * 2 + 8), 0)
        painter.setFont(gauges.fit_font(painter, self.caption, 17, box.width() - 4))
        painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, self.caption)
        locked = self.frame.view.locked
        dim = QtGui.QColor(text.red(), text.green(), text.blue(), 110)
        for name, box in self.icons().items():
            if name == "close":
                draw_expand_icon(painter, box, dim if locked else text)
            elif name == "move":
                draw_move_icon(painter, box, text)
            elif name == "lock":
                draw_lock_icon(painter, box, text, self.frame.view.attributes.get("MenuLocked") == "true")
            else:
                draw_triangle(painter, box, text, name == "next")

    def _hit(self, point: QtCore.QPointF) -> str:
        return next((name for name, box in self.icons().items() if box.contains(point)), "")

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        frame = self.frame
        hit = self._hit(event.position())
        if hit == "close":
            if not frame.view.locked:
                frame.closed.emit(frame)
        elif hit == "lock":
            frame.toggle_menu_lock()
        elif hit in ("previous", "next"):
            frame.step_table(-1 if hit == "previous" else 1)
        else:
            frame.begin_move(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self.frame.move_to(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        self.frame.end_drag()


class SizeGrip(QtWidgets.QWidget):
    """The corner an unlocked view is resized from."""

    def __init__(self, frame: "ViewFrame") -> None:
        super().__init__(frame)
        self.frame = frame
        self.setFixedSize(GRIP, GRIP)
        self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setPen(QtGui.QPen(gauges.theme_colour("text"), 1))
        for step in (4, 8, 12):
            painter.drawLine(GRIP - 1, GRIP - 1 - step, GRIP - 1 - step, GRIP - 1)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.frame.begin_resize(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        self.frame.resize_to(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        self.frame.end_drag()


class ViewFrame(QtWidgets.QFrame):
    """A view with its caption bar. Unlocked views move and resize with the mouse; locked views do neither."""

    closed = QtCore.Signal(object)

    def __init__(self, view: LayoutView, body: QtWidgets.QWidget, caption: str, host: WorkspaceHost) -> None:
        super().__init__()
        self.view, self.body, self.host = view, body, host
        self.active = False
        self.bar = CaptionBar(self, caption)
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.bar)
        column.addWidget(body, 1)
        self.grip = SizeGrip(self)
        self.grip.setVisible(not view.locked)
        self._press: QtCore.QPoint | None = None
        self._origin = QtCore.QRect()
        self._mode = ""
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)

    @property
    def title(self) -> CaptionBar:
        return self.bar

    def set_caption(self, text: str) -> None:
        self.bar.set_caption(text)

    def set_active(self, active: bool) -> None:
        if active != self.active:
            self.active = active
            self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        if self.active:
            painter = QtGui.QPainter(self)
            painter.setPen(QtGui.QPen(gauges.theme_colour("active"), 3))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.grip.move(self.width() - GRIP, self.height() - GRIP)
        self.grip.raise_()

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        self.build_menu().exec(event.globalPos())

    def build_menu(self) -> QtWidgets.QMenu:
        """PCLink's view menu: What's this?, Copy View, Convert to, Lock, Delete, Properties."""
        menu = QtWidgets.QMenu(self)
        menu.addAction("What's this?", self.show_whats_this)
        menu.addSeparator()
        menu.addAction("Copy View", lambda: self.parent().copy_view(self))
        convert = menu.addMenu("Convert to")
        kinds = [(kind, kind) for kind in LIVE_CONVERT] + list(LOG_CONVERT)
        for title, kind in kinds:
            if kind != self.view.type and CANONICAL.get(title, title) != self.view.type:
                convert.addAction(title, lambda kind=kind: self.parent().convert_view(self, kind))
        convert.setEnabled(self.view.type in LIVE_VIEW_TYPES or self.view.type in LOG_CONVERT_TYPES)
        menu.addAction("Unlock" if self.view.locked else "Lock", lambda: self.set_locked(not self.view.locked))
        delete = menu.addAction("Delete", lambda: self.closed.emit(self))
        delete.setEnabled(not self.view.locked)
        menu.addSeparator()
        properties = menu.addAction("Properties", lambda: self.host.view_properties(self) if hasattr(self.host, "view_properties") else None)
        properties.setEnabled(hasattr(self.host, "view_properties") and (self.view.type in LIVE_VIEW_TYPES or hasattr(self.body, "properties_dialog")))
        self.host.view_context_menu(self, menu)
        return menu

    def show_whats_this(self) -> None:
        text = WHATS_THIS.get(self.view.type) or f"{self.view.type}: a view you can move, resize and delete once it is unlocked."
        QtWidgets.QMessageBox.information(self, "What's this?", text)

    def set_locked(self, locked: bool) -> None:
        self.view.locked = locked
        self.grip.setVisible(not locked)
        self.bar.update()
        self.host.layout_changed(self.owner())

    def toggle_menu_lock(self) -> None:
        self.view.attributes["MenuLocked"] = "false" if self.view.attributes.get("MenuLocked") == "true" else "true"
        self.bar.update()
        self.host.layout_changed(self.owner())

    def step_table(self, direction: int) -> None:
        step = getattr(self.host, "step_table", None)
        if step is not None:
            step(self, direction)

    def owner(self) -> "Workspace | None":
        page = self.parent()
        return getattr(page, "workspace", None)

    # moving and resizing: the page converts between its scaled pixels and layout pixels and snaps
    def begin_move(self, point: QtCore.QPoint) -> None:
        if self.view.locked:
            return
        self._mode, self._press, self._origin = "move", point, self.geometry()
        self.raise_()

    def begin_resize(self, point: QtCore.QPoint) -> None:
        self._mode, self._press, self._origin = "resize", point, self.geometry()

    def move_to(self, point: QtCore.QPoint) -> None:
        if self._mode != "move" or self._press is None:
            return
        self.parent().drag(self, self._origin.translated(point - self._press), moving=True)

    def resize_to(self, point: QtCore.QPoint) -> None:
        if self._mode != "resize" or self._press is None:
            return
        delta = point - self._press
        self.parent().drag(self, QtCore.QRect(self._origin.topLeft(), QtCore.QSize(self._origin.width() + delta.x(), self._origin.height() + delta.y())), moving=False)

    def end_drag(self) -> None:
        if self._mode:
            self._mode, self._press = "", None
            self.parent().frame_moved(self)


class PageWidget(QtWidgets.QWidget):
    """Places views in proportion, so a layout made for one screen fits on another."""

    def __init__(self, page: LayoutPage, host: WorkspaceHost, workspace: "Workspace | None" = None) -> None:
        super().__init__()
        self.page, self.host, self.workspace = page, host, workspace
        self.frames: list[ViewFrame] = []
        self.basis = self.measure()
        self.unsupported: list[str] = []
        for view in page.views:
            self._add_frame(view)

    def measure(self) -> tuple[int, int]:
        width, height = self.page.extent()
        return width + GAP, height + GAP

    def _add_frame(self, view: LayoutView) -> ViewFrame | None:
        body = LiveView(view, self.host) if view.type in LIVE_VIEW_TYPES else self.host.build_view(view)
        if body is None:
            self.unsupported.append(view.type)
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
        self.basis = (max(self.basis[0], view.left + view.width + GAP), max(self.basis[1], view.top + view.height + GAP))
        frame = self._add_frame(view)
        if frame:
            self._relayout()
            frame.raise_()
            self.host.layout_changed(self.workspace)
        else:
            self.page.views.remove(view)
        return frame

    def remove_frame(self, frame: ViewFrame) -> None:
        if frame.view.locked:
            return
        self.frames.remove(frame)
        self.page.views.remove(frame.view)
        frame.hide()
        frame.deleteLater()
        self.host.layout_changed(self.workspace)

    def copy_view(self, frame: ViewFrame) -> ViewFrame | None:
        view = copy.deepcopy(frame.view)
        view.guid = new_guid()
        view.locked = False
        view.left, view.top = self.free_spot(view.width, view.height, (view.left + 20, view.top + 20))
        return self.add_view(view)

    def convert_view(self, frame: ViewFrame, kind: str) -> ViewFrame | None:
        """Change a view's type, keeping its place and the parameters it shows."""
        old = frame.view
        view = copy.deepcopy(old)
        view.type = CANONICAL.get(kind, kind)
        ids = old.groups.get(GROUP_ALL, []) or [data_id for data_id, _ in old.log_parameters]
        view.attributes = {name: value for name, value in old.attributes.items() if name in ("Caption", "CaptionStrId")}
        view.groups, view.log_parameters, view.extra = {}, [], []
        if view.type in LIVE_VIEW_TYPES:
            view.groups[GROUP_ALL] = ids
            if view.type == "Runtime Value List":
                view.attributes.update(ColumnsCount="1", SortMode="0", BoldFont="true")
            else:
                view.attributes.update(GaugeSettings().attributes())
        else:
            view.log_parameters = [(data_id, 0) for data_id in ids]
        index = self.frames.index(frame)
        was_locked = old.locked
        old.locked = False
        self.remove_frame(frame)
        view.locked = was_locked
        new = self.add_view(view)
        if new is not None:
            self.frames.remove(new)
            self.frames.insert(index, new)
            self.page.views.remove(view)
            self.page.views.insert(min(index, len(self.page.views)), view)
        return new

    def free_spot(self, width: int, height: int, prefer: tuple[int, int] | None = None) -> tuple[int, int]:
        """A place on the page where a view of this size overlaps none: PCLink's views never stack."""
        taken = [rect_of(view) for view in self.page.views]
        limit_w, limit_h = max(self.basis[0], width + GAP * 2), max(self.basis[1], height + GAP * 2)
        spots = ([prefer] if prefer else []) + [(x, y) for y in range(GAP, limit_h, 10) for x in range(GAP, limit_w, 10)]
        for x, y in spots:
            box = QtCore.QRect(x, y, width, height)
            if x + width <= limit_w and y + height <= limit_h and not any(box.adjusted(-GAP, -GAP, GAP, GAP).intersects(other) for other in taken):
                return x, y
        return GAP, max((box.bottom() + 1 for box in taken), default=0) + GAP

    def _scale(self) -> tuple[float, float]:
        return max(self.width(), 1) / max(self.basis[0], 1), max(self.height(), 1) / max(self.basis[1], 1)

    def _place(self, frame: ViewFrame) -> None:
        x, y = self._scale()
        view = frame.view
        frame.setGeometry(round(view.left * x), round(view.top * y), max(round(view.width * x), 1), max(round(view.height * y), 1))

    def _relayout(self) -> None:
        for frame in self.frames:
            self._place(frame)

    def snapped(self, frame: ViewFrame, box: QtCore.QRect, moving: bool) -> QtCore.QRect:
        """Move a layout-pixel rectangle's edges onto neighbouring views' edges, leaving PCLink's 2 pixel gap."""
        others = [rect_of(other.view) for other in self.frames if other is not frame]
        left, top, right, bottom = box.left(), box.top(), box.left() + box.width(), box.top() + box.height()

        def pull(edge: int, targets: list[int]) -> int | None:
            near = min(targets, key=lambda target: abs(target - edge), default=None)
            return near if near is not None and abs(near - edge) <= SNAP else None

        xs_left = [GAP] + [o.right() + 1 + GAP for o in others] + [o.left() for o in others]
        xs_right = [o.left() - GAP for o in others] + [o.left() + o.width() for o in others] + [self.basis[0] - GAP]
        ys_top = [GAP] + [o.bottom() + 1 + GAP for o in others] + [o.top() for o in others]
        ys_bottom = [o.top() - GAP for o in others] + [o.top() + o.height() for o in others] + [self.basis[1] - GAP]
        if moving:
            dx = pull(left, xs_left)
            dx = (dx - left) if dx is not None else (pull(right, xs_right) - right if pull(right, xs_right) is not None else 0)
            dy = pull(top, ys_top)
            dy = (dy - top) if dy is not None else (pull(bottom, ys_bottom) - bottom if pull(bottom, ys_bottom) is not None else 0)
            return box.translated(dx, dy)
        new_right, new_bottom = pull(right, xs_right), pull(bottom, ys_bottom)
        return QtCore.QRect(box.left(), box.top(), (new_right if new_right is not None else right) - left, (new_bottom if new_bottom is not None else bottom) - top)

    def drag(self, frame: ViewFrame, pixels: QtCore.QRect, moving: bool) -> None:
        x, y = self._scale()
        box = QtCore.QRect(round(pixels.left() / x), round(pixels.top() / y), max(round(pixels.width() / x), 10), max(round(pixels.height() / y), 10))
        box = self.snapped(frame, box, moving)
        if moving:
            box.moveTo(max(box.left(), 0), max(box.top(), 0))
        view = frame.view
        view.left, view.top, view.width, view.height = box.left(), box.top(), max(box.width(), 10), max(box.height(), 10)
        self._place(frame)

    def frame_moved(self, frame: ViewFrame) -> None:
        self._place(frame)
        self.host.layout_changed(self.workspace)

    def resizeEvent(self, _event: QtGui.QResizeEvent) -> None:
        self._relayout()

    def refresh_live(self) -> None:
        for frame in self.frames:
            if isinstance(frame.body, LiveView):
                frame.body.refresh()

    def bodies(self, kind: type | tuple[type, ...]) -> list[QtWidgets.QWidget]:
        return [frame.body for frame in self.frames if isinstance(frame.body, kind)]

    def set_active(self, frame: ViewFrame | None) -> None:
        for other in self.frames:
            other.set_active(other is frame)


# ---- the tab row ------------------------------------------------------------------------------------------------

class PageTabs(QtWidgets.QWidget):
    """The page names with PCLink's previous and next arrows; the current page is a coloured pill."""

    currentChanged = QtCore.Signal(int)
    ARROW = 26

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.current = -1
        self.first = 0
        self.setFixedHeight(30)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        metrics = QtGui.QFontMetricsF(self._font())
        wanted = 2 * self.ARROW + 12 + sum(metrics.horizontalAdvance(name) + 26 for name in self.names)
        return QtCore.QSize(int(min(max(wanted, 200), 760)), 30)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(120, 30)

    def count(self) -> int:
        return len(self.names)

    def currentIndex(self) -> int:
        return self.current

    def tabText(self, index: int) -> str:
        return self.names[index]

    def addTab(self, name: str) -> int:
        self.names.append(name)
        self.updateGeometry()
        if self.current < 0:
            self.setCurrentIndex(0)
        self.update()
        return len(self.names) - 1

    def removeTab(self, index: int) -> None:
        del self.names[index]
        if index < self.current:
            self.current -= 1
        elif index == self.current:
            self.current = min(self.current, len(self.names) - 1)
            if self.names:
                self.currentChanged.emit(self.current)
        self.first = min(self.first, max(len(self.names) - 1, 0))
        self.updateGeometry()
        self.update()

    def setTabText(self, index: int, name: str) -> None:
        self.names[index] = name
        self.updateGeometry()
        self.update()

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < len(self.names) and index != self.current:
            self.current = index
            self.update()
            self.currentChanged.emit(index)

    def _font(self) -> QtGui.QFont:
        font = QtGui.QFont(self.font())
        font.setPixelSize(17)
        return font

    def _slots(self) -> list[tuple[int, QtCore.QRectF]]:
        metrics = QtGui.QFontMetricsF(self._font())
        limit = self.width() - 2 * self.ARROW - 6
        if self.current >= 0:
            self.first = min(self.first, self.current)
        spots, used = [], 0.0
        index = self.first
        while index < len(self.names):
            width = metrics.horizontalAdvance(self.names[index]) + 24
            if used + width > limit and spots:
                if index > self.current:
                    break
                self.first += 1
                return self._slots()
            spots.append((index, QtCore.QRectF(self.ARROW + 3 + used, 2, width, self.height() - 4)))
            used += width + 2
            index += 1
        return spots

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), gauges.theme_colour("base"))
        painter.setFont(self._font())
        text = gauges.theme_colour("text")
        for index, box in self._slots():
            if index == self.current:
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(gauges.theme_colour("active"))
                painter.drawRoundedRect(box, 6, 6)
                painter.setPen(QtGui.QColor(0, 0, 0) if gauges.theme_colour("active").lightness() > 100 else text)
            else:
                painter.setPen(text)
            painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, self.names[index])
        can_back, can_forward = self.current > 0, 0 <= self.current < len(self.names) - 1
        dim = QtGui.QColor(90, 90, 90)
        draw_triangle(painter, QtCore.QRectF(2, 2, self.ARROW, self.height() - 4), QtGui.QColor(225, 225, 225) if can_back else dim, False)
        draw_triangle(painter, QtCore.QRectF(self.width() - self.ARROW - 2, 2, self.ARROW, self.height() - 4), QtGui.QColor(0, 150, 0) if can_forward else dim, True)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        point = event.position()
        if point.x() < self.ARROW + 2:
            self.setCurrentIndex(self.current - 1)
        elif point.x() > self.width() - self.ARROW - 2:
            self.setCurrentIndex(self.current + 1)
        else:
            for index, box in self._slots():
                if box.contains(point):
                    self.setCurrentIndex(index)
                    break


class IndicatorLabel(QtWidgets.QLabel):
    """One of the boxes at the right of the tab row: PC Log, Online, and the update rate."""

    def __init__(self, text: str = "", on_text: tuple[str, ...] = (), show_state: bool = True) -> None:
        super().__init__()
        self.on_text, self.show_state = on_text, show_state
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        if self.show_state:
            on = any(text.lower().endswith(word) for word in self.on_text)
            self.setStyleSheet(f"background:{'#00a000' if on else '#ff0000'};color:white;padding:2px 12px;font-size:15px")
        else:
            self.setStyleSheet("background:transparent;color:white;padding:2px 8px;font-size:15px")


class Indicators(QtWidgets.QWidget):
    def __init__(self, *labels: QtWidgets.QLabel) -> None:
        super().__init__()
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 4, 0)
        row.setSpacing(2)
        for label in labels:
            row.addWidget(label)
        self.setStyleSheet("background:transparent")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        for label in labels:
            label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)


class RateMeter:
    """Counts the runtime updates that arrive each second, as the figure PCLink shows beside Online."""

    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self.times: collections.deque[float] = collections.deque()

    def tick(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        self.times.append(now)
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()
        return f"{self.rate():.1f}Hz"

    def rate(self) -> float:
        if len(self.times) < 2 or self.times[-1] <= self.times[0]:
            return 0.0
        return (len(self.times) - 1) / (self.times[-1] - self.times[0])


class TabRow(QtWidgets.QWidget):
    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        QtGui.QPainter(self).fillRect(self.rect(), gauges.theme_colour("base"))


class Workspace(QtWidgets.QWidget):
    """PCLink's page tabs over the current page."""

    page_changed = QtCore.Signal()

    def __init__(self, host: WorkspaceHost) -> None:
        super().__init__()
        self.host = host
        self.layout_: Layout = Layout()
        self.path = None
        self.unsupported: list[str] = []
        self.tabs = PageTabs()
        self.row_widget = TabRow()
        self.row = QtWidgets.QHBoxLayout(self.row_widget)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self.row.addWidget(self.tabs, 1)
        self.indicators: QtWidgets.QWidget | None = None
        self.stack = QtWidgets.QStackedWidget()
        column = QtWidgets.QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.row_widget)
        column.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self._select)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_moved)

    def attach_indicators(self, widget: QtWidgets.QWidget | None) -> None:
        """Show PC Log, Online and the rate at the right of this workspace's tab row."""
        if self.indicators is widget:
            return
        previous = getattr(widget, "owner_workspace", None) if widget is not None else None
        if previous is not None and previous is not self:
            previous.row.removeWidget(widget)
            previous.indicators = None
        if self.indicators is not None:
            self.row.removeWidget(self.indicators)
            self.indicators.setParent(None)
        self.indicators = widget
        if widget is not None:
            widget.owner_workspace = self
            self.row.addWidget(widget)
            widget.show()

    def _page_title(self, page: LayoutPage) -> str:
        title = getattr(self.host, "caption_of_page", None)
        return (title(page) if title else "") or page.name

    def _focus_moved(self, _old: QtWidgets.QWidget | None, new: QtWidgets.QWidget | None) -> None:
        widget = new
        while widget is not None and not isinstance(widget, ViewFrame):
            widget = widget.parentWidget()
        page = self.current()
        if isinstance(widget, ViewFrame) and page is not None and widget.parentWidget() is page:
            page.set_active(widget)

    def load(self, layout: Layout) -> None:
        self.tabs.blockSignals(True)
        self.tabs.names.clear()
        self.tabs.current, self.tabs.first = -1, 0
        while self.stack.count():
            page = self.stack.widget(0)
            self.stack.removeWidget(page)
            page.deleteLater()
        self.layout_ = layout
        self.unsupported = []
        for page in layout.pages:
            widget = PageWidget(page, self.host, self)
            self.unsupported += widget.unsupported
            self.stack.addWidget(widget)
            self.tabs.addTab(self._page_title(page))
        names = [page.name for page in layout.pages]
        at = names.index(layout.selected) if layout.selected in names else 0
        self.tabs.current = at if names else -1
        self.stack.setCurrentIndex(max(at, 0))
        self.tabs.blockSignals(False)
        self.tabs.updateGeometry()
        self.tabs.update()
        self.page_changed.emit()

    def _select(self, index: int) -> None:
        if index < 0 or index >= len(self.layout_.pages):
            return
        self.stack.setCurrentIndex(index)
        self.layout_.selected = self.layout_.pages[index].name
        self.page_changed.emit()
        self.host.layout_changed(self)

    def current(self) -> PageWidget | None:
        return self.stack.currentWidget()

    def step_page(self, direction: int) -> None:
        """The [ and ] keys; they wrap around."""
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + direction) % count)

    def unique_name(self, name: str) -> str:
        names = {page.name.lower() for page in self.layout_.pages}
        candidate, number = name, 2
        while candidate.lower() in names:
            candidate, number = f"{name} {number}", number + 1
        return candidate

    def add_page(self, name: str) -> None:
        page = LayoutPage(self.unique_name(name))
        self.layout_.pages.append(page)
        self.stack.addWidget(PageWidget(page, self.host, self))
        self.tabs.addTab(page.name)
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.host.layout_changed(self)

    def delete_page(self) -> bool:
        index = self.tabs.currentIndex()
        if index < 0 or self.tabs.count() <= 1:
            return False
        widget = self.stack.widget(index)
        self.tabs.blockSignals(True)
        self.stack.removeWidget(widget)
        widget.deleteLater()
        del self.layout_.pages[index]
        self.tabs.removeTab(index)
        self.tabs.blockSignals(False)
        self.stack.setCurrentIndex(self.tabs.currentIndex())
        self.layout_.selected = self.layout_.pages[self.tabs.currentIndex()].name
        self.page_changed.emit()
        self.host.layout_changed(self)
        return True

    def rename_page(self, name: str) -> None:
        index = self.tabs.currentIndex()
        if index >= 0:
            self.layout_.pages[index].name = name
            self.layout_.pages[index].name_string = ""
            self.layout_.selected = name
            self.tabs.setTabText(index, name)
            self.host.layout_changed(self)

    def show_page(self, name: str) -> bool:
        for index, page in enumerate(self.layout_.pages):
            if page.name.lower() == name.lower():
                self.tabs.setCurrentIndex(index)
                return True
        return False

    def add_view(self, view_type: str, on_view: Callable[[LayoutView], None] | None = None) -> ViewFrame | None:
        """Add a new view on the current page, at PCLink's default size and the first free place."""
        page = self.current()
        if page is None:
            return None
        width, height = DEFAULT_SIZES.get(view_type, (320, 240))
        view = LayoutView(CANONICAL.get(view_type, view_type), width=width, height=height)
        if view_type in GAUGE_TYPES:
            view.attributes.update(GaugeSettings().attributes())
        elif view_type == "Runtime Value List":
            view.attributes.update(ColumnsCount="1", Caption="", SortMode="0", BoldFont="true")
        view.left, view.top = page.free_spot(width, height)
        if on_view:
            on_view(view)
        return page.add_view(view)

    def refresh_live(self) -> None:
        page = self.current()
        if page is not None:
            page.refresh_live()
