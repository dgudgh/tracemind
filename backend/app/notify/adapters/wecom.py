import json
import logging

import requests

from .base import BaseAdapter, NotifyResult

logger = logging.getLogger("app.notify.wecom")


class WeComAdapter(BaseAdapter):
    def validate_config(self) -> list[str]:
        errors = []
        if not self.config.get("webhook_url"):
            errors.append("webhook_url is required")
        return errors

    def send(self, recipient: str, title: str, content: str) -> NotifyResult:
        url = self.config.get("webhook_url", "")
        if not url:
            return NotifyResult(False, error_message="webhook_url not configured")

        msgtype = self.config.get("msgtype", "markdown")
        if msgtype == "markdown":
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": f"## {title}\n{content}"},
            }
        elif msgtype == "text":
            payload = {
                "msgtype": "text",
                "text": {"content": f"{title}\n{content}", "mentioned_list": [recipient] if recipient else []},
            }
        else:
            payload = {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}

        try:
            resp = requests.post(url, json=payload, timeout=10)
            body = resp.text
            if resp.status_code == 200:
                data = resp.json()
                if data.get("errcode") == 0:
                    return NotifyResult(True, resp.status_code, body)
                return NotifyResult(False, resp.status_code, body, data.get("errmsg", ""))
            return NotifyResult(False, resp.status_code, body, f"HTTP {resp.status_code}")
        except Exception as e:
            return NotifyResult(False, error_message=str(e))
