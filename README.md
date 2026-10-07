# MapleStory EXP Tracker

Windows 冒险岛经验统计悬浮工具。通过屏幕截图和 OCR 读取经验，使用 Python / Tkinter 绘制悬浮球、统计面板与历史图表。仅观察屏幕，不读取游戏内存、不注入进程、不控制游戏操作。

## 功能

- 可拖动、可调透明度的悬浮球与夜航统计面板。
- 累计经验、有效计时，以及 1 / 2 / 5 / 10 / 30 分钟分均窗口。
- 升级识别与动画；无法确认的经验下降会暂停核对。
- SQLite 本地历史、日期查询、分钟经验与全天曲线，日期统一按北京时间（UTC+8）。
- 正常退出后接续累计值和已确认的窗口样本；重新进入游戏时先建立基线，离线收益与离线时间不计入。
- 实验性的战斗数字、怪物识别与明细。识别效果受游戏版本、字体、分辨率和遮挡影响。

## 运行

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

超过 5 秒未取得可信读数时，保留已有窗口样本；恢复读数后接续进度。中断的时长与补记经验不加入分均窗口，累计账本仍按能够确认的经验净差记账。短暂失败仍显示识别重试状态，过期读数不作为实时结果。

切屏、游戏隐藏和程序重启需要重新建立基线；这些不可观察期间的收益不会补造。手动暂停、清空或重新基准仍会清空当前分均采样。升级尾部收益不可确定时，保留已确认收益并以 `≥` 标记下界。详见 [采样中断接续规则](docs/observation-continuity.md)。

## 数据与资源

- 历史记录默认保存在 `data/history.sqlite3`，可用 `MAPLE_EXP_DATA_DIR` 指定数据目录。
- 用户配置、实时状态与识别诊断保存在 `%LOCALAPPDATA%\MapleExpTracker`。
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
