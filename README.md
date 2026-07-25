# Database-for-me：Sakura 本地任务助手

这是 Sakura 桌宠的本地优先任务助手。任务保存在本机 SQLite；桌宠可查询今天工作，Windows 可在后台发送 QQ 邮件提醒；每周日生成周总结草稿，只有你明确确认后才会提交到专用私有 GitHub 仓库。

## Windows 首次安装

在项目根目录的 PowerShell 或 CMD 中依次执行：

```bat
install.bat
setup-task-assistant.bat
start.bat
```

`setup-task-assistant.bat` 会配置任务助手。随后按提示运行：

```bat
runtime\python.exe tools\configure_task_assistant.py register-tasks --base-dir .
```

这会注册 `\Sakura\TaskReminderWorker`（每 10 分钟检查邮件提醒）和 `\Sakura\WeeklySummaryWorker`（周日 20:00 及登录补偿生成草稿）。两个任务均允许在电池供电时运行，不会唤醒电脑。

## QQ 邮件提醒

QQ 邮件使用 QQ 邮箱的 SMTP 授权码，不是网页登录密码。配置程序会将授权码保存到 Windows 凭据管理器；它不会写入仓库、`data/` 以外的日志或 Git 提交。默认提醒为截止前一天和前两小时。

## 周总结与私有 GitHub 仓库

周总结草稿和事实快照保存在 `data/weekly_summary/`，而 SQLite 数据库绝不会上传 GitHub。若要启用确认后的上传：

1. 建立或克隆私有仓库 `AngleBeatrowcolum/personal-weekly-summaries`。
2. 安装 GitHub CLI，并执行 `gh auth login`。
3. 在 `config/task_assistant.json` 写入该仓库的本地路径和 slug；不要写入 API Key 或 SSH 私钥。

DeepSeek 是可选项。其 API Key 仅保存在 Windows 凭据管理器的 `Sakura/DeepSeekAPI`；未设置或调用失败时，系统自动使用本地模板总结。上传操作始终要求桌宠中的明确确认。

## 备份、清理与恢复

数据库备份在 `data/backups/sqlite/`。默认最多保留 7 个有效文件、总量最多 200 MB。只有任务已进入成功上传的周总结、此后未被修改且已满 14 天，才会被清理；清理前后都会生成备份。

恢复是明确确认操作，先选择一个通过完整性检查的 `.db` 文件，再在恢复工具中确认。数据库损坏时停止写入和清理，从这里的有效备份恢复。

若要停止后台任务，请在管理员终端执行：

```bat
schtasks /Delete /TN "\Sakura\TaskReminderWorker" /F
schtasks /Delete /TN "\Sakura\WeeklySummaryWorker" /F
```

可用以下命令检查是否已注册：

```bat
schtasks /Query /TN "\Sakura\TaskReminderWorker" /V /FO LIST
schtasks /Query /TN "\Sakura\WeeklySummaryWorker" /V /FO LIST
```
