"""真实回环 HTTP + Qt 离屏接收测试；不接触手机、真实签名或系统剪贴板。"""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, RemoteDisconnected
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import cv2
import numpy as np
from PIL import Image, ImageDraw
import pytest
from PyQt6.QtCore import QMimeData, QSettings, QThread, QUrl
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from sigmarker.app import APP_STYLE, MainWindow, ZoomableGraphicsView
from sigmarker.image_io import load_image
from sigmarker import image_io, phone_import, phone_dialog
from sigmarker.phone_dialog import qr_image
from sigmarker.phone_import import PhoneImportServer, ReceivedPhoto
from sigmarker.package_smoke import qr_matches

HEIC = Path(__file__).parents[1] / 'sigmarker' / 'assets' / 'synthetic.heic'


def image_bytes(kind='PNG'):
    image = Image.new('RGB', (280, 160), 'white')
    ImageDraw.Draw(image).line([(60, 110), (80, 45), (110, 105), (160, 60), (215, 100)],
                               fill='black', width=5)
    output = BytesIO()
    image.save(output, kind)
    return output.getvalue()


def wait_for(predicate, app=None, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app:
            app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), '等待状态超时'


def request(server, path='/api/upload', body=None, *, token=None, headers=None, method=None):
    connection = HTTPConnection(*server._server.server_address, timeout=3)
    request_headers = {'Authorization': 'Bearer ' + (server.token if token is None else token)}
    request_headers.update(headers or {})
    try:
        connection.request(method or ('POST' if body is not None else 'GET'), path,
                           body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


@pytest.fixture
def service():
    photos = []
    server = PhoneImportServer('127.0.0.1', photos.append)
    try:
        yield server, photos
    finally:
        server.stop()
        server._thread.join(2)
        for photo in photos:
            photo.cleanup()


def test_server_start_does_not_depend_on_reverse_dns(monkeypatch):
    def unexpected_lookup(*args):
        pytest.fail('扫码服务启动不应查询反向 DNS')

    monkeypatch.setattr(socket, 'getfqdn', unexpected_lookup)
    server = PhoneImportServer('127.0.0.1', lambda photo: photo.cleanup())
    try:
        assert server._server.server_name == '127.0.0.1'
        assert server._server.server_port == server._server.server_address[1]
        assert request(server, '/')[0] == 200
        assert request(server, '/api/session')[0] == 200
    finally:
        server.stop()


@pytest.mark.parametrize('kind', ['JPEG', 'PNG', 'HEIC'])
def test_http_receives_actual_format_and_closes(service, kind):
    server, photos = service
    payload = HEIC.read_bytes() if kind == 'HEIC' else image_bytes(kind)
    status, body, _ = request(server, body=payload,
                              headers={'Content-Type': 'text/plain', 'X-Filename': '../../bad.exe'})
    assert status == 200 and json.loads(body) == {'received': True}
    wait_for(lambda: photos and server._server.fileno() == -1)
    assert len(photos) == 1 and photos[0].path.read_bytes() == payload
    assert photos[0].path.name.startswith('手机照片_')
    assert photos[0].path.suffix == {'JPEG': '.jpg', 'PNG': '.png', 'HEIC': '.heic'}[kind]
    assert photos[0].image.mode == 'RGB'
    with pytest.raises(OSError):
        socket.create_connection(server._server.server_address, timeout=.2)


def test_page_status_qr_and_no_local_file_access(service):
    server, _ = service
    code, body, headers = request(server, '/')
    assert code == 200 and server.token.encode() not in body
    assert 'capture="environment"' in body.decode() and b'cdn' not in body.lower()
    assert headers['Cache-Control'] == 'no-store'
    assert request(server, '/api/session')[0] == 200
    assert request(server, '/README.md')[0] == 404
    assert request(server, '/../requirements.txt')[0] == 404
    assert qr_matches(np.array(qr_image(server.url)), server.url)


def test_qr_self_test_recognizes_pattern_missed_by_default_detector():
    # 固定合成链接，非真实会话令牌；备用识别器能还原完全相同的链接。
    url = 'http://127.0.0.1:54321/#token=D_1XziWW6zmhVEnFKLH3TY9k8J514oWvBC1lYlIEM30'
    assert qr_matches(np.array(qr_image(url)), url)


@pytest.mark.parametrize('headers,token', [({}, 'wrong'), ({'Origin': 'http://other.local'}, None),
                                         ({'Host': 'other.local'}, None)])
def test_http_auth_origin_and_host(service, headers, token):
    server, photos = service
    assert request(server, body=image_bytes(), headers=headers, token=token)[0] == 403
    assert not photos and not server.consumed
    assert request(server, '/api/session')[0] == 200


@pytest.mark.parametrize('payload,status', [(b'not an image', 415), (image_bytes('GIF'), 415), (b'', 413)])
def test_invalid_upload_can_retry(service, payload, status):
    server, photos = service
    assert request(server, body=payload)[0] == status
    assert not photos and not server.consumed
    assert request(server, body=image_bytes())[0] == 200
    wait_for(lambda: len(photos) == 1)


@pytest.mark.parametrize('header,expected', [({'Content-Length': str(phone_import.MAX_UPLOAD_BYTES + 1)}, 413),
                                           ({'Content-Length': '-1'}, 411),
                                           ({'Transfer-Encoding': 'chunked'}, 411)])
def test_http_size_and_framing_rejected_before_read(service, header, expected):
    server, photos = service
    assert request(server, body=b'x', headers=header)[0] == expected
    assert not photos


def test_pixel_limit_before_full_decode(service, monkeypatch):
    server, photos = service
    monkeypatch.setattr(image_io, 'MAX_PHONE_PIXELS', 100)
    assert request(server, body=image_bytes())[0] == 415
    assert not photos and not server.consumed


def test_expired_token_and_auto_stop(service):
    server, photos = service
    server._expires = time.monotonic() - 1
    assert request(server, body=image_bytes())[0] == 410
    server._expire()
    wait_for(lambda: server._server.fileno() == -1)
    assert not photos


def test_short_session_timer_stops_itself():
    server = PhoneImportServer('127.0.0.1', lambda p: p.cleanup(), lifetime=.05)
    try:
        wait_for(lambda: server._server.fileno() == -1)
    finally:
        server.stop()


def test_concurrent_upload_and_duplicate_delivery_once(service, monkeypatch):
    server, photos = service
    entered, release = threading.Event(), threading.Event()
    original = phone_import.load_image

    def held_decode(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(phone_import, 'load_image', held_decode)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(request, server, body=image_bytes())
        try:
            assert entered.wait(2)
            assert request(server, body=image_bytes())[0] == 409
        finally:
            release.set()
        assert first.result()[0] == 200
    wait_for(lambda: len(photos) == 1)
    # 已消费会话在监听关闭前也不能重复交付。
    with pytest.raises(phone_import.UploadError) as error:
        server.receive(BytesIO(image_bytes()), len(image_bytes()))
    assert error.value.status in (409, 410) and len(photos) == 1


@pytest.mark.parametrize('cancel', [False, True])
def test_cancel_or_expire_during_decode_cleans_partial(service, monkeypatch, tmp_path, cancel):
    server, photos = service
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    original = phone_import.load_image

    def held_decode(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(phone_import, 'load_image', held_decode)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(request, server, body=image_bytes())
        try:
            assert entered.wait(2)
            if cancel:
                server.stop()
            else:
                server._expires = time.monotonic() - 1
        finally:
            release.set()
        if cancel:
            with pytest.raises((OSError, RemoteDisconnected)):
                future.result()
        else:
            assert future.result()[0] == 410
    wait_for(lambda: not list(tmp_path.iterdir()))
    assert not photos


@pytest.mark.parametrize('disconnect', [False, True])
def test_incomplete_or_slow_upload_cleanup(service, monkeypatch, tmp_path, disconnect):
    server, photos = service
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(phone_import, 'READ_TIMEOUT', .15)
    connection = socket.create_connection(server._server.server_address, timeout=2)
    header = (f'POST /api/upload HTTP/1.1\r\nHost: {server.authority}\r\n'
              f'Authorization: Bearer {server.token}\r\nContent-Length: 100\r\n\r\nx')
    connection.sendall(header.encode())
    if disconnect:
        connection.shutdown(socket.SHUT_WR)
    try:
        response = connection.recv(4096)
        assert (b'400' if disconnect else b'408') in response
    finally:
        connection.close()
    wait_for(lambda: not list(tmp_path.iterdir()))
    assert not photos and not server.consumed


def test_connection_cap_and_stop_unblocks_idle_clients(service):
    server, _ = service
    connections = [socket.create_connection(server._server.server_address, timeout=2) for _ in range(2)]
    try:
        wait_for(lambda: len(server._server.connections) == 2)
        third = socket.create_connection(server._server.server_address, timeout=2)
        try:
            assert third.recv(1) == b''
        finally:
            third.close()
        server.stop()
        for connection in connections:
            assert connection.recv(1) == b''
    finally:
        for connection in connections:
            connection.close()


def test_shared_image_loader_orientation_alpha_and_heic(tmp_path):
    rgba = Image.new('RGBA', (12, 8), (0, 0, 0, 0))
    rgba.putpixel((1, 2), (5, 20, 40, 255))
    path = tmp_path / 'transparent.png'
    rgba.save(path)
    image, _ = load_image(path)
    assert image.size == rgba.size and image.getpixel((0, 0)) == (255, 255, 255)
    assert image.getpixel((1, 2)) == (5, 20, 40)
    exif = Image.Exif()
    exif[274] = 6
    Image.new('RGB', (24, 16), 'white').save(path.with_suffix('.jpg'), exif=exif)
    assert load_image(path.with_suffix('.jpg'))[0].size == (16, 24)
    heic, actual = load_image(HEIC, phone=True)
    assert actual == 'HEIF' and heic.size == (160, 96)
    assert np.mean(heic.crop((8, 8, 30, 30))) < 20
    assert np.mean(heic.crop((120, 65, 150, 85))) > 240


def test_heic_container_orientation_and_primary_image():
    rotated, _ = load_image(HEIC.with_name('synthetic-oriented.heic'), phone=True)
    assert rotated.size == (96, 160)
    assert np.mean(rotated.crop((65, 8, 85, 30))) < 20
    primary, _ = load_image(HEIC.with_name('synthetic-primary.heic'), phone=True)
    assert primary.size == (160, 96)
    assert np.mean(primary.crop((8, 8, 30, 30))) < 20


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle(APP_STYLE)
    return app


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(phone_dialog, 'network_addresses', lambda: [('回环测试', '127.0.0.1')])
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    instance = MainWindow(settings=settings)
    try:
        yield instance
    finally:
        instance.close()
        qt_app.processEvents()


def open_dialog(window):
    window._open_phone_import()
    return window._phone_dialog


def current_document(window, tmp_path):
    path = tmp_path / 'current.png'
    path.write_bytes(image_bytes())
    window._load_path(str(path))
    return window._page_img, window._crop_pixels(), window._wm_image


def test_qt_empty_auto_import_on_gui_thread(window, qt_app, monkeypatch):
    calls = []
    original = window._accept_phone_photo

    def accept(photo):
        calls.append(QThread.currentThread() == qt_app.thread())
        return original(photo)

    monkeypatch.setattr(window, '_accept_phone_photo', accept)
    dialog = open_dialog(window)
    server = dialog.server
    window._open_phone_import()
    assert window._phone_dialog is dialog and dialog.server is server
    assert request(server, body=image_bytes())[0] == 200
    # 即使后台已关闭，计时器也不能清掉尚未处理的成功信号。
    dialog._tick()
    wait_for(lambda: window._phone_photo is not None, qt_app)
    assert calls == [True] and window._phone_dialog is None
    photo_path = window._phone_photo.path
    assert photo_path.exists() and window.btn_export_png.isEnabled()
    window._close_file()
    assert not photo_path.exists()


@pytest.mark.parametrize('use', [False, True])
def test_qt_existing_document_preview_use_or_discard(window, qt_app, tmp_path, use):
    previous = current_document(window, tmp_path)
    dialog = open_dialog(window)
    assert request(dialog.server, body=image_bytes('JPEG'))[0] == 200
    wait_for(lambda: dialog.pending is not None, qt_app)
    assert window._page_img is previous[0] and window._crop_pixels() == previous[1]
    assert window._wm_image is previous[2] and dialog.server is None
    path = dialog.pending.path
    if use:
        dialog._use_photo()
        assert window._page_img is not previous[0] and path.exists()
        window._load_path(str(tmp_path / 'current.png'))
        assert not path.exists()
    else:
        dialog.reject()
        assert window._page_img is previous[0] and not path.exists()


def test_qt_failed_accept_preserves_current_state(window, qt_app, tmp_path, monkeypatch):
    previous = current_document(window, tmp_path)
    dialog = open_dialog(window)
    request(dialog.server, body=image_bytes())
    wait_for(lambda: dialog.pending is not None, qt_app)
    monkeypatch.setattr(window, '_pil_to_qpixmap', lambda *_: (_ for _ in ()).throw(ValueError('模拟失败')))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    path = dialog.pending.path
    dialog._use_photo()
    assert dialog.pending is not None and path.exists()
    assert window._page_img is previous[0] and window._crop_pixels() == previous[1]
    assert window._wm_image is previous[2]
    dialog.reject()
    assert not path.exists()


def test_qt_restart_close_and_stale_result_cleanup(window, qt_app, tmp_path):
    dialog = open_dialog(window)
    old = dialog.server
    folder = tempfile.TemporaryDirectory(dir=tmp_path)
    path = Path(folder.name) / 'stale.png'
    path.write_bytes(image_bytes())
    photo = ReceivedPhoto(old.session_id, path, load_image(path)[0], folder)
    dialog._refresh_addresses()
    assert old.closed and dialog.server.token != old.token
    dialog._on_received(photo)
    assert not path.exists() and window._page_img is None
    active = dialog.server
    window.close()
    assert active.closed and window._phone_dialog is None


def test_qt_phone_export_directory_and_exit_cleanup(window, qt_app, monkeypatch):
    dialog = open_dialog(window)
    request(dialog.server, body=image_bytes())
    wait_for(lambda: window._phone_photo is not None, qt_app)
    path = window._phone_photo.path
    defaults = []
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (defaults.append(args[2]) or '', ''))
    window._export('png')
    assert defaults and str(path.parent) not in defaults[0]
    assert Path(defaults[0]).stem.endswith('_签名')
    window.close()
    assert not path.exists()


def test_qt_close_before_queued_delivery_cleans_immediately(window, qt_app):
    dialog = open_dialog(window)
    server = dialog.server
    assert request(server, body=image_bytes())[0] == 200
    photo = server._photo
    assert photo is not None and photo.path.exists()
    dialog.reject()  # 尚未 processEvents，照片仍在 queued signal 中。
    assert not photo.path.exists()
    qt_app.processEvents()
    assert window._page_img is None


def test_heic_local_open_drop_and_no_network(window):
    window._load_path(str(HEIC))
    assert window._page_img is not None and window._phone_dialog is None
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(HEIC))])
    assert Path(ZoomableGraphicsView.dropped_path(mime)) == HEIC


def test_no_network_and_start_failure_preserve_document(window, tmp_path, monkeypatch):
    previous = current_document(window, tmp_path)
    monkeypatch.setattr(phone_dialog, 'network_addresses', lambda: [])
    dialog = open_dialog(window)
    assert dialog.server is None and not dialog.btn_copy.isEnabled()
    assert '未找到' in dialog.status.text()
    dialog.reject()
    monkeypatch.setattr(phone_dialog, 'network_addresses', lambda: [('失效地址', '192.0.2.1')])
    dialog = open_dialog(window)
    assert dialog.server is None and '无法启动' in dialog.status.text()
    assert window._page_img is previous[0]
