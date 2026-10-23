"""SigMarker — 签名水印提取小工具

Usage: python main.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from PyQt6.QtCore import Qt, QRectF, QPointF, QSize, QSettings, QTimer, QEvent, pyqtSignal, QMimeData, QStandardPaths, QUrl, QBuffer, QIODevice, QSignalBlocker
from PyQt6.QtGui import (
    QAction, QPixmap, QImage, QPainter, QPen, QColor, QBrush, QPainterPath, QDesktopServices,
    QIcon, QIntValidator, QKeySequence, QFont, QPalette,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QSlider, QCheckBox, QComboBox,
    QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsPixmapItem, QStatusBar, QSizePolicy,
    QSplitter, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox,
    QDoubleSpinBox, QAbstractSpinBox, QGridLayout, QFrame, QToolButton,
)

from .sigextract import (
    WatermarkParams,
    DEFAULT_OUTPUT_DPI,
    load_pdf_page,
    detect_signature_orientation,
    detect_ink_bbox,
    render_watermark,
    export_png,
    export_pdf,
)
from .stamp_import import append_stamp
from .clipboard_image import clipboard_payloads, set_windows_clipboard
from .image_io import load_image
from .phone_dialog import PhoneImportDialog

# 支持拖入/打开的扩展名
_SUPPORTED_EXTS = ('.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.heic', '.heif')
APP_STYLE = 'windows11'
APP_FONT_FAMILY = 'Microsoft YaHei'
REPOSITORY_URL = 'https://github.com/winloud/sigmarker'
APP_ICON = Path(__file__).with_name('assets') / 'sigmarker.ico'


class OutputDpiComboBox(QComboBox):
    """预设和手动输入共用一个有效 DPI，未提交的输入不影响导出。"""

    valueChanged = pyqtSignal(int)

    def __init__(self, value=DEFAULT_OUTPUT_DPI):
        super().__init__()
        self._value = DEFAULT_OUTPUT_DPI
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for dpi in (100, 150, 200, 300, 600, 1200):
            self.addItem(f'{dpi} DPI' + ('（推荐）' if dpi == 300 else ''), dpi)
        self.lineEdit().setValidator(QIntValidator(72, 1200, self))
        self.lineEdit().installEventFilter(self)
        self.lineEdit().editingFinished.connect(self._commit_text)
        self.activated.connect(self._activate_preset)
        self.setToolTip('可下拉选择或直接输入 72～1200。DPI 越大，物理尺寸越小；像素和清晰度不变。')
        self.setValue(value)

    def value(self):
        return self._value

    def _activate_preset(self, index):
        value = self.itemData(index)
        if value is not None:
            self.setValue(value)

    def setValue(self, value):
        value = max(72, min(1200, int(value)))
        changed = value != self._value
        self._value = value
        with QSignalBlocker(self):
            self.setCurrentIndex(self.findData(value))
            self.setEditText(str(value))
        if changed:
            self.valueChanged.emit(value)

    def _commit_text(self):
        text = self.currentText().strip()
        value = int(text) if text.isascii() and text.isdecimal() else self._value
        self.setValue(value if 72 <= value <= 1200 else self._value)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusOut and event.reason() != Qt.FocusReason.PopupFocusReason:
            self._commit_text()
        return super().eventFilter(watched, event)

    def focusOutEvent(self, event):
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._commit_text()
        super().focusOutEvent(event)


# ---------- 裁剪框: 单 item, 手绘手柄, 手动事件, 全程边界钳制 ----------

class CropRectItem(QGraphicsRectItem):
    """可拖拽/可缩放的裁剪框。

    - 8 个手柄用 paint() 直接绘制 (纯装饰, 不是子 item, 避开事件复杂度)
    - 移动/缩放全部在 mouse 事件里手动完成, 并钳制在 bounds (图像范围) 内
    - rect_cb 回调在每次几何变化后触发
    """

    TOL = 12  # 手柄命中容差 (item 坐标)
    MIN_SIZE = 20

    CURSOR_BY_ZONE = {
        'tl': Qt.CursorShape.SizeFDiagCursor,
        'br': Qt.CursorShape.SizeFDiagCursor,
        'tr': Qt.CursorShape.SizeBDiagCursor,
        'bl': Qt.CursorShape.SizeBDiagCursor,
        't':  Qt.CursorShape.SizeVerCursor,
        'b':  Qt.CursorShape.SizeVerCursor,
        'l':  Qt.CursorShape.SizeHorCursor,
        'r':  Qt.CursorShape.SizeHorCursor,
        'move': Qt.CursorShape.SizeAllCursor,
    }

    def __init__(self, rect: QRectF, bounds: QRectF, rect_cb=None):
        super().__init__(rect)
        self._bounds = QRectF(bounds)
        self._rect_cb = rect_cb
        self._mode = None          # 'move' | 'tl'..'br' | None
        self._press_scene = None   # 按下时鼠标场景坐标 (快照)
        self._press_pos = None     # 按下时 item pos (快照)
        self._press_rect = None    # 按下时 item rect (快照)
        self.setAcceptHoverEvents(True)

    # ---- 几何工具 ----

    def scene_rect(self) -> QRectF:
        """裁剪框在场景坐标中的矩形。"""
        r = self.rect()
        return QRectF(self.pos() + r.topLeft(), r.size())

    def _handle_anchors(self) -> dict:
        r = self.rect()
        return {
            'tl': QPointF(r.left(), r.top()),
            'tr': QPointF(r.right(), r.top()),
            'bl': QPointF(r.left(), r.bottom()),
            'br': QPointF(r.right(), r.bottom()),
            't':  QPointF(r.center().x(), r.top()),
            'b':  QPointF(r.center().x(), r.bottom()),
            'l':  QPointF(r.left(), r.center().y()),
            'r':  QPointF(r.right(), r.center().y()),
        }

    def _zone_at(self, pos: QPointF):
        """判定 item 坐标处命中的区域: 手柄名 / 'move' / None。"""
        t = self.TOL
        a = self._handle_anchors()
        for name in ('tl', 'tr', 'bl', 'br', 't', 'b', 'l', 'r'):
            if abs(pos.x() - a[name].x()) <= t and abs(pos.y() - a[name].y()) <= t:
                return name
        if self.rect().contains(pos):
            return 'move'
        return None

    # ---- 绘制: 边框 + 8 个手柄方块 ----

    def boundingRect(self):
        # 包含外侧手柄和命中区域, 让场景在移动/缩放时清理整个旧绘制区域。
        margin = self.TOL + 1
        return self.rect().adjusted(-margin, -margin, margin, margin)

    def shape(self):
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRect(self.rect())
        for point in self._handle_anchors().values():
            path.addRect(QRectF(point.x() - self.TOL, point.y() - self.TOL,
                               self.TOL * 2, self.TOL * 2))
        return path

    def paint(self, painter: QPainter, option, widget=None):
        painter.setPen(QPen(QColor(0, 120, 215), 0))  # cosmetic 1px
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect())

        painter.setPen(QPen(QColor(0, 120, 215), 0))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        s = self.TOL
        for p in self._handle_anchors().values():
            painter.drawRect(QRectF(p.x() - s / 2, p.y() - s / 2, s, s))

    # ---- hover: 光标提示 ----

    def hoverMoveEvent(self, e):
        zone = self._zone_at(e.pos())
        cur = self.CURSOR_BY_ZONE.get(zone)
        if cur is not None:
            self.setCursor(cur)
        elif self._bounds.contains(self.scene_rect().center()):
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverMoveEvent(e)

    # ---- 鼠标: 手动移动/缩放 ----

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        zone = self._zone_at(e.pos())
        if zone is None:
            e.ignore()
            return
        self._mode = zone
        self._press_scene = QPointF(e.scenePos())
        self._press_pos = QPointF(self.pos())
        self._press_rect = QRectF(self.rect())
        e.accept()

    def mouseMoveEvent(self, e):
        if self._mode is None or self._press_scene is None:
            return
        delta = QPointF(e.scenePos()) - self._press_scene
        if self._mode == 'move':
            self._apply_move(delta)
        else:
            self._apply_resize(delta)
        e.accept()

    def mouseReleaseEvent(self, e):
        self._mode = None
        self._press_scene = None
        self._press_pos = None
        self._press_rect = None
        e.accept()

    # ---- 应用移动 (带钳制) ----

    def _apply_move(self, delta: QPointF):
        r = self._press_rect
        new_pos = self._press_pos + delta
        # 场景中的框 = new_pos + r.topLeft() .. +r.bottomRight(), 钳制进 bounds
        min_x = self._bounds.left() - r.left()
        max_x = self._bounds.right() - r.right()
        min_y = self._bounds.top() - r.top()
        max_y = self._bounds.bottom() - r.bottom()
        new_pos.setX(min(max(new_pos.x(), min_x), max_x))
        new_pos.setY(min(max(new_pos.y(), min_y), max_y))
        self.setPos(new_pos)
        if self._rect_cb:
            self._rect_cb()

    # ---- 应用缩放 (带钳制) ----

    def _apply_resize(self, delta: QPointF):
        sr = QRectF(self._press_pos + self._press_rect.topLeft(),
                    self._press_rect.size())  # 按下时的场景矩形
        m = self._mode
        if 'l' in m: sr.setLeft(sr.left() + delta.x())
        if 'r' in m: sr.setRight(sr.right() + delta.x())
        if 't' in m: sr.setTop(sr.top() + delta.y())
        if 'b' in m: sr.setBottom(sr.bottom() + delta.y())
        # 注意 'tl','tr','bl','br' 同时含 x/y 侧字符, 上面四个 if 已覆盖

        sr = sr.normalized()
        if sr.width() < self.MIN_SIZE or sr.height() < self.MIN_SIZE:
            return
        # 钳制进图像边界
        sr = sr.intersected(self._bounds)
        if sr.width() < self.MIN_SIZE or sr.height() < self.MIN_SIZE:
            return

        self.setPos(sr.topLeft())
        self.setRect(QRectF(0, 0, sr.width(), sr.height()))
        if self._rect_cb:
            self._rect_cb()


# ---------- 带滚轮缩放的 QGraphicsView ----------

class ZoomableGraphicsView(QGraphicsView):
    fileDropped = pyqtSignal(str)
    viewportResized = pyqtSignal()

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    @staticmethod
    def dropped_path(mime):
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(_SUPPORTED_EXTS):
                    return url.toLocalFile()
        return None

    def dragEnterEvent(self, e):
        if self.dropped_path(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        path = self.dropped_path(e.mimeData())
        if path:
            e.acceptProposedAction()
            self.fileDropped.emit(path)
        else:
            e.ignore()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.viewportResized.emit()

    def wheelEvent(self, e):
        factor = 1.25 if e.angleDelta().y() > 0 else 1 / 1.25
        self.scale(factor, factor)


class ImportCard(QFrame):
    """透明导入面板；手绘等距虚线，避免样式表边框在圆角处挤成点。"""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor('#55585e'), 1)
        pen.setDashPattern([3, 3])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 6, 6)


# ---------- 主窗口 ----------

class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings if settings is not None else QSettings('SigMarker', 'SigMarker')
        self.setWindowTitle('SigMarker — 签名水印提取')
        self.setFont(QFont(APP_FONT_FAMILY, 9))
        self.setWindowIcon(QIcon(str(APP_ICON)))
        # 原型使用统一的白色工作区；明确调色板，避免系统 Window/Base 色混成分区色块。
        palette = self.palette()
        for role, color in (
                (QPalette.ColorRole.Window, '#ffffff'),
                (QPalette.ColorRole.Base, '#ffffff'),
                (QPalette.ColorRole.Button, '#ffffff'),
                (QPalette.ColorRole.WindowText, '#20242a'),
                (QPalette.ColorRole.Text, '#20242a'),
                (QPalette.ColorRole.ButtonText, '#20242a')):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.resize(1180, 780)
        self.setMinimumSize(900, 640)

        self._page_img: Image.Image | None = None
        self._pix_item: QGraphicsPixmapItem | None = None
        self._crop_item: CropRectItem | None = None
        self._source_path = ''
        self._source_name = ''
        self._phone_photo = None
        self._phone_dialog = None
        self._wm_image = None
        self._params = self._load_params()   # 上次会话的参数 (无则用默认)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(70)
        self._preview_timer.timeout.connect(self._refresh_preview)

        self._build_ui()
        self._build_actions()
        self.setStatusBar(QStatusBar())
        self.statusBar().setStyleSheet('''
            QStatusBar { background: #ffffff; border-top: 1px solid #e0e4e9; }
            QStatusBar::item { border: none; }
        ''')

        # 拖放打开
        self.setAcceptDrops(True)
        self.view_page.viewportResized.connect(self._position_canvas_overlays)
        self.view_page.fileDropped.connect(self._load_path)
        self._set_document_enabled(False)
        self._restore_window_layout()
        self._position_canvas_overlays()
        QTimer.singleShot(0, self._position_canvas_overlays)

    def _restore_window_layout(self):
        geometry = self._settings.value('window/geometry')
        splitter_state = self._settings.value('window/splitter')
        if geometry is not None:
            self.restoreGeometry(geometry)
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self._preview_timer.stop()
            if self._phone_dialog is not None:
                self._phone_dialog.reject()
            self._cleanup_phone_photo()
            self._settings.setValue('window/geometry', self.saveGeometry())
            self._settings.setValue('window/splitter', self.splitter.saveState())
            self._settings.sync()

    # ---------- 参数持久化 ----------

    def _load_params(self) -> WatermarkParams:
        s = self._settings
        # 旧版可关闭纸色校正, 会将偏灰纸面提取成墨迹; 移除该历史状态。
        s.remove('flatten_bg')
        try:
            return WatermarkParams(
                threshold=int(s.value('threshold', 140)),
                feather=int(s.value('feather', 0)),
                contrast=int(s.value('contrast_x100', 100)) / 100.0,
                ink_density=max(100, min(300, int(s.value('ink_density_x100', 100)))) / 100.0,
                opacity=int(s.value('opacity_x100', 100)) / 100.0,
                sharpness=int(s.value('sharpness_x100', 0)) / 100.0,
                color_mode=s.value('color_mode', 'black'),
                clear_background=s.value('clear_bg', 'true') in ('true', True),
                denoise=s.value('denoise', 'true') in ('true', True),
                output_dpi=max(72, min(1200, int(s.value('output_dpi', DEFAULT_OUTPUT_DPI)))),
            )
        except Exception:
            return WatermarkParams()

    def _save_params(self):
        s = self._settings
        p = self._params
        s.setValue('threshold', p.threshold)
        s.setValue('feather', p.feather)
        s.setValue('contrast_x100', int(round(p.contrast * 100)))
        s.setValue('ink_density_x100', int(round(p.ink_density * 100)))
        s.setValue('opacity_x100', int(round(p.opacity * 100)))
        s.setValue('sharpness_x100', int(round(p.sharpness * 100)))
        s.setValue('color_mode', p.color_mode)
        s.setValue('clear_bg', 'true' if p.clear_background else 'false')
        s.setValue('denoise', 'true' if p.denoise else 'false')
        s.setValue('output_dpi', p.output_dpi)

    # ---------- 拖放打开 ----------

    def dragEnterEvent(self, e):
        if ZoomableGraphicsView.dropped_path(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        path = ZoomableGraphicsView.dropped_path(e.mimeData())
        if path:
            e.acceptProposedAction()
            self._load_path(path)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._position_canvas_overlays()

    def _position_canvas_overlays(self):
        if not hasattr(self, 'empty_card'):
            return
        viewport = self.view_page.viewport()
        self.empty_card.adjustSize()
        self.empty_card.move(max((viewport.width() - self.empty_card.width()) // 2, 0),
                             max((viewport.height() - self.empty_card.height()) // 2, 0))
        self.canvas_toolbar.adjustSize()
        self.canvas_toolbar.move(max(viewport.width() - self.canvas_toolbar.width() - 10, 0), 10)
        self.canvas_toolbar.raise_()

    # ---------- UI ----------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName('mainSurface')
        self.setCentralWidget(central)
        central.setStyleSheet("""
            QWidget#mainSurface { background: #ffffff; }
            QPushButton {
                background: #ffffff; color: #20242a;
                border: 1px solid #cbd1da; border-radius: 4px; padding: 5px 10px;
            }
            QPushButton:hover { background: #f3f6fa; border-color: #b2bfce; }
            QPushButton:pressed { background: #e7edf5; }
            QPushButton:disabled { background: #f7f8fa; color: #9aa3af; border-color: #e1e5eb; }
            QSpinBox, QDoubleSpinBox, QComboBox {
                background: palette(base); color: palette(text);
                border: 1px solid #cbd1da; border-radius: 4px; padding: 4px 6px;
            }
            QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #0078d4; }
            QComboBox { padding-right: 24px; }
            QComboBox::drop-down { width: 22px; border: none; }
            QSlider::groove:horizontal { height: 4px; background: #cbd1da; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #0078d4; border-radius: 2px; }
            QSlider::handle:horizontal {
                width: 12px; height: 12px; margin: -5px 0;
                background: palette(base); border: 2px solid #0078d4; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover { background: #dcefff; }
            QFrame#parameterPanel {
                background: #ffffff; border: none;
                border-left: 1px solid #e0e4e9; border-radius: 0;
            }
            QSplitter::handle { background: #ffffff; }
            QLabel#sectionTitle { font-weight: bold; }
            QLabel#sectionHint { color: #687483; }
            QLabel#fileBadge {
                background: rgba(128, 128, 128, 16); border-radius: 3px; padding: 3px 7px;
            }
            QPushButton#primaryAction {
                background: #0078d4; color: white; border: 1px solid #0078d4;
                border-radius: 4px; padding: 7px 12px;
            }
            QPushButton#primaryAction:hover { background: #106ebe; }
            QPushButton#primaryAction:pressed { background: #005a9e; }
            QPushButton#primaryAction:disabled {
                background: #edf1f6; color: #9aa3af; border-color: #e1e5eb;
            }
        """)
        # 显式箭头避免原生样式在离屏表面或不同系统主题下漏绘。
        arrow_path = (APP_ICON.parent / 'chevron-down.svg').as_posix()
        central.setStyleSheet(central.styleSheet() +
            f'QComboBox::down-arrow {{ image: url("{arrow_path}"); width: 12px; height: 12px; }}')
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(10, 8, 10, 8)
        main_layout.setSpacing(12)

        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)

        top_box = QWidget()
        top_layout = QVBoxLayout(top_box)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        file_header, file_row = self._section_header('原图画布')
        self.lbl_file = QLabel('未载入文件')
        self.lbl_file.setObjectName('fileBadge')
        self.lbl_file.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lbl_file.setMaximumWidth(self.lbl_file.fontMetrics().horizontalAdvance(self.lbl_file.text()) + 18)
        file_row.addWidget(self.lbl_file, 1, Qt.AlignmentFlag.AlignVCenter)
        file_row.addStretch(1)
        top_layout.addWidget(file_header)

        self.scene = QGraphicsScene()
        self.view_page = ZoomableGraphicsView(self.scene)
        self.view_page.setStyleSheet('QGraphicsView { border: 1px solid #48515e; border-radius: 5px; }')
        self.view_page.setBackgroundBrush(QBrush(QColor('#222428')))
        self.view_page.setMinimumHeight(250)
        self.view_page.setToolTip('滚轮缩放；拖动裁剪框或手柄调整范围。支持拖入文件和 Ctrl+V 粘贴。')
        top_layout.addWidget(self.view_page, 1)

        # 浮动控件属于视口，不写入场景或导出图像，也不随缩放滚动。
        viewport = self.view_page.viewport()
        self.canvas_toolbar = QFrame(viewport)
        self.canvas_toolbar.setObjectName('canvasToolbar')
        self.canvas_toolbar.setStyleSheet("""
            QFrame#canvasToolbar {
                background: #ffffff; border: 1px solid #d5d9df; border-radius: 5px;
            }
            QToolButton {
                background: transparent; color: #343e4a;
                border: none; padding: 5px 7px; border-radius: 3px;
            }
            QToolButton:hover { background: #eef3f9; }
            QToolButton:pressed { background: #e2ebf6; }
            QToolButton:disabled { background: transparent; color: #9aa3af; }
            QFrame#toolbarSeparator { background: #e0e4e9; border: none; }
        """)
        tools = QHBoxLayout(self.canvas_toolbar)
        tools.setContentsMargins(3, 3, 3, 3)
        tools.setSpacing(2)
        self.btn_rot_ccw = QToolButton()
        self.btn_rot_ccw.setText('90°')
        self.btn_rot_ccw.setToolTip('逆时针旋转 90°，保留当前裁剪范围')
        self.btn_rot_ccw.clicked.connect(lambda: self._rotate_page(-90))
        self.btn_rot_cw = QToolButton()
        self.btn_rot_cw.setText('90°')
        self.btn_rot_cw.setToolTip('顺时针旋转 90°，保留当前裁剪范围')
        self.btn_rot_cw.clicked.connect(lambda: self._rotate_page(90))
        self.btn_auto = QToolButton()
        self.btn_auto.setText('自动框选范围')
        self.btn_auto.setToolTip('重新检测墨迹范围；姓名、日期和正文需自行区分。')
        self.btn_auto.clicked.connect(self._auto_detect_bbox)
        self.btn_close_file = QToolButton()
        self.btn_close_file.setText('关闭文件')
        self.btn_close_file.setToolTip('关闭当前文件，保留软件和参数 (Ctrl+W)')
        self.btn_close_file.clicked.connect(self._close_file)
        for button, icon in ((self.btn_rot_ccw, 'rotate-ccw'), (self.btn_rot_cw, 'rotate-cw'),
                             (self.btn_auto, 'auto-crop'), (self.btn_close_file, 'close')):
            button.setIcon(QIcon(str(APP_ICON.with_name(icon + '.svg'))))
            button.setIconSize(QSize(15, 15))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for button in (self.btn_rot_ccw, self.btn_rot_cw, self.btn_auto):
            tools.addWidget(button)
        separator = QFrame()
        separator.setObjectName('toolbarSeparator')
        separator.setFixedSize(1, 16)
        tools.addWidget(separator)
        tools.addWidget(self.btn_close_file)

        self.empty_card = ImportCard(viewport)
        self.empty_card.setFixedWidth(388)
        self.empty_card.setObjectName('emptyCard')
        self.empty_card.setStyleSheet("""
            QFrame#emptyCard {
                background: transparent; border: none;
            }
            QLabel { background: transparent; color: #e2e7ee; }
            QLabel#pasteHint { color: #aeb8c5; }
            QPushButton {
                background: #ffffff; color: #20242a; border: 1px solid #d4d9e0;
                border-radius: 4px; padding: 5px 10px;
            }
            QPushButton:hover { background: #f2f7fd; border-color: #91b4d9; }
            QPushButton:pressed { background: #e3eefb; border-color: #6e9fce; }
            QPushButton:focus { border-color: #0078d4; }
        """)
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(22, 20, 22, 20)
        empty_layout.setSpacing(12)
        empty_title = QLabel('拖拽 PDF 或图片至此处')
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        import_row = QHBoxLayout()
        import_row.setSpacing(10)
        self.btn_open = QPushButton('打开 PDF / 图片…')
        self.btn_open.setMinimumHeight(32)
        self.btn_open.clicked.connect(self._open_file)
        self.btn_phone_import = QPushButton('手机扫码导入…')
        self.btn_phone_import.setMinimumHeight(32)
        self.btn_phone_import.clicked.connect(self._open_phone_import)
        for button, icon in ((self.btn_open, 'folder'), (self.btn_phone_import, 'phone')):
            button.setIcon(QIcon(str(APP_ICON.with_name(icon + '.svg'))))
            button.setIconSize(QSize(16, 16))
        import_row.addWidget(self.btn_open, 1)
        import_row.addWidget(self.btn_phone_import, 1)
        empty_layout.addLayout(import_row)
        paste_hint = QLabel('支持从剪贴板 Ctrl+V 粘贴图片或文件')
        paste_hint.setObjectName('pasteHint')
        paste_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(paste_hint)

        bottom_box = QWidget()
        bottom_layout = QVBoxLayout(bottom_box)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(6)
        preview_header, preview_header_layout = self._section_header('效果实时预览')
        preview_hint = QLabel('透明通道验证')
        preview_hint.setObjectName('sectionHint')
        preview_header_layout.addWidget(preview_hint)
        preview_header_layout.addStretch(1)
        bottom_layout.addWidget(preview_header)
        preview_row = QHBoxLayout()
        preview_row.setSpacing(0)
        preview_labels = []
        for title, color, text_color, border_color in [
                ('暗底 · 检查白边', '#30353e', '#e2e7ee', '#48515e'),
                ('白底 · 模拟盖章', '#ffffff', '#596573', '#d5d9df')]:
            preview = QLabel('请先导入图片或 PDF')
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumHeight(150)
            preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            preview.setStyleSheet(
                f'border: 1px solid {border_color}; background-color: {color}; color: {text_color};')
            # 标注固定在各底板内左上角；图像另留空间，避免覆盖文字。
            heading = QLabel(title, preview)
            heading.setObjectName('previewCornerLabel')
            heading.setStyleSheet(f'background: transparent; border: none; color: {text_color};')
            heading.move(8, 6)
            heading.adjustSize()
            heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            preview.setContentsMargins(12, max(30, heading.height() + 14), 12, 12)
            preview_row.addWidget(preview, 1)
            preview_labels.append(preview)
        self.lbl_wm_dark, self.lbl_wm_white = preview_labels
        self._preview_labels = preview_labels
        bottom_layout.addLayout(preview_row, 1)
        self.lbl_wm_info = QLabel('底色仅用于预览，复制和导出保持透明。')
        self.lbl_wm_info.setWordWrap(True)
        bottom_layout.addWidget(self.lbl_wm_info)
        for preview in preview_labels:
            preview.installEventFilter(self)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(top_box)
        self.splitter.addWidget(bottom_box)
        self.splitter.setSizes([470, 230])
        left_layout.addWidget(self.splitter)

        right_box = QFrame()
        right_box.setObjectName('parameterPanel')
        right_box.setFixedWidth(320)
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(12, 0, 0, 0)
        right_layout.setSpacing(10)
        parameter_header, parameter_header_layout = self._section_header('参数调整')
        parameter_header_layout.addStretch(1)
        self.btn_reset = QPushButton('恢复默认')
        self.btn_reset.clicked.connect(self._reset_params)
        parameter_header_layout.addWidget(self.btn_reset)
        right_layout.addWidget(parameter_header)
        self.lbl_bbox = QLabel('当前裁剪：暂无选区')
        right_layout.addWidget(self.lbl_bbox)

        # 共用三列网格，数值框等宽，所有原生滑条等长、等粗。
        parameter_grid = QGridLayout()
        parameter_grid.setHorizontalSpacing(8)
        parameter_grid.setVerticalSpacing(10)
        parameter_grid.setColumnStretch(1, 1)
        definitions = [
            ('threshold', '淡墨保留', 60, 200, 1, self._params.threshold,
             '越大保留越多；细笔画缺失时先调此项，过高可能带入纸纹。'),
            ('contrast', '对比度', 50, 200, 100, self._params.contrast,
             '调整已保留的笔迹明暗。'),
            ('ink_density', '墨迹浓度', 100, 300, 100, self._params.ink_density,
             '1.00× 保持原效果；加深已提取的笔画，透明背景不变。'),
            ('opacity', '不透明度', 10, 100, 100, self._params.opacity,
             '最后应用的整体不透明度。'),
            ('sharpness', '锐利度', 0, 200, 100, self._params.sharpness,
             '手机照片偏糊时适度增加，不能恢复原图中不存在的细节。'),
        ]
        for row, (name, title, low, high, scale, value, tip) in enumerate(definitions):
            label = QLabel(title)
            label.setToolTip(tip)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            slider.setValue(round(value * scale))
            slider.setToolTip(tip)
            slider.setMinimumWidth(90)
            editor = QSpinBox() if scale == 1 else QDoubleSpinBox()
            if scale == 1:
                editor.setRange(low, high)
            else:
                editor.setRange(low / scale, high / scale)
                editor.setDecimals(2)
                editor.setSingleStep(.01)
            if name == 'ink_density':
                editor.setSuffix('×')
            editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            editor.setKeyboardTracking(False)
            editor.setAlignment(Qt.AlignmentFlag.AlignRight)
            editor.setFixedSize(68, 30)
            editor.setValue(value)
            editor.setToolTip(tip)
            label.setBuddy(editor)
            setattr(self, 'sld_' + name, slider)
            setattr(self, 'spn_' + name, editor)
            slider.valueChanged.connect(lambda v, e=editor, s=scale: self._sync_parameter_editor(e, v / s))
            editor.valueChanged.connect(lambda v, s=slider, factor=scale: s.setValue(round(v * factor)))
            slider.valueChanged.connect(self._on_params_changed)
            parameter_grid.addWidget(label, row, 0)
            parameter_grid.addWidget(slider, row, 1)
            parameter_grid.addWidget(editor, row, 2)

        self.cmb_color = QComboBox()
        self.cmb_color.addItems(['黑色（默认）', '白色', '原彩'])
        self.cmb_color.setCurrentIndex({'black': 0, 'white': 1, 'original': 2}.get(self._params.color_mode, 0))
        self.cmb_color.currentIndexChanged.connect(self._on_params_changed)
        parameter_grid.addWidget(QLabel('墨色'), 5, 0)
        parameter_grid.addWidget(self.cmb_color, 5, 1, 1, 2)
        self.cmb_output_dpi = OutputDpiComboBox(self._params.output_dpi)
        self.cmb_output_dpi.valueChanged.connect(self._on_params_changed)
        parameter_grid.addWidget(QLabel('导出 DPI'), 6, 0)
        parameter_grid.addWidget(self.cmb_output_dpi, 6, 1, 1, 2)
        right_layout.addLayout(parameter_grid)
        self.chk_clear_bg = QCheckBox('自动清除近白背景')
        self.chk_clear_bg.setChecked(self._params.clear_background)
        self.chk_clear_bg.stateChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.chk_clear_bg)
        self.chk_denoise = QCheckBox('清除孤立噪点')
        self.chk_denoise.setChecked(self._params.denoise)
        self.chk_denoise.stateChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.chk_denoise)
        right_layout.addStretch(1)

        self.btn_copy = QPushButton('复制透明签名')
        self.btn_copy.setObjectName('primaryAction')
        self.btn_copy.setMinimumHeight(36)
        self.btn_copy.clicked.connect(self._copy_watermark)
        right_layout.addWidget(self.btn_copy)
        export_row = QHBoxLayout()
        export_row.setSpacing(6)
        self.btn_export_png = QPushButton('导出透明 PNG')
        self.btn_export_png.setMinimumHeight(30)
        self.btn_export_png.clicked.connect(lambda: self._export('png'))
        self.btn_export_pdf = QPushButton('导出 PDF')
        self.btn_export_pdf.setMinimumHeight(30)
        self.btn_export_pdf.clicked.connect(lambda: self._export('pdf'))
        export_row.addWidget(self.btn_export_png, 1)
        export_row.addWidget(self.btn_export_pdf, 1)
        right_layout.addLayout(export_row)
        self.btn_add_stamp = QPushButton('添加到 PDF-XChange 图章…')
        self.btn_add_stamp.setMinimumHeight(30)
        self.btn_add_stamp.setToolTip('将当前签名追加到图章集合并备份；面板是否自动更新需实测。')
        self.btn_add_stamp.clicked.connect(self._add_stamp)
        right_layout.addWidget(self.btn_add_stamp)
        main_layout.addWidget(left_col, 1)
        main_layout.addWidget(right_box)

    @staticmethod
    def _section_header(title):
        """分区共用独立标题行，避免混用旧式带标题外框。"""
        header = QWidget()
        header.setFixedHeight(30)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName('sectionTitle')
        layout.addWidget(label)
        return header, layout

    @staticmethod
    def _sync_parameter_editor(editor, value):
        with QSignalBlocker(editor):
            editor.setValue(round(value) if isinstance(editor, QSpinBox) else value)

    # ---------- 菜单栏 ----------

    def _build_actions(self):
        menu_bar = self.menuBar()
        # 参考 Fluent 的轻量菜单：内缩圆角反馈，边界弱于文字。
        # 半透明中性色叠在系统调色板上，明暗主题共用同一套规则。
        menu_bar.setStyleSheet('''
            QMenuBar {
                background-color: #ffffff;
                color: #20242a;
                border-bottom: 1px solid rgba(128, 128, 128, 32);
                padding: 3px 6px;
            }
            QMenuBar::item {
                background: transparent;
                padding: 4px 12px;
                margin: 1px 2px;
                border-radius: 4px;
            }
            QMenuBar::item:selected {
                background-color: rgba(128, 128, 128, 26);
            }
            QMenuBar::item:pressed {
                background-color: rgba(128, 128, 128, 40);
            }
            QMenu {
                background-color: palette(base);
                color: palette(text);
                border: 1px solid rgba(128, 128, 128, 45);
                border-radius: 8px;
                padding: 5px;
            }
            QMenu::item {
                padding: 7px 12px;
                margin: 1px 0;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: rgba(128, 128, 128, 26);
            }
            QMenu::item:disabled { color: palette(mid); }
            QMenu::separator {
                height: 1px;
                background-color: rgba(128, 128, 128, 32);
                margin: 5px 12px;
            }
        ''')
        menu_file = menu_bar.addMenu('文件')

        act_open = QAction('打开…', self)
        act_open.setShortcut('Ctrl+O')
        act_open.triggered.connect(self._open_file)
        menu_file.addAction(act_open)

        self.act_paste = QAction('从剪贴板粘贴图片 / 文件', self)
        self.act_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.act_paste.triggered.connect(self._paste_from_clipboard)
        menu_file.addAction(self.act_paste)

        self.act_phone_import = QAction('手机扫码导入…', self)
        self.act_phone_import.triggered.connect(self._open_phone_import)
        menu_file.addAction(self.act_phone_import)

        self.act_close_file = QAction('关闭当前文件', self)
        self.act_close_file.setShortcut('Ctrl+W')
        self.act_close_file.triggered.connect(self._close_file)
        menu_file.addAction(self.act_close_file)
        menu_file.addSeparator()

        act_quit = QAction('退出', self)
        act_quit.setShortcut('Ctrl+Q')
        act_quit.triggered.connect(self.close)
        menu_file.addAction(act_quit)

        menu_about = self.menuBar().addMenu('关于')
        act_about = QAction('关于 SigMarker…', self)
        act_about.triggered.connect(self._show_about)
        menu_about.addAction(act_about)
        act_repository = QAction('GitHub 仓库', self)
        act_repository.triggered.connect(self._open_repository)
        menu_about.addAction(act_repository)

        for menu in (menu_file, menu_about):
            menu.setMinimumWidth(220)
            # 系统的矩形投影会露在透明圆角外；只保留样式表绘制的圆角边界。
            menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.NoDropShadowWindowHint)
            menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def _open_repository(self, *_):
        if not QDesktopServices.openUrl(QUrl(REPOSITORY_URL)):
            QMessageBox.warning(self, '无法打开浏览器', f'请在浏览器中打开：\n{REPOSITORY_URL}')

    def _show_about(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('关于 SigMarker')
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)
        title = QLabel('<h2>SigMarker</h2>签名水印提取工具')
        layout.addWidget(title)
        description = QLabel('从扫描件或手机照片提取手写签名，\n导出透明 PNG / PDF，用于文件水印。')
        layout.addWidget(description)
        repository = QLabel(f'GitHub 仓库：<br><a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a>')
        repository.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        repository.linkActivated.connect(self._open_repository)
        layout.addWidget(repository)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    # ---------- 打开文件: PDF 或图片 ----------

    def _paste_from_clipboard(self):
        """只在用户主动粘贴时读取；文件沿用解码入口，图片不落地临时原图。"""
        try:
            mime = QApplication.clipboard().mimeData()
            path = ZoomableGraphicsView.dropped_path(mime) if mime is not None else None
            if path:
                self._load_path(path)
                return
            image = mime.imageData() if mime is not None and mime.hasImage() else None
            if isinstance(image, QPixmap):
                image = image.toImage()
            if not isinstance(image, QImage) or image.isNull():
                self.statusBar().showMessage('剪贴板中没有可导入的图片或受支持的本地文件。')
                return
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, 'PNG'):
                raise ValueError('无法读取剪贴板图片。')
            page_img, _ = load_image(BytesIO(bytes(buffer.data())))
            self._load_page_image(page_img, '', display_name='剪贴板图片')
        except Exception as error:
            QMessageBox.warning(self, '粘贴失败', str(error))

    def _open_phone_import(self):
        if self._phone_dialog is None:
            dialog = PhoneImportDialog(self)
            self._phone_dialog = dialog
            dialog.finished.connect(lambda: setattr(self, '_phone_dialog', None))
            dialog.finished.connect(dialog.deleteLater)
        self._phone_dialog.show()
        self._phone_dialog.raise_()
        self._phone_dialog.activateWindow()

    def _cleanup_phone_photo(self):
        if self._phone_photo is not None:
            self._phone_photo.cleanup()
            self._phone_photo = None

    def _accept_phone_photo(self, photo):
        try:
            self._load_page_image(photo.image, str(photo.path))
        except Exception as error:
            QMessageBox.warning(self, '加载失败', str(error))
            return False
        self._phone_photo = photo
        return True

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择签名扫描件', '',
            '所有支持 (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.heic *.heif);;'
            'PDF 文件 (*.pdf);;'
            '图片文件 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.heic *.heif)')
        if not path:
            return
        self._load_path(path)

    def _load_path(self, path: str):
        try:
            if path.lower().endswith('.pdf'):
                page_img = load_pdf_page(path, dpi=300)
            else:
                page_img, _ = load_image(path)
            self._load_page_image(page_img, path)
        except Exception as e:
            QMessageBox.critical(self, '加载失败', str(e))
            return

    def _load_page_image(self, page_img, path, *, display_name=None):
        # 所有可能失败的图像计算先完成，成功后才替换当前文档。
        gray = np.asarray(page_img.convert('L')).astype(np.float32)
        if detect_signature_orientation(gray):
            page_img = page_img.rotate(90, expand=True)
            gray = np.asarray(page_img.convert('L')).astype(np.float32)
        bbox = detect_ink_bbox(gray, padding=self._params.bbox_padding)
        watermark = render_watermark(page_img.crop(bbox), self._params)
        pm = self._pil_to_qpixmap(page_img)
        self._preview_timer.stop()
        self._cleanup_phone_photo()
        self._page_img = page_img
        self._source_path = os.path.abspath(path) if path else ''
        self._source_name = display_name or os.path.basename(path)
        self._wm_image = None

        self._crop_item = None
        self._pix_item = None
        self.scene.clear()
        self.view_page.resetTransform()
        self._pix_item = self.scene.addPixmap(pm)
        self._pix_item.setZValue(-1)
        # 页面边框 (视觉边界, 不影响墨迹检测 — 检测基于 PIL 原图)
        self._page_border = self.scene.addRect(
            self._pix_item.sceneBoundingRect(), QPen(QColor(90, 90, 90), 0))
        self.scene.setSceneRect(self._pix_item.sceneBoundingRect())

        bounds = self._pix_item.sceneBoundingRect()
        x0, y0, x1, y1 = bbox
        rect = QRectF(x0, y0, x1 - x0, y1 - y0).intersected(bounds)
        self._crop_item = CropRectItem(rect, bounds, rect_cb=self._on_crop_changed)
        self.scene.addItem(self._crop_item)
        self._update_bbox_label()
        self._wm_image = watermark
        self._display_preview()
        self.view_page.fitInView(self._pix_item, Qt.AspectRatioMode.KeepAspectRatio)
        self.empty_card.hide()
        self.lbl_file.setText(self._source_name)
        self.lbl_file.setMaximumWidth(self.lbl_file.fontMetrics().horizontalAdvance(self.lbl_file.text()) + 18)
        self.lbl_file.setToolTip(self._source_path or self._source_name)
        self._set_document_enabled(True)
        self.statusBar().showMessage(
            f'已加载 · {self._page_img.size[0]}×{self._page_img.size[1]} px · 拖动选框或手柄调整范围')

    def _set_document_enabled(self, enabled):
        for widget in (self.btn_close_file, self.btn_rot_ccw, self.btn_rot_cw,
                       self.btn_auto, self.btn_copy,
                       self.btn_export_png, self.btn_export_pdf, self.btn_add_stamp, self.act_close_file):
            widget.setEnabled(enabled)

    def _close_file(self):
        self._preview_timer.stop()
        self._cleanup_phone_photo()
        self._crop_item = None
        self._pix_item = None
        self._page_border = None
        self._page_img = None
        self._wm_image = None
        self._source_path = ''
        self._source_name = ''
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.view_page.resetTransform()
        self.lbl_file.setText('未载入文件')
        self.lbl_file.setMaximumWidth(self.lbl_file.fontMetrics().horizontalAdvance(self.lbl_file.text()) + 18)
        self.lbl_file.setToolTip('')
        for preview in self._preview_labels:
            preview.clear()
            preview.setText('请打开文件，或拖入文档预览区')
        self.lbl_wm_info.setText('底色仅用于预览，导出仍为透明背景。')
        self._update_bbox_label()
        self.empty_card.show()
        self._position_canvas_overlays()
        self._set_document_enabled(False)
        self.setWindowTitle('SigMarker — 签名水印提取')
        self.statusBar().showMessage('已关闭当前文件，可以继续打开或拖入新文件。')

    # ---------- 裁剪框操作 ----------

    def _auto_detect_bbox(self):
        if self._page_img is None or self._pix_item is None:
            return
        gray = np.asarray(self._page_img.convert('L')).astype(np.float32)
        x0, y0, x1, y1 = detect_ink_bbox(gray, padding=self._params.bbox_padding)
        bounds = self._pix_item.sceneBoundingRect()
        rect = QRectF(x0, y0, x1 - x0, y1 - y0).intersected(bounds)

        if self._crop_item is not None:
            self.scene.removeItem(self._crop_item)

        self._crop_item = CropRectItem(rect, bounds, rect_cb=self._on_crop_changed)
        self.scene.addItem(self._crop_item)
        self._update_bbox_label()
        self._refresh_preview()

    def _on_crop_changed(self):
        self._update_bbox_label()
        self._preview_timer.start()

    def _update_bbox_label(self):
        if self._crop_item is None:
            self.lbl_bbox.setText('当前裁剪：暂无选区')
            self.lbl_bbox.setToolTip('')
            return
        sr = self._crop_item.scene_rect()
        self.lbl_bbox.setText(f'当前裁剪：{int(sr.width())}×{int(sr.height())} px')
        self.lbl_bbox.setToolTip(f'左上角：({int(sr.left())}, {int(sr.top())})')

    def _crop_pixels(self):
        """裁剪框 → 图像像素坐标 (与图像边界求交, 杜绝越界黑边)。返回 (x0,y0,x1,y1) 或 None。"""
        if self._page_img is None or self._crop_item is None:
            return None
        W, H = self._page_img.size
        sr = self._crop_item.scene_rect().intersected(QRectF(0, 0, W, H))
        if sr.width() < 2 or sr.height() < 2:
            return None
        x0 = max(0, int(round(sr.left())))
        y0 = max(0, int(round(sr.top())))
        x1 = min(W, int(round(sr.right())))
        y1 = min(H, int(round(sr.bottom())))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        return (x0, y0, x1, y1)

    # ---------- 旋转 ----------

    def _rotate_page(self, degrees: int):
        """顺时针旋转图像和当前选框, 保留用户选定的像素范围。"""
        if self._page_img is None:
            return
        # 只允许 90/180/270 度, 其他角度用 PIL 4 次采样避免插值糊
        valid = abs(degrees) % 360 in (0, 90, 180, 270)
        if not valid:
            degrees = round(degrees / 90) * 90
        if degrees % 360 == 0:
            return
        rect = self._crop_item.scene_rect() if self._crop_item is not None else None
        width, height = self._page_img.size
        for _ in range((degrees % 360) // 90):
            if rect is not None:
                rect = QRectF(height - rect.bottom(), rect.left(), rect.height(), rect.width())
            width, height = height, width
        self._page_img = self._page_img.rotate(-degrees, expand=True, fillcolor='white')

        pm = self._pil_to_qpixmap(self._page_img)
        self._crop_item = None   # scene.clear() 会删掉它, 先断引用
        self.scene.clear()
        self._pix_item = self.scene.addPixmap(pm)
        self._pix_item.setZValue(-1)
        self._page_border = self.scene.addRect(
            self._pix_item.sceneBoundingRect(), QPen(QColor(90, 90, 90), 0))
        self.scene.setSceneRect(self._pix_item.sceneBoundingRect())

        if rect is None:
            self._auto_detect_bbox()
        else:
            bounds = self._pix_item.sceneBoundingRect()
            self._crop_item = CropRectItem(rect.intersected(bounds), bounds, rect_cb=self._on_crop_changed)
            self.scene.addItem(self._crop_item)
            self._update_bbox_label()
            self._refresh_preview()
        self.view_page.fitInView(self._pix_item, Qt.AspectRatioMode.KeepAspectRatio)
        dir_str = '顺时针' if degrees > 0 else '逆时针'
        self.statusBar().showMessage(f'已{dir_str}旋转 {abs(degrees)}°')

    # ---------- 参数 ----------

    def _read_params_from_ui(self) -> WatermarkParams:
        color_map = {0: 'black', 1: 'white', 2: 'original'}
        return WatermarkParams(
            threshold=self.sld_threshold.value(),
            feather=0,   # 已从 UI 移除, 固定 0 (不做边缘羽化)
            contrast=self.sld_contrast.value() / 100.0,
            ink_density=self.sld_ink_density.value() / 100.0,
            opacity=self.sld_opacity.value() / 100.0,
            sharpness=self.sld_sharpness.value() / 100.0,
            color_mode=color_map[self.cmb_color.currentIndex()],
            clear_background=self.chk_clear_bg.isChecked(),
            denoise=self.chk_denoise.isChecked(),
            output_dpi=self.cmb_output_dpi.value(),
        )

    def _on_params_changed(self, *_):
        self._params = self._read_params_from_ui()
        self._save_params()   # 持久化, 下次启动沿用
        self._preview_timer.start()

    def _reset_params(self):
        d = WatermarkParams()
        self.sld_threshold.setValue(d.threshold)
        self.sld_contrast.setValue(int(d.contrast * 100))
        self.sld_ink_density.setValue(int(d.ink_density * 100))
        self.sld_opacity.setValue(int(d.opacity * 100))
        self.sld_sharpness.setValue(int(d.sharpness * 100))
        self.cmb_color.setCurrentIndex({'black': 0, 'white': 1, 'original': 2}[d.color_mode])
        self.chk_clear_bg.setChecked(d.clear_background)
        self.chk_denoise.setChecked(d.denoise)
        self.cmb_output_dpi.setValue(d.output_dpi)

    # ---------- 水印预览 ----------

    def eventFilter(self, watched, event):
        if watched in getattr(self, '_preview_labels', ()) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._display_preview)
        return super().eventFilter(watched, event)

    def _refresh_preview(self):
        box = self._crop_pixels()
        if box is None:
            return
        try:
            self._wm_image = render_watermark(self._page_img.crop(box), self._params)
            self._display_preview()
        except Exception as error:
            self._wm_image = None
            for preview in self._preview_labels:
                preview.clear()
                preview.setText('预览失败，请调整范围或重新打开文件。')
            self.statusBar().showMessage(f'预览失败: {error}')

    def _display_preview(self, *_):
        if self._wm_image is None:
            return
        avail_w = max(min(preview.contentsRect().width() for preview in self._preview_labels), 10)
        avail_h = max(min(preview.contentsRect().height() for preview in self._preview_labels), 10)
        disp = self._wm_image.copy()
        disp.thumbnail((avail_w, avail_h), Image.Resampling.LANCZOS)
        w, h = disp.size
        for preview, color in zip(self._preview_labels, [(48, 53, 62), (255, 255, 255)]):
            board = Image.new('RGB', (w, h), color)
            board.paste(disp, (0, 0), disp)
            ImageDraw.Draw(board).rectangle([0, 0, w - 1, h - 1], outline=(0, 120, 215), width=1)
            preview.setPixmap(self._pil_to_qpixmap(board))
        empty = self._wm_image.getchannel('A').getbbox() is None
        dpi = self._params.output_dpi
        width_mm, height_mm = (pixels / dpi * 25.4 for pixels in self._wm_image.size)
        self.lbl_wm_info.setText('未检测到墨迹，请调整范围或增大淡墨保留。' if empty else
            f'{self._wm_image.width}×{self._wm_image.height} px · {width_mm:.1f}×{height_mm:.1f} mm · {dpi} DPI\n'
            '底色仅用于预览，复制和导出保持透明')

    def _add_stamp(self):
        wm = self._watermark_for_export()
        if wm is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('添加到 PDF-XChange 图章')
        dialog.resize(610, 220)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_edit = QLineEdit(str(self._settings.value('pdfxchange/stamp_collection',
            r'D:\Program Files\PDF-XChange_Editor_Plus\Stamps\王华彬签名.pdf')))
        browse = QPushButton('浏览…')

        def choose_collection():
            path, _ = QFileDialog.getOpenFileName(dialog, '选择现有图章集合', path_edit.text(), 'PDF 图章集合 (*.pdf)')
            if path:
                path_edit.setText(path)

        browse.clicked.connect(choose_collection)
        path_layout.addWidget(path_edit, 1)
        path_layout.addWidget(browse)
        form.addRow('图章集合', path_row)
        title_edit = QLineEdit(datetime.now().strftime('签名 %Y-%m-%d %H:%M'))
        form.addRow('图章名称', title_edit)
        layout.addLayout(form)
        hint = QLabel('可保持 PDF-XChange 打开，导入后检查图章面板是否自动更新。\n若未出现，可尝试重新打开面板或重启软件。已有图章会保留并自动备份。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('添加图章')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            result = append_stamp(wm, path_edit.text().strip(), title_edit.text().strip(), dpi=self._params.output_dpi)
            self._settings.setValue('pdfxchange/stamp_collection', str(result.collection_path))
            self.statusBar().showMessage('图章已写入集合 · 请检查 PDF-XChange 图章面板是否更新')
            QMessageBox.information(self, '图章已写入集合',
                f'已写入图章「{result.title}」，保留原有图章。\n请检查 PDF-XChange 图章面板是否自动更新；未出现时，可尝试重新打开面板或重启软件。\n\n备份：{result.backup_path}')
        except PermissionError:
            QMessageBox.warning(self, '图章添加失败',
                '图章文件被占用或目录无写入权限，本次未替换原文件。\n可关闭 PDF-XChange 后重试，或选择允许写入的图章集合。')
        except Exception as error:
            QMessageBox.warning(self, '图章添加失败', str(error))

    def _watermark_for_export(self):
        box = self._crop_pixels()
        if box is None:
            return None
        wm = render_watermark(self._page_img.crop(box), self._params)
        if wm.getchannel('A').getbbox() is None:
            QMessageBox.warning(self, '没有可导出的墨迹', '请调整裁剪范围或增大淡墨保留。')
            return None
        return wm

    def _copy_watermark(self):
        wm = self._watermark_for_export()
        if wm is None:
            return
        dpi = self._params.output_dpi
        try:
            if sys.platform == 'win32':
                # Qt 的透明 DIBV5 转换将 DPI 置零, 直接提供正确的 Windows 格式。
                set_windows_clipboard(clipboard_payloads(wm, dpi), int(self.winId()))
            else:
                QApplication.clipboard().setMimeData(self._watermark_mime_data(wm, dpi))
        except Exception as error:
            QMessageBox.warning(self, '复制失败', str(error))
            return
        width_mm, height_mm = (pixels / dpi * 25.4 for pixels in wm.size)
        self.statusBar().showMessage(f'已复制透明签名 · {dpi} DPI · {width_mm:.1f}×{height_mm:.1f} mm')

    @staticmethod
    def _watermark_mime_data(wm, dpi=DEFAULT_OUTPUT_DPI):
        png = BytesIO()
        export_png(wm, png, dpi=dpi)
        rgba = wm.convert('RGBA')
        data = rgba.tobytes('raw', 'RGBA')
        image = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
        dpm = round(dpi / .0254)
        image.setDotsPerMeterX(dpm)
        image.setDotsPerMeterY(dpm)
        mime = QMimeData()
        mime.setImageData(image)
        # 同时提供带 pHYs 的 PNG, 供接收软件选择保留透明度和尺寸的格式。
        mime.setData('image/png', png.getvalue())
        return mime

    # ---------- 导出 ----------

    def _export(self, kind: str):
        box = self._crop_pixels()
        if box is None:
            QMessageBox.warning(self, '未就绪', '请先打开文件并调整好裁剪框。')
            return
        try:
            wm = self._watermark_for_export()
            if wm is None:
                return
            default_dir = os.path.dirname(self._source_path)
            if self._phone_photo is not None or not self._source_path:
                default_dir = (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
                               or os.path.expanduser('~'))
            default_path = os.path.join(default_dir,
                                        os.path.splitext(self._source_name)[0] + '_签名')

            if kind == 'png':
                path, _ = QFileDialog.getSaveFileName(
                    self, '导出 PNG', default_path + '.png', 'PNG 图像 (*.png)')
                if not path:
                    return
                if not path.lower().endswith('.png'):
                    path += '.png'
                export_png(wm, path, dpi=self._params.output_dpi)
            else:
                path, _ = QFileDialog.getSaveFileName(
                    self, '导出 PDF', default_path + '.pdf', 'PDF 文档 (*.pdf)')
                if not path:
                    return
                if not path.lower().endswith('.pdf'):
                    path += '.pdf'
                export_pdf(wm, path, dpi=self._params.output_dpi)

            self.statusBar().showMessage(f'已导出: {path}')
        except Exception as e:
            QMessageBox.critical(self, '导出失败', str(e))

    # ---------- PIL -> QPixmap (RGB, bytes 命名引用防 GC) ----------

    @staticmethod
    def _pil_to_qpixmap(img: Image.Image) -> QPixmap:
        rgb = img.convert('RGB')
        data = rgb.tobytes('raw', 'RGB')  # 命名保存, 防 GC 悬垂
        qimg = QImage(data, rgb.width, rgb.height, rgb.width * 3,
                      QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg.copy())


def main():
    app = QApplication(sys.argv)
    app.setStyle(APP_STYLE)
    app.setFont(QFont(APP_FONT_FAMILY, 9))
    app.setWindowIcon(QIcon(str(APP_ICON)))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
