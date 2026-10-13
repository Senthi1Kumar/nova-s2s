"""Cloud LLM failover: retry a voice turn on the other chat-completions provider.

Callers: nova_hailo.pipeline (wraps the cloud LLM it builds), tests.

A free-tier rate limit (Groq's 8k tokens per minute), a provider outage or a
dropped connection used to close the turn with "could you say that again?".
FailoverLLM answers the same call from the other provider instead (groq ⇄
openrouter), as long as nothing has been spoken yet: a reply that already
started is never repeated. A provider that failed is skipped for ``hold_s``,
so the next turns do not pay for the same error again.

Everything else (last_tool_calls, model, hef_path, parse_tool, max_tokens)
reads through to the client that answered last.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from nova_hailo.backends.openrouter_llm import PROVIDERS

# Which provider takes over when one fails. Cerebras has no key on the demo Pi,
# but a cerebras primary still falls back to OpenRouter.
PAIRS = {"groq": "openrouter", "openrouter": "groq", "cerebras": "openrouter"}
_HTTP = re.compile(r"\bHTTP (\d{3})\b")


def retry_reason(exc: BaseException) -> str | None:
    """Why another provider could answer instead, or None when it could not."""
    m = _HTTP.search(str(exc))
    if m:
        status = int(m.group(1))
        return str(status) if status == 429 or status >= 500 else None
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "Connect" in name or "RemoteProtocol" in name or "NetworkError" in name:
        return "connection"
    return None


def _provider(llm: Any) -> str:
    return str(getattr(llm, "provider", "") or type(llm).__name__)


class FailoverLLM:
    def __init__(
        self,
        primary: Any,
        secondary: Any,
        *,
        hold_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.hold_s = float(hold_s)
        self._clock = clock
        self._failed_at: dict[int, float] = {}
        self.active = primary
        self.last_failover: str | None = None

    def _order(self) -> list[Any]:
        now = self._clock()
        healthy = [c for c in (self.primary, self.secondary)
                   if now - self._failed_at.get(id(c), -1e18) >= self.hold_s]
        held = [c for c in (self.primary, self.secondary) if c not in healthy]
        return healthy + held

    def generate(self, messages: list[dict], max_tokens: int | None = None,
                 token_callback: Callable[[str], None] | None = None, **kw: Any):
        self.last_failover = None
        order = self._order()
        for i, llm in enumerate(order):
            spoke = [False]

            def on_token(tok: str, _cb=token_callback, _spoke=spoke) -> None:
                _spoke[0] = True
                if _cb is not None:
                    _cb(tok)

            try:
                text, metrics = llm.generate(messages, max_tokens,
                                             token_callback=on_token, **kw)
            except Exception as exc:
                reason = retry_reason(exc)
                if reason is None or spoke[0] or i == len(order) - 1:
                    raise
                self._failed_at[id(llm)] = self._clock()
                nxt = order[i + 1]
                self.last_failover = f"{_provider(llm)}→{_provider(nxt)} ({reason})"
                print(f"[llm] failover {self.last_failover}", flush=True)
                continue
            self.active = llm
            if metrics is not None and self.last_failover:
                metrics.failover = self.last_failover
            return text, metrics
        raise RuntimeError("no LLM provider answered")  # unreachable: the last error re-raises

    def clear(self) -> None:
        for llm in (self.primary, self.secondary):
            llm.clear()

    def release(self) -> None:
        for llm in (self.primary, self.secondary):
            llm.release()

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes FailoverLLM itself does not define.
        if name in ("primary", "secondary", "active"):
            raise AttributeError(name)
        return getattr(self.active, name)


def build_failover(
    backend: str,
    primary: Any,
    make: Callable[[str], Any],
    *,
    enabled: bool = True,
    env: Mapping[str, str] = os.environ,
) -> Any:
    """``primary`` wrapped with the paired provider when failover is on and that
    provider has a key; otherwise ``primary`` unchanged. ``make(provider)`` builds
    the second client."""
    other = PAIRS.get(backend)
    if not enabled or other is None:
        return primary
    if not any(str(env.get(k) or "").strip() for k in PROVIDERS[other][1]):
        return primary
    return FailoverLLM(primary, make(other))
