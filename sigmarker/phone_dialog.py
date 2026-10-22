"""Qt 扫码接收界面；服务线程只通过 queued signal 交付照片。"""

import ipaddress
import math

import qrcode
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtNetwork import QAbstractSocket, QNetworkInterface
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from .phone_import import PhoneImportServer


def network_addresses():
    """Wifi、以太网优先；虚拟/代理接口置后，排除不可用 IPv4。"""
    candidates = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not flags & QNetworkInterface.InterfaceFlag.IsUp or not flags & QNetworkInterface.InterfaceFlag.IsRunning:
            continue
        name = interface.humanReadableName()
        kind = interface.type()
        virtual = kind == QNetworkInterface.InterfaceType.Virtual or any(
            word in name.lower() for word in ('vmware', 'virtual', 'vpn', 'mihomo', 'tun', 'vethernet'))
        rank = 3 if virtual else {QNetworkInterface.InterfaceType.Wifi: 0,
                                QNetworkInterface.InterfaceType.Ethernet: 1}.get(kind, 2)
        for entry in interface.addressEntries():
            if entry.ip().protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            address = entry.ip().toString()
            ip = ipaddress.ip_address(address)
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                continue
            if ip in ipaddress.ip_network('198.18.0.0/15'):
                rank = 3
            candidates.append((rank, name, address))
    return [(name, address) for _, name, address in sorted(set(candidates))]


def image_pixmap(image):
    rgb = image.convert('RGB')
    return QPixmap.fromImage(QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3,
                                   QImage.Format.Format_RGB888).copy())


def qr_image(url):
    qr = qrcode.QRCode(box_size=5, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    qr.box_size = max(1, 290 // (qr.modules_count + 8))
    return qr.make_image(fill_color='black', back_color='white').get_image().convert('RGB')


class PhoneImportDialog(QDialog):
    received = pyqtSignal(object)

    def __init__(self, parent, *, address_provider=None):
        super().__init__(parent)
        self.setWindowTitle('手机扫码导入')
        self.setMinimumWidth(510)
        self.server = None
        self.pending = None
        self._closed = False
        self._address_provider = address_provider or network_addresses
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel('手机扫码后拍照或选择相册图片，一次接收一张。'))
        row = QHBoxLayout()
        row.addWidget(QLabel('网络地址'))
        self.addresses = QComboBox()
        row.addWidget(self.addresses, 1)
        self.btn_refresh = QPushButton('刷新网络 / 新二维码')
        self.btn_refresh.clicked.connect(self._refresh_addresses)
        row.addWidget(self.btn_refresh)
        layout.addLayout(row)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setMinimumHeight(290)
        layout.addWidget(self.qr)
        link_row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setReadOnly(True)
        self.link.setPlaceholderText('服务启动后显示手机访问链接')
        link_row.addWidget(self.link, 1)
        self.btn_copy = QPushButton('复制链接')
        self.btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.link.text()))
        link_row.addWidget(self.btn_copy)
        layout.addLayout(link_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        hint = QLabel('手机和电脑需在可互访的局域网。HTTP 传输未加密，请仅在可信网络使用。\n'
                      '支持 JPEG、PNG、HEIC/HEIF；最多 30 MiB、6000 万像素。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        help_button = QPushButton('连接不上？查看排查提示')
        layout.addWidget(help_button)
        self.help = QLabel('1. 选择手机能访问的 WLAN 或以太网地址，避免代理和虚拟网卡。\n'
                           '2. Windows 防火墙询问时，仅在可信的专用网络允许 SigMarker 通信。\n'
                           '3. 检查公司 Wi-Fi 是否隔离客户端、是否分属不同 VLAN 或网段。\n'
                           '4. 检查 VPN/代理路由；网络变化后刷新二维码。\n'
                           '5. 扫码应用无法打开时，复制链接到手机系统浏览器。')
        self.help.setWordWrap(True)
        self.help.hide()
        help_button.clicked.connect(lambda: self.help.setVisible(not self.help.isVisible()))
        layout.addWidget(self.help)
        actions = QHBoxLayout()
        self.btn_use = QPushButton('使用此照片')
        self.btn_use.hide()
        self.btn_use.clicked.connect(self._use_photo)
        actions.addWidget(self.btn_use)
        self.btn_cancel = QPushButton('关闭')
        self.btn_cancel.clicked.connect(self.reject)
        actions.addWidget(self.btn_cancel)
        layout.addLayout(actions)
        self.received.connect(self._on_received, Qt.ConnectionType.QueuedConnection)
        self.addresses.currentIndexChanged.connect(self._start_session)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self._refresh_addresses()

    def _stop_session(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.timer.stop()
        self.link.clear()
        self.btn_copy.setEnabled(False)

    def _refresh_addresses(self):
        previous = self.addresses.currentData()
        self.addresses.blockSignals(True)
        self.addresses.clear()
        for name, address in self._address_provider():
            self.addresses.addItem(f'{name} · {address}', address)
        index = self.addresses.findData(previous)
        if index >= 0:
            self.addresses.setCurrentIndex(index)
        self.addresses.blockSignals(False)
        self._start_session()

    def _start_session(self, *_):
        self._stop_session()
        self.qr.clear()
        address = self.addresses.currentData()
        if not address:
            self.status.setText('未找到可用的局域网 IPv4 地址，请连接网络后刷新。')
            return
        try:
            self.server = PhoneImportServer(address, self.received.emit)
            self.qr.setPixmap(image_pixmap(qr_image(self.server.url)))
        except Exception:
            self._stop_session()
            self.status.setText('无法启动收图服务，请检查所选网络地址是否仍可用后刷新。')
            return
        self.link.setText(self.server.url)
        self.link.setCursorPosition(0)
        self.btn_copy.setEnabled(True)
        self.timer.start()
        self._tick()

    def _tick(self):
        if self.server is None or self.pending is not None or self.server.consumed:
            return
        seconds = max(0, math.ceil(self.server.remaining))
        if seconds == 0 or self.server.closed:
            self._stop_session()
            self.qr.clear()
            self.status.setText('扫码会话已结束。点击“刷新网络 / 新二维码”重新开启。')
        else:
            self.status.setText(f'等待上传 · 二维码剩余 {seconds // 60:02d}:{seconds % 60:02d}')

    def _on_received(self, photo):
        if self._closed or self.server is None or photo.session_id != self.server.session_id:
            photo.cleanup()
            return
        if not self.server.claim_photo(photo):
            photo.cleanup()
            return
        self.pending = photo
        self._stop_session()
        self.addresses.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        if self.parent()._page_img is None:
            self._use_photo()
            return
        ratio = min(440 / photo.image.width, 260 / photo.image.height, 1)
        preview = photo.image.resize((max(1, round(photo.image.width * ratio)),
                                      max(1, round(photo.image.height * ratio))))
        self.qr.setPixmap(image_pixmap(preview))
        self.status.setText('照片已接收，收图服务已关闭。\n当前文件仍保留，点击“使用此照片”才会替换。')
        self.btn_use.show()
        self.btn_cancel.setText('放弃')

    def _use_photo(self):
        if self.pending is not None and self.parent()._accept_phone_photo(self.pending):
            self.pending = None  # 临时文件所有权交给主窗口。
            self.accept()
        elif self.pending is not None:
            self.status.setText('照片载入失败，当前文件仍保留。可以重试或放弃。')

    def done(self, result):
        self._closed = True
        self._stop_session()
        if self.pending is not None:
            self.pending.cleanup()
            self.pending = None
        super().done(result)
