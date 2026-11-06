"""One JSON call with urllib, shared by the Gitea and Supervisor clients. Redirects are not followed, so the token in
a request never reaches another host."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

TIMEOUT = 30


class ApiError(RuntimeError):
    """A failed call; status 0 when nothing answered."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def call(method: str, url: str, authorization: str, body: dict | None = None, timeout: float = TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", authorization)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _opener.open(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as e:
        with e:
            if 300 <= e.code < 400:
                raise ApiError(e.code, f"redirected to {e.headers.get('Location')}; not followed") from None
            raise ApiError(e.code, _message(e.read())) from None
    except (OSError, http.client.HTTPException) as e:
        raise ApiError(0, f"{method} {url}: {e}") from None
    try:
        return json.loads(payload) if payload else None
    except ValueError:
        raise ApiError(0, f"{method} {url}: the answer was not JSON") from None


def _message(payload: bytes) -> str:
    """Gitea and the Supervisor both put the reason in "message"."""
    try:
        return str(json.loads(payload)["message"])
    except ValueError, KeyError, TypeError:
        return payload.decode(errors="replace")[:300]
