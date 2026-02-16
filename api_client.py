import json
import re
import time
from typing import Dict, Optional

import aiohttp
import asyncio


class AlpinnApiError(Exception):
    def __init__(self, message: str, retry_after: Optional[int] = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class ApiRateLimitError(Exception):
    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = remaining_seconds
        super().__init__(f"Cooldown global actif: {remaining_seconds}s")


class AlpinnApiClient:
    def __init__(self, rate_limit_seconds: int = 60) -> None:
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request_at = 0.0
        self._lock = asyncio.Lock()

    def _check_cooldown(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < self.rate_limit_seconds:
            remaining = int(self.rate_limit_seconds - elapsed)
            raise ApiRateLimitError(max(1, remaining))

    async def get_json(
        self,
        base_url: str,
        path: str,
        api_key: str,
        params: Optional[Dict[str, str]] = None,
    ) -> Dict:
        async with self._lock:
            self._check_cooldown()
            self._last_request_at = time.monotonic()

            url = f"{base_url.rstrip('/')}{path}"
            headers = {"X-API-Key": api_key}

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params or {}, headers=headers) as resp:
                    text = await resp.text()

                if resp.status == 401:
                    raise AlpinnApiError("401: Cle API invalide ou absente")
                if resp.status == 403:
                    raise AlpinnApiError("403: IP bloquee")
                if resp.status == 429:
                    retry_after = self._extract_retry_after(resp, text)
                    msg = "429: Rate limit API distant"
                    if retry_after:
                        msg += f" (retry_after={retry_after}s)"
                    raise AlpinnApiError(msg, retry_after=retry_after)
                if resp.status >= 400:
                    raise AlpinnApiError(f"HTTP {resp.status}: {text[:200]}")

                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}

    def _extract_retry_after(self, resp: aiohttp.ClientResponse, body_text: str) -> Optional[int]:
        header_value = resp.headers.get("Retry-After")
        if header_value and header_value.isdigit():
            return max(1, int(header_value))

        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            return None

        # Common JSON shapes for cooldown metadata.
        candidates = [
            payload.get("retry_after"),
            payload.get("cooldown"),
            payload.get("wait_seconds"),
        ]
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            candidates.extend(
                [
                    error_obj.get("retry_after"),
                    error_obj.get("cooldown"),
                    error_obj.get("wait_seconds"),
                ]
            )
            message = error_obj.get("message")
            if isinstance(message, str):
                m = re.search(r"(\d+)\s*(?:s|sec|secondes?)", message.lower())
                if m:
                    return max(1, int(m.group(1)))

        for value in candidates:
            if isinstance(value, int):
                return max(1, value)
            if isinstance(value, str) and value.isdigit():
                return max(1, int(value))
        return None
