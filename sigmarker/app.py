"""SigMarker — 签名水印提取小工具

Usage: python main.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from PyQt6.QtCore import Qt, QRectF, QPointF, QSettings, QTimer, QEvent, pyqtSignal, QMimeData
from PyQt6.QtGui import (
    QAction, QPixmap, QImage, QPainter, QPen, QColor, QBrush, QPainterPath,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QSlider, QCheckBox, QComboBox, QGroupBox,
    QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsPixmapItem, QStatusBar, QSizePolicy,
    QSplitter, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QSpinBox,
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

# 支持拖入/打开的扩展名
_SUPPORTED_EXTS = ('.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff')
APP_STYLE = 'windows11'


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


# ---------- 主窗口 ----------

class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings if settings is not None else QSettings('SigMarker', 'SigMarker')
        self.setWindowTitle('SigMarker — 签名水印提取')
        self.resize(1280, 860)

        self._page_img: Image.Image | None = None
        self._pix_item: QGraphicsPixmapItem | None = None
        self._crop_item: CropRectItem | None = None
        self._source_path = ''
        self._wm_image = None
        self._params = self._load_params()   # 上次会话的参数 (无则用默认)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(70)
        self._preview_timer.timeout.connect(self._refresh_preview)

        self._build_ui()
        self._build_actions()
        self.setStatusBar(QStatusBar())

        # 拖放打开
        self.setAcceptDrops(True)
        # 未打开文件时的居中"打开"按钮 (叠在预览区上)
        self.btn_open = QPushButton('打开 PDF 或图片…\n也可拖入整个文档预览区', self.view_page.viewport())
        self.btn_open.setFixedSize(220, 60)
        self.btn_open.clicked.connect(self._open_file)
        self.btn_open.setVisible(self._page_img is None)
        self.view_page.viewportResized.connect(self._position_open_btn)
        self.view_page.fileDropped.connect(self._load_path)
        self._set_document_enabled(False)
        self._restore_window_layout()
        self._position_open_btn()
        QTimer.singleShot(0, self._position_open_btn)

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
        self._position_open_btn()

    def _position_open_btn(self):
        if not hasattr(self, 'btn_open'):
            return
        vw = self.view_page.viewport().width()
        vh = self.view_page.viewport().height()
        self.btn_open.move(max((vw - self.btn_open.width()) // 2, 0),
                           max((vh - self.btn_open.height()) // 2, 0))

    # ---------- UI ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        # 只弱化分区容器边框，保留原生控件及裁剪框的交互提示。
        central.setStyleSheet('''
            QGroupBox {
                border: 1px solid #d5d9df;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 4px;
            }
        ''')

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # === 左栏: 上下堆叠 ===
        left_col = QWidget()
        left_col_layout = QVBoxLayout(left_col)
        left_col_layout.setContentsMargins(0, 0, 0, 0)
        left_col_layout.setSpacing(8)

        file_row = QHBoxLayout()
        self.lbl_file = QLabel('未打开文件 · 拖入 PDF / 图片开始')
        self.lbl_file.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.btn_close_file = QPushButton('关闭文件')
        self.btn_close_file.setToolTip('关闭当前文件，保留软件和参数 (Ctrl+W)')
        self.btn_close_file.clicked.connect(self._close_file)
        file_row.addWidget(self.lbl_file, 1)
        file_row.addWidget(self.btn_close_file)
        left_col_layout.addLayout(file_row)

        # 左上: 文档预览 + 裁剪框
        self.scene = QGraphicsScene()
        self.view_page = ZoomableGraphicsView(self.scene)
        self.view_page.setStyleSheet('QGraphicsView { border: 1px solid #d5d9df; }')
        # 灰色底衬, 让白纸页面有清晰边界
        self.view_page.setBackgroundBrush(QBrush(QColor(176, 176, 176)))
        self.view_page.setMinimumHeight(300)
        self.view_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        top_box = QGroupBox('文档预览 (滚轮缩放, 拖边框/手柄调整裁剪区)')
        top_layout = QVBoxLayout(top_box)
        top_layout.addWidget(self.view_page)

        # 两个底色使用同一份水印和相同缩放, 可以直接比较透明边缘及白纸效果。
        bottom_box = QGroupBox('水印预览 · 深灰 / 白底对照')
        bottom_layout = QVBoxLayout(bottom_box)
        preview_row = QHBoxLayout()
        preview_labels = []
        for title, color, text_color, border_color in [
                ('深灰 · 检查白边', '#30353e', '#e2e7ee', '#48515e'),
                ('白底 · 盖章效果', '#ffffff', '#596573', '#d5d9df')]:
            column = QVBoxLayout()
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(heading)
            preview = QLabel('请先打开 PDF 或图片')
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumHeight(160)
            preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            preview.setStyleSheet(
                f'border: 1px solid {border_color}; background-color: {color}; color: {text_color};')
            column.addWidget(preview, 1)
            preview_row.addLayout(column, 1)
            preview_labels.append(preview)
        self.lbl_wm_dark, self.lbl_wm_white = preview_labels
        self._preview_labels = tuple(preview_labels)
        bottom_layout.addLayout(preview_row)
        self.lbl_wm_info = QLabel('底色仅用于预览，导出仍为透明背景。')
        self.lbl_wm_info.setWordWrap(True)
        bottom_layout.addWidget(self.lbl_wm_info)
        for preview in self._preview_labels:
            preview.installEventFilter(self)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(top_box)
        self.splitter.addWidget(bottom_box)
        self.splitter.setSizes([520, 260])
        left_col_layout.addWidget(self.splitter, 1)

        # === 右栏: 参数 ===
        right_box = QGroupBox('参数调整')
        right_box.setFixedWidth(300)
        right_layout = QVBoxLayout(right_box)
        right_layout.setSpacing(6)

        self.lbl_bbox = QLabel('裁剪框: —')
        right_layout.addWidget(self.lbl_bbox)

        right_layout.addWidget(self._mk_label('淡墨保留 (越大保留越多)'))
        self.sld_threshold = QSlider(Qt.Orientation.Horizontal)
        self.sld_threshold.setRange(60, 200)
        self.sld_threshold.setValue(self._params.threshold)
        self.lbl_threshold_val = QLabel(str(self._params.threshold))
        self.sld_threshold.valueChanged.connect(self._on_params_changed)
        self.sld_threshold.valueChanged.connect(
            lambda v: self.lbl_threshold_val.setText(str(v)))
        right_layout.addWidget(self._slider_row(self.sld_threshold, self.lbl_threshold_val))

        right_layout.addWidget(self._mk_label('对比度'))
        self.sld_contrast = QSlider(Qt.Orientation.Horizontal)
        self.sld_contrast.setRange(50, 200)
        self.sld_contrast.setValue(int(self._params.contrast * 100))
        self.lbl_contrast_val = QLabel(f'{self._params.contrast:.2f}')
        self.sld_contrast.valueChanged.connect(self._on_params_changed)
        self.sld_contrast.valueChanged.connect(
            lambda v: self.lbl_contrast_val.setText(f'{v/100:.2f}'))
        right_layout.addWidget(self._slider_row(self.sld_contrast, self.lbl_contrast_val))

        right_layout.addWidget(self._mk_label('墨迹浓度 (越大越深)'))
        self.sld_ink_density = QSlider(Qt.Orientation.Horizontal)
        self.sld_ink_density.setRange(100, 300)
        self.sld_ink_density.setValue(int(round(self._params.ink_density * 100)))
        self.sld_ink_density.setToolTip('1.00× 保持原效果；向右加深已提取的笔画，透明背景不变。')
        self.lbl_ink_density_val = QLabel(f'{self._params.ink_density:.2f}×')
        self.sld_ink_density.valueChanged.connect(self._on_params_changed)
        self.sld_ink_density.valueChanged.connect(
            lambda v: self.lbl_ink_density_val.setText(f'{v/100:.2f}×'))
        right_layout.addWidget(self._slider_row(self.sld_ink_density, self.lbl_ink_density_val))

        right_layout.addWidget(self._mk_label('不透明度'))
        self.sld_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sld_opacity.setRange(10, 100)
        self.sld_opacity.setValue(int(self._params.opacity * 100))
        self.lbl_opacity_val = QLabel(f'{self._params.opacity:.2f}')
        self.sld_opacity.valueChanged.connect(self._on_params_changed)
        self.sld_opacity.valueChanged.connect(
            lambda v: self.lbl_opacity_val.setText(f'{v/100:.2f}'))
        right_layout.addWidget(self._slider_row(self.sld_opacity, self.lbl_opacity_val))

        right_layout.addWidget(self._mk_label('锐利度 (手机拍照推荐)'))
        self.sld_sharpness = QSlider(Qt.Orientation.Horizontal)
        self.sld_sharpness.setRange(0, 200)
        self.sld_sharpness.setValue(int(self._params.sharpness * 100))
        self.lbl_sharpness_val = QLabel(f'{self._params.sharpness:.2f}')
        self.sld_sharpness.valueChanged.connect(self._on_params_changed)
        self.sld_sharpness.valueChanged.connect(
            lambda v: self.lbl_sharpness_val.setText(f'{v/100:.2f}'))
        right_layout.addWidget(self._slider_row(self.sld_sharpness, self.lbl_sharpness_val))

        right_layout.addWidget(self._mk_label('墨色'))
        self.cmb_color = QComboBox()
        self.cmb_color.addItems(['黑色 (默认)', '白色', '原彩'])
        self.cmb_color.setCurrentIndex(
            {'black': 0, 'white': 1, 'original': 2}.get(self._params.color_mode, 0))
        self.cmb_color.currentIndexChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.cmb_color)

        self.chk_clear_bg = QCheckBox('自动清除近白背景')
        self.chk_clear_bg.setChecked(self._params.clear_background)
        self.chk_clear_bg.stateChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.chk_clear_bg)

        self.chk_denoise = QCheckBox('清除孤立噪点')
        self.chk_denoise.setChecked(self._params.denoise)
        self.chk_denoise.stateChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.chk_denoise)

        right_layout.addWidget(self._mk_label('粘贴 / 导出 DPI'))
        self.spn_output_dpi = QSpinBox()
        self.spn_output_dpi.setRange(72, 1200)
        self.spn_output_dpi.setSingleStep(50)
        self.spn_output_dpi.setSuffix(' DPI')
        self.spn_output_dpi.setValue(self._params.output_dpi)
        self.spn_output_dpi.setToolTip('DPI 越大，粘贴和导出的物理尺寸越小；像素和清晰度不变。')
        self.spn_output_dpi.valueChanged.connect(self._on_params_changed)
        right_layout.addWidget(self.spn_output_dpi)

        btn_reset = QPushButton('恢复默认参数')
        btn_reset.clicked.connect(self._reset_params)
        right_layout.addWidget(btn_reset)

        right_layout.addStretch(1)

        # 旋转按钮组 (顺时针/逆时针 90°)
        rot_row = QWidget()
        rot_lay = QHBoxLayout(rot_row)
        rot_lay.setContentsMargins(0, 0, 0, 0)
        self.btn_rot_ccw = QPushButton('↺ 逆时针 90°')
        self.btn_rot_ccw.clicked.connect(lambda: self._rotate_page(-90))
        self.btn_rot_cw = QPushButton('↻ 顺时针 90°')
        self.btn_rot_cw.clicked.connect(lambda: self._rotate_page(90))
        rot_lay.addWidget(self.btn_rot_ccw)
        rot_lay.addWidget(self.btn_rot_cw)
        right_layout.addWidget(rot_row)

        self.btn_auto = QPushButton('自动检测签名范围')
        self.btn_auto.clicked.connect(self._auto_detect_bbox)
        right_layout.addWidget(self.btn_auto)

        self.btn_copy = QPushButton('复制透明签名')
        self.btn_copy.clicked.connect(self._copy_watermark)
        right_layout.addWidget(self.btn_copy)

        self.btn_export_png = QPushButton('导出透明 PNG')
        self.btn_export_png.clicked.connect(lambda: self._export('png'))
        right_layout.addWidget(self.btn_export_png)

        self.btn_export_pdf = QPushButton('导出 PDF')
        self.btn_export_pdf.clicked.connect(lambda: self._export('pdf'))
        right_layout.addWidget(self.btn_export_pdf)

        self.btn_add_stamp = QPushButton('添加到 PDF-XChange 图章…')
        self.btn_add_stamp.setToolTip('将当前签名追加到图章集合并备份；允许运行中尝试导入，面板是否自动更新需实测。')
        self.btn_add_stamp.clicked.connect(self._add_stamp)
        right_layout.addWidget(self.btn_add_stamp)

        # === 拼起来 ===
        main_layout.addWidget(left_col, 1)
        main_layout.addWidget(right_box, 0)

    def _mk_label(self, text):
        l = QLabel(text)
        l.setStyleSheet('font-weight: bold; margin-top: 6px;')
        return l

    def _slider_row(self, slider: QSlider, val_label: QLabel) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(slider, 1)
        lay.addWidget(val_label)
        return w

    # ---------- 菜单栏 ----------

    def _build_actions(self):
        menu_file = self.menuBar().addMenu('文件')

        act_open = QAction('打开…', self)
        act_open.setShortcut('Ctrl+O')
        act_open.triggered.connect(self._open_file)
        menu_file.addAction(act_open)

        self.act_close_file = QAction('关闭当前文件', self)
        self.act_close_file.setShortcut('Ctrl+W')
        self.act_close_file.triggered.connect(self._close_file)
        menu_file.addAction(self.act_close_file)

        act_quit = QAction('退出', self)
        act_quit.setShortcut('Ctrl+Q')
        act_quit.triggered.connect(self.close)
        menu_file.addAction(act_quit)

    # ---------- 打开文件: PDF 或图片 ----------

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择签名扫描件', '',
            '所有支持 (*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff);;'
            'PDF 文件 (*.pdf);;'
            '图片文件 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)')
        if not path:
            return
        self._load_path(path)

    def _load_path(self, path: str):
        try:
            if path.lower().endswith('.pdf'):
                page_img = load_pdf_page(path, dpi=300)
            else:
                with Image.open(path) as original:
                    oriented = ImageOps.exif_transpose(original)
                    # 透明 PNG 的透明区按白纸处理, 避免透明黑色像素被当成墨迹。
                    rgba = oriented.convert('RGBA')
                    page_img = Image.new('RGB', rgba.size, 'white')
                    page_img.paste(rgba, mask=rgba.getchannel('A'))
        except Exception as e:
            QMessageBox.critical(self, '加载失败', str(e))
            return

        self._preview_timer.stop()
        self._page_img = page_img
        self._source_path = os.path.abspath(path)
        self._wm_image = None

        # 自动方向检测: 竖排签名旋转 90° 变横排 (rotate(90)=逆时针)
        gray = np.asarray(self._page_img.convert('L')).astype(np.float32)
        if detect_signature_orientation(gray):
            self._page_img = self._page_img.rotate(90, expand=True)
            self.statusBar().showMessage('检测到竖排签名, 已自动旋转 90° (如方向反了用右侧按钮翻转)')

        pm = self._pil_to_qpixmap(self._page_img)
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

        self._auto_detect_bbox()
        self.view_page.fitInView(self._pix_item, Qt.AspectRatioMode.KeepAspectRatio)
        self.btn_open.setVisible(False)
        self.lbl_file.setText(os.path.basename(path))
        self.lbl_file.setToolTip(self._source_path)
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
        self._crop_item = None
        self._pix_item = None
        self._page_border = None
        self._page_img = None
        self._wm_image = None
        self._source_path = ''
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.view_page.resetTransform()
        self.lbl_file.setText('未打开文件 · 拖入 PDF / 图片开始')
        self.lbl_file.setToolTip('')
        for preview in self._preview_labels:
            preview.clear()
            preview.setText('请打开文件，或拖入文档预览区')
        self.lbl_wm_info.setText('底色仅用于预览，导出仍为透明背景。')
        self._update_bbox_label()
        self.btn_open.show()
        self._position_open_btn()
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
            self.lbl_bbox.setText('裁剪框: —')
            return
        sr = self._crop_item.scene_rect()
        self.lbl_bbox.setText(
            f'裁剪框: ({int(sr.left())},{int(sr.top())}) {int(sr.width())}×{int(sr.height())}')

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
        """顺时针旋转 degrees (正=顺时针, 负=逆时针)。重建 scene + 重新检测 bbox。"""
        if self._page_img is None:
            return
        # 只允许 90/180/270 度, 其他角度用 PIL 4 次采样避免插值糊
        valid = abs(degrees) % 360 in (0, 90, 180, 270)
        if not valid:
            degrees = round(degrees / 90) * 90
        if degrees % 360 == 0:
            return
        self._page_img = self._page_img.rotate(-degrees, expand=True, fillcolor='white')

        pm = self._pil_to_qpixmap(self._page_img)
        self._crop_item = None   # scene.clear() 会删掉它, 先断引用
        self.scene.clear()
        self._pix_item = self.scene.addPixmap(pm)
        self._pix_item.setZValue(-1)
        self._page_border = self.scene.addRect(
            self._pix_item.sceneBoundingRect(), QPen(QColor(90, 90, 90), 0))
        self.scene.setSceneRect(self._pix_item.sceneBoundingRect())

        self._auto_detect_bbox()
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
            output_dpi=self.spn_output_dpi.value(),
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
        self.spn_output_dpi.setValue(d.output_dpi)

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
        avail_w = max(min(preview.width() for preview in self._preview_labels) - 24, 10)
        avail_h = max(min(preview.height() for preview in self._preview_labels) - 24, 10)
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
            default_path = os.path.join(os.path.dirname(self._source_path),
                                        os.path.splitext(os.path.basename(self._source_path))[0] + '_签名')

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
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
