# 本地分析日志

软件自动记录采样、统计和显示状态的关键事件，供复现异常时分析。日志只写本机，不上传，不触发模型、聊天或定时评审。它不改变经验接受条件和计账规则。

## 位置与读取

- 分析事件：`%LOCALAPPDATA%\MapleExpTracker\analysis-logs\analysis.jsonl`，历史文件为 `.1`、`.2`。
- 失败画面：同级 `recognition-failures` 下的既有 24 个小区域证据槽。
- 当前日志健康状态：`live-status.json` 的 `diagnostics` 字段。

在项目目录运行只读摘要：

```powershell
.\.venv\Scripts\python.exe analysis_log.py --summary "$env:LOCALAPPDATA\MapleExpTracker\analysis-logs"
```

摘要统计已有事件、原因和日志丢失计数，不自动把一种可能性判断为根因。原始 JSONL 每行是一个事件；读取过程中可能遇到尚未写完的末行，摘要会注明。

## 如何串起证据

`run_id` 区分运行实例，`sample_id` 标识一次采样尝试，`generation` 区分暂停/继续或切换采样来源。正常采样约每 5 秒选择一次共同的 `anchor`，其 worker、判定、持久化事件沿用同一编号；其他失败、状态变化及缓慢操作也会记录。重复的同一种异常合并计数，原因改变时立即尝试记录。

事件的 `wall_time` 是 Unix 时间，便于与历史记录对时；时长和事件排序使用同一次运行中的单调时钟，不能跨进程直接相减。`worker_started_at`、`observed_at`、`enqueued_at`、`consumed_at` 分别帮助区分采样启动、截图、排队与界面消费。缺失字段保持未知，不当成耗时为零。

`sample_decision` 的 `before` 是应用处理该结果前的统计快照；`stage=after_feed` 时，`after` 是 Tracker 判定后、应用状态更新及 SQLite 保存前的快照。它不是已保存的最终状态。只有 `history_committed` 的 `persisted=true` 及 `session_id/history_seq` 证明该记录已入库。`history_attempt` 和 `history_commit_failed` 不能作为已入账证据。

前因最多保存 8 条受限摘要，异常事件尽力附带。日志本身有损：不能假设每个采样都存在全部阶段，也不能仅凭日志缺行认定程序没有执行。

## 可检查的异常路径

| 事件或字段 | 已观察到的事实 | 可继续检查的可能性与边界 |
| --- | --- | --- |
| `window_not_found/window_geometry/window_size` | 目标窗口查找或几何检查失败 | 游戏退出、窗口标题变化、窗口状态；不能统一归为遮挡 |
| `minimized/occluded/visibility_suspended` | 明确的可见性检查未通过 | 窗口最小化或经验区域被挡；恢复首帧只建立基线 |
| `anchor/anchor_template_missing` | 经验栏定位或模板加载失败 | HUD位置、缩放、布局及资源文件；不是经验为零 |
| `ocr_rejected`、`ocr_reason/percent_reason` | 读数未通过当前校验 | 截断、缺字、候选冲突、低质量；原因是校验结论，不保证等同真实画面根因 |
| `level_pending`、`level` | 候选等级、票数和核验等待状态 | 升级闪光、读数变化等需后续证据；不能提前写成已升级或死亡 |
| `worker_wait/worker_completed`、`timing_ms` | 任务进行中或阶段耗时 | 区分引擎初始化、捕获、EXP OCR、等级 OCR；长耗时不等同永久卡死 |
| `tick_late/tick_slow`、`queue_age/frame_age` | Tk调用迟到、调用耗时或结果滞留 | UI工作、保存或调度延迟；结合 worker 耗时定位，不猜测CPU/磁盘根因 |
| `expired_frame/generation_discard/timestamp_discard` | 结果因时间或采样代次被丢弃 | 比较原截图时间、排队时间和暂停/切换操作 |
| `gap` 与前后五窗口摘要 | 长中断后累计净差和窗口接续结果 | `ledger_only` 表示净差只进入累计；segment增加本身不证明窗口清空 |
| `display_cache_cleared/display_transition` | 显示缓存清除原因、raw与display状态及数值是否存在 | 区分首次采样、暂停、非法值、旧缓存、代次/segment变化；0与缺失分别记录 |
| `surface_configured` | 悬浮球、主面板或多尺度窗口的控件配置调用已返回 | 可对比后台与控件路径；不是用户屏幕像素已经呈现的证明，动画期间单独标明 |
| `history_commit_failed/checkpoint_failed/storage_failure` | 保存失败，含异常类型和处理状态 | 数据库占用、权限、磁盘等需另查；不会将内存增量误标为已保存 |
| `statistics_restore` | 恢复成功、旧版恢复、未找到或拒绝、保存层错误 | 首版保留 `absent_or_rejected` 的歧义，不为细分日志改变恢复流程 |
| `tick_exception/ui_callback_exception` | 回调异常类型及函数名 | 不吞掉原有异常处理，不自动重启或重试；不记录异常消息、堆栈或局部变量 |
| `live_status_write_failed` | 状态快照写入失败 | 文本日志与状态快照相互独立，结合健康状态检查 |

等级 `vote_span` 按采集时间计算，等同投票使用的时间轴；`wait_age` 是当前单调时刻减开始等待的时刻，包含排队与界面等待，二者不能混用。

## 证据槽与资源上限

截图槽会轮换。查看 `recognition_evidence` 对应文件时，必须核对该 JSON 的 `metadata.run_id` 和 `metadata.sample_id` 是否与事件一致；仅文件名相同不能证明还是同一次异常。旧槽可能已经被后一次失败覆盖。

文本队列默认最多 256 条，单行最多 8 KiB，三个文件各最多 2 MiB，合计约 6 MiB；保留时长取决于事件频率。截图写入另用独立后台线程，最多 1 条待写任务和 1 条进行中任务；仍只保留既有尺寸限制内的小区域，不增加全屏截图。

生产者不做磁盘、JSON或PNG编码。满队列或磁盘错误时丢弃日志并计数；关闭只等待有限时间，不能为日志卡住退出。健康状态中的 `dropped`、`suppressed`、`write_errors`、`pending` 等说明日志完整性，诊断失败不会变成计账失败。

分析日志只接受有限的数值、布尔值、标识符和枚举，不复制 OCR 原文、任意错误消息、路径、窗口标题、配置或截图。既有 ROI 证据仍可能包含玩家的局部游戏画面，应留在本机。程序/系统强杀、断电、完整Tk线程冻结、轮转和关闭未完成的写入都可能留下取证缺口。
