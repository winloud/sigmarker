# SigMarker 签名水印提取

面向白纸扫描件和手机拍照的手写签名。打开 `dist/SigMarker/SigMarker.exe` 使用，无需安装 Python。

1. 将 PDF 或图片拖入整个文档预览框，或点空白预览区中央的打开按钮。也可通过文件菜单或 `Ctrl+O` 打开。支持 PNG、JPEG、BMP、TIFF 和 PDF（读取第 1 页）。
2. 自动范围使用背景亮度校正、连通笔画聚组和桌沿过滤。范围包含日期或其他内容时，拖动现有裁剪框及其手柄调整范围。
3. 深灰和白底同时显示，并保持相同缩放；左侧检查白边，右侧看盖到白纸上的效果。预览底色不会写入导出文件。
4. 纸色和光照校正自动执行，白纸扫描件与手机照片均无需手动开启；旧版记住的关闭状态会自动清理。淡墨缺失时增大“淡墨保留”；已提取的笔画偏浅时提高“墨迹浓度”（1.00～3.00×，默认 1.00×），在去背景后加深笔画，自动记忆。整体水印需要更透明时降低“不透明度”；小污点可用“清除孤立噪点”。
5. 复制透明签名，或导出透明 PNG / PDF。右侧“粘贴 / 导出 DPI”统一控制剪贴板、PNG、PDF 和图章的物理尺寸，默认 300 DPI，可在 72～1200 范围调整并记忆。预览下方显示像素和毫米尺寸；DPI 越大，粘贴尺寸越小，像素不变。例如宽 476 px 的签名在 300 DPI 时约 40.3 mm，在 600 DPI 时约 20.2 mm。Windows 复制同时提供带透明度及 DPI 的 PNG 和 DIBV5；接收软件最终采用的格式和缩放仍以其粘贴结果为准。
6. 完成后点顶部“关闭文件”或按 `Ctrl+W`，继续处理下一张。关闭软件时保存窗口尺寸、最大化状态和上下预览区比例，下次启动恢复。
7. 点“添加到 PDF-XChange 图章…”可将当前签名追加到现有图章集合，无需先手动导出 PDF。默认使用用户指定的 `D:\Program Files\PDF-XChange_Editor_Plus\Stamps\王华彬签名.pdf`，可浏览选择其他集合，成功后记住路径。允许运行中尝试导入，不因 PDF-XChange 进程存在而拦截。成功后查看图章面板是否自动更新，未出现时可尝试重新打开面板或重启软件。

图章导入保留已有图章及名称登记，并在集合所在目录的 `SigMarker-backups/` 中保存原文件副本（`.pdf.bak`，不会作为图章加载）。校验通过后才替换集合；普通 PDF 不会被当作集合覆盖。格式依据 [PDF-XChange 厂商说明](https://forum.pdf-xchange.com/viewtopic.php?t=19555)。图章路径需允许当前用户写入，受保护或加密的集合不支持修改。软件不会自动启动或关闭 PDF-XChange。

“已写入集合”只表示文件保存成功，不代表 PDF-XChange 图章面板已加载。用户已在当前环境确认运行时实际锁定集合文件，导入时需退出 PDF-XChange 释放锁定。工具仍允许尝试；遇到实际锁定或集合同时被其他程序修改时停止替换并保留原文件。参考：[集合文件锁定](https://forum.pdf-xchange.com/viewtopic.php?p=175349)、[重启加载集合](https://forum.pdf-xchange.com/viewtopic.php?f=62&t=36122)。

自动范围针对白纸上的手写内容，不区分姓名、日期和印刷正文；有复杂文字或桌面背景时可拖动裁剪框及手柄微调。图像加载遵循 EXIF 方向，保留原始像素尺寸，不通过放大制造细节。

## 源码运行和验证

Python 3.10 或以上，安装 `requirements.txt` 后运行 `python main.py`。

运行 `python -m pytest -q tests`（需安装 pytest）。回归覆盖靠边笔画、淡墨、阴影、桌沿、透明 PNG/PDF、剪贴板 PNG/DIBV5 的透明度和 DPI、各格式物理尺寸一致、DPI 记忆、预览区拖放、连续打开、关闭和旋转方向，以及图章登记保留、原页面渲染一致、备份和写入失败保护。剪贴板测试只检查内存数据与模拟 API，不读写系统剪贴板。

打包需要 PyInstaller，双击 `build.bat` 或运行 `python -m PyInstaller --noconfirm --clean SigMarker.spec`。OpenCV 随便携包分发。

`SigMarker.spec` 在构建进程内隔离外部工具的 DLL 搜索路径，避免混入同名但不兼容的系统运行库。`build.bat` 打包后会自动执行无界面成品自检，报告保存在 `.verification/package-self-test.json`；自检不会打开桌面窗口。直接执行 PyInstaller 命令时，需另行运行 `dist/SigMarker/SigMarker.exe --self-test .verification/package-self-test.json`。自检覆盖实际依赖加载、窗口构建、图片加载与范围检测、双底色预览、裁剪旋转、透明 PNG/PDF 导出、合成图章集合追加和关闭后重开，不会修改用户真实图章。

## 项目目录

- `main.py`：统一启动入口，保留 `python main.py` 和 `--self-test` 用法。
- `sigmarker/`：界面（`app.py`）、算法、剪贴板、图章导入及成品自检代码。
- `tests/`：合成图像及 Qt 回归测试，从项目根目录运行 `python -m pytest -q tests`。
- `SigMarker.spec`、`build.bat`：打包配置与 Windows 打包入口。
- `.verification/`：本地验证记录、旧包备份及 `diagnostics/` 历史诊断脚本，不纳入 Git。
- `build/`、`dist/`：构建中间文件及成品，不纳入 Git。分发时保留 `SigMarker.exe` 与 `_internal/` 整个目录。

算法命令行入口使用 `python -m sigmarker.sigextract --help`。包内模块通过包导入，不直接运行 `sigmarker/` 内的脚本。
