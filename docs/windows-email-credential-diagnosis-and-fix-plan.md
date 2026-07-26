# Windows 邮件凭据与通知诊断修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Windows 上的 Sakura QQ 邮件提醒可被安全诊断，并确保计划任务始终使用绝对项目路径启动。

**Architecture:** 配置命令把传入的项目目录规范化为绝对路径；计划任务 XML 写入绝对的 Python、worker 和工作目录。新增只读诊断命令，逐层报告设置、凭据可读性、计划任务、到期实例和投递状态，但绝不打印授权码或发送测试邮件。

**Tech Stack:** Python 3.12、`pathlib`、`subprocess`、Windows Task Scheduler、SQLite、pytest。

---

## 当前已确认的问题

- `task_assistant.json` 中 `email_enabled` 已为 `true`，QQ 发件与收件地址已配置。
- `schtasks /Query /TN "\Sakura\TaskReminderWorker"` 未找到任务，因此后台 worker 没有运行。
- 已生成的 XML 曾包含 `runtime\python.exe` 与 `--base-dir "."`。计划任务从非项目目录启动时会找不到运行时和数据库。
- 当前排查不得读取、打印、提交 QQ SMTP 授权码；真实发信必须由用户显式触发。

## 文件结构

- Modify: `app/platforms/reminder_task.py` — 生成带绝对路径和工作目录的 XML。
- Modify: `tools/configure_task_assistant.py` — 统一规范化 `--base-dir`，注册后做可读校验。
- Modify: `app/notifications/credentials.py` — 增加只返回布尔值的凭据存在性检查。
- Create: `tools/diagnose_task_assistant.py` — 无副作用的 Windows 邮件提醒诊断 CLI。
- Create: `tests/platforms/test_reminder_task_paths.py` — 验证 XML 没有相对执行路径。
- Create: `tests/notifications/test_credentials_diagnosis.py` — 验证凭据诊断不泄露秘密。
- Create: `tests/tools/test_diagnose_task_assistant.py` — 验证诊断输出包含状态、不包含授权码。
- Modify: `README.md` — 更新正确的注册和诊断命令。

### Task 1: 固定计划任务的绝对路径和工作目录

**Files:**

- Modify: `app/platforms/reminder_task.py`
- Test: `tests/platforms/test_reminder_task_paths.py`

- [ ] **Step 1: 写失败测试，禁止相对路径进入 XML**

```python
from pathlib import Path

from app.platforms.reminder_task import build_reminder_task_xml


def test_reminder_task_xml_uses_absolute_program_and_working_directory(tmp_path: Path) -> None:
    base_dir = (tmp_path / "sakura").resolve()
    xml = build_reminder_task_xml(
        python_exe=base_dir / "runtime" / "python.exe",
        worker_path=base_dir / "app" / "workers" / "reminder_worker.py",
        base_dir=base_dir,
    )

    assert str(base_dir / "runtime" / "python.exe") in xml
    assert str(base_dir / "app" / "workers" / "reminder_worker.py") in xml
    assert f"<WorkingDirectory>{base_dir}</WorkingDirectory>" in xml
    assert '"."' not in xml
```

- [ ] **Step 2: 运行测试确认失败**

Run: `runtime\python.exe -m pytest tests/platforms/test_reminder_task_paths.py -q`

Expected: FAIL，当前 XML 未包含 `<WorkingDirectory>`。

- [ ] **Step 3: 在 XML 生成器中解析绝对路径**

```python
project_dir = Path(base_dir).resolve()
python_path = Path(python_exe).resolve()
worker = Path(worker_path).resolve()
command = escape(str(python_path))
arguments = escape(f'"{worker}" --base-dir "{project_dir}"')
working_directory = escape(str(project_dir))
```

在 `<Exec>` 中写入：

```xml
<WorkingDirectory>{working_directory}</WorkingDirectory>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `runtime\python.exe -m pytest tests/platforms/test_reminder_task.py tests/platforms/test_reminder_task_paths.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/platforms/reminder_task.py tests/platforms/test_reminder_task_paths.py
git commit -m "fix: use absolute paths for reminder task"
```

### Task 2: 配置命令注册后验证任务存在

**Files:**

- Modify: `tools/configure_task_assistant.py`
- Test: `tests/tools/test_configure_task_assistant.py`

- [ ] **Step 1: 写失败测试，确保尾随引号和相对目录会被规范化**

```python
from pathlib import Path

from tools.configure_task_assistant import _normalise_base_dir


def test_normalise_base_dir_removes_literal_quote_and_returns_absolute(tmp_path: Path) -> None:
    value = Path(f'{tmp_path}"')
    assert _normalise_base_dir(value) == tmp_path.resolve()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `runtime\python.exe -m pytest tests/tools/test_configure_task_assistant.py -q`

Expected: FAIL，函数尚未定义。

- [ ] **Step 3: 实现目录规范化和注册校验**

```python
def _normalise_base_dir(value: Path) -> Path:
    return Path(str(value).rstrip('"')).resolve()
```

让 `main()` 对两个子命令都调用该函数。注册完成后调用：

```python
subprocess.run(
    ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
    check=True,
    capture_output=True,
    text=True,
)
```

对邮件任务和周总结任务分别校验；任一个失败时抛出包含任务名的可读错误，不能显示凭据。

- [ ] **Step 4: 运行测试确认通过**

Run: `runtime\python.exe -m pytest tests/tools/test_configure_task_assistant.py tests/platforms -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tools/configure_task_assistant.py tests/tools/test_configure_task_assistant.py
git commit -m "fix: verify Windows notification task registration"
```

### Task 3: 提供不泄密的凭据与队列诊断

**Files:**

- Modify: `app/notifications/credentials.py`
- Create: `tools/diagnose_task_assistant.py`
- Test: `tests/notifications/test_credentials_diagnosis.py`
- Test: `tests/tools/test_diagnose_task_assistant.py`

- [ ] **Step 1: 写失败测试，凭据诊断只能返回布尔值**

```python
from app.notifications.credentials import KeyringCredentialStore
from tests.fakes import FakeCredentialStore


def test_credential_presence_check_never_returns_secret() -> None:
    store = FakeCredentialStore({"qq-smtp": "private-code"})
    assert KeyringCredentialStore.has_value(store, "qq-smtp") is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `runtime\python.exe -m pytest tests/notifications/test_credentials_diagnosis.py -q`

Expected: FAIL，`has_value` 尚未定义。

- [ ] **Step 3: 实现安全诊断数据**

```python
@staticmethod
def has_value(store: CredentialStore, name: str) -> bool:
    return bool(store.get(name))
```

诊断 CLI 输出 JSON，字段固定为：

```python
{
    "email_enabled": settings.email_enabled,
    "sender_configured": bool(settings.qq_email),
    "recipient_configured": bool(settings.recipient_email),
    "smtp_credential_available": credential_available,
    "task_registered": task_registered,
    "pending_email_deliveries": pending_count,
    "failed_email_deliveries": failed_count,
}
```

SQL 只统计 `notification_deliveries.channel='email'` 的状态数量；不得 SELECT、写日志或返回授权码、邮箱密码、完整任务详情。

- [ ] **Step 4: 运行测试确认通过**

Run: `runtime\python.exe -m pytest tests/notifications/test_credentials_diagnosis.py tests/tools/test_diagnose_task_assistant.py -q`

Expected: PASS，序列化输出中不包含 `private-code`。

- [ ] **Step 5: 提交**

```bash
git add app/notifications/credentials.py tools/diagnose_task_assistant.py tests/notifications tests/tools
git commit -m "feat: add safe email notification diagnostics"
```

### Task 4: 更新 Windows 操作文档并完成验证

**Files:**

- Modify: `README.md`

- [ ] **Step 1: 写入绝对路径注册命令**

```bat
cd /d C:\Users\28028\Downloads\sakura-v0.9.9-windows-x64
runtime\python.exe tools\configure_task_assistant.py register-tasks --base-dir C:\Users\28028\Downloads\sakura-v0.9.9-windows-x64
```

并注明：`setup-task-assistant.bat` 只保存 QQ 配置；`register-tasks` 才会创建 Windows 后台任务。

- [ ] **Step 2: 写入诊断和手动 worker 命令**

```bat
runtime\python.exe tools\diagnose_task_assistant.py --base-dir C:\Users\28028\Downloads\sakura-v0.9.9-windows-x64
runtime\python.exe app\workers\reminder_worker.py --base-dir C:\Users\28028\Downloads\sakura-v0.9.9-windows-x64
```

说明第二条命令可能发送已到期的邮件，必须在用户确认要测试时才执行。

- [ ] **Step 3: 执行全量自动验证**

Run:

```bash
runtime\python.exe -m pytest -q
runtime\python.exe -m compileall -q app tools
git diff --check
```

Expected: 全部测试通过，编译成功，空白检查无输出。

- [ ] **Step 4: 手动验收（用户执行）**

```bat
schtasks /Query /TN "\Sakura\TaskReminderWorker" /V /FO LIST
```

Expected: 显示任务详情，`任务状态` 不是“找不到指定文件”。随后创建一个有截止时间的虚构任务，等待其到达“截止前两小时”提醒时间，再检查收件箱。

- [ ] **Step 5: 提交**

```bash
git add README.md
git commit -m "docs: document Windows email notification diagnostics"
```

## 自审

- 计划覆盖了设置、凭据、计划任务、worker 队列和用户可执行的验收步骤。
- 所有凭据步骤只产生存在性布尔值，未要求打印或提交授权码。
- 真正发送邮件仅列为用户明确确认后的手动验收，不包含在自动测试中。
