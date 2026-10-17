# 冒险岛经验统计 / MapleStory EXP Tracker

Windows 冒险岛经验统计悬浮工具。通过屏幕截图和 OCR 读取经验，使用 Python / Tkinter 绘制悬浮球、统计面板与历史图表。仅观察屏幕，不读取游戏内存、不注入进程、不控制游戏操作。

用于冒险岛怀旧服的经验监控与练级效率统计：查看每分钟经验（EXP/min）、每小时经验（EXP/hour）、升级进度和历史收益，比较不同时间段的打怪效率。冒险岛也常以枫之谷／楓之谷、MapleStory 等名称检索。

A Windows **MapleStory EXP / XP tracker** with an **OCR game overlay** for experience monitoring, EXP per minute, EXP per hour, leveling efficiency, level-up progress and local session history. Includes a floating desktop widget and a standalone Windows release; reads visible screen pixels only.

**直接使用：** [下载 Windows x64 独立版 1.1.0](https://github.com/wanpidan1234-design/maplestory-exp/releases/download/v1.1.0/MapleExpTracker-1.1.0-windows-x64.zip)（无需安装 Python） · [版本说明](https://github.com/wanpidan1234-design/maplestory-exp/releases/tag/v1.1.0)

## 网页版

[打开网页版](https://wanpidan1234-design.github.io/maplestory-exp/) · [启动与功能说明](docs/web-companion.md)。运行 `start-web.cmd` 启动配套本机服务；首次启动需安装 Python 3.10+。本机负责截图、识别和存储，网页负责显示与操作。

## 功能

- 可拖动、可调透明度的悬浮球与窄版夜航统计面板。
- 累计经验、有效计时，以及 1 / 2 / 5 / 10 / 30 分钟分均窗口。
- 每分钟／每小时经验百分比，以及按当前 1 分钟分均折算的预计升级剩余时间。百分比优先采用已知升级经验上限，否则由同次已确认的当前经验与进度折算；读数缺失或没有经验增长时不提供升级时间，保留旧读数时标明“上次估算”。
- [花 Buff 观察与音乐提醒](docs/flower-buff.md)：识别神圣祈祷图标和剩余时间，默认提前 15 秒响铃，每轮一次；识别过的 Buff 被喇叭遮挡时保留图标、阴影和预计倒计时，重新读到秒数后校准。可在识别设置中关闭提醒或调整提前量。
- 升级识别与动画；无法确认的经验下降会暂停核对。
- SQLite 本地历史、日期查询、分钟经验与全天曲线，日期统一按北京时间（UTC+8）。
- 正常退出后接续累计值和已确认的窗口样本；重新进入游戏时先建立基线，离线收益与离线时间不计入。
- 实验性的战斗数字、怪物识别与明细。识别效果受游戏版本、字体、分辨率和遮挡影响。

## 运行

### Windows 独立版（无需安装 Python）

适用于 **Windows 10/11 x64**。已内置 Python、OCR 模型及界面动画，启动不需要下载依赖，也不需要安装 Codex。

1. 下载上方独立版 ZIP，或打开 [Release 1.1.0](https://github.com/wanpidan1234-design/maplestory-exp/releases/tag/v1.1.0)，在 **Assets** 中选择 `MapleExpTracker-1.1.0-windows-x64.zip`。GitHub 自动提供的 `Source code` 是源码包，不含可直接启动的程序。
2. 右键压缩包，选择“全部解压”，放到自己方便找到的文件夹。支持中文和空格路径，请勿直接在压缩包内运行。
3. 打开解压后的 `MapleExpTracker` 文件夹，双击 `MapleExpTracker.exe`。保留旁边的 `_internal` 文件夹，不能只复制 EXE。
4. 进入游戏并让经验栏保持可见。程序会尝试自动定位游戏；点击悬浮球可展开统计面板，拖动悬浮球可调整位置。
5. 等待首个可信读数建立基线。后续经验变化开始计入统计；“采样中”表示观察窗口还未填满，并非程序故障。

解压后的主要文件如下：

```text
MapleExpTracker/
├─ MapleExpTracker.exe       # 双击启动
├─ _internal/               # 必须保留的运行库、OCR 模型和界面资源
├─ README.md
├─ BUILD.json               # 源码提交和依赖版本
└─ THIRD_PARTY_LICENSES/    # 第三方许可
```

### 日常操作与识别设置

**右键悬浮球**可打开操作菜单：

| 操作 | 菜单或使用方法 |
| --- | --- |
| 查看主统计 | 点击悬浮球，或选择“展开 / 收起统计” |
| 暂停或继续 | “暂停 / 继续”；手动暂停会清空当前分均采样 |
| 自动定位失败 | 确认游戏窗口可见，选择“重新定位游戏” |
| 手动选择识别区域 | “框选经验栏…”；选取完整经验数字区域，避免混入聊天和其他文字 |
| 调整识别参数 | “识别设置…” |
| 调整花 Buff 提醒 | “识别设置…”中的花 Buff 提醒、声音和提前秒数 |
| 查看已保存记录 | “历史回看 / 每日总计” |
| 对比各时间窗口 | “多尺度分均” |
| 查看战斗统计 | “战斗伤害 / 血蓝消耗”（实验性功能） |
| 导出或退出 | “导出本次记录”导出 CSV；“退出”正常保存并关闭 |

游戏经验栏被遮挡、游戏最小化或切屏时，可能显示“等待读数”或“切屏暂停”。恢复可见后等待新的可信读数，不需要反复重置。界面显示“核验变化”或要求处理经验下降时，先核对实际升级情况，再通过识别设置中的确认操作处理。

### 保存数据、升级与校验

独立版的数据保存在程序目录之外：

| 内容 | 默认位置 |
| --- | --- |
| 历史数据库 | `%LOCALAPPDATA%\MapleExpTracker\data\history.sqlite3` |
| 识别设置和悬浮球配置 | `%LOCALAPPDATA%\MapleExpTracker` |
| 本地诊断日志 | `%LOCALAPPDATA%\MapleExpTracker\analysis-logs` |

可将 `%LOCALAPPDATA%\MapleExpTracker` 粘贴到资源管理器地址栏打开。升级前先从悬浮球菜单正常退出，备份该目录，再将新版解压到新文件夹启动；保留用户数据目录即可保留历史。若设置过 `MAPLE_EXP_DATA_DIR`，历史会写入指定目录，备份时也要包含它。源码版默认使用项目内 `data/history.sqlite3`，切换到独立版时不会自动搬迁该数据库。

校验下载时，另行下载 Release 附件 [SHA256SUMS.txt](https://github.com/wanpidan1234-design/maplestory-exp/releases/download/v1.1.0/SHA256SUMS.txt)，与 ZIP 放在同一个文件夹。打开该文件夹中的 PowerShell，运行：

```powershell
Get-FileHash .\MapleExpTracker-1.1.0-windows-x64.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

两处的 SHA-256 应完全相同（字母大小写可忽略）。当前 1.1.0 未做代码签名；若 Windows 提示无法验证发布者，可先核对本仓库的下载来源和校验值。

### 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 找不到 EXE | 检查是否误下载了 `Source code`，应下载名称含 `windows-x64` 的 ZIP |
| 启动提示缺少 DLL、模型或资源 | 重新完整解压，确认 `_internal` 与 EXE 位于同一目录 |
| 再次双击没有新窗口 | 程序使用单实例，请先查看已经运行的悬浮球 |
| 未找到游戏或长期没有读数 | 保持经验栏完整可见；尝试“重新定位游戏”或手动框选。游戏版本、字体、分辨率和遮挡都可能影响识别 |
| 战斗面板没有怪物原画 | 发行包不包含怪物原画包，使用中性占位图属于预期行为 |

需要检查安装包是否完整可运行时，可在解压目录的 PowerShell 中执行：

```powershell
Start-Process .\MapleExpTracker.exe -ArgumentList '--self-test-report self-test.json' -Wait
Get-Content .\self-test.json
```

结果中的 `status` 为 `passed` 表示自检通过。自检会短暂打开测试窗口，使用临时数据目录，不采集实际游戏画面，也不修改已有历史。独立版始终关闭自动开发评审。

### 从源码运行

需要 Windows 10/11 和 Python 3.10+（带 Tkinter）。开发环境验证使用 Python 3.13。首次安装需要网络下载依赖。

```powershell
git clone https://github.com/wanpidan1234-design/maplestory-exp.git
cd maplestory-exp
.\start.cmd
```

也可以手动运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe orb.py
```

保持游戏经验栏可见，在识别设置中选择游戏窗口或屏幕区域。首次可信读数建立基线，后续读数开始计入增量。不同版本可能需要调整识别区域。

## 统计规则

主分均使用最近 60 秒**有效观察时间**；多尺度窗口采用相同端点和规则。采样尚未填满窗口时，以已观察时长计算，界面显示采样进度。趋势曲线另使用时间加权平滑。

超过 5 秒未取得可信读数时，保留已有窗口样本；恢复读数后接续进度。中断的时长与补记经验不加入分均窗口，累计账本仍按能够确认的经验净差记账。识别重试、截图中断或升级核验时，界面保留最后确认的分均、经验、百分比和进度，以暖色及“等待读数／核验变化”和数值年龄标明状态；这些显示值不作为新采样或实时结果。

切屏、游戏隐藏和程序重启需要重新建立基线；这些不可观察期间的收益不会补造。手动暂停、清空或重新基准仍会清空当前分均采样。升级尾部收益不可确定时，保留已确认收益并以 `≥` 标记下界。详见 [采样中断接续规则](docs/observation-continuity.md)。

## 数据与资源

- 源码版历史默认保存在项目内 `data/history.sqlite3`；独立版默认保存在 `%LOCALAPPDATA%\MapleExpTracker\data\history.sqlite3`。两者均可用 `MAPLE_EXP_DATA_DIR` 指定数据目录。
- 用户配置、实时状态与识别诊断保存在 `%LOCALAPPDATA%\MapleExpTracker`。
- 有界、异步的[本地分析日志](docs/diagnostics.md)记录采样中断、拒绝原因、入库结果、显示变化和恢复过程；日志故障不阻塞计账。
- 仓库不包含玩家历史、识别截图、运行日志、本机配置、凭据及私人评审记录。
- 怪物原画包不随仓库分发；缺失时显示中性占位图。已有本地资源可放入 `assets/monster-sprites/`。游戏名称和图像的相关权利归其权利人。
- 界面动画帧在 `assets/levelup-d-dynamic/`，首次克隆体积较大。

源码包含可选的事件驱动开发评审集成。公开版没有本地 `STARTUP_REVIEW.md` 授权文档，不会自动派发评审；统计功能不依赖 Codex。此仓库不提供私人自动开发策略或聊天收件人配置。

## 验证

在 Windows 上安装依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
```

公开测试覆盖统计、接续、SQLite 历史、图表、原生界面与评审基础设施。少量可选图片测试会在资源不存在时跳过；私人截图语料与依赖这些语料的专用测试不随仓库发布。原生界面测试需要可用的 Windows 桌面。

## 构建独立版

在 Windows x64 的项目虚拟环境安装 `requirements-build.txt` 后，运行 `python build_release.py`。构建仅使用当前 Git 提交中的公开源码和资源，未提交的修改不会进入发行包；产物位于 `dist/`。`BUILD.json` 记录源提交与依赖版本，`THIRD_PARTY_LICENSES/` 包含随包依赖的许可。

发行程序支持 `MapleExpTracker.exe --self-test-report report.json`，使用临时数据目录检查内置 OCR、资源、窗口和数据库，不采集实际游戏画面。

## 相关关键词

中文：冒险岛、冒险岛怀旧服、冒險島、枫之谷、楓之谷、经验统计、经验监控、经验计数器、经验计算器、分均经验、时均经验、练级效率、打怪效率、升级进度、游戏悬浮窗、桌面悬浮球、屏幕文字识别。

English: MapleStory, MapleStory Classic, EXP tracker, XP tracker, experience tracker, EXP counter, EXP calculator, EXP per minute, EXP per hour, leveling efficiency, level-up tracker, game overlay, floating widget, OCR, screen capture, Windows desktop app, Python, Tkinter, RapidOCR, ONNX Runtime, SQLite.
