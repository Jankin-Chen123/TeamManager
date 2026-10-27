import asyncio


class ConcurrencyGate:
    """Resize without replacing the gate used by in-flight requests and queued waiters."""

    def __init__(self, limit):
        self.limit, self.active = limit, 0
        self.condition = asyncio.Condition()

    async def resize(self, limit):
        async with self.condition:
            self.limit = limit
            self.condition.notify_all()

    async def __aenter__(self):
        async with self.condition:
            await self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        return self

    async def __aexit__(self, *_):
        async with self.condition:
            self.active -= 1
            self.condition.notify_all()
