"""Measured request evidence, separate from provider payloads and value clocks.

Clients retain observations until the poller persists them in its finally path.
Fakes that implement only the old payload API provide no measured evidence;
absence stays unknown rather than becoming a synthetic request duration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Any
from uuid import uuid4


@dataclass(frozen=True)
class RequestObservation:
    request_id: str
    feed: str
    target_id: str
    request_ts: float
    received_ts: float
    outcome: str
    error: str | None = None
    duration_seconds: float | None = None


def observations(owner: object) -> list[RequestObservation]:
    return getattr(owner, "_request_observations", [])


def drain(owner: object) -> list[RequestObservation]:
    result = observations(owner)
    if result:
        owner._request_observations = []
    return result


def measured_request(
    owner: object,
    feed: str,
    targets: list[str],
    send: Callable[[], Any],
    decode: Callable[[Any], Any],
    present: Callable[[Any, str], bool],
) -> Any:
    """One actual HTTP request, including failed and empty responses.

    The completion clock is captured before parsing. A decoding error preserves
    that clock; a transport error records when the failure reached the caller.
    Request identity is shared by all targets of this chunk or page.
    """
    request_id = uuid4().hex
    started = time.time()
    monotonic_started = time.monotonic()
    received = None
    duration = None
    result = None
    error = None
    presence = {}
    try:
        response = send()
        received = time.time()
        duration = time.monotonic() - monotonic_started
        result = decode(response)
        presence = {target: present(result, target) for target in targets}
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        received = time.time() if received is None else received
        duration = time.monotonic() - monotonic_started if duration is None else duration
        queue = observations(owner)
        for target in targets:
            queue.append(RequestObservation(
                request_id, feed, target, started, received,
                "error" if error else "changed" if presence[target] else "empty",
                error, duration,
            ))
        owner._request_observations = queue


def received_at(owner: object, target: str, feeds: tuple[str, ...]) -> float | None:
    # The final request occurrence establishes availability. Wall clocks can
    # step backwards, so a numerical maximum would pick an earlier response.
    return next((o.received_ts for o in reversed(observations(owner))
                 if o.target_id == target and o.feed in feeds), None)
