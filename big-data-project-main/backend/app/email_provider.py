import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ProviderResult:
    """
    结构化返回结果类
    状态定义：
    - accepted: 服务商已接收（不代表用户收到）
    - delivered: 本地测试收件箱写入成功
    - failed: 明确失败（格式错误等）
    - unknown: 超时、连接中断等无法判断的情况
    """

    def __init__(self, status: str, message_id: str | None = None, error: str | None = None):
        self.status = status
        self.message_id = message_id
        self.error = error

    def to_dict(self):
        return {
            "status": self.status,
            "message_id": self.message_id,
            "error": self.error
        }


class EmailProvider:
    def __init__(self):
        # 从环境变量读取，默认使用本地测试适配器
        self.provider_type = os.getenv("EMAIL_PROVIDER", "local_inbox")

        # 定义本地收件箱目录路径 (定位到 backend 同级目录下的 data/test-inbox)
        self.inbox_dir = Path(__file__).resolve().parent.parent.parent / "data" / "test-inbox"

    def send(self, to_email: str, subject: str, content: str) -> ProviderResult:
        """
        发送邮件并返回结构化结果
        """
        # 基础校验：模拟永久失败的情况
        if not to_email or "@" not in to_email:
            return ProviderResult(status="failed", error="invalid_email_format")

        if self.provider_type == "local_inbox":
            return self._send_to_local_inbox(to_email, subject, content)
        else:
            # 这里保留你以后接入真实邮件服务（如SMTP、SendGrid）的逻辑
            # 真实发送如果成功，通常返回 accepted
            return ProviderResult(status="failed", error="real_provider_not_implemented")

    def _send_to_local_inbox(self, to_email: str, subject: str, content: str) -> ProviderResult:
        """
        本地测试收件箱实现：将邮件写成 .json 文件
        """
        try:
            # 1. 确保收件箱目录存在
            self.inbox_dir.mkdir(parents=True, exist_ok=True)

            # 2. 生成唯一的 message_id
            message_id = f"mock_{uuid.uuid4().hex[:12]}"

            # 3. 构建邮件数据
            email_data = {
                "message_id": message_id,
                "to": to_email,
                "subject": subject,
                "content": content,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "status": "delivered"
            }

            # 4. 写入 JSON 文件
            file_path = self.inbox_dir / f"{message_id}.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(email_data, f, ensure_ascii=False, indent=2)

            # 5. 打印日志（满足你最初 PR 要求截图的那个“模拟发送”日志）
            print(f"[LocalInbox] 模拟发送邮件成功 -> 收件人: {to_email}, 主题: {subject}, ID: {message_id}")

            # 6. 返回 delivered 结构化结果
            return ProviderResult(status="delivered", message_id=message_id)

        except Exception as e:
            # 写入文件失败，认为是临时错误
            print(f"[LocalInbox] 写入收件箱失败: {str(e)}")
            return ProviderResult(status="unknown", error=str(e))