"""运行: python -m pytest -q tests/test_sigmarker.py。合成样本不包含个人签名。"""
import os
import ctypes
from io import BytesIO
import struct

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import cv2
import fitz
import numpy as np
import pytest
from PIL import Image
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl, QRectF, QSettings, QObject, QEvent, QBuffer, QIODevice
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QRegion
from PyQt6.QtWidgets import QApplication, QGraphicsScene, QPushButton, QDialog
from PyQt6.QtTest import QTest

from sigmarker.app import MainWindow, CropRectItem, ZoomableGraphicsView, APP_STYLE
from sigmarker.sigextract import WatermarkParams, detect_ink_bbox, export_pdf, export_png, render_watermark
from sigmarker.clipboard_image import clipboard_payloads, set_windows_clipboard


def signature_sample(ink=25, offset=(180, 150), shadow=False):
    h, w = 420, 720
    yy, xx = np.mgrid[:h, :w]
    paper = 245 - (70 * xx / w + 25 * yy / h if shadow else 0)
    rgb = np.repeat(np.broadcast_to(paper, (h, w))[..., None], 3, axis=2).astype(np.uint8)
    mask = np.zeros((h, w), np.uint8)
    ox, oy = offset
    for dx in (0, 65, 130):
        pts = np.array([(ox + dx, oy + 42), (ox + dx + 15, oy),
                        (ox + dx + 36, oy + 58), (ox + dx + 52, oy + 15)], np.int32)
        cv2.polylines(mask, [pts], False, 255, 3, cv2.LINE_AA)
    rgb[mask > 0] = ink
    return Image.fromarray(rgb), mask > 0


def mixed_ink_sample():
    pixels = np.full((240, 500, 3), 235, dtype=np.uint8)
    for y, tone in [(45, (25, 25, 25)), (95, (100, 100, 100)),
                    (145, (170, 170, 170)), (195, (130, 170, 205))]:
        cv2.line(pixels, (80, y), (420, y), tone, 4, cv2.LINE_AA)
    return Image.fromarray(pixels)


@pytest.mark.parametrize('offset,shadow,ink', [((180, 150), False, 25),
    ((4, 5), False, 60), ((510, 350), False, 45), ((180, 150), True, 100),
    ((180, 150), False, 215)])
def test_bounds_include_all_strokes(offset, shadow, ink):
    image, ink_mask = signature_sample(ink=ink, offset=offset, shadow=shadow)
    x0, y0, x1, y1 = detect_ink_bbox(np.array(image.convert('L')), padding=8)
    ys, xs = np.nonzero(ink_mask)
    assert x0 <= xs.min() and x1 > xs.max()
    assert y0 <= ys.min() and y1 > ys.max()
    assert x1 - x0 < 240 and y1 - y0 < 100


def test_desk_edge_and_distant_dust_excluded():
    image, _ = signature_sample(shadow=True)
    rgb = np.array(image)
    cv2.rectangle(rgb, (0, 0), (40, 419), (35, 35, 35), -1)
    cv2.line(rgb, (40, 0), (75, 419), (15, 15, 15), 2)
    cv2.circle(rgb, (650, 360), 2, (35, 35, 35), -1)
    box = detect_ink_bbox(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), padding=8)
    assert 150 < box[0] < 190 and box[2] < 400
    assert box[3] < 235


def test_shadow_background_transparent_and_faint_ink_kept():
    image, ink_mask = signature_sample(ink=125, shadow=True)
    rgba = np.array(render_watermark(image, WatermarkParams()))
    safe_background = cv2.dilate(ink_mask.astype(np.uint8), np.ones((9, 9), np.uint8)) == 0
    assert np.count_nonzero(rgba[..., 3][safe_background]) == 0
    assert np.percentile(rgba[..., 3][ink_mask], 50) > 160


@pytest.mark.parametrize('paper,shadow,contrast,color', [
    (245, False, 1, 'black'), (180, False, .5, 'black'),
    (180, True, .5, 'black'), (180, True, .5, 'original')])
def test_paper_correction_is_automatic_for_scans_and_dim_photos(paper, shadow, contrast, color):
    h, w = 250, 476
    yy, xx = np.mgrid[:h, :w]
    brightness = np.full((h, w), paper, dtype=float)
    if shadow:
        brightness -= 30 * xx / w + 15 * yy / h
    rgb = np.repeat(brightness[..., None], 3, axis=2).astype(np.uint8)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.polylines(mask, [np.array([(80, 160), (110, 70), (150, 170), (190, 90)], np.int32)],
                  False, 255, 4, cv2.LINE_AA)
    rgb[mask > 0] = (25, 45, 90)
    rgba = np.array(render_watermark(Image.fromarray(rgb), WatermarkParams(contrast=contrast, color_mode=color)))
    paper_only = cv2.dilate(mask, np.ones((9, 9), dtype=np.uint8)) == 0
    assert np.count_nonzero(rgba[..., 3][paper_only]) == 0
    assert np.percentile(rgba[..., 3][mask > 0], 50) > 160


@pytest.mark.parametrize('value', [0, 80, 190, 255])
@pytest.mark.parametrize('density', [1.0, 3.0])
def test_uniform_blank_is_fully_transparent(value, density):
    wm = render_watermark(Image.new('RGB', (300, 150), (value,) * 3), WatermarkParams(ink_density=density))
    assert wm.getchannel('A').getbbox() is None


def test_color_and_opacity_preserved_without_white_fringe():
    image, _ = signature_sample()
    rgb = np.array(image)
    cv2.line(rgb, (200, 250), (300, 260), (30, 70, 170), 5, cv2.LINE_AA)
    cv2.line(rgb, (350, 250), (450, 260), (180, 30, 25), 5, cv2.LINE_AA)
    full = np.array(render_watermark(Image.fromarray(rgb), WatermarkParams(color_mode='original')))
    half = np.array(render_watermark(Image.fromarray(rgb), WatermarkParams(opacity=.5)))
    assert full[255, 250, 2] > full[255, 250, 0] + 60
    assert full[255, 400, 0] > full[255, 400, 2] + 60
    assert half[..., 3].max() <= 128
    visible = full[..., 3] > 10
    assert np.percentile(full[..., :3][visible].min(axis=1), 99) < 5


@pytest.mark.parametrize('color', ['black', 'white', 'original'])
@pytest.mark.parametrize('opacity', [1.0, .5])
def test_ink_density_darkens_retained_strokes_and_keeps_paper_clear(color, opacity):
    image = mixed_ink_sample()
    baseline = np.array(render_watermark(image, WatermarkParams(color_mode=color, opacity=opacity)))
    previous = baseline
    paper = np.all(np.array(image) == 235, axis=2)
    for density in (1.5, 2.0, 3.0):
        boosted = np.array(render_watermark(image, WatermarkParams(
            color_mode=color, opacity=opacity, ink_density=density)))
        assert boosted.shape == baseline.shape
        assert np.array_equal(boosted[..., :3], baseline[..., :3])
        assert np.all(boosted[..., 3] >= previous[..., 3])
        assert boosted[145, 200, 3] > previous[145, 200, 3]
        assert np.count_nonzero(boosted[..., 3][paper]) == 0
        assert boosted[..., 3].max() <= round(255 * opacity)
        # 明暗笔画之外的细边缘仍有半透明层次。
        assert np.any((boosted[..., 3] > 0) & (boosted[..., 3] < round(255 * opacity)))
        previous = boosted
    if color == 'black':
        base_on_white = Image.new('RGBA', image.size, 'white')
        base_on_white.alpha_composite(Image.fromarray(baseline))
        dark_on_white = Image.new('RGBA', image.size, 'white')
        dark_on_white.alpha_composite(Image.fromarray(previous))
        assert dark_on_white.getpixel((200, 145))[0] < base_on_white.getpixel((200, 145))[0]


@pytest.mark.parametrize('dpi', [150, 300, 600])
def test_png_pdf_keep_alpha_and_same_physical_size(tmp_path, dpi):
    image, _ = signature_sample()
    wm = render_watermark(image, WatermarkParams())
    png, pdf = tmp_path / '签名.png', tmp_path / '签名.pdf'
    export_png(wm, str(png), dpi=dpi); export_pdf(wm, str(pdf), dpi=dpi)
    with Image.open(png) as reopened:
        assert reopened.mode == 'RGBA'
        assert np.array_equal(np.array(reopened), np.array(wm))
        assert reopened.info['dpi'] == pytest.approx((dpi, dpi), abs=.02)
    with fitz.open(pdf) as doc:
        assert len(doc) == 1
        assert doc[0].rect.width == pytest.approx(wm.width / dpi * 72, abs=.001)
        assert doc[0].rect.height == pytest.approx(wm.height / dpi * 72, abs=.001)
        assert doc[0].get_images()[0][1] > 0  # PDF SMask 透明蒙版
        pix = doc[0].get_pixmap(alpha=True)
        assert np.array(Image.frombytes('RGBA', (pix.width, pix.height), pix.samples))[0, 0, 3] == 0
    assert not list(tmp_path.glob('*.__tmp__*'))


@pytest.fixture(scope='module')
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle(APP_STYLE)
    return app


@pytest.fixture(autouse=True)
def isolate_user_settings(monkeypatch, tmp_path):
    monkeypatch.setattr('sigmarker.app.QSettings', lambda *args: QSettings(
        str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))


def test_ink_density_preview_export_persistence_and_reset(qt_app, tmp_path):
    path = tmp_path / 'tones.png'
    mixed_ink_sample().save(path)
    window = MainWindow()
    try:
        assert window.sld_ink_density.value() == 100 and window._params.ink_density == 1.0
        window._load_path(str(path))
        baseline = np.array(window._wm_image)
        window.sld_ink_density.setValue(200)
        QTest.qWait(100)  # 等待真实预览定时器, 不主动调用刷新掩盖信号连接错误。
        boosted = np.array(window._wm_image)
        assert np.any(boosted[..., 3] > baseline[..., 3])
        assert np.array_equal(boosted, np.array(window._watermark_for_export()))
        assert boosted[0, 0, 3] == 0
        assert window.lbl_ink_density_val.text() == '2.00×'
        assert window.chk_clear_bg.isChecked() and window.sld_opacity.value() == 100
        assert int(window._settings.value('ink_density_x100')) == 200
    finally:
        window.close()
    restored = MainWindow()
    try:
        assert restored.sld_ink_density.value() == 200 and restored._params.ink_density == 2.0
        restored._reset_params()
        assert restored.sld_ink_density.value() == 100 and restored._params.ink_density == 1.0
        assert int(restored._settings.value('ink_density_x100')) == 100
    finally:
        restored.close()


@pytest.mark.parametrize('dpi', [150, 300, 600])
def test_clipboard_formats_carry_dpi_alpha_and_pixels(qt_app, dpi):
    image = Image.new('RGBA', (476, 250))
    image.putpixel((200, 100), (0, 0, 0, 180))
    mime = MainWindow._watermark_mime_data(image, dpi)
    qt_image = mime.imageData()
    assert qt_image.width() == 476 and qt_image.height() == 250
    assert qt_image.dotsPerMeterX() * .0254 == pytest.approx(dpi, abs=.02)
    assert qt_image.dotsPerMeterY() * .0254 == pytest.approx(dpi, abs=.02)
    assert qt_image.pixelColor(0, 0).alpha() == 0
    assert qt_image.pixelColor(200, 100).alpha() == 180
    with Image.open(BytesIO(bytes(mime.data('image/png')))) as png:
        assert png.info['dpi'] == pytest.approx((dpi, dpi), abs=.02)
        assert np.array_equal(np.array(png), np.array(image))
        expected_width_mm = image.width / dpi * 25.4
        assert png.width / png.info['dpi'][0] * 25.4 == pytest.approx(expected_width_mm, abs=.01)
    # Windows DIB 的每米像素字段也必须携带密度, 不能只修 PNG 元数据。
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert qt_image.save(buffer, 'BMP')
    dpm_x, dpm_y = struct.unpack_from('<ii', bytes(buffer.data()), 38)
    assert dpm_x == dpm_y == round(dpi / .0254)


@pytest.mark.parametrize('legacy_disabled', ['false', False])
def test_legacy_disabled_paper_correction_is_migrated(qt_app, tmp_path, legacy_disabled):
    settings = QSettings(str(tmp_path / 'legacy.ini'), QSettings.Format.IniFormat)
    settings.setValue('flatten_bg', legacy_disabled)
    settings.setValue('contrast_x100', 50)
    settings.setValue('output_dpi', 600)
    path = tmp_path / 'photo.png'
    signature_sample(shadow=True)[0].save(path)
    window = MainWindow(settings=settings)
    try:
        assert not hasattr(window, 'chk_flatten_bg')
        assert not settings.contains('flatten_bg')
        assert window.sld_contrast.value() == 50 and window.spn_output_dpi.value() == 600
        window._load_path(str(path))
        rgba = np.array(window._watermark_for_export())
        assert rgba[0, 0, 3] == 0 and np.mean(rgba[..., 3] > 0) < .25
        assert rgba[..., 3].max() > 200
        window._reset_params()
        assert not settings.contains('flatten_bg')
    finally:
        window.close()


def test_copy_uses_selected_dpi_and_remembers_it(qt_app, tmp_path, monkeypatch):
    class CapturedClipboard:
        def setMimeData(self, mime):
            self.mime = mime

    clipboard = CapturedClipboard()
    monkeypatch.setattr('sigmarker.app.QApplication.clipboard', lambda: clipboard)
    native_copies = []
    monkeypatch.setattr('sigmarker.app.set_windows_clipboard', lambda payloads, hwnd: native_copies.append(payloads))
    path = tmp_path / 'source.png'
    signature_sample()[0].save(path)
    window = MainWindow()
    window._load_path(str(path))
    window.spn_output_dpi.setValue(600)
    window._copy_watermark()
    if os.name == 'nt':
        with Image.open(BytesIO(native_copies[0]['PNG'])) as copied:
            assert copied.info['dpi'] == pytest.approx((600, 600), abs=.02)
        assert struct.unpack_from('<ii', native_copies[0][17], 24) == (23622, 23622)
    else:
        assert clipboard.mime.imageData().dotsPerMeterX() * .0254 == pytest.approx(600, abs=.02)
    assert '600 DPI' in window.statusBar().currentMessage() and 'mm' in window.statusBar().currentMessage()
    window._refresh_preview()
    assert '600 DPI' in window.lbl_wm_info.text()
    window.close()
    restored = MainWindow()
    try:
        assert restored.spn_output_dpi.value() == 600
        assert restored._params.output_dpi == 600
    finally:
        restored.close()


@pytest.mark.parametrize('dpi', [150, 300, 600])
def test_windows_clipboard_png_and_dibv5_roundtrip(dpi):
    image = Image.new('RGBA', (476, 250))
    image.putpixel((100, 0), (20, 60, 170, 180))
    image.putpixel((300, 249), (180, 30, 10, 255))
    payloads = clipboard_payloads(image, dpi)
    dib = payloads[17]
    assert struct.unpack_from('<IiiHHI', dib) == (124, 476, 250, 1, 32, 3)
    assert struct.unpack_from('<ii', dib, 24) == (round(dpi / .0254),) * 2
    assert struct.unpack_from('<IIII', dib, 40) == (0xff0000, 0xff00, 0xff, 0xff000000)
    bmp = struct.pack('<2sIHHI', b'BM', 14 + len(dib), 0, 0, 138) + dib
    for encoded in (payloads['PNG'], bmp):
        with Image.open(BytesIO(encoded)) as decoded:
            assert np.array_equal(np.array(decoded), np.array(image))
            assert decoded.info['dpi'] == pytest.approx((dpi, dpi), abs=.02)


@pytest.mark.parametrize('failure', [None, 'busy', 'allocation', 'set'])
def test_native_clipboard_memory_ownership_without_system_clipboard(monkeypatch, failure):
    class FakeKernel:
        def __init__(self):
            self.buffers = {}
            self.freed = []

        def GlobalAlloc(self, flags, size):
            assert flags == 2
            if failure == 'allocation' and self.buffers:
                return 0
            handle = len(self.buffers) + 1
            self.buffers[handle] = ctypes.create_string_buffer(size)
            return handle

        def GlobalLock(self, handle):
            return ctypes.addressof(self.buffers[handle])

        def GlobalUnlock(self, handle):
            return 0

        def GlobalFree(self, handle):
            self.freed.append(handle)

    class FakeUser:
        def __init__(self):
            self.opened = self.emptied = self.closed = 0
            self.written = {}

        def RegisterClipboardFormatW(self, name):
            assert name == 'PNG'
            return 50000

        def OpenClipboard(self, owner):
            assert owner == 123
            self.opened += 1
            return failure != 'busy'

        def EmptyClipboard(self):
            self.emptied += 1
            return 1

        def SetClipboardData(self, format_id, handle):
            if failure == 'set' and format_id == 17:
                return 0
            self.written[format_id] = kernel.buffers[handle].raw
            return handle

        def CloseClipboard(self):
            self.closed += 1
            return 1

    kernel, user = FakeKernel(), FakeUser()
    monkeypatch.setattr('sigmarker.clipboard_image._win32_libraries', lambda: (user, kernel))
    if failure:
        with pytest.raises(OSError):
            set_windows_clipboard({'PNG': b'png', 17: b'dib'}, 123)
    else:
        set_windows_clipboard({'PNG': b'png', 17: b'dib'}, 123)
    if failure == 'allocation':
        assert user.opened == user.emptied == user.closed == 0 and kernel.freed == [1]
    elif failure == 'busy':
        assert user.emptied == user.closed == 0 and kernel.freed == [1, 2]
    else:
        assert user.emptied == user.closed == 1
        assert user.written[50000] == b'png'
        assert kernel.freed == ([2] if failure == 'set' else [])
        if failure is None:
            assert user.written[17] == b'dib'


def test_crop_paint_region_clears_old_handles_after_move(qt_app):
    class PaintProbe(QObject):
        def __init__(self):
            super().__init__()
            self.regions = []

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Paint:
                self.regions.append(QRegion(event.region()))
            return False

    scene = QGraphicsScene()
    scene.setSceneRect(0, 0, 800, 500)
    view = ZoomableGraphicsView(scene)
    view.resize(850, 550)
    item = CropRectItem(QRectF(100, 100, 200, 100), scene.sceneRect())
    scene.addItem(item)
    probe = PaintProbe()
    view.viewport().installEventFilter(probe)
    view.show()
    QTest.qWait(20)
    try:
        assert item.shape().contains(QPointF(103, 103))
        assert item.shape().contains(QPointF(95, 95))
        # 旧手柄左侧有实际绘制像素; 移动后该点必须进入清理区域。
        old_handle_pixel = view.mapFromScene(QPointF(95, 100))
        probe.regions.clear()
        item.setPos(350, 150)
        QTest.qWait(20)
        assert any(region.contains(old_handle_pixel) for region in probe.regions), \
            '移动时旧手柄像素未进入重绘区域，会遗留拖动残影'
        item.setPos(0, 0)
        QTest.qWait(20)
        old_right_handle_pixel = view.mapFromScene(QPointF(305, 150))
        probe.regions.clear()
        item.setRect(QRectF(100, 100, 100, 100))
        QTest.qWait(20)
        assert any(region.contains(old_right_handle_pixel) for region in probe.regions), \
            '缩小选框时旧手柄像素也必须被清理'
    finally:
        view.close()


def test_preview_drop_reload_close_and_center(qt_app, tmp_path):
    path = tmp_path / 'drop.png'
    signature_sample()[0].save(path)
    window = MainWindow()
    window.show(); qt_app.processEvents()
    view = window.view_page.viewport()
    try:
        buttons = [button.text() for button in window.findChildren(QPushButton)]
        assert '打开文件…' not in buttons
        assert '手动画框' not in buttons and '适应窗口' not in buttons
        assert sum('打开' in text for text in buttons) == 1
        file_actions = window.menuBar().actions()[0].menu().actions()
        assert any(action.shortcut().toString() == 'Ctrl+O' for action in file_actions)
        for size in [(1280, 860), (1000, 800)]:
            window.resize(*size); qt_app.processEvents()
            delta = window.btn_open.geometry().center() - view.rect().center()
            assert abs(delta.x()) <= 1 and abs(delta.y()) <= 1
        # 在远离打开按钮的视口左上角发送真实 Qt 拖放事件。
        mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(path))])
        enter = QDragEnterEvent(QPoint(15, 15), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, enter)
        assert enter.isAccepted()
        move = QDragMoveEvent(QPoint(15, 15), Qt.DropAction.CopyAction, mime,
                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, move)
        assert move.isAccepted()
        drop = QDropEvent(QPointF(15, 15), Qt.DropAction.CopyAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, drop)
        assert drop.isAccepted() and window._page_img is not None
        assert window._page_img.size == (720, 420)  # 不再盲目放大低分辨率图片
        window._load_path(str(path))  # 连续打开不访问已删除的裁剪 item
        assert not hasattr(window, 'cmb_preview_bg')
        assert path.name == window.lbl_file.text()
        assert path.name not in window.windowTitle()
        assert path.name not in window.statusBar().currentMessage()
        for preview, color in zip(window._preview_labels, [(48, 53, 62), (255, 255, 255)]):
            assert not preview.pixmap().isNull()
            pixel = preview.pixmap().toImage().pixelColor(5, 5)
            assert (pixel.red(), pixel.green(), pixel.blue()) == color
        assert window.lbl_wm_dark.pixmap().size() == window.lbl_wm_white.pixmap().size()
        window._on_crop_changed(); window._close_file(); qt_app.processEvents()
        assert window._page_img is None and window._crop_item is None
        assert window.btn_open.isVisible() and not window.btn_export_png.isEnabled()
        assert not window.btn_add_stamp.isEnabled()
        assert all(preview.pixmap() is None or preview.pixmap().isNull() for preview in window._preview_labels)
        assert not window._preview_timer.isActive()
        window._load_path(str(path))
        assert window.btn_export_png.isEnabled()
    finally:
        window.close()


def test_rotate_buttons_follow_labels(qt_app):
    window = MainWindow()
    image, _ = signature_sample()
    window._page_img = image
    window._rotate_page(90)
    assert np.array_equal(np.array(window._page_img), np.array(image.rotate(-90, expand=True)))
    window.close()


def test_stamp_import_does_not_require_editor_exit(qt_app, tmp_path, monkeypatch):
    from tests.test_stamp_import import make_collection
    from pypdf import PdfReader

    path = tmp_path / 'source.png'
    signature_sample()[0].save(path)
    collection = tmp_path / '测试集合.pdf'
    make_collection(collection)
    original = collection.read_bytes()
    window = MainWindow()
    window._load_path(str(path))
    window._settings.setValue('pdfxchange/stamp_collection', str(collection))
    messages = []
    monkeypatch.setattr('sigmarker.app.QDialog.exec', lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr('subprocess.run', lambda *args, **kwargs: pytest.fail('Import must not query or control editor processes'))
    monkeypatch.setattr('sigmarker.app.QMessageBox.information', lambda *args: messages.append(args[2]))
    try:
        window._add_stamp()
        assert len(PdfReader(collection).pages) == 3
        backups = list((tmp_path / 'SigMarker-backups').glob('*.bak'))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert len(messages) == 1 and '是否自动更新' in messages[0]
        assert '已写入集合' in window.statusBar().currentMessage()
    finally:
        window.close()


@pytest.mark.parametrize('maximized', [False, True])
def test_window_layout_restored_after_restart(qt_app, maximized):
    window = MainWindow()
    window.resize(760, 740)
    window.show()
    qt_app.processEvents()
    window.splitter.setSizes([350, 270])
    qt_app.processEvents()
    expected_size = window.size()
    expected_split = window.splitter.sizes()
    if maximized:
        window.showMaximized()
        qt_app.processEvents()
    window.close()
    restored = MainWindow()
    restored.show()
    qt_app.processEvents()
    try:
        assert restored.isMaximized() == maximized
        if maximized:
            restored.showNormal()
            qt_app.processEvents()
        assert restored.size() == expected_size
        actual_split = restored.splitter.sizes()
        assert abs(actual_split[0] / sum(actual_split) - expected_split[0] / sum(expected_split)) < .02
    finally:
        restored.close()
