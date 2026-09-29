import asyncio
from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


SYSTEM_CLOCK = Clock()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
