# Sakura 任务提醒、工作查询与周总结设计

状态：设计已确认，等待用户审阅
日期：2026-07-25
目标版本：第一版
运行环境：Windows 11、Sakura v0.9.9、Python 3.12、Asia/Shanghai

## 1. 文档目的

本设计把 Sakura 桌宠扩展为一个不依赖 Codex 或 Claude Code 持续运行的个人任务入口，第一版完成以下闭环：

1. 通过自然语言创建、修改、完成、取消和查询任务。
2. 在 Sakura 中用文字气泡和语音播报今日工作与到期提醒。
3. 在 Sakura 未运行时，通过 Windows 任务计划程序和 QQ 邮箱继续发送截止提醒。
4. 每周日自动生成个人周总结草稿，经用户确认后上传到私有 GitHub 仓库。
5. 只在总结成功归档后，清理满足 14 天期限的本地原始任务记录。

本文档是任务子系统的实施依据。若与
`docs/plans/2026-07-23-sakura-personal-assistant-roadmap.md` 中的旧描述冲突，以本文档为准。特别是：

- 今日查询顺序确定为“逾期 → 今天截止 → 今天计划”。
- 逾期未完成任务在逾期满 14 天、且已经成功归档后也允许清理。

## 2. 范围

### 2.1 第一版包含

- 本地 SQLite 任务数据库。
- 高、普通、低三级优先级，默认普通。
- 计划日期和截止时间。
- 今日工作、全部未完成工作和逾期工作查询。
- 截止前 24 小时和截止前 2 小时提醒。
- Sakura 文字气泡和语音提醒。
- QQ SMTP 邮件提醒。
- Windows 任务计划程序省电运行。
- 简单的按星期周期提醒。
- 周一至周五 14:00 股票计划检查提醒，允许延迟 30 分钟。
- 每周日 20:00 周总结。
- DeepSeek V4 Pro 总结和本地降级总结。
- 用户确认后的私有 GitHub 归档。
- 满足安全条件后的 14 天数据清理。
- 现有 `tasks.json` 和 `reminders.json` 的幂等迁移。

### 2.2 第一版不包含

- 个人资料库或网页表单自动填写。
- 复杂项目、依赖关系、甘特图和多人协作。
- 股票行情分析、投资建议或证券账户操作。
- 自动识别证券市场实际交易日。
- 笔记本关机时仍准点发送的服务器提醒模式。
- 自动公开周总结或自动发布博客。
- 修改博客、Nginx 或线上服务器。
- 未经确认的 GitHub 上传。
- 密码、身份证、银行卡等个人敏感资料管理。
- 使用 Codex CLI 或 Claude Code 生成周总结。

## 3. 已确认的产品规则

### 3.1 Sakura 是主要入口

用户可以直接对桌宠说：

- “明天下午三点完成实验报告，优先级高。”
- “把实验报告延期到周五。”
- “实验报告做完了。”
- “取消买书这个任务。”
- “今天有什么工作？”
- “还有哪些逾期任务？”
- “查看本周总结。”
- “上传本周总结。”

明确的单项操作可以直接执行并回显。日期含糊、同名任务冲突、批量操作和破坏性操作必须先确认。

### 3.2 显示和语音

- 文字气泡是完整、权威的信息来源。
- 语音只播报数量、逾期数量、高优先级数量和最近截止事项。
- 语言模型可以润色表达，但不得增加、删除或隐藏查询结果。
- 提醒展示成功不会自动把任务标记为完成。

### 3.3 优先级

- `high`：高。
- `normal`：普通，默认值。
- `low`：低。

优先级影响查询排序、气泡标签和语音强调，不增加或减少默认提醒次数。

### 3.4 时间和保留

- 用户可见时间统一使用 `Asia/Shanghai`。
- 数据库中的时间点统一保存为 UTC。
- 本地日期，如 `planned_date`，按 `Asia/Shanghai` 解释。
- 已完成任务从 `completed_at` 起满 14 天后可清理。
- 已取消任务从 `cancelled_at` 起满 14 天后可清理。
- 仍为 `pending` 的逾期任务从 `due_at` 起满 14 天后可清理。
- 未逾期且未完成的任务不自动删除。
- 没有截止时间且未完成的任务不自动删除。
- 任何任务只有在成功进入已上传的周总结后才允许清理。

14 天是满足条件后的最早清理时间，不是强制删除时间。如果总结尚未确认、上传失败或任务归档版本失效，原始记录必须继续保留，直至后续某次成功归档。

## 4. 总体架构

```text
Sakura 自然语言输入
        │
        ▼
任务工具适配层 app/agent/task_tools.py
        │
        ▼
TaskService ──────────────── TodayQueryService
        │                            │
        ├── TaskRepository           └── 确定性查询结果
        ├── ReminderRepository
        └── ReminderScheduler
                    │
                    ▼
             SQLite tasks.db
                    ▲
                    │
Windows Reminder Worker ─── QQ SMTP
                    │
Windows Weekly Summary Worker
                    │
                    ├── DeepSeek V4 Pro
                    └── 本地规则总结

ReminderScheduler ── AgentEvent ── 文字气泡/TTS
WeeklySummaryService ── 用户确认 ── 私有 GitHub
```

架构遵循以下边界：

- 所有任务写操作必须通过 `TaskService`。
- UI、工具适配器和后台程序不得拼接 SQL。
- `pet_window.py` 只接收准备好的事件，不承担任务业务逻辑。
- 后台邮件程序不加载 PySide6、Sakura 窗口或大语言模型。
- 周总结程序只读取经过筛选的任务快照，不向模型提供数据库权限。
- Git 发布器只能提交指定的周总结文件。

## 5. 模块设计

### 5.1 任务模块

```text
app/tasks/
├── models.py
├── database.py
├── repository.py
├── service.py
├── today_query.py
├── scheduler.py
└── migration.py
```

- `models.py`：任务、任务事件、提醒规则和提醒实例的领域模型及枚举。
- `database.py`：连接、建表、迁移、事务、WAL 和 `busy_timeout`。
- `repository.py`：所有任务及提醒的持久化操作。
- `service.py`：创建、修改、完成、取消、恢复任务，并维护提醒一致性。
- `today_query.py`：生成确定性的今日工作结构。
- `scheduler.py`：生成到期实例、领取通知、补发、合并和跳过。
- `migration.py`：迁移旧 JSON 数据。

### 5.2 通知模块

```text
app/notifications/
├── dispatcher.py
├── qq_mail.py
└── credentials.py
```

- `dispatcher.py`：按通知渠道分发并维护发送状态。
- `qq_mail.py`：QQ SMTP SSL 客户端、邮件模板和错误分类。
- `credentials.py`：访问 Windows 凭据管理器，不提供明文日志接口。

### 5.3 周总结模块

```text
app/summaries/
├── models.py
├── snapshot.py
├── repository.py
├── service.py
├── renderer.py
└── providers/
    ├── base.py
    ├── deepseek.py
    └── local_fallback.py
```

- `snapshot.py`：从数据库生成事实快照和确定性统计。
- `repository.py`：保存总结运行状态及任务归档关系。
- `service.py`：协调生成、确认、发布和清理。
- `renderer.py`：把校验后的结构化结果渲染为 Markdown。
- `providers/base.py`：统一的 `SummaryProvider` 接口。
- `providers/deepseek.py`：默认 DeepSeek V4 Pro API 提供者。
- `providers/local_fallback.py`：不联网的固定模板总结。

### 5.4 平台与工具适配

```text
app/agent/task_tools.py
app/integrations/git_publisher.py
app/platforms/reminder_task.py
app/platforms/weekly_summary_task.py
app/workers/reminder_worker.py
app/workers/weekly_summary_worker.py
```

- `task_tools.py`：把 Sakura 工具调用转换为服务请求。
- `git_publisher.py`：只发布已确认的总结 Markdown。
- `reminder_task.py`：注册、查询和维护 Windows 提醒计划任务。
- `weekly_summary_task.py`：注册周日 20:00 和登录补偿触发器。
- 两个 worker 都必须是短生命周期命令，完成一次检查后退出。

## 6. SQLite 数据设计

数据库路径：

```text
data/tasks.db
```

数据库必须启用：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

所有表使用应用级 schema migration 管理。主键使用 UUID 字符串。时间点保存为带 `Z` 的 RFC 3339 UTC 字符串；本地日期保存为 `YYYY-MM-DD`。

### 6.1 `tasks`

| 字段 | 含义 |
|---|---|
| `id` | UUID 主键 |
| `title` | 必填标题 |
| `details` | 可选说明 |
| `status` | `pending`、`completed`、`cancelled` |
| `priority` | `high`、`normal`、`low` |
| `planned_date` | 可选本地计划日期 |
| `due_at` | 可选 UTC 截止时间 |
| `created_at` | 创建时间 |
| `updated_at` | 最后修改时间 |
| `completed_at` | 完成时间 |
| `cancelled_at` | 取消时间 |

约束：

- `title` 去除首尾空格后不能为空。
- `completed` 必须具有 `completed_at`。
- `cancelled` 必须具有 `cancelled_at`。
- `pending` 的两个终态时间必须为空。

### 6.2 `task_events`

用于生成可靠的周总结，记录：

- 创建。
- 标题或详情修改。
- 计划日期修改。
- 截止时间修改或延期。
- 优先级修改。
- 完成、取消和重新打开。

字段包括：

| 字段 | 含义 |
|---|---|
| `id` | UUID |
| `task_id` | 任务 ID |
| `event_type` | 事件类型 |
| `before_json` | 修改前的必要字段 |
| `after_json` | 修改后的必要字段 |
| `occurred_at` | UTC 时间 |

事件只保存总结需要的任务字段，不保存聊天全文。任务被安全清理时，其事件通过外键级联删除。

### 6.3 `reminder_rules`

| 字段 | 含义 |
|---|---|
| `id` | UUID |
| `task_id` | 可为空；周期提醒不一定属于任务 |
| `message` | 提醒内容 |
| `kind` | `deadline_offset` 或 `weekly` |
| `offset_seconds` | 截止偏移，如 `-86400`、`-7200` |
| `weekdays_mask` | 周期提醒星期位掩码 |
| `time_of_day` | 本地时间，如 `14:00:00` |
| `timezone` | 默认 `Asia/Shanghai` |
| `grace_seconds` | 允许补发时长 |
| `desktop_enabled` | 是否生成桌面通知 |
| `email_enabled` | 是否生成邮件通知 |
| `enabled` | 是否启用 |
| `created_at`、`updated_at` | 审计时间 |

带截止时间的任务默认创建两条规则：

- `offset_seconds = -86400`
- `offset_seconds = -7200`

默认启用桌面和邮件两个渠道。

股票提醒是一条独立周期规则：

- 星期一至星期五。
- 14:00。
- `grace_seconds = 1800`。
- 文字气泡、语音和 QQ 邮件。
- 文案只提醒检查个人计划，不提供投资建议。

### 6.4 `reminder_occurrences`

每条规则每次实际发生的提醒实例：

| 字段 | 含义 |
|---|---|
| `id` | UUID |
| `rule_id` | 规则 ID |
| `task_id` | 可为空 |
| `scheduled_at` | 计划时间 |
| `expires_at` | 周期提醒过期时间；截止提醒可为空 |
| `status` | `pending`、`completed`、`skipped`、`cancelled` |
| `skip_reason` | 如 `expired`、`coalesced` |
| `created_at`、`updated_at` | 审计时间 |

唯一约束：

```text
UNIQUE(rule_id, scheduled_at)
```

### 6.5 `notification_deliveries`

每个提醒实例按渠道维护独立状态：

| 字段 | 含义 |
|---|---|
| `id` | UUID |
| `occurrence_id` | 提醒实例 |
| `channel` | `desktop` 或 `email` |
| `status` | `pending`、`sending`、`sent`、`failed`、`skipped` |
| `attempt_count` | 尝试次数 |
| `next_attempt_at` | 下次重试时间 |
| `claimed_at` | 领取时间 |
| `claim_token` | 本次领取令牌 |
| `sent_at` | 成功时间 |
| `last_error_code` | 脱敏错误码 |

唯一约束：

```text
UNIQUE(occurrence_id, channel)
```

桌面成功不会把邮件标记成功，反之亦然。

### 6.6 `weekly_summary_runs`

| 字段 | 含义 |
|---|---|
| `id` | UUID |
| `iso_year`、`iso_week` | 总结周次，联合唯一 |
| `week_start`、`week_end` | 总结时间范围 |
| `status` | 总结状态 |
| `provider` | 实际使用的提供者 |
| `snapshot_sha256` | 事实快照哈希 |
| `draft_path` | 本地 Markdown 草稿 |
| `git_commit_sha` | 远程确认的提交 |
| `last_error_code` | 脱敏错误码 |
| 各阶段时间 | 创建、生成、确认、发布和清理时间 |

状态流转：

```text
pending
→ generating
→ awaiting_approval
→ publishing
→ published
→ cleaned
```

失败进入 `failed`，重试时从最后一个安全阶段继续。

### 6.7 `task_summary_archives`

该表只保留每个现存任务最近一次有效归档关系：

| 字段 | 含义 |
|---|---|
| `task_id` | 任务 ID，唯一 |
| `summary_run_id` | 已发布的总结 |
| `task_updated_at` | 生成快照时的任务版本 |
| `archived_at` | 归档确认时间 |

任务发生任何修改时删除其旧归档关系。总结发布后，仅当任务当前 `updated_at` 与快照一致时建立归档关系。任务清理后关系级联删除，因此该表不会无限增长。

## 7. 服务接口

### 7.1 `TaskService`

```text
create_task(title, details?, planned_date?, due_at?, priority=normal)
update_task(task_ref, changes)
complete_task(task_ref)
cancel_task(task_ref)
reopen_task(task_ref)
get_task(task_ref)
list_tasks(filter)
query_today(now)
```

`task_ref` 优先使用 UUID；通过标题查找时：

1. 精确匹配且唯一，可以执行。
2. 多个同名任务，返回候选项，等待用户选择。
3. 没有精确匹配，不擅自修改近似任务。

任务状态只允许：

```text
pending → completed
pending → cancelled
completed/cancelled → pending（必须明确要求重新打开）
```

### 7.2 Sakura 工具

```text
task_create
task_update
task_complete
task_cancel
task_reopen
task_query
weekly_summary_get
weekly_summary_regenerate
weekly_summary_publish
```

旧的 `add_todo`、`list_todos` 和 `complete_todo` 暂时保留为兼容别名，内部调用 `TaskService`，不再直接操作 JSON。

### 7.3 输入确认

- 明确日期、时间和单项动作：保存后完整回显。
- 只给日期没有时间：保存为计划日期，不虚构截止时刻。
- “下午”“晚点”等不能唯一确定的时间：询问。
- 解析出的截止时间已经过去：保存前确认。
- 同名任务冲突：展示 ID 简写、标题和截止时间供选择。
- 批量完成、取消或修改：执行前确认。
- 服务写入失败：明确告知未保存，不生成成功式回复。

成功示例：

```text
已记录：完成实验报告
优先级：高
计划日期：2026-07-26
截止时间：2026-07-26 15:00
提醒：提前一天、提前两小时
```

## 8. 今日工作查询

`TodayQueryService` 只查询 `status = pending` 的任务，并返回：

```json
{
  "overdue": [],
  "due_today": [],
  "planned_today": [],
  "summary": {
    "total": 0,
    "high_priority": 0,
    "overdue": 0
  }
}
```

分类规则：

1. `overdue`：`due_at < now`。
2. `due_today`：截止时间的上海本地日期为今天，且尚未逾期。
3. `planned_today`：`planned_date` 为今天，且未进入前两组。

去重优先级：

```text
overdue > due_today > planned_today
```

组内排序：

1. 高、普通、低优先级。
2. 截止时间从早到晚，空值最后。
3. 创建时间从早到晚。

气泡显示所有任务。语音示例：

```text
今天共有 5 项工作，其中 1 项逾期、2 项高优先级。
最近截止的是下午三点的实验报告。
```

## 9. 任务与提醒事务

### 9.1 创建任务

带截止时间的任务在同一个事务中：

1. 写入任务。
2. 写入 `created` 事件。
3. 创建两条默认截止提醒规则。
4. 生成必要的提醒实例和渠道发送记录。
5. 提交。

任何一步失败，整次事务回滚。

如果任务创建时截止仍在未来、但一个或两个默认提醒检查点已经过去，则把错过的检查点合并为一条立即可发送的补充提醒；尚未来到的检查点正常保留。如果创建时任务已经逾期，经用户确认后保存，但不自动发送已经失去意义的默认截止邮件。

### 9.2 修改截止时间

在一个事务中：

1. 锁定并读取当前任务。
2. 取消尚未发送的旧截止提醒。
3. 更新 `due_at` 和 `updated_at`。
4. 写入任务事件。
5. 删除旧归档关系。
6. 根据新截止时间重建提醒。

已经发送的通知只作为历史保留，不撤回也不重复发送。

### 9.3 完成、取消和重新打开

- 完成或取消任务时取消其所有未发送提醒。
- 已发送记录继续保留到任务安全清理。
- 重新打开任务时，根据当前截止时间重新生成尚有意义的提醒。
- 这些状态变化都写入 `task_events` 并使旧归档关系失效。

## 10. 提醒执行链路

### 10.1 Sakura 桌面链路

现有 `pet_window.py` 的 30 秒定时器保留，但只调用调度服务：

```text
QTimer
→ ReminderScheduler 生成并领取到期的 desktop delivery
→ AgentEvent(type="reminder_due")
→ 文字气泡
→ TTS 摘要
→ 标记 desktop delivery 已发送
```

如果 TTS 失败但气泡成功，桌面通知仍视为已展示，并记录 TTS 错误；不得因此反复弹出同一气泡。

Sakura 未运行时没有气泡。下次启动时，仍有效的桌面提醒可以补显示。

### 10.2 QQ 邮件链路

Windows 任务计划程序在电脑清醒时每 10 分钟运行一次：

```text
启动轻量 worker
→ 打开 SQLite
→ 生成到期实例
→ 原子领取 email delivery
→ 从 Windows 凭据管理器读取授权码
→ 通过 QQ SMTP SSL 发送
→ 更新结果
→ 退出
```

worker 不导入 PySide6，不启动 Sakura，不调用 Codex、Claude 或 DeepSeek。

计划任务：

- 允许使用电池运行。
- 不允许主动唤醒睡眠电脑。
- 用户登录后执行一次补偿检查。
- 正常误差为 0～10 分钟。

### 10.3 截止提醒补发与合并

截止任务提醒不设置 30 分钟过期限制。电脑恢复后：

- 只错过一个检查点：发送一封补充提醒。
- 同一任务错过两个检查点：合并为一封邮件和一次桌面通知。
- 被合并实例标记为 `skipped/coalesced`。
- 补发内容注明原计划时间和当前是否逾期。
- 已成功发送的渠道不得再次发送。

### 10.4 周期提醒

周期提醒设置 `expires_at = scheduled_at + grace_seconds`：

- 14:00 至 14:30 可以发送股票提醒。
- 超过 14:30 标记 `skipped/expired`。
- 跳过不影响下一个周期。
- 第一版的周一至周五不排除节假日和休市日。

## 11. 并发与幂等

Sakura 和 Windows worker 可能同时访问数据库。领取发送任务时必须：

1. 开启短事务。
2. 选择状态为 `pending` 或租约已过期的记录。
3. 写入唯一 `claim_token`、`claimed_at` 和 `sending` 状态。
4. 提交事务后执行网络发送。
5. 只有持有相同 `claim_token` 的执行者才能写回结果。

发送租约默认 5 分钟。进程崩溃后，租约过期的记录可被下一次 worker 重新领取。

数据库唯一约束防止重复生成提醒实例。外部发送存在极小的“SMTP 已接收但本地未记账”窗口；邮件应包含稳定的通知 ID，并在本地优先保守重试。第一版不承诺由 QQ 邮箱提供端到端精确一次语义，但应用内部必须做到不会由正常并发造成重复发送。

## 12. QQ 邮件与凭据

本地非敏感配置包括：

- 发件 QQ 邮箱。
- 收件邮箱，默认与发件邮箱相同。
- SMTP 主机、端口和 SSL 开关。
- 是否启用邮件。

QQ SMTP 授权码保存在 Windows 凭据管理器，建议凭据目标名：

```text
Sakura/QQSMTP
```

禁止：

- 使用 QQ 登录密码代替授权码。
- 在日志、异常、数据库或配置中记录授权码。
- 把授权码写入环境示例、测试夹具或 Git。

错误处理：

- 网络和 SMTP 临时错误：按后续运行重试，最多连续三次。
- 身份验证错误：停止自动重试，创建一次桌宠配置告警。
- 永久地址错误：标记失败并提示用户。
- 日志只保存稳定错误码和脱敏说明。

## 13. 周总结

### 13.1 触发时间

- 每周日 20:00，`Asia/Shanghai`。
- 总结范围为当周周一 00:00 至实际生成时刻。
- 若电脑关机或休眠，则下次登录或下次计划任务运行时补生成。
- 补生成不受周期提醒的 30 分钟限制。
- 同一个 ISO 周只能生成一个当前正式草稿；重新生成产生新版本并替换未发布草稿。

### 13.2 事实快照

本地程序先生成不可由模型改写的事实：

- 本周创建、完成和取消的任务。
- 本周修改计划日期、截止时间和优先级的任务。
- 当前未完成任务。
- 当前逾期任务。
- 各优先级数量。
- 完成率和逾期数量。
- 本周独立周期提醒的发送、失败和过期跳过数量。
- 下周仍需处理的任务。

快照保存到：

```text
data/weekly_summaries/snapshots/<ISO-YEAR>-W<WEEK>.json
```

快照只保留到发布完成并建立任务归档关系，之后删除。其 SHA-256 保存到 `weekly_summary_runs`。

### 13.3 模型提供者

统一接口：

```text
generate(snapshot) -> StructuredSummary
```

默认顺序：

1. DeepSeek V4 Pro API。
2. 本地规则模板。

第一版不安装、不配置也不调用 Codex CLI 或 Claude Code，因此不需要 Claude API Key、Claude Code 订阅或对应的本机登录状态。

DeepSeek API Key 保存在 Windows 凭据管理器：

```text
Sakura/DeepSeekAPI
```

模型仅接收允许字段组成的事实快照，不接收：

- 数据库文件。
- 数据库路径。
- 聊天全文。
- QQ 授权码。
- API Key。
- GitHub SSH 私钥。
- 服务器凭据。

模型必须返回符合 schema 的 JSON。本地校验失败时允许一次格式修复请求；再次失败即切换到本地规则模板。最终由 `renderer.py` 生成 Markdown，模型不能直接写文件、操作数据库或运行 Git。

### 13.4 Markdown

本地草稿：

```text
data/weekly_summaries/drafts/<ISO-YEAR>-W<WEEK>.md
```

固定结构：

```markdown
# <年份> 年第 <周数> 周个人总结

## 本周概览
## 主要完成事项
## 仍在进行的工作
## 逾期未完成事项
## 计划变更与延期
## 时间与任务统计
## 下周建议关注
## 系统归档清单
```

模型生成的评价和建议必须与事实段落分离。任务数量、状态和日期全部使用本地计算结果。

“系统归档清单”由本地渲染器生成，模型无权删改。清单必须逐项列出事实快照中的任务 ID 简写、标题、状态、计划日期、截止时间和本周状态变化。文件末尾同时写入不可见的 `snapshot_sha256` 标记。这样，后续清理可以证明某个任务确实进入了已经上传的总结，而不是只依赖模型的一段概括。

### 13.5 审阅

草稿完成后，Sakura 在下次可用时显示气泡并播放摘要：

```text
本周总结已经生成，共完成 8 项工作，还有 2 项逾期。
是否查看并上传？
```

支持：

- 查看。
- 重新生成。
- 暂不上传。
- 明确确认上传。

只有明确的当次确认才能把状态从 `awaiting_approval` 变为 `publishing`。

## 14. 私有 GitHub 归档

目标仓库：

```text
AngleBeatrowcolum/personal-weekly-summaries
```

首次发布前必须确认远程仓库存在且可验证为私有。无法验证私有状态时拒绝上传。

仓库结构：

```text
personal-weekly-summaries/
├── README.md
└── summaries/
    └── 2026/
        └── 2026-W30.md
```

发布流程：

1. 获取当前草稿及其 SHA-256。
2. 再次确认运行状态为 `awaiting_approval`。
3. 核对最终 Markdown 的系统归档清单及 `snapshot_sha256`。
4. 复制到指定年份目录。
5. 只 `git add` 本次总结文件。
6. 创建普通提交，不改写历史。
7. 推送默认分支。
8. 使用远程引用核对提交 SHA。
9. 把状态标记为 `published`。
10. 为快照中版本未变化且确实出现在系统归档清单中的任务建立 `task_summary_archives`。
11. 删除已发布的本地事实快照和重复的本地草稿。
12. 执行一次符合条件的数据清理。

禁止使用：

- `git add .`
- `git push --force`
- 未确认的自动推送
- 把周总结推送到 Sakura 源码仓库
- 把个人周总结自动发布到公开博客

推送失败时保留草稿，不建立归档关系，不清理任务。

## 15. 数据清理

清理程序每周总结发布后运行一次，也可由后续 worker 重试。

### 15.1 候选条件

根据任务当前状态采用互斥条件：

```text
status = completed
且 completed_at <= now - 14 天

或

status = cancelled
且 cancelled_at <= now - 14 天

或

status = pending
且 due_at <= now - 14 天
```

### 15.2 必要安全条件

候选任务还必须：

1. 存在 `task_summary_archives`。
2. 对应总结状态为 `published` 或 `cleaned`。
3. 对应总结具有已验证的远程 commit SHA。
4. `tasks.updated_at` 与归档版本完全一致。

在一个事务中删除：

- 任务。
- 任务事件。
- 任务专属提醒规则。
- 提醒实例。
- 各渠道发送记录。
- 任务归档关系。

独立周期提醒不删除。

独立周期提醒产生的实例和通知日志也需要控制体积。状态已经终结、计划时间超过 14 天、且其统计已进入成功上传的周总结后，可以删除实例及发送日志；提醒规则本身继续保留。`weekly_summary_runs` 只保存周次、哈希、提供者、提交 SHA 和状态等少量审计元数据，可以长期保留。

需要明确接受：清理后的 Markdown 总结不能完整恢复原始任务记录。GitHub 总结是长期个人记录，不是数据库逐字段备份。

## 16. 旧 JSON 迁移

旧文件：

```text
data/tasks.json
data/reminders.json
```

迁移流程：

1. 检查 schema migration 版本。
2. 在 `data/backups/` 创建带时间戳的原文件备份。
3. 解析并验证 JSON。
4. 在单个 SQLite 事务中导入。
5. 对比成功导入数量和关键字段。
6. 写入 migration 版本。
7. 保留原文件，不自动删除。

旧任务默认：

- `priority = normal`。
- 没有计划日期。
- 没有截止时间。
- 不生成默认邮件提醒。

迁移必须幂等。中途失败时事务回滚，再次启动不会产生重复任务。

## 17. `.gitignore` 和本地数据

以下内容不得进入任何源码仓库：

```text
data/tasks.db
data/tasks.db-wal
data/tasks.db-shm
data/backups/
data/weekly_summaries/
*.log
.env
```

正式周总结只能进入专用私有仓库。测试数据必须使用虚构姓名、邮箱和任务内容。

## 18. 错误处理

### 18.1 数据库

- 锁冲突：`busy_timeout` 后进行有限重试。
- 事务失败：完整回滚。
- 数据库损坏：停止写入和清理，提示用户从备份恢复。
- schema 版本过新：拒绝用旧程序打开。

### 18.2 提醒

- 单渠道失败不影响其他渠道状态。
- 领取后进程崩溃：租约超时后恢复。
- 周期提醒过期：标记跳过，不重试。
- 截止提醒错过：合并补发。

### 18.3 周总结

- DeepSeek 调用失败：使用本地模板。
- Markdown 写入失败：保持原始任务，不进入发布阶段。
- Git 提交失败：保留草稿。
- Git 推送或远程校验失败：不归档、不清理。
- 总结后任务发生修改：该任务不建立有效归档关系。

### 18.4 UI

- 气泡失败不应让主窗口崩溃。
- TTS 失败只降级为文字。
- 后台错误使用可读摘要，不显示堆栈和密钥。

## 19. 测试设计

### 19.1 单元测试

- 三级优先级默认值和排序。
- 逾期、今天截止、今天计划的分类。
- 三组去重优先级。
- 上海日期和 UTC 转换。
- 提前 24 小时和 2 小时的计算。
- 创建时间晚于提醒检查点时的合并提醒。
- 修改截止时间取消旧提醒并生成新提醒。
- 完成、取消和重新打开状态机。
- 周期提醒 30 分钟边界。
- 截止提醒不使用 30 分钟过期规则。
- 周总结事实统计。
- 结构化模型输出校验。
- 14 天清理边界。
- 无归档、版本变化和推送失败时禁止清理。

### 19.2 集成测试

- SQLite 建表和 migration。
- 多执行者并发领取只产生一个有效 claim。
- 租约超时恢复。
- 使用模拟 SMTP 验证邮件内容和重试。
- 使用模拟 DeepSeek 响应，不消耗真实额度。
- 使用临时本地 bare Git 仓库验证提交和推送。
- 推送失败不建立 `task_summary_archives`。
- JSON 迁移重复运行不重复导入。

### 19.3 UI 测试

- `AgentEvent` 正确进入现有气泡和 TTS 链路。
- 气泡显示完整列表。
- 语音只播报摘要。
- 提醒后任务仍为 `pending`。
- 同名任务返回候选项。
- 周总结生成后出现确认提示。

### 19.4 Windows 人工验收

- 登录后提醒 worker 会运行。
- 使用电池时允许短时运行。
- 不主动唤醒休眠电脑。
- 恢复后截止提醒可以补发。
- Sakura 关闭时 QQ 邮件仍可发送。
- Sakura 不需要持续打开；Codex 和 Claude Code完全不是第一版运行依赖。
- 周日 20:00 能生成周总结。
- 未确认时不上传。
- GitHub 私有状态无法验证时拒绝上传。

## 20. 验收标准

第一版必须通过以下场景：

1. “明天下午三点完成实验报告，优先级高”能保存并回显。
2. “今天有什么工作”按“逾期 → 今天截止 → 今天计划”返回。
3. 同一任务只出现一次。
4. 文字气泡完整，语音摘要正确。
5. 截止前 24 小时和 2 小时存在独立提醒记录。
6. Sakura 打开时显示桌面提醒。
7. Sakura 关闭时 Windows worker 能发送 QQ 邮件。
8. 关机错过的截止提醒在恢复后合并补发。
9. 超过 30 分钟的股票周期提醒跳过。
10. 周日 20:00 自动生成总结草稿。
11. DeepSeek 不可用时生成本地基础总结。
12. 未确认时不提交、不推送。
13. 推送成功并验证远程 SHA 后才建立归档关系。
14. 已完成、已取消或逾期未完成满 14 天的任务能够安全清理。
15. 未归档、被修改、未逾期或无截止的未完成任务不会被清理。
16. 旧 JSON 数据可以一次性、幂等地迁移。

## 21. 实施边界

本文档批准后，下一步应编写单独的实施计划，并按测试驱动方式分阶段实现：

1. SQLite schema、模型和仓储。
2. `TaskService`、今日查询和旧数据迁移。
3. Sakura 工具、气泡和 TTS 集成。
4. 提醒调度、QQ 邮件和 Windows worker。
5. 周总结、DeepSeek、本地降级和人工确认。
6. 私有 GitHub 发布和安全清理。
7. Windows 端到端验收。

在个人资料库、博客发布或服务器提醒开始实施前，应分别完成各自独立的设计和实施计划。
