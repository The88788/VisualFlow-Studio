"""Playwright response sniffer for Google Flow generation errors.

The Flow UI drives video generation by polling ``/fx/api/trpc/...`` endpoints
that hit the ``generateVideos`` / ``batchGenerate`` backends. When Google
rejects a prompt (safety filter, user-image unsafe, throttling, etc.), the
failure surfaces as a ``PUBLIC_ERROR_*`` / ``publicErrorCode`` /
``failureReason`` field deep inside that JSON — the same shape the HTTP
(``veo``) provider already parses in ``VeoClient._extract_flow_error``.

``FlowVideoClient`` in the vendored fork doesn't inspect those responses;
it just waits for a success signal until timeout. That means a filtered
prompt would burn the full ``video_timeout`` before failing with a generic
error. This sniffer attaches a network listener on the Playwright
``BrowserContext`` and fires an ``asyncio.Event`` the moment a Flow response
carries a filter/policy error, so the caller can cancel generation early
and fail the video with the same ``error_code`` HTTP path uses.

Usage::

    sniffer = FlowErrorSniffer(context)
    sniffer.attach()
    try:
        gen_task = asyncio.create_task(client.generate_video(...))
        fail_task = asyncio.create_task(sniffer.wait_for_failure())
        done, _ = await asyncio.wait(
            {gen_task, fail_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if fail_task in done and gen_task not in done:
            gen_task.cancel()
            raise FlowPublicError(*fail_task.result())
        # Even if gen_task finished first, check the sniffer — Flow
        # sometimes reports success via the DOM while the last poll
        # response still carried an error.
        err = sniffer.peek_error()
        if err:
            raise FlowPublicError(*err)
    finally:
        sniffer.detach()
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Optional

from ...core.logger import logger


def _find_operation_name(blob: Any) -> Optional[str]:
    """Recursively look for an `operation.name` looking like
    ``projects/.../operations/...`` — Google's batch async generation API
    returns this both at top-level and nested inside `operation`.
    """
    if isinstance(blob, dict):
        name = blob.get("name")
        if isinstance(name, str) and "operations/" in name:
            return name
        for v in blob.values():
            found = _find_operation_name(v)
            if found:
                return found
    elif isinstance(blob, list):
        for item in blob:
            found = _find_operation_name(item)
            if found:
                return found
    return None

# Flow backend endpoints that carry per-generation status. Any URL containing
# one of these substrings is inspected for a PUBLIC_ERROR / failureReason.
_FLOW_RESPONSE_MARKERS: tuple[str, ...] = (
    # Direct Google backend (Flow UI calls these with a Bearer token, same
    # endpoints the HTTP `veo` provider uses — the JSON carries the same
    # PUBLIC_ERROR_* / failureReason / generatedVideos[*].video.error shape).
    "aisandbox-pa.googleapis.com",
    "batchAsyncGenerate",
    "batchCheckAsync",
    "upscaleVideo",
    "uploadUserImage",
    "runVideoFx",
    "runWorkflow",
    "Concatenation",
    # Generic Flow naming variants
    "generateVideos",
    "batchGenerate",
    "workflowservice",
    "videoGeneration",
    "videofx",
    # tRPC batches almost everything under /fx/api/trpc, and the safety
    # filter can be reported inside `runWorkflow`, `project.get` etc.
    "/fx/api/trpc/",
)


class FlowPublicError(Exception):
    """Raised when Flow's network traffic reports a PUBLIC_ERROR_*.

    The ``error_code`` matches the HTTP path's convention so the DB
    error_code column looks identical regardless of provider.
    """

    def __init__(self, error_code: str, error_message: str):
        super().__init__(f"{error_code}: {error_message}")
        self.error_code = error_code
        self.error_message = error_message


def _find_public_error(blob: Any) -> Optional[str]:
    """Recursively search a dict/list/str for a ``PUBLIC_ERROR_*`` token.

    Mirrors ``VeoClient._find_public_error`` but lives here to avoid
    pulling the whole HTTP client into the browser path.
    """
    if isinstance(blob, str):
        if "PUBLIC_ERROR" in blob:
            m = re.search(r"(PUBLIC_ERROR\w+)", blob)
            return m.group(1) if m else None
        return None
    if isinstance(blob, dict):
        for v in blob.values():
            found = _find_public_error(v)
            if found:
                return found
    if isinstance(blob, list):
        for item in blob:
            found = _find_public_error(item)
            if found:
                return found
    return None


def _collect_failure_reasons(blob: Any, out: list[str]) -> None:
    """Collect any ``failureReason`` / ``publicErrorCode`` string values."""
    if isinstance(blob, dict):
        for k, v in blob.items():
            if k in ("failureReason", "publicErrorCode") and isinstance(v, str) and v:
                out.append(v)
            _collect_failure_reasons(v, out)
    elif isinstance(blob, list):
        for item in blob:
            _collect_failure_reasons(item, out)


def extract_flow_error(payload: Any) -> Optional[tuple[str, str]]:
    """Return ``(error_code, error_message)`` if ``payload`` carries a
    Flow failure, else ``None``.

    Mirrors :py:meth:`VeoClient._extract_flow_error` so that browser-mode
    errors surface with the same ``error_code`` the HTTP provider writes.
    """
    if payload is None:
        return None

    # 1) Collect candidate strings from all known PUBLIC_ERROR locations,
    #    in the same priority order the HTTP provider uses.
    candidates: list[str] = []
    if isinstance(payload, dict):
        # Top-level / operation-level gRPC error blocks
        for outer_key in ("error", "operation"):
            outer = payload.get(outer_key)
            if isinstance(outer, dict):
                op_err = outer.get("error") if outer_key == "operation" else outer
                if isinstance(op_err, dict):
                    for f in ("message", "code", "status"):
                        v = op_err.get(f)
                        if v:
                            candidates.append(str(v))
                    # gRPC-style error.details[*]
                    for detail in op_err.get("details") or []:
                        if isinstance(detail, dict):
                            for v in detail.values():
                                if v:
                                    candidates.append(str(v))
                # operation.metadata.{failureReason,publicErrorCode,error}
                meta = outer.get("metadata") if outer_key == "operation" else None
                if isinstance(meta, dict):
                    for f in ("failureReason", "publicErrorCode"):
                        v = meta.get(f)
                        if v:
                            candidates.append(str(v))
                    meta_err = meta.get("error")
                    if isinstance(meta_err, dict):
                        for f in ("code", "message", "failureReason"):
                            v = meta_err.get(f)
                            if v:
                                candidates.append(str(v))
                    # generatedVideos[*].video.{failureReason,publicErrorCode,error}
                    for gv in meta.get("generatedVideos") or []:
                        if isinstance(gv, dict):
                            vid = gv.get("video") or {}
                            if isinstance(vid, dict):
                                for f in ("failureReason", "publicErrorCode", "error"):
                                    v = vid.get(f)
                                    if v:
                                        candidates.append(str(v))

    for cand in candidates:
        if "PUBLIC_ERROR" in cand:
            m = re.search(r"(PUBLIC_ERROR\w+)", cand)
            if m:
                return m.group(1), cand

    # 2) Deep recursive scan for any PUBLIC_ERROR_* token anywhere
    code = _find_public_error(payload)
    if code:
        reasons: list[str] = []
        _collect_failure_reasons(payload, reasons)
        msg = next((r for r in reasons if r and r != code), None)
        if not msg:
            msg = next((c for c in candidates if c and c != code), code)
        return code, msg

    # 3) Explicit failureReason / publicErrorCode without PUBLIC_ERROR_*
    reasons: list[str] = []
    _collect_failure_reasons(payload, reasons)
    if reasons:
        return reasons[0], reasons[0]

    # 4) Top-level Google gRPC error style — error.message with a policy
    #    keyword. Avoid false positives on ordinary status JSON.
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            code_val = str(err.get("code") or "")
            upper = message.upper()
            if any(kw in upper for kw in ("SAFETY", "RAI", "FILTER", "BLOCK", "UNSAFE")):
                return (code_val or "FLOW_SAFETY_ERROR", message)

    return None


# ---------- broad retroactive search ----------

_FAILURE_STATUS_VALUES = frozenset({
    "FAILED", "FAILURE", "BLOCKED", "FILTERED", "REJECTED",
    "ERROR", "SAFETY_BLOCKED", "CONTENT_FILTERED", "CANCELLED",
})

_STRONG_ERROR_KEYWORDS = (
    "policy", "violat", "prohibited", "not allowed", "cannot generate",
    "unable to generate", "content policy", "safety filter", "blocked",
    "rejected", "inappropriate", "harmful", "celebrity", "person",
    "notable", "public figure", "famous", "nổi tiếng",
)


def _broad_error_search(blob: Any, _depth: int = 0) -> Optional[tuple[str, str]]:
    """Broader error search — catches formats ``extract_flow_error`` misses.

    Looks for:
    - ``status``/``state`` fields with failure values + accompanying message.
    - Any ``message``/``description``/``reason`` string containing strong
      policy-error keywords.
    """
    if _depth > 15 or blob is None:
        return None

    if isinstance(blob, dict):
        status = str(blob.get("status") or blob.get("state") or "").upper().strip()
        message = str(
            blob.get("message") or blob.get("description")
            or blob.get("reason") or blob.get("errorMessage") or ""
        ).strip()

        # A clear failure status with any message → surface it
        if status in _FAILURE_STATUS_VALUES and message:
            code = str(blob.get("code") or blob.get("errorCode") or status)
            return (code, message)

        # A message string containing a strong policy keyword
        if message:
            lower = message.lower()
            if any(kw in lower for kw in _STRONG_ERROR_KEYWORDS):
                code = str(blob.get("code") or blob.get("errorCode") or "FLOW_POLICY_ERROR")
                return (code, message)

        for v in blob.values():
            found = _broad_error_search(v, _depth + 1)
            if found:
                return found

    elif isinstance(blob, list):
        for item in blob:
            found = _broad_error_search(item, _depth + 1)
            if found:
                return found

    return None


class FlowErrorSniffer:
    """Attach a Playwright response listener that watches Flow traffic for
    PUBLIC_ERROR failures.

    Parameters
    ----------
    context:
        The ``BrowserContext`` the generation tab lives in. Listening on
        the context rather than a single page catches responses even if
        Flow opens an auxiliary tab (rare, but happens for thumbnail
        previews).
    tag:
        Short label used in debug logs (e.g. ``"generate"``, ``"extend"``).
    """

    def __init__(
        self,
        context,
        tag: str = "flow",
        on_operation_name: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self._context = context
        self._tag = tag
        self._failed: asyncio.Event = asyncio.Event()
        self._error: Optional[tuple[str, str]] = None
        self._handler = None
        # Bounded to avoid memory growth on very long generations.
        self._max_body_bytes = 512 * 1024
        # Store all Flow API response payloads for retroactive broader
        # search when DOM error card is detected but the narrow realtime
        # matching didn't fire.
        self._payloads: list[tuple[str, Any]] = []   # (url, payload)
        self._max_stored_payloads = 200
        # First captured Google operation name (projects/.../operations/...).
        # Identical to the HTTP path's task_id — once captured the video can
        # be polled via HTTP regardless of the browser session state.
        self._operation_name: Optional[str] = None
        self._on_operation_name = on_operation_name

    # ---- lifecycle ------------------------------------------------------

    def attach(self) -> None:
        if self._handler is not None:
            return
        loop = asyncio.get_event_loop()

        def _dispatch(response):
            # Playwright invokes this synchronously; do the real work async.
            try:
                loop.create_task(self._on_response(response))
            except Exception as e:  # pragma: no cover
                logger.debug("[FlowErrorSniffer:%s] dispatch failed: %s", self._tag, e)

        self._handler = _dispatch
        self._context.on("response", _dispatch)

    def detach(self) -> None:
        if self._handler is None:
            return
        try:
            self._context.remove_listener("response", self._handler)
        except Exception:
            pass
        self._handler = None

    # ---- inspection ----------------------------------------------------

    def peek_error(self) -> Optional[tuple[str, str]]:
        return self._error

    def peek_operation_name(self) -> Optional[str]:
        """Most recently captured Google operation name (task_id), if any."""
        return self._operation_name

    async def wait_for_failure(self) -> tuple[str, str]:
        """Block until a PUBLIC_ERROR / filter response is observed.

        Never returns ``None`` — pair with ``asyncio.wait(..., FIRST_COMPLETED)``
        against the actual generation task.
        """
        await self._failed.wait()
        assert self._error is not None
        return self._error

    # ---- internals -----------------------------------------------------

    async def _on_response(self, response) -> None:
        if self._failed.is_set():
            return
        try:
            url = response.url or ""
            if not any(m in url for m in _FLOW_RESPONSE_MARKERS):
                return

            # Only inspect JSON. Flow endpoints always return
            # application/json; octet/binary responses (e.g. GCS video
            # downloads) are never relevant here.
            content_type = ""
            try:
                headers = response.headers
                content_type = (headers.get("content-type") or "").lower() if headers else ""
            except Exception:
                pass
            if content_type and "json" not in content_type:
                return

            try:
                payload = await response.json()
            except Exception:
                # Some tRPC responses are chunked/streamed; fall back to text.
                try:
                    text = await response.text()
                    if not text:
                        return
                    if len(text) > self._max_body_bytes:
                        text = text[: self._max_body_bytes]
                    # Cheap substring pre-check before spending regex cycles.
                    if (
                        "PUBLIC_ERROR" not in text
                        and "failureReason" not in text
                        and "publicErrorCode" not in text
                    ):
                        return
                    import json as _json
                    try:
                        payload = _json.loads(text)
                    except Exception:
                        # Not JSON — still try a regex grep.
                        m = re.search(r"(PUBLIC_ERROR\w+)", text)
                        if m:
                            self._record(m.group(1), m.group(1), url=url)
                        return
                except Exception:
                    return

            # Store payload for retroactive broader search
            if len(self._payloads) < self._max_stored_payloads:
                self._payloads.append((url, payload))

            # Capture the Google operation name (task_id) on submit responses
            # so this video can be HTTP-polled even if the browser session
            # dies later (e.g. service restart).
            if self._operation_name is None and (
                "batchAsyncGenerate" in url or "batchGenerate" in url
            ):
                op_name = _find_operation_name(payload)
                if op_name:
                    self._operation_name = op_name
                    logger.info(
                        "[FlowErrorSniffer:%s] captured task_id=%s",
                        self._tag, op_name,
                    )
                    if self._on_operation_name:
                        try:
                            await self._on_operation_name(op_name)
                        except Exception as cb_exc:  # pragma: no cover
                            logger.warning(
                                "[FlowErrorSniffer:%s] on_operation_name callback failed: %s",
                                self._tag, cb_exc,
                            )

            err = extract_flow_error(payload)
            if err:
                code, message = err
                self._record(code, message, url=url)
        except Exception as e:  # pragma: no cover
            logger.debug("[FlowErrorSniffer:%s] response handler error: %s", self._tag, e)

    def search_all_for_error(self) -> Optional[tuple[str, str]]:
        """Retroactive broad search through ALL stored response payloads.

        Useful when the DOM shows an error card but the narrow realtime
        sniffer (``_on_response`` → ``extract_flow_error``) didn't fire.
        Iterates stored payloads newest-first and applies:
        1. ``extract_flow_error`` (standard narrow match)
        2. ``_broad_error_search`` (catches status=FAILED + policy keywords)
        """
        # Search newest-first — the error is most likely in the latest responses
        for url, payload in reversed(self._payloads):
            err = extract_flow_error(payload)
            if err:
                logger.info(
                    "[FlowErrorSniffer:%s] Retroactive narrow match in %s: %s",
                    self._tag, url, err,
                )
                return err
            err = _broad_error_search(payload)
            if err:
                logger.info(
                    "[FlowErrorSniffer:%s] Retroactive broad match in %s: %s",
                    self._tag, url, err,
                )
                return err
        return None

    def _record(self, code: str, message: str, *, url: str) -> None:
        if self._failed.is_set():
            return
        self._error = (code, message)
        self._failed.set()
        logger.warning(
            "[FlowErrorSniffer:%s] Flow failure detected in %s: code=%s message=%s",
            self._tag, url, code, message,
        )
