from __future__ import annotations

import json
from urllib import error, parse, request


class PushoverNotifier:
    def __init__(
        self,
        app_token: str,
        user_key: str,
        api_url: str,
        timeout_seconds: int,
    ) -> None:
        self._app_token = app_token.strip()
        self._user_key = user_key.strip()
        self._api_url = api_url.strip() or "https://api.pushover.net/1/messages.json"
        self._timeout_seconds = max(1, int(timeout_seconds))

    def is_configured(self) -> bool:
        return bool(self._app_token and self._user_key)

    def send_message(self, title: str, message: str) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "Pushover is not configured."

        payload = {
            "token": self._app_token,
            "user": self._user_key,
            "title": title.strip() or "VStreamware",
            "message": message.strip(),
        }

        encoded = parse.urlencode(payload).encode("utf-8")
        req = request.Request(self._api_url, data=encoded, method="POST")

        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            return False, f"Pushover HTTP error: {exc.code}"
        except error.URLError as exc:
            return False, f"Pushover request failed: {exc.reason}"
        except OSError as exc:
            return False, f"Pushover request failed: {exc}"

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return False, "Pushover returned an invalid response."

        status = body.get("status")
        if status == 1:
            return True, "Notification sent."

        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return False, f"Pushover rejected notification: {errors[0]}"

        return False, "Pushover rejected notification."
