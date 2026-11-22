"""Recover an observed native SnowLuma child through its existing owner."""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import asyncio
import time


class SnowLumaRecovery:
    @staticmethod
    def can_attach(manager) -> bool:
        process = getattr(manager, "process", None)
        return (getattr(manager, "deploy_mode", None) == "native"
                and process is not None and getattr(process, "returncode", 1) is None
                and callable(getattr(manager, "start", None))
                and callable(getattr(manager, "stop", None)))

    def __init__(self, manager, events, *, interval: float = 3):
        if not self.can_attach(manager):
            raise ValueError("Recovery requires an observed native managed child")
        self.manager = manager
        self.events = events
        self.interval = interval
        self.desired = True
        self.closed = False
        self.failures = 0
        self.next_try = 0.0
        self.healthy_since = time.monotonic()
        self._revision = 0
        self._starting = set()
        self._task = None
        self._start = manager.start
        self._stop = manager.stop
        self._prior = {name: vars(manager).get(name) for name in ("start", "stop")}
        self._had = {name: name in vars(manager) for name in ("start", "stop")}

        async def start(*args, **kwargs):
            task, revision = asyncio.current_task(), self._revision
            self._starting.add(task)
            try:
                result = await self._start(*args, **kwargs)
                if result and revision != self._revision and not self.closed:
                    # A user stop arriving during startup takes precedence over
                    # that in-flight attempt, including a child spawned late.
                    await self._stop()
                    return False
                if result and self.can_attach(manager) and revision == self._revision and not self.closed:
                    self.desired = True
                return result
            finally:
                self._starting.discard(task)

        async def stop(*args, **kwargs):
            # A failed native start invokes stop() internally. Only an explicit
            # stop by a different task changes the user's desired state.
            if asyncio.current_task() not in self._starting:
                self.desired = False
                self._revision += 1
            return await self._stop(*args, **kwargs)

        self._wrapped_start, self._wrapped_stop = start, stop
        manager.start, manager.stop = start, stop

    def start(self):
        if not self.closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="mover-snowluma-recovery")

    async def tick(self):
        if self.closed or not self.desired or getattr(self.manager, "deploy_mode", None) != "native":
            return
        process = getattr(self.manager, "process", None)
        if process is not None and getattr(process, "returncode", 1) is None:
            if self.healthy_since is None:
                self.healthy_since = time.monotonic()
            if time.monotonic() - self.healthy_since >= 60:
                self.failures = 0
            return
        self.healthy_since = None
        if time.monotonic() < self.next_try:
            return
        self.events.write("recovery", "snowluma", state="restarting")
        try:
            recovered = await self.manager.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            recovered = False
        self.failures += 1
        self.next_try = time.monotonic() + min(300, 5 * 2 ** min(self.failures - 1, 6))
        self.events.write("recovery", "snowluma", bool(recovered),
                          state="started" if recovered else "retry_wait")

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.tick()

    async def close(self):
        self.closed, self.desired = True, False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        for name, wrapper in (("start", self._wrapped_start), ("stop", self._wrapped_stop)):
            if getattr(self.manager, name, None) is wrapper:
                if self._had[name]:
                    setattr(self.manager, name, self._prior[name])
                else:
                    delattr(self.manager, name)
