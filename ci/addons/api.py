"""HTTP for the registry and Gitea: one request and its answer, whatever the status. Redirects are refused, so the
credentials in a request's headers never reach another host."""

import json
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class Unreachable(Exception):
    """No answer came."""


@dataclass(frozen=True)
class Answer:
    status: int
    headers: Message
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def said(self) -> str:
        """The start of the body, for an error message."""
        return self.body.decode(errors="replace").strip()[:300]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(_NoRedirect)


def request(
    method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: int = 60
) -> Answer:
    try:
        with opener.open(Request(url, data=body, headers=headers or {}, method=method), timeout=timeout) as answer:
            return Answer(answer.status, answer.headers, answer.read())
    except HTTPError as exc:
        with exc:
            return Answer(exc.code, exc.headers, exc.read())
    except OSError as exc:
        raise Unreachable(f"{method} {url}: no answer ({getattr(exc, 'reason', exc)})") from exc
