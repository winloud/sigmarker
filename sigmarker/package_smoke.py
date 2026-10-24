"""成品后台自检: SigMarker.exe --self-test <报告.json>, 不显示桌面窗口。"""

import json
import os
from io import BytesIO
from pathlib import Path
import struct
import sys
import tempfile
import traceback


def qr_matches(image, expected):
    """双识别器校验；默认 OpenCV 检测器会漏检部分合法的二维码图案。"""
    import cv2
    for detector_class in (cv2.QRCodeDetector, getattr(cv2, 'QRCodeDetectorAruco', None)):
        if detector_class is not None and detector_class().detectAndDecode(image)[0] == expected:
            return True
    return False


def run(report_path=None):
    from .metadata import APP_VERSION
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    report['version'] = APP_VERSION
    destination = Path(report_path) if report_path else Path(sys.executable).with_name('self-test.json')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    window = None
    settings_folder = None
    try:
        import cv2
        import fitz
        import numpy as np
        import pypdf
        import qrcode
        import pi_heif
        import _pi_heif
        from PIL import Image
        from PyQt6 import QtCore, QtNetwork
        from PyQt6.QtCore import QPointF, QRectF, QSettings
        from PyQt6.QtWidgets import QApplication
        from .app import MainWindow, APP_STYLE, APP_FONT_FAMILY
        from PyQt6.QtGui import QFont, QFontDatabase
        from .sigextract import export_pdf, export_png

        report['checks'].append('Qt/QtNetwork/OpenCV/numpy/Pillow/PDF/pypdf/qrcode/pi-heif imports')
        report['qt_version'] = QtCore.qVersion()
        report['native_versions'] = {'opencv': cv2.__version__, 'mupdf': fitz.version[1],
                                     'heif': pi_heif.libheif_info()}
        report['module_paths'] = {name: module.__file__ for name, module in (
            ('QtCore', QtCore), ('QtNetwork', QtNetwork), ('cv2', cv2), ('numpy', np),
            ('Pillow', Image), ('fitz', fitz), ('pypdf', pypdf), ('qrcode', qrcode),
            ('pi_heif', pi_heif), ('_pi_heif', _pi_heif))}
        if report['frozen']:
            bundle = Path(sys._MEIPASS).resolve()
            assert all(Path(path).resolve().is_relative_to(bundle)
                       for path in report['module_paths'].values()), 'Runtime escaped the bundle'
            product_folder = Path(sys.executable).parent
            for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'SOURCE_CODE.md',
                         'requirements-lock.txt', 'CHANGELOG.md'):
                assert (product_folder / name).is_file(), f'Missing release document: {name}'
            assert (product_folder / 'licenses' / 'GPL-3.0-only.txt').is_file()
            report['checks'].append('Release version and bundled license/source notices')

        app = QApplication.instance() or QApplication([])
        assert app.platformName() == 'offscreen'
        if sys.platform == 'win32' and not QFontDatabase.families():
            assert QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR']) / 'Fonts' / 'msyh.ttc')) >= 0
        app.setStyle(APP_STYLE)
        app.setFont(QFont(APP_FONT_FAMILY, 9))
        assert app.style().objectName().lower() == APP_STYLE
        report['app_style'] = app.style().objectName()
        settings_folder = tempfile.TemporaryDirectory(prefix='sigmarker-settings-')
        settings = QSettings(str(Path(settings_folder.name) / 'settings.ini'), QSettings.Format.IniFormat)
        settings.setValue('flatten_bg', 'false')  # 模拟旧版记住的关闭状态。
        window = MainWindow(settings=settings)
        app.processEvents()
        assert not settings.contains('flatten_bg') and not hasattr(window, 'chk_flatten_bg')
        assert not window.windowIcon().isNull()
        assert window.btn_auto.parentWidget() is window.canvas_toolbar
        assert window.btn_open.parentWidget() is window.btn_phone_import.parentWidget()
        for button in (window.btn_rot_ccw, window.btn_rot_cw, window.btn_auto,
                       window.btn_close_file, window.btn_open, window.btn_phone_import):
            assert not button.icon().pixmap(32, 32).isNull(), button.text()
        report['checks'].append('Offscreen application and main window construction')
        report['checks'].append('Bundled S icon, canvas toolbar and grouped import actions')
        report['checks'].append('Toolbar and import vector icons rendered through the SVG plugin')

        with tempfile.TemporaryDirectory(prefix='sigmarker-smoke-') as folder:
            work = Path(folder)
            source = work / '合成签名.png'
            yy, xx = np.mgrid[:240, :480]
            paper = (190 - 30 * xx / 480 - 15 * yy / 240).astype(np.uint8)
            pixels = np.repeat(paper[..., None], 3, axis=2)
            for offset in (0, 65, 130):
                points = np.array([(100 + offset, 140), (115 + offset, 80),
                                   (136 + offset, 155), (153 + offset, 100)], dtype=np.int32)
                cv2.polylines(pixels, [points], False, (25, 25, 25), 3, cv2.LINE_AA)
            Image.fromarray(pixels).save(source)
            window._load_path(str(source))
            assert window._page_img is not None and window._wm_image is not None
            box = window._crop_pixels()
            assert box and box[0] > 0 and box[2] < 480
            window.sld_contrast.setValue(50)
            window._refresh_preview()
            alpha = np.array(window._wm_image)[..., 3]
            assert alpha[0, 0] == 0 and np.mean(alpha > 0) < .35 and alpha.max() > 200
            report['checks'].append('Automatic gray-paper/shadow correction and legacy disabled-state migration')
            window._load_path(str(source))
            report['checks'].append('Image open, signature detection, repeated open')

            # 淡墨与深墨混合, 确保浓度控制实际加深淡墨而非被归一化抵消。
            tones = np.full((240, 500, 3), 235, dtype=np.uint8)
            for y, tone in ((45, 25), (95, 100), (145, 170), (195, 205)):
                cv2.line(tones, (80, y), (420, y), (tone, tone, tone), 4, cv2.LINE_AA)
            density_source = work / '淡墨测试.png'
            Image.fromarray(tones).save(density_source)
            window._load_path(str(density_source))
            baseline_alpha = np.array(window._wm_image)[..., 3]
            window.spn_ink_density.setValue(2.0)
            window._refresh_preview()
            boosted_alpha = np.array(window._wm_image)[..., 3]
            assert np.any(boosted_alpha > baseline_alpha) and boosted_alpha[0, 0] == 0
            assert window._params.ink_density == 2.0
            report['checks'].append('Ink density boosts retained strokes after background removal without opaque paper')
            window._load_path(str(source))

            for preview in window._preview_labels:
                assert not preview.pixmap().isNull()
            assert window.lbl_wm_dark.pixmap().size() == window.lbl_wm_white.pixmap().size()
            assert source.name not in window.windowTitle()
            assert source.name not in window.statusBar().currentMessage()
            item = window._crop_item
            item._press_rect = QRectF(item.rect())
            item._press_pos = QPointF(item.pos())
            item._apply_move(QPointF(5, 5))
            item._press_rect = QRectF(item.rect())
            item._press_pos = QPointF(item.pos())
            item._mode = 'br'
            item._apply_resize(QPointF(-5, -5))
            assert item.boundingRect().contains(item.rect().adjusted(-6, -6, 6, 6))
            window._refresh_preview()
            window._rotate_page(90)
            report['checks'].append('Simultaneous dark/white previews, single filename, crop move/resize, rotation')

            watermark = window._watermark_for_export()
            assert watermark is not None and watermark.getchannel('A').getbbox()
            dpi = 600
            window.cmb_output_dpi.activated.emit(window.cmb_output_dpi.findData(dpi))
            assert window.cmb_output_dpi.currentText() == str(dpi)
            window._refresh_preview()
            assert '600 DPI' in window.lbl_output_size.text() and 'mm' in window.lbl_output_size.text()
            assert window.lbl_output_size.parentWidget() is window.statusBar()
            from .clipboard_image import clipboard_payloads, _win32_libraries
            payloads = clipboard_payloads(watermark, dpi)
            assert struct.unpack_from('<ii', payloads[17], 24) == (round(dpi / .0254),) * 2
            dib = payloads[17]
            bmp = struct.pack('<2sIHHI', b'BM', 14 + len(dib), 0, 0, 138) + dib
            for encoded in (payloads['PNG'], bmp):
                with Image.open(BytesIO(encoded)) as image:
                    assert image.info['dpi'] == (dpi, dpi) or all(abs(v - dpi) < .02 for v in image.info['dpi'])
                    assert np.array_equal(np.array(image), np.array(watermark))
            if sys.platform == 'win32':
                _win32_libraries()  # 仅加载并检查 API 签名, 不读写系统剪贴板。
            report['checks'].append('Clipboard PNG/DIBV5 DPI and exact RGBA roundtrip without touching system clipboard')
            png, pdf = work / '签名.png', work / '签名.pdf'
            export_png(watermark, str(png), dpi=dpi)
            export_pdf(watermark, str(pdf), dpi=dpi)
            with Image.open(png) as image:
                assert image.mode == 'RGBA' and image.getchannel('A').getextrema()[0] == 0
                assert all(abs(v - dpi) < .02 for v in image.info['dpi'])
            with fitz.open(pdf) as document:
                assert document[0].get_images()[0][1] > 0
                assert abs(document[0].rect.width - watermark.width * 72 / dpi) < .001
                assert abs(document[0].rect.height - watermark.height * 72 / dpi) < .001
            report['checks'].append('Transparent PNG/PDF export with selected DPI and matching physical size')
            from .stamp_import import append_stamp, _entries, _page_map
            from pypdf import PdfReader, PdfWriter
            from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject
            # 只操作临时合成集合, 不接触用户安装目录中的图章。
            collection = work / '测试图章集合.pdf'
            stamp_writer = PdfWriter(clone_from=str(pdf))
            stamp_writer.metadata = {'/Title': '后台测试图章'}
            stamp_writer._root_object[NameObject('/Names')] = DictionaryObject({
                NameObject('/Pages'): DictionaryObject({NameObject('/Names'): ArrayObject([
                    TextStringObject('#existing=原有测试图章'), stamp_writer.pages[0].indirect_reference])})})
            with collection.open('wb') as stream:
                stamp_writer.write(stream)
            original_collection = collection.read_bytes()
            imported = append_stamp(watermark, collection, '新增测试签名', dpi=dpi)
            assert imported.backup_path.read_bytes() == original_collection
            stamp_reader = PdfReader(collection)
            mapping = _page_map(stamp_reader, _entries(stamp_reader.trailer['/Root']))
            assert len(stamp_reader.pages) == 2 and mapping['#existing=原有测试图章'] == 0
            assert abs(float(stamp_reader.pages[1].mediabox.width) - watermark.width * 72 / dpi) < .001
            report['checks'].append('PDF-XChange stamp append, registry preservation, exact backup on synthetic collection')
            window._close_file()
            assert window._page_img is None and window._crop_item is None
            assert not window.lbl_output_size.text()
            window._load_path(str(source))
            assert window._wm_image is not None
            report['checks'].append('Close document and reopen')

            # 自检只绑定回环地址，不暴露局域网端口，也不操作系统剪贴板。
            from http.client import HTTPConnection
            import time
            from .image_io import load_image
            from .phone_dialog import PhoneImportDialog, qr_image
            fixture = Path(__file__).with_name('assets') / 'synthetic.heic'
            decoded, actual_format = load_image(fixture, phone=True)
            assert actual_format == 'HEIF' and decoded.size == (160, 96)
            assert np.mean(decoded.crop((8, 8, 30, 30))) < 20
            window._close_file()
            dialog = PhoneImportDialog(window, address_provider=lambda: [('self-test', '127.0.0.1')])
            window._phone_dialog = dialog
            dialog.finished.connect(lambda: setattr(window, '_phone_dialog', None))
            receiver = dialog.server
            assert receiver is not None
            assert qr_matches(np.array(qr_image(receiver.url)), receiver.url)
            connection = HTTPConnection(*receiver._server.server_address, timeout=5)
            try:
                connection.request('POST', '/api/upload', body=fixture.read_bytes(),
                                   headers={'Authorization': 'Bearer ' + receiver.token})
                response = connection.getresponse()
                assert response.status == 200 and json.loads(response.read())['received']
            finally:
                connection.close()
            deadline = time.monotonic() + 5
            while window._phone_photo is None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            assert window._phone_photo is not None and window._wm_image is not None
            received_path = window._phone_photo.path
            assert received_path.exists()
            receiver._thread.join(2)
            deadline = time.monotonic() + 2
            while receiver._server.fileno() != -1 and time.monotonic() < deadline:
                time.sleep(.01)
            assert not receiver._thread.is_alive() and receiver._server.fileno() == -1
            window._close_file()
            assert not received_path.exists()
            report['checks'].append('HEIC native decode, QR roundtrip, loopback upload, queued Qt auto-load, listener shutdown and temporary-file cleanup')

        window.resize(760, 740)
        window.show()  # offscreen 平台只建立离屏表面。
        app.processEvents()
        expected_size = window.size()
        window.close()
        window = MainWindow(settings=settings)
        window.show()
        app.processEvents()
        assert window.size() == expected_size
        assert window.cmb_output_dpi.value() == 600 and window._params.output_dpi == 600
        assert window.sld_ink_density.value() == 200 and window._params.ink_density == 2.0
        report['checks'].append('Window size, output DPI and ink density persisted with isolated test settings')

        if sys.platform == 'win32':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel.GetModuleHandleW.restype = ctypes.c_void_p
            kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
            report['system_dlls'] = {}
            for name in ('icuuc.dll', 'ucrtbase.dll'):
                handle = kernel.GetModuleHandleW(name)
                if handle:
                    buffer = ctypes.create_unicode_buffer(32768)
                    kernel.GetModuleFileNameW(handle, buffer, len(buffer))
                    report['system_dlls'][name] = buffer.value
            icu = report['system_dlls'].get('icuuc.dll')
            assert icu and Path(icu).resolve().is_relative_to(Path(os.environ['SystemRoot']).resolve())
        report['ok'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
    finally:
        if window is not None:
            window.close()
        if settings_folder is not None:
            settings_folder.cleanup()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1
