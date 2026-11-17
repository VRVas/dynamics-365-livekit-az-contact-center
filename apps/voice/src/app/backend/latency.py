"""Bounded monotonic voice timings; never records audio, text, tokens or phone numbers."""

import asyncio
import json
import logging
import math
import re
import struct
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

logger = logging.getLogger("voice_latency")
MAX_EVENTS = 256
_FIELDS = frozenset({
    "transport", "warm", "language", "revision", "tool", "status", "error_type",
    "prompt_bytes", "count", "threshold_dbfs", "frames", "fast_start", "llm", "asr",
})


class LatencyTrace:
    def __init__(self, conversation_id: str | None = None):
        self.conversation_id = str(UUID(conversation_id)) if conversation_id else None
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._events: list[dict] = []
        self._first: set[str] = set()
        self._energy: dict[str, tuple[int, float]] = {}
        self.dropped_events = 0

    def elapsed_ms(self):
        return (time.perf_counter() - self.started) * 1000

    @staticmethod
    def _validate(stage, fields):
        if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage):
            raise ValueError("Use a fixed, bounded timing stage name.")
        if set(fields) - _FIELDS:
            raise ValueError("Voice timing fields must not contain free-form data.")
        for value in fields.values():
            if isinstance(value, str):
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
                    raise ValueError("Voice timing labels must be bounded identifiers.")
            elif type(value) not in {int, float, bool} or not math.isfinite(value):
                raise ValueError("Voice timing values must be finite scalars.")

    def _append(self, stage, elapsed_ms, fields, duration_ms=None):
        if len(self._events) >= MAX_EVENTS:
            self.dropped_events += 1
            if self.dropped_events == 1:
                logger.warning("Voice timing event limit reached conversation=%s", self.conversation_id)
            return
        event = {
            "stage": stage, "elapsed_ms": round(elapsed_ms, 3), **fields,
        }
        if duration_ms is not None:
            event["duration_ms"] = round(duration_ms, 3)
        self._events.append(event)
        logger.info("VOICE_LATENCY %s", json.dumps({
            "conversation_id": self.conversation_id, "origin_utc": self.started_at, **event,
        }, separators=(",", ":")))

    def mark(self, stage: str, *, once=False, **fields):
        self._validate(stage, fields)
        if once:
            if stage in self._first:
                return
            self._first.add(stage)
        self._append(stage, self.elapsed_ms(), fields)

    @contextmanager
    def span(self, stage: str, **fields):
        self._validate(stage, fields)
        start = self.elapsed_ms()
        try:
            yield
        finally:
            error = sys.exc_info()[0]
            outcome = "cancelled" if error and issubclass(error, asyncio.CancelledError) else "error" if error else "ok"
            end = self.elapsed_ms()
            self._append(stage, end, {
                **fields, "status": outcome, **({"error_type": error.__name__} if error else {}),
            }, duration_ms=end - start)

    def audio(self, direction: Literal["input", "output"], pcm):
        if direction not in {"input", "output"}:
            raise ValueError("Unknown voice timing direction.")
        self.mark(f"first_{direction}_pcm", once=True)
        stage = f"first_{direction}_energy"
        if stage in self._first:
            return
        view = memoryview(pcm)
        if not view.nbytes or view.nbytes % 2:
            raise ValueError("Voice timing expects complete PCM16 samples.")
        values = struct.iter_unpack("<h", view)
        mean_square = sum(value * value for (value,) in values) / (view.nbytes // 2)
        count, start = self._energy.get(direction, (0, self.elapsed_ms()))
        if mean_square >= 328**2:
            count += view.nbytes // 2
            self._energy[direction] = (count, start)
            if count >= 1440:  # 60 ms at the native 24 kHz rate; an energy metric, not speech recognition.
                self._first.add(stage)
                self._append(stage, start, {"threshold_dbfs": -40})
        else:
            self._energy.pop(direction, None)

    def snapshot(self):
        return {
            "conversation_id": self.conversation_id,
            "origin_utc": self.started_at,
            "clock": "monotonic",
            "events": [dict(event) for event in self._events],
            "dropped_events": self.dropped_events,
        }


@contextmanager
def measure(trace: LatencyTrace | None, stage: str, **fields):
    if trace is None:
        yield
    else:
        with trace.span(stage, **fields):
            yield
