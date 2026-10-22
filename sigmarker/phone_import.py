"""单次局域网 HTTP 收图；不依赖 Qt，不公开本地文件。"""

from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
from socketserver import TCPServer
import tempfile
import threading
import time

from PIL import Image

from .image_io import load_image, PHONE_FORMATS
from .phone_page import PAGE

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SESSION_SECONDS = 600
READ_TIMEOUT = 15
UPLOAD_SECONDS = 90


@dataclass
class ReceivedPhoto:
    session_id: str
    path: Path
    image: Image.Image
    folder: tempfile.TemporaryDirectory

    def cleanup(self):
        self.folder.cleanup()


class UploadError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 2

    def server_bind(self):
        # HTTPServer 默认会 getfqdn(IP)，局域网反向 DNS 超时会阻塞
        # Qt 按钮回调数秒。服务和二维码都只使用 IPv4，无需查询主机名。
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(self, address, owner):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(2)
        self.connections = set()
        self.connection_lock = threading.Lock()
        super().__init__(address, _Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.connection_lock:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            self._release(request)
            raise

    def _release(self, request):
        with self.connection_lock:
            self.connections.discard(request)
        self.slots.release()

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._release(request)

    def handle_error(self, request, address):
        # 不把客户端内容、令牌或原图路径写入 stderr/日志。
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = 'SigMarker'
    sys_version = ''

    def setup(self):
        self.request.settimeout(READ_TIMEOUT)
        super().setup()

    def log_message(self, *args):
        pass

    def _reply(self, status, data, content_type='application/json; charset=utf-8'):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True
        if status >= 400 and self.command == 'POST':
            # 先结束响应方向，再有界排空已到达的请求体，避免 Windows
            # 在尚有未读数据时 close() 以 RST 吞掉客户端应看到的错误。
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(.05)
                budget, deadline = 128 * 1024, time.monotonic() + .1
                while budget > 0 and time.monotonic() < deadline:
                    chunk = self.rfile.read1(min(budget, 64 * 1024))
                    if not chunk:
                        break
                    budget -= len(chunk)
            except OSError:
                pass

    def _check_host(self):
        owner = self.server.owner
        if self.headers.get('Host') != owner.authority:
            raise UploadError(403, '网络地址不匹配，请重新扫码。')
        origin = self.headers.get('Origin')
        if origin is not None and origin != owner.origin:
            raise UploadError(403, '不允许跨来源上传，请使用扫码页面。')

    def _authorize(self):
        self._check_host()
        expected = 'Bearer ' + self.server.owner.token
        supplied = self.headers.get('Authorization', '')
        if not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('ascii')):
            raise UploadError(403, '访问令牌无效，请重新扫码。')
        self.server.owner.check_active()

    def do_GET(self):
        try:
            self._check_host()
            if self.path == '/':
                self.server.owner.check_active()
                self._reply(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
            elif self.path == '/api/session':
                self._authorize()
                self._reply(200, {'remaining': max(0, int(self.server.owner.remaining))})
            else:
                raise UploadError(404, '页面不存在。')
        except UploadError as error:
            self._reply(error.status, {'error': error.message})

    def do_POST(self):
        photo = None
        try:
            self._authorize()
            if self.path != '/api/upload':
                raise UploadError(404, '接口不存在。')
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise UploadError(411, '需要明确的照片大小。')
            if len(lengths[0]) > 10:
                raise UploadError(413, '照片超过 30 MiB，请重新选择。')
            length = int(lengths[0])
            if not 0 < length <= MAX_UPLOAD_BYTES:
                raise UploadError(413, '照片为空或超过 30 MiB，请重新选择。')
            photo = self.server.owner.receive(self.rfile, length)
            try:
                self._reply(200, {'received': True})
            finally:
                # 即使响应丢失，已接收照片也只交付一次。
                self.server.owner.deliver(photo)
                photo = None
        except UploadError as error:
            self._reply(error.status, {'error': error.message})
        except (OSError, TimeoutError):
            pass
        finally:
            if photo is not None:
                photo.cleanup()


class PhoneImportServer:
    def __init__(self, host, on_received, *, lifetime=SESSION_SECONDS):
        self.session_id = secrets.token_hex(16)
        self.token = secrets.token_urlsafe(32)
        self.on_received = on_received
        self.lifetime = lifetime
        self._lock = threading.Lock()
        self._closed = False
        self._consumed = False
        self._uploading = False
        self._photo = None
        self._stop_event = threading.Event()
        self._server = _Server((host, 0), self)
        self.authority = '%s:%s' % self._server.server_address
        self.origin = 'http://' + self.authority
        self.url = self.origin + '/#token=' + self.token
        self._expires = time.monotonic() + lifetime
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': .05}, daemon=True)
        self._timer = threading.Timer(lifetime, self._expire)
        self._timer.daemon = True
        self._thread.start()
        self._timer.start()

    @property
    def remaining(self):
        return self._expires - time.monotonic()

    @property
    def closed(self):
        return self._stop_event.is_set()

    @property
    def consumed(self):
        with self._lock:
            return self._consumed

    def _check_locked(self):
        if self._closed or self.remaining <= 0:
            raise UploadError(410, '扫码会话已结束，请在电脑端重新开启。')
        if self._consumed:
            raise UploadError(409, '电脑已接收一张照片，请回到电脑查看。')

    def check_active(self):
        with self._lock:
            self._check_locked()

    def receive(self, stream, length):
        with self._lock:
            self._check_locked()
            if self._uploading:
                raise UploadError(409, '正在接收照片，请稍候。')
            self._uploading = True
        folder = None
        handed_off = False
        try:
            folder = tempfile.TemporaryDirectory(prefix='sigmarker-phone-')
            path = Path(folder.name) / 'incoming'
            deadline = time.monotonic() + UPLOAD_SECONDS
            with path.open('wb') as output:
                remaining = length
                while remaining:
                    if self.closed or self.remaining <= 0:
                        raise UploadError(410, '扫码会话已结束，请重新扫码。')
                    if time.monotonic() >= deadline:
                        raise UploadError(408, '上传超时，请重试。')
                    chunk = stream.read1(min(64 * 1024, remaining))
                    if not chunk:
                        raise UploadError(400, '照片没有传输完整，请重试。')
                    output.write(chunk)
                    remaining -= len(chunk)
            try:
                image, image_format = load_image(path, phone=True)
            except Exception:
                raise UploadError(415, '无法读取照片：仅支持完整的 JPEG、PNG、HEIC/HEIF，且不超过 6000 万像素。') from None
            name = '手机照片_' + datetime.now().strftime('%Y%m%d_%H%M%S') + PHONE_FORMATS[image_format]
            destination = path.with_name(name)
            path.rename(destination)
            photo = ReceivedPhoto(self.session_id, destination, image, folder)
            with self._lock:
                self._check_locked()
                self._consumed = True
                self._photo = photo
                self._timer.cancel()
            handed_off = True
            return photo
        except (socket.timeout, TimeoutError):
            raise UploadError(408, '上传超时，请重试。') from None
        except OSError:
            raise UploadError(500, '接收失败，请检查电脑临时目录空间后重试。') from None
        finally:
            if folder is not None and not handed_off:
                folder.cleanup()
            with self._lock:
                self._uploading = False

    def deliver(self, photo):
        try:
            if self.closed:
                photo.cleanup()
            else:
                self.on_received(photo)
        except Exception:
            photo.cleanup()
            self.claim_photo(photo)
        finally:
            self.stop(keep_result=True)

    def claim_photo(self, photo):
        """界面确认接管临时文件；此前取消会话也能清理已排队的结果。"""
        with self._lock:
            if self._photo is photo:
                self._photo = None
                return True
            return False

    def _expire(self):
        self.stop(only_if_unused=True)

    def stop(self, *, keep_result=False, only_if_unused=False):
        with self._lock:
            if only_if_unused and self._consumed:
                return
            photo = None if keep_result else self._photo
            if not keep_result:
                self._photo = None
            was_closed = self._closed
            if not was_closed:
                self._closed = True
                self._stop_event.set()
        if photo is not None:
            photo.cleanup()
        if was_closed:
            return
        self._timer.cancel()
        self._server.shutdown()
        with self._server.connection_lock:
            connections = list(self._server.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.server_close()
