from datetime import timedelta

from app.db import SessionLocal, utcnow
from sqlalchemy import select
from app.models import OutboxEvent, EmailDelivery, EmailStatus, User
# 1. 从 reminders 只导入它里面有的函数
from app.services.reminders import claim_events, deliver_event, materialize_due

# 2. 另外单独写一行导入 EmailProvider
from app.services.email_provider import EmailProvider


def run_inline_cycle(session_factory=SessionLocal) -> dict:
    """Single-process SQLite mode. No broker required; durable events still used."""

    # 1. 事务一：生成任务和认领任务（只做数据库操作）
    with session_factory.begin() as db:
        due_count = materialize_due(db)

    with session_factory.begin() as db:
        ids = claim_events(db)

    completed = 0
    for event_id in ids:
        with session_factory.begin() as db:
            completed += int(deliver_event(db, event_id) == "recorded")

    sent_emails_count = 0
    provider = EmailProvider()  # 在事务外初始化 Provider

    # 2. 事务二：查询待发送的邮件，并更新状态为“正在发送”
    with session_factory.begin() as db:
        pending_emails = db.scalars(
            select(EmailDelivery).where(EmailDelivery.status == EmailStatus.pending).limit(10)
        ).all()

        # 提取需要发送的数据，脱离 db 上下文，避免事务过长
        tasks_to_send = []
        for record in pending_emails:
            user = db.get(User, record.user_id)
            if user and user.email:
                # 标记为 accepted，表示已被认领（对应状态机要求）
                record.status = EmailStatus.accepted
                tasks_to_send.append({
                    "record_id": record.id,
                    "to_email": user.email,
                    "subject": "赛事提醒",
                    "content": "您的赛事即将截止，请查看。"
                })
            else:
                # 没有邮箱的用户直接标记失败
                record.status = EmailStatus.failed
                record.error_message = "User email not found"

    # 3. 事务外：真正调用 Provider 发送邮件 (这里不占用数据库连接)
    send_results = []
    for task in tasks_to_send:
        try:
            result = provider.send(
                to_email=task["to_email"],
                subject=task["subject"],
                content=task["content"]
            )
            send_results.append((task["record_id"], result))
        except Exception as e:
            # 捕获未知异常
            from app.email_provider import ProviderResult
            send_results.append((task["record_id"], ProviderResult(status="unknown", error=str(e))))

    # 4. 事务三：保存发送结果回数据库
    with session_factory.begin() as db:
        for record_id, result in send_results:
            record = db.get(EmailDelivery, record_id)
            if not record:
                continue

            # 将你的 Provider 返回结果对应到数据库状态
            record.provider_message_id = result.message_id
            if result.status == "delivered" or result.status == "accepted":
                record.status = EmailStatus.delivered
                sent_emails_count += 1
            elif result.status == "failed":
                record.status = EmailStatus.failed
                record.error_message = result.error
            else:
                record.status = EmailStatus.unknown
                record.error_message = result.error

            # 如果你的模型里有 retry_count 或者 attempts 字段，在这里更新
            # record.attempts += 1

    return {
        "due_jobs": due_count,
        "claimed_events": len(ids),
        "recorded_notifications": completed,
        "sent_emails": sent_emails_count
    }

def publish_cycle() -> dict:
    """PostgreSQL + Redis mode: expired queued leases are retried as well."""
    from app.celery_app import celery_app
    with SessionLocal.begin() as db:
        due_count = materialize_due(db)
    with SessionLocal.begin() as db:
        ids = claim_events(db)
    published = 0
    for event_id in ids:
        try:
            celery_app.send_task("campuscomp.deliver", args=[event_id], queue="notify")
            published += 1
        except Exception as error:
            # Durable row remains available if the broker rejects / loses publication.
            with SessionLocal.begin() as db:
                event = db.get(OutboxEvent, event_id)
                if event.state != "done":
                    event.state = "pending"
                    event.last_error = type(error).__name__  # Avoid leaking credentials from URLs.
                    event.next_attempt_at = utcnow() + timedelta(seconds=30)
    return {"due_jobs": due_count, "published_events": published}
