# SigMarker 项目约定

## 项目目标

从白纸扫描件或手机照片提取手写签名，导出透明 PNG 或带透明蒙版的 PDF，用于文件水印。先读 `README.md` 了解当前功能和限制；目前 PDF 读取第 1 页，自动检测不区分姓名、日期和印刷正文。

## 文件职责

- `main.py`：源码及打包启动入口，自检分支在加载 Qt 前执行。
- `sigmarker/app.py`：PyQt6 界面、文件加载、裁剪交互、预览、剪贴板及导出入口。
- `sigmarker/sigextract.py`：背景校正、墨迹范围检测、方向判断、RGBA 渲染及 PNG/PDF 导出；算法保持独立于界面。
- `sigmarker/stamp_import.py`：PDF-XChange 图章集合的验证、追加、原文件备份和原子替换。
- `sigmarker/clipboard_image.py`：带 DPI 的 PNG/CF_DIBV5 编码及 Windows 剪贴板内存所有权管理。
- `tests/test_sigmarker.py`：合成图像和 Qt 回归测试。
- `tests/test_stamp_import.py`：图章名称登记、页面与元数据保留、透明度和写入失败保护测试。
- `sigmarker/package_smoke.py`：成品无界面启动及核心流程自检，由 `--self-test` 参数调用。
- `build.bat`：供用户运行的打包入口；包含结束暂停。
- `SigMarker.spec`：PyInstaller 打包配置的唯一来源。
- `README.md`：使用方法、限制及开发命令。
- `build/`、`dist/`：构建中间文件和便携包，不作为源码修改。
- `.verification/`：本地验证图片、日志、检查报告和旧包备份；按需读取，不把其全部内容作为项目上下文。
- `.verification/diagnostics/_diag.py`：历史样本诊断脚本，含本机样本路径，不属于通用测试入口。

## 修改原则

- 先检查现有实现并定位原因，再做与任务直接相关的修改。
- 保留原始文件和真实签名样本；通用测试优先使用合成图像。
- 预览底色仅用于检查，不能写进导出的透明图像。验证淡墨、细笔画和透明边缘时同时考虑保留墨迹与去除纸色。
- 纸色/光照校正是签名提取的必经步骤，不提供关闭选项；加载设置时清理旧版 `flatten_bg` 状态，避免灰纸被当作墨迹。自动检测和渲染均按局部纸色分离墨迹。
- 墨迹浓度独立于背景清除，在归一化之后增强 alpha，范围 1～3 倍、默认 1 倍并记忆；保持默认输出一致，背景透明、墨色及像素尺寸不变，整体不透明度最后应用。
- 预览同时显示同尺寸的深灰/白底对照；文件名仅在顶部文件栏显示。
- 用户指定下次打包采用 Windows 11 样式，正式启动使用 Qt `windows11`，验收配置应与成品保持一致。仅核对布局时若使用不同样式或字体，须明确说明差异。
- 复制、PNG、PDF 和图章共用输出 DPI，默认 300，界面可调 72～1200 并记忆；显示毫米尺寸，不重采样。Windows 原生 CF_DIBV5 的密度字段需显式写入，Qt 默认转换会置零。剪贴板测试仅检查内存数据和模拟 API，不读写用户系统剪贴板。
- 图章功能只在用户点击导入时写入所选集合。用户已要求允许运行中尝试，不按 PDF-XChange 进程主动拦截；实际文件锁定或并发变更时停止，保留原文件。保留已有页面、名称登记及元数据；验证通过、保存原文件备份后原子替换。成功仅确认文件已写入，不声称图章面板已刷新。测试使用合成集合或真实集合的本地副本，不改用户正在使用的图章文件。
- 修改算法时补充或运行相关回归；修改界面时关注拖放、连续打开、关闭文件、裁剪和旋转的相邻行为。
- 更新文件入口、功能限制或构建方式时，同步相关说明。项目规模较小，按实际需要调整目录，避免无关重构。

## 运行与验证

以下命令均从项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q tests
```

pytest 和 PyInstaller 为开发工具，使用相关命令前检查是否已安装。Qt 回归测试使用 `offscreen` 模式。仅修改说明文档时无需重跑应用测试或重新打包；未变更输入的已有验证结果可复用。

## 打包与成品验证

- 用户可运行 `build.bat`。代理在后台执行其相同构建与自检命令，避免交互式 `pause`：先 `python -m PyInstaller --noconfirm --clean SigMarker.spec`，再 `dist/SigMarker/SigMarker.exe --self-test .verification/package-self-test.json`。自检应等待进程退出，确认退出码为 0 且报告 `ok` 为 `true`。
- 项目已接入 GitHub；后续打包不再备份旧便携包，目标目录未被占用时直接重新构建。已有历史备份按需保留，不自动清理。
- 运行中的旧版可能锁住目录；此时不关闭用户进程。使用 PyInstaller 的 `--distpath` 输出到独立目录，无需复制或备份旧包，成品自检和交付链接必须指向该实际新包。
- `build.bat` 负责构建及无界面成品自检，不执行旧包备份。
- 成品为 `dist/SigMarker/SigMarker.exe`，必须与同目录的 `_internal/` 一起分发。
- 检查成品包含本次界面和算法代码，以及 OpenCV、Qt Windows 平台插件和 PDF 运行依赖，并实际运行成品自检。仅检查文件存在不足以证明 DLL 能加载。保持 spec 的构建 PATH 隔离，避免外部工具的 ICU/UCRT DLL 混入。明确区分后台自检与用户桌面实机验收。

## 桌面操作约定

默认通过后台命令完成开发、测试和打包，不自行启动、置前或操控桌面窗口，不抢占用户键鼠。软件界面由用户自行启动验收；只有用户明确要求桌面操作时才进行。交付时给出可点击的 EXE 路径及必要的使用说明。

允许后台执行 `--self-test`：它强制使用 Qt `offscreen` 平台，不显示桌面窗口、不发送键鼠输入。

## Git 提交约定

- 提交信息遵循 Conventional Commits：`<type>(<scope>): <description>`，`scope` 可省略。
- 按修改内容使用 `feat`、`fix`、`refactor`、`docs`、`test`、`build`、`ci` 或 `chore`；描述简洁明确，可使用中文。
- 示例：`fix(ui): 提高深灰预览提示文字对比度`、`docs: 更新打包约定`。
