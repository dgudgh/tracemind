import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .base import BaseAdapter, NotifyResult

logger = logging.getLogger("app.notify.smtp")


class SmtpAdapter(BaseAdapter):
    def validate_config(self) -> list[str]:
        errors = []
        for key in ["host", "port", "sender"]:
            if not self.config.get(key):
                errors.append(f"{key} is required")
        return errors

    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 465))
        sender = self.config.get("sender", "")
        password = self.config.get("password", "")
        use_tls = self.config.get("use_tls", port == 465)
        use_ssl = self.config.get("use_ssl", port == 465)

        if not host or not sender:
            return NotifyResult(False, error_message="SMTP host/sender not configured")
        if not recipient:
            return NotifyResult(False, error_message="recipient (email address) is required")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = title
        msg["From"] = sender
        msg["To"] = recipient
        msg.attach(MIMEText(content, "html", "utf-8"))

        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                server = smtplib.SMTP(host, port, timeout=15)
                if use_tls:
                    server.starttls()
            if password:
                server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
            server.quit()
            return NotifyResult(True, 250, "OK")
        except Exception as e:
            return NotifyResult(False, error_message=str(e))
