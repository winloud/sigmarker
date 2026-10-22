"""Windows 透明剪贴板图像: 同时提供 PNG 和带 DPI 的标准 CF_DIBV5。"""

import ctypes
from io import BytesIO
import struct

from .sigextract import DEFAULT_OUTPUT_DPI, export_png


def clipboard_payloads(image, dpi=DEFAULT_OUTPUT_DPI):
    png = BytesIO()
    export_png(image, png, dpi=dpi)
    rgba = image.convert('RGBA')
    dpm = round(float(dpi) / .0254)
    header = bytearray(124)
    struct.pack_into('<IiiHHIIiiII', header, 0,
                     124, rgba.width, rgba.height, 1, 32, 3,
                     rgba.width * rgba.height * 4, dpm, dpm, 0, 0)
    # BI_BITFIELDS, straight BGRA, sRGB; 正高度对应从下向上的像素行。
    struct.pack_into('<IIIII', header, 40,
                     0x00ff0000, 0x0000ff00, 0x000000ff, 0xff000000, 0x73524742)
    struct.pack_into('<I', header, 108, 4)  # LCS_GM_IMAGES
    dib = bytes(header) + rgba.tobytes('raw', 'BGRA', 0, -1)
    return {'PNG': png.getvalue(), 17: dib}  # 17 = CF_DIBV5


def _win32_libraries():
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    for library, name, args, result in (
        (user, 'RegisterClipboardFormatW', [ctypes.c_wchar_p], ctypes.c_uint),
        (user, 'OpenClipboard', [ctypes.c_void_p], ctypes.c_int),
        (user, 'EmptyClipboard', [], ctypes.c_int),
        (user, 'SetClipboardData', [ctypes.c_uint, ctypes.c_void_p], ctypes.c_void_p),
        (user, 'CloseClipboard', [], ctypes.c_int),
        (kernel, 'GlobalAlloc', [ctypes.c_uint, ctypes.c_size_t], ctypes.c_void_p),
        (kernel, 'GlobalLock', [ctypes.c_void_p], ctypes.c_void_p),
        (kernel, 'GlobalUnlock', [ctypes.c_void_p], ctypes.c_int),
        (kernel, 'GlobalFree', [ctypes.c_void_p], ctypes.c_void_p),
    ):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
    return user, kernel


def set_windows_clipboard(payloads, owner_hwnd):
    """仅在用户点击复制时调用。成功移交的内存由 Windows 持有, 关闭工具后仍可粘贴。"""
    if not owner_hwnd:
        raise ValueError('复制需要有效的窗口句柄。')
    user, kernel = _win32_libraries()
    pending = {}
    opened = False
    try:
        # 先准备全部格式和内存, 避免编码或分配失败时清空原剪贴板。
        for name, data in payloads.items():
            format_id = user.RegisterClipboardFormatW(name) if isinstance(name, str) else name
            if not format_id:
                raise ctypes.WinError(ctypes.get_last_error())
            handle = kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            pending[format_id] = handle
            pointer = kernel.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(owner_hwnd):
            raise OSError('剪贴板正被其他程序占用，请稍后再次复制。')
        opened = True
        if not user.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        for format_id, handle in tuple(pending.items()):
            if not user.SetClipboardData(format_id, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            del pending[format_id]  # Windows 接管所有权, 不能 GlobalFree。
    finally:
        if opened:
            user.CloseClipboard()
        for handle in pending.values():
            kernel.GlobalFree(handle)
