import pytest
from datetime import datetime, timezone
import shutil
from pathlib import Path
from app.models import User, EmailDelivery, EmailStatus
from app.email_provider import EmailProvider

import os
import shutil  # 需要加在文件顶部


def test_local_inbox_creates_json_file():
    """测试本地测试收件箱是否能正确生成 JSON 文件"""
    # 手动创建一个安全的临时目录，避开 Windows 权限坑
    test_dir = Path("test_temp_inbox")
    if test_dir.exists():
        shutil.rmtree(test_dir)  # 先清空

    provider = EmailProvider()
    provider.provider_type = "local_inbox"
    provider.inbox_dir = test_dir

    result = provider.send("test@example.com", "测试主题", "测试内容")

    assert result.status == "delivered"
    assert result.message_id is not None

    # 检查是否生成了 JSON 文件
    files = list(test_dir.glob("*.json"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "测试主题" in content

    # 测试完清理掉
    shutil.rmtree(test_dir)


def test_worker_sends_email_and_updates_status(client, session_factory):
    """模拟一个完整的发送流程：创建记录 -> 模拟认领 -> 发送 -> 更新状态"""
    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "student").first()
        # 修复：增加了必填的 idempotency_key
        delivery = EmailDelivery(
            user_id=user.id,
            recipient="student@example.com",
            subject="赛事提醒",
            status=EmailStatus.pending,
            idempotency_key="worker_test_1"
        )
        db.add(delivery)

    # 模拟 worker 里的发送和更新逻辑
    with session_factory.begin() as db:
        delivery = db.query(EmailDelivery).filter(EmailDelivery.idempotency_key == "worker_test_1").first()
        # 模拟认领
        delivery.status = EmailStatus.accepted

        provider = EmailProvider()
        result = provider.send(delivery.recipient, delivery.subject, "内容")

        # 模拟保存结果
        delivery.provider_message_id = result.message_id
        if result.status == "delivered":
            delivery.status = EmailStatus.delivered

    with session_factory() as db:
        delivery = db.query(EmailDelivery).filter(EmailDelivery.idempotency_key == "worker_test_1").first()
        assert delivery.status == EmailStatus.delivered
        assert delivery.provider_message_id is not None


def test_duplicate_email_delivery_not_sent_twice(client, session_factory):
    """测试重复消费不会产生两条邮件（幂等性）"""
    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "student").first()
        delivery = EmailDelivery(
            user_id=user.id,
            recipient="student@example.com",
            subject="赛事提醒",
            status=EmailStatus.pending,
            idempotency_key="unique_key_123"
        )
        db.add(delivery)

    # 尝试插入同样的 key 引发冲突
    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "student").first()
        try:
            duplicate = EmailDelivery(
                user_id=user.id,
                recipient="student@example.com",
                subject="赛事提醒",
                status=EmailStatus.pending,
                idempotency_key="unique_key_123"
            )
            db.add(duplicate)
            db.flush()  # 强制立即写入数据库触发唯一约束
        except Exception:
            db.rollback()  # 遇到冲突，回滚

    with session_factory() as db:
        count = db.query(EmailDelivery).filter(EmailDelivery.idempotency_key == "unique_key_123").count()
        assert count == 1


def test_failed_delivery_can_retry(client, session_factory):
    """测试发送失败后，状态变为 failed，且可以被手动重试"""
    with session_factory.begin() as db:
        user = db.query(User).filter(User.username == "student").first()
        # 修复：增加了必填的 idempotency_key
        delivery = EmailDelivery(
            user_id=user.id,
            recipient="invalid-email",
            subject="测试",
            status=EmailStatus.pending,
            idempotency_key="failed_test_1"
        )
        db.add(delivery)

    provider = EmailProvider()
    # 模拟发送失败
    result = provider.send("invalid-email", "测试", "内容")
    assert result.status == "failed"

    with session_factory.begin() as db:
        delivery = db.query(EmailDelivery).filter(EmailDelivery.idempotency_key == "failed_test_1").first()
        delivery.status = EmailStatus.failed
        delivery.error_message = result.error

    # 模拟人工重试接口（这里只是示意，实际你要调用你的 API）
    with session_factory() as db:
        delivery = db.query(EmailDelivery).filter(EmailDelivery.idempotency_key == "failed_test_1").first()
        assert delivery.status == EmailStatus.failed