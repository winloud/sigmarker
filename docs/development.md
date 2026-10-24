# 开发与打包

[返回 README](../README.md)

## 源码运行

需要 Python 3.10 或以上。以下命令均在项目根目录执行，建议使用独立虚拟环境。

```powershell
python -m pip install -r requirements.txt
python main.py
```

算法命令行入口：

```powershell
python -m sigmarker.sigextract --help
```

包内模块通过包导入，不直接运行 `sigmarker/` 内的脚本。正式启动使用 Qt `windows11` 样式。

V0.1.0 的实际构建环境为 Windows x64 / CPython 3.10.7。重建发行版时请使用独立环境和固定依赖：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
```

`requirements-lock.txt` 固定运行依赖及传递依赖；`requirements-build.txt` 在此基础上固定 pytest、PyInstaller 和构建工具。`requirements.txt` 仍是普通源码安装入口。

## 版本与开源发行

`sigmarker/metadata.py` 中的 `APP_VERSION` 是程序、关于窗口、EXE 版本属性和成品自检的唯一版本来源。显示版本为 V0.1.0，Git 标签为 `v0.1.0`。

项目采用 AGPL-3.0-only；源码和便携包必须保留 LICENSE、THIRD_PARTY_NOTICES.md、SOURCE_CODE.md 及 licenses/。Qt、MuPDF、libheif/libde265 等组件保留其自身许可。

固定环境安装后，`python scripts/prepare_release.py --sources` 收集上游许可证并下载 `third_party_sources.json` 指定的源码到 `.verification/release-sources/`；已有清单时核对 SHA-256，不运行任何上游安装或构建脚本。首次维护清单须核对原生库实际版本：pi-heif 1.4.0 的 wheel 自带说明仍列旧版本，实际 Windows wheel 解码器报告 libheif 1.23.0、libde265 1.1.1，以实测版本为准。

发行时先运行完整回归和下述成品自检；将整个 SigMarker/ 目录压成 `SigMarker-v0.1.0-windows-x64.zip`。从发行提交制作 `SigMarker-v0.1.0-source.zip`，将清单中的归档及构建说明压成 `SigMarker-v0.1.0-third-party-sources.zip`。同一 GitHub Release 同时提供三个 ZIP、SHA256SUMS.txt、更新记录及明确的验收范围；标签与二进制须对应同一提交。

发行输入提交后，从干净工作区构建并自检，再运行 `python scripts/package_release.py`，在 `dist/release/` 生成上述三个 ZIP 和校验文件。脚本要求工作区干净、便携包完整且第三方源码哈希一致。发布前还需解压最终 ZIP 并实际运行其中 EXE 的 `--self-test`，核对 EXE 版本与 Git 提交。

本地诊断在加载 Qt 前初始化，错误只保留异常类型及内部模块/函数/行号。更改诊断时运行 `tests/test_release.py`，避免把异常正文、用户文件路径或扫码令牌写入日志。测试使用临时日志和模拟剪贴板。

## 回归验证

pytest 是开发工具，安装后执行：

```powershell
python -m pip install pytest
python -m pytest -q tests
```

回归包括纸色与光照、淡墨与细笔画、裁剪旋转、透明 PNG/PDF、DPI 与物理尺寸、剪贴板编码、图章集合保留和失败保护，以及手机 HTTP 收图、会话、HEIC 方向/主图及临时文件清理。

Qt 回归使用 `offscreen` 模式；网络测试只绑定回环地址。剪贴板测试检查内存数据与模拟 API，不读写用户系统剪贴板。图章测试使用合成集合或本地副本，不修改用户正在使用的集合。

### 合成 HEIC 样本

`sigmarker/assets/` 中的 HEIC 是合成几何图，无个人签名。日常运行、测试和便携包只需要只读解码器 `pi-heif`。

仅在重建样本时，在**独立开发环境**安装 `pillow-heif==1.8.1`，再运行：

```powershell
python tests/generate_heic_fixtures.py
```

不要把编码器加入运行依赖。

## Windows 便携包

用户可运行根目录 `build.bat`：它按 `SigMarker.spec` 构建，再执行无界面成品自检，最后暂停以便查看结果，不备份旧便携包。

需要手动构建时，先确认 PyInstaller 已安装：

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean SigMarker.spec
```

产物是整个 `dist/SigMarker/` 目录，`SigMarker.exe` 与 `_internal/` 必须一起分发。若旧版正在运行并占用目录，不关闭用户进程，可改用独立输出目录：

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist/new-build SigMarker.spec
```

此时成品、自检和交付路径都应指向 `dist/new-build/SigMarker/`。

### 成品自检

手动执行 PyInstaller 后，仍需实际运行新 EXE。以下 PowerShell 示例会等待自检退出，并同时核对退出码和报告：

```powershell
$sigmarkerProcess = Start-Process -FilePath '.\dist\SigMarker\SigMarker.exe' `
    -ArgumentList @('--self-test', '.verification/package-self-test.json') `
    -WindowStyle Hidden -PassThru -Wait
$sigmarkerReport = Get-Content '.verification/package-self-test.json' -Raw | ConvertFrom-Json
if ($sigmarkerProcess.ExitCode -ne 0 -or -not $sigmarkerReport.ok) {
    throw 'SigMarker 成品自检失败，请检查报告。'
}
```

自检强制 Qt `offscreen`，不显示桌面窗口、不发送键鼠输入，只监听回环地址。它验证实际依赖加载、窗口构建、签名检测、双底色预览、裁剪旋转、PNG/PDF 导出、合成图章追加、HEIC 原生解码、二维码、上传和退出清理，不触碰真实图章或系统剪贴板。

后台自检不能代替真实桌面外观、手机扫码、拍照和局域网连通性验收。

### DLL 与构建配置

`SigMarker.spec` 是唯一打包配置来源，在构建进程内隔离外部工具的 DLL 搜索路径，避免混入不兼容的 ICU/UCRT。pi-heif 的 Windows wheel 可能将 DLL 放在 `site-packages` 根目录，因此按发行记录收集其原生库。

OpenCV、Qt Windows 平台插件和 PDF 运行依赖随便携包分发。仅检查文件存在不能证明 DLL 能加载，必须实际运行成品自检。

## 项目结构

| 位置 | 职责 |
| --- | --- |
| `main.py` | 源码与打包统一入口，加载 Qt 前处理 `--self-test`。 |
| `sigmarker/app.py` | Qt 界面、文件与菜单、裁剪交互、预览、剪贴板及导出入口。 |
| `sigmarker/sigextract.py` | 背景校正、范围检测、方向判断、透明渲染及导出；独立于界面。 |
| `sigmarker/image_io.py` | 本地与手机图片共用的读取、方向修正、透明区合成及 HEIC 解码。 |
| `sigmarker/phone_import.py` | 临时 HTTP 服务、令牌、上传限制与临时原图所有权。 |
| `sigmarker/phone_dialog.py` / `phone_page.py` | Qt 扫码/接收界面与内置手机页面。 |
| `sigmarker/stamp_import.py` | 图章集合验证、追加、备份和原子替换。 |
| `sigmarker/clipboard_image.py` | 带 DPI 的 PNG/CF_DIBV5 与 Windows 剪贴板内存管理。 |
| `sigmarker/assets/sigmarker.svg` / `sigmarker.ico` | 蓝色 S 标识的矢量源与多尺寸 Windows 图标；ICO 同时用于窗口和 EXE。 |
| `sigmarker/assets/*.svg` | 工具条与导入按钮的矢量图标；运行图标由 spec 随包收集，自检实际渲染确认 SVG 插件可用。 |
| `sigmarker/package_smoke.py` | 成品无界面自检。 |
| `tests/` | 合成图像、真实回环 HTTP 及 Qt 回归。 |
| `docs/` | 使用指南、开发说明和合成效果图。 |
| `build/` / `dist/` | 构建中间文件及便携包，不纳入 Git。 |
| `.verification/` | 本地验证报告、图片、历史备份与诊断记录，不纳入 Git。 |

`.verification/diagnostics/_diag.py` 含本机样本路径，是历史诊断脚本，不是通用测试入口。真实签名和本地测试样本不应提交到仓库。
