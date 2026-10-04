"""邮件（SMTP）推送。

配置：
    type: smtp
    options:
      host: ${SMTP_HOST}
      port: 465
      user: ${SMTP_USER}
      password: ${SMTP_PASSWORD}     # QQ/163 邮箱填「授权码」，不是登录密码
      sender: ${SMTP_USER}           # 可选，默认用 user
      to:                            # 支持字符串或列表
        - you@example.com
      use_ssl: true                  # 可选。465 用 true，587 用 false + starttls
      starttls: false                # 可选
      subject_prefix: "[余票监控] "  # 可选

实现说明
--------
``smtplib`` 是阻塞的，直接 await 会卡住事件循环，所以丢到线程池里跑
（``asyncio.to_thread``）。这样不引入 aiosmtplib 依赖，行为也够用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from .base import Message, Notifier
from .registry import register_notifier

log = logging.getLogger("radar.notifier.mail")


@register_notifier("smtp")
class SmtpNotifier(Notifier):
    name = "smtp"
    requires = ("host", "user", "password", "to")

    def _recipients(self) -> list[str]:
        raw = self.options.get("to")
        if isinstance(raw, str):
            items = [x.strip() for x in raw.replace(";", ",").split(",")]
        elif isinstance(raw, list):
            items = [str(x).strip() for x in raw]
        else:
            items = []
        items = [x for x in items if x]
        if not items:
            raise ValueError("smtp 通知的 options.to 为空")
        return items

    def _build_mime(self, message: Message) -> MIMEText:
        body = message.plain_text
        if message.url:
            body += f"\n\n官方页面：{message.url}"

        prefix = str(self.options.get("subject_prefix") or "[余票监控] ")
        subject = f"{prefix}{message.title}"

        mime = MIMEText(body, "plain", "utf-8")
        mime["Subject"] = Header(subject, "utf-8")
        sender = str(self.options.get("sender") or self._require("user"))
        mime["From"] = formataddr(("Ticket Radar", sender))
        mime["To"] = ", ".join(self._recipients())
        mime["Date"] = formatdate(localtime=True)
        return mime

    def _send_sync(self, mime: MIMEText) -> None:
        host = self._require("host")
        port = int(self.options.get("port") or 465)
        user = self._require("user")
        password = self._require("password")
        use_ssl = bool(self.options.get("use_ssl", port == 465))
        starttls = bool(self.options.get("starttls", False))

        context = ssl.create_default_context()
        if use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=30)

        try:
            if starttls and not use_ssl:
                server.starttls(context=context)
            server.login(user, password)
            server.sendmail(user, self._recipients(), mime.as_string())
        finally:
            with contextlib.suppress(smtplib.SMTPException):
                server.quit()

    async def send(self, message: Message) -> None:
        mime = self._build_mime(message)
        # smtplib 是阻塞的，必须丢线程池，否则会卡住整个事件循环
        await asyncio.to_thread(self._send_sync, mime)


__all__ = ["SmtpNotifier"]
