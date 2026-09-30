"""Run-local concurrency for read-only service operational collection."""
import asyncio


class DeviceCalls:
    def __init__(self, call, workers=1):
        if workers < 1:
            raise ValueError("spine concurrent devices must be positive")
        self.call = call
        self.slots = asyncio.Semaphore(workers)
        self.locks = {}

    async def __call__(self, client, tool, params):
        # This adapter only wraps the exec_show operational-check path.
        # Lock first: waiting for one busy device must not consume global slots.
        device = params.get("device_name")
        async with self.locks.setdefault(device, asyncio.Lock()):
            async with self.slots:
                return await self.call(client, tool, params)


class ProbeCache(dict):
    """Coalesce concurrent requests, including unsuccessful observations."""
    def __init__(self):
        super().__init__()
        self.locks = {}


async def run_groups(items, action, key, workers):
    if workers == 1:
        for item in items:
            await action(item)
        return
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    async def run_group(group):
        for item in group:
            await action(item)
    # One task per primary device, not thousands of service tasks. DeviceCalls
    # limits active wire calls even when a service touches secondary devices.
    async with asyncio.TaskGroup() as tasks:
        for group in groups.values():
            tasks.create_task(run_group(group))
