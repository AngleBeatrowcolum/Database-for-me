from __future__ import annotations

from datetime import timedelta

from app.tasks.models import Priority
from app.tasks.repository import ReminderRepository, TaskRepository
from app.tasks.service import TaskService
from app.tasks.today_query import TodayQueryService


def _services(task_database, fixed_now) -> tuple[TaskService, TodayQueryService]:
    tasks = TaskRepository(task_database)
    task_service = TaskService(
        task_database,
        tasks,
        ReminderRepository(task_database),
        clock=lambda: fixed_now,
    )
    return task_service, TodayQueryService(tasks)


def test_today_query_groups_each_pending_task_once_and_sorts_by_priority_due_and_created(
    task_database, fixed_now
) -> None:
    service, today = _services(task_database, fixed_now)
    overdue_normal = service.create_task(
        "逾期普通",
        priority="normal",
        due_at=fixed_now - timedelta(minutes=1),
        allow_past_due=True,
        now=fixed_now,
    )
    overdue_high = service.create_task(
        "逾期高",
        priority="high",
        due_at=fixed_now - timedelta(hours=2),
        allow_past_due=True,
        now=fixed_now,
    )
    due_low = service.create_task(
        "今日低", priority="low", due_at=fixed_now + timedelta(hours=1), now=fixed_now
    )
    due_high_late = service.create_task(
        "今日高晚", priority="high", due_at=fixed_now + timedelta(hours=3), now=fixed_now
    )
    due_high_early = service.create_task(
        "今日高早", priority="high", due_at=fixed_now + timedelta(hours=2), now=fixed_now
    )
    planned = service.create_task("今日计划", planned_date="2026-07-25", now=fixed_now)
    both_due_and_planned = service.create_task(
        "截止优先", planned_date="2026-07-25", due_at=fixed_now + timedelta(hours=4), now=fixed_now
    )
    completed = service.create_task("已完成", planned_date="2026-07-25", now=fixed_now)
    service.complete_task(completed.id, now=fixed_now + timedelta(seconds=1))

    result = today.query(fixed_now)

    assert tuple(task.id for task in result.overdue) == (overdue_high.id, overdue_normal.id)
    assert tuple(task.id for task in result.due_today) == (
        due_high_early.id,
        due_high_late.id,
        both_due_and_planned.id,
        due_low.id,
    )
    assert tuple(task.id for task in result.planned_today) == (planned.id,)
    assert result.summary.total == 7
    assert result.summary.high_priority == 3
    assert result.summary.overdue == 2
    all_ids = [task.id for group in (result.overdue, result.due_today, result.planned_today) for task in group]
    assert len(all_ids) == len(set(all_ids))
    assert isinstance(result.overdue, tuple)


def test_today_query_uses_asia_shanghai_day_boundary_and_display_and_speech_are_complete(
    task_database, fixed_now
) -> None:
    service, today = _services(task_database, fixed_now)
    # 04:00Z is 12:00 in Shanghai. 15:59Z is still today, 16:00Z is tomorrow.
    current_local_day = service.create_task("本地今日", due_at=fixed_now + timedelta(hours=11, minutes=59), now=fixed_now)
    tomorrow_local = service.create_task("本地明日", due_at=fixed_now + timedelta(hours=12), now=fixed_now)
    overdue = service.create_task("已逾期", priority=Priority.HIGH, due_at=fixed_now - timedelta(seconds=1), now=fixed_now, allow_past_due=True)

    result = today.query(fixed_now)

    assert [task.id for task in result.due_today] == [current_local_day.id]
    assert tomorrow_local.id not in {
        task.id for group in (result.overdue, result.due_today, result.planned_today) for task in group
    }
    assert [task.id for task in result.overdue] == [overdue.id]
    display = result.display_text()
    assert display.count("本地今日") == 1
    assert display.count("已逾期") == 1
    speech = result.speech_text()
    assert "共有2项待办" in speech
    assert "1项已逾期" in speech
    assert "1项高优先级" in speech
    assert "已逾期" in speech


def test_empty_today_query_has_natural_display_and_speech(task_database, fixed_now) -> None:
    _, today = _services(task_database, fixed_now)

    result = today.query(fixed_now)

    assert result.summary.total == 0
    assert result.display_text() == "今天没有待办任务。"
    assert result.speech_text() == "今天没有待办任务。"


def test_today_query_can_use_task_service_pending_list(task_database, fixed_now) -> None:
    service, _ = _services(task_database, fixed_now)
    service.create_task("通过服务查询", planned_date="2026-07-25", now=fixed_now)

    result = TodayQueryService(service).query(fixed_now)

    assert [task.title for task in result.planned_today] == ["通过服务查询"]
