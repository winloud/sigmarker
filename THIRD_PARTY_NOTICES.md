# 第三方软件声明 — SigMarker V0.1.0

SigMarker Copyright (C) 2026 winloud，应用源码采用 AGPL-3.0-only。以下组件按原作者各自许可证分发，应用许可证不替代第三方许可证。完整原文保存在随包 `licenses/`，源码获取方式见 SOURCE_CODE.md。

| 组件 | 固定版本 | 许可证及说明 | 上游 |
| --- | --- | --- | --- |
| CPython | 3.10.7 | PSF-2.0 及随附第三方许可 | https://www.python.org/ |
| PyQt6 | 6.11.0 | GPL-3.0-only；与本应用按 GPLv3/AGPLv3 第 13 条组合 | https://www.riverbankcomputing.com/software/pyqt/ |
| Qt | 6.11.2 | LGPL-3.0；QtBase / QtSvg 动态库，包含其第三方组件 | https://www.qt.io/ |
| PyQt6-sip | 13.13.0 | BSD-2-Clause | https://www.riverbankcomputing.com/software/sip/ |
| PyMuPDF / MuPDF | 1.24.14 / 1.24.11 | AGPL-3.0；MuPDF 包含其第三方组件 | https://github.com/pymupdf/PyMuPDF |
| Pillow | 12.2.0 | MIT-CMU 及其图像编解码器许可 | https://python-pillow.github.io/ |
| NumPy | 2.2.5 | BSD-3-Clause 及 OpenBLAS、LAPACK、GCC Runtime Exception 等随附条款 | https://numpy.org/ |
| OpenCV / opencv-python | 4.10.0 / 4.10.0.84 | Apache-2.0 / MIT 包装；包含 LGPL FFmpeg 及其他组件，完整条款见 licenses/opencv-python/ | https://github.com/opencv/opencv-python |
| pypdf | 5.1.0 | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| qrcode | 8.2 | BSD-3-Clause | https://github.com/lincolnloop/python-qrcode |
| pi-heif | 1.4.0 | BSD-3-Clause；Windows wheel 中的原生库为 LGPL-3.0 | https://github.com/bigcat88/pillow_heif |
| libheif / libde265 | 1.23.0 / 1.1.1 | LGPL-3.0；按 Windows wheel 实际 `libheif_info()` 核对 | https://github.com/strukturag/libheif / https://github.com/strukturag/libde265 |
| MinGW GCC runtime / winpthreads | 随 pi-heif Windows wheel | GPLv3 + GCC Runtime Library Exception / MIT 与 BSD；原文随包提供 | https://gcc.gnu.org/ / https://www.mingw-w64.org/ |
| colorama | 0.4.6 | BSD-3-Clause | https://github.com/tartley/colorama |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| PyInstaller bootloader | 6.20.0 | GPL-2.0-or-later，附带应用分发例外；见随附 COPYING.txt | https://pyinstaller.org/ |

`licenses/` 保留来自已安装发行包的版权与完整许可。PyMuPDF wheel 的 COPYING 仅为短标记，因此同时附上完整 AGPL-3.0 文本。pi-heif wheel 自带 LICENSES_bundled.txt 中仍列 libheif 1.18.1 / libde265 1.0.15；该文件原样保留作为上游声明，实际随本版 Windows 包加载的版本为上表所列 1.23.0 / 1.1.1，对应源码按实测版本提供。

Qt、libheif 和 libde265 动态库允许按相应许可证替换、修改和调试，本项目不附加禁止逆向调试或重新链接的限制。许可原文同时附有 GPLv3、LGPLv3 和 LGPLv2.1。第三方源代码及原生库构建脚本与应用源码在同一个 Release 提供，清单记录源地址和 SHA-256。

PyInstaller 是构建工具，其 bootloader 随 EXE 分发；pytest 等开发工具不作为应用功能分发。Microsoft 系统组件的名称和商标归各自权利人。SigMarker 与 PDF-XChange、Qt 或上述组件的作者没有隶属或背书关系。
