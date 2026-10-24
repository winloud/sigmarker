# 源码与重新构建

SigMarker V0.1.0 Copyright (C) 2026 winloud，采用 GNU Affero General Public License version 3 only (AGPL-3.0-only)。完整条款见同目录 LICENSE。

- 项目及反馈：https://github.com/winloud/sigmarker
- 本版源码：https://github.com/winloud/sigmarker/tree/v0.1.0
- 本版源码归档：https://github.com/winloud/sigmarker/releases/download/v0.1.0/SigMarker-v0.1.0-source.zip
- 第三方源码归档：https://github.com/winloud/sigmarker/releases/download/v0.1.0/SigMarker-v0.1.0-third-party-sources.zip

两个源码归档与便携包在同一个 GitHub Release 提供，无需付费。第三方源码清单、上游地址和 SHA-256 见源码包中的 `third_party_sources.json`。第三方组件保留各自许可，详见 THIRD_PARTY_NOTICES.md 和 licenses/。

源码包包含应用代码、测试、合成样本、构建配置和依赖锁定文件，不包含真实签名。构建环境为 Windows x64、CPython 3.10.7；在独立虚拟环境安装 `requirements-build.txt` 后，按 `docs/development.md` 构建与自检。

Qt、libheif、libde265 以动态库形式分发。允许按其许可证修改、调试、替换这些组件及重新构建本应用。未对这些操作增加限制。第三方源码包附上相应上游构建脚本及版本来源。

本程序按许可证提供，不作担保。使用、修改和再分发请遵循许可证；第三方名称和商标仍归各自权利人。
