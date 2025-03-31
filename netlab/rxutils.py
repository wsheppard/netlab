import aioreactive as rx
import zstandard as zstd
import asyncio
from pathlib import Path
from aioreactive import AsyncIteratorObserver, AsyncObservable
from typing import AsyncIterator, TypeVar, Generic

from aioreactive.subject import AsyncSubject
from typing import Any
from aioreactive.subject import AsyncSubject
from aioreactive import AsyncObserver, AsyncDisposable, AsyncAnonymousObserver
from aioreactive.types import SendAsync, ThrowAsync, CloseAsync
from typing import Generic, TypeVar, Optional

_T = TypeVar("_T")

class LazyIterSubscription(Generic[_T]):
    def __init__(self, source: AsyncObservable[_T]):
        self._observer = AsyncIteratorObserver(source)

    async def __aenter__(self) -> AsyncIterator[_T]:
        # triggers lazy subscription on first iteration
        return self._observer

    async def __aexit__(self, *args):
        await self._observer.dispose_async()

    def __aiter__(self) -> AsyncIterator[_T]:
        return self._observer


class BehaviorSubject(AsyncSubject[_T], Generic[_T]):
    """
    Mimick the rxjs class of the same name
    """
    def __init__(self, initial_value: _T) -> None:
        super().__init__()
        self._last: _T = initial_value

    def get_value(self) -> _T:
        return self._last

    async def asend(self, value: _T) -> None:
        self._last = value
        await super().asend(value)

    async def subscribe_async(
        self,
        send: Optional[SendAsync[_T] | AsyncObserver[_T]] = None,
        throw: Optional[ThrowAsync] = None,
        close: Optional[CloseAsync] = None,
    ) -> AsyncDisposable:
        observer: AsyncObserver[_T]

        if isinstance(send, AsyncObserver):
            observer = send
        else:
            observer = AsyncAnonymousObserver(send, throw, close)

        # Send the current value immediately to new subscriber
        await observer.asend(self._last)

        # Subscribe for future updates
        return await super().subscribe_async(observer)



class AsyncZstdLogWriter:
    """
    Context manager for zstd stream
    """
    def __init__(self, path: Path, mode: str = "ab"):
        self._path = path
        self._mode = mode
        self._file = None
        self._stream = None

    async def __aenter__(self):
        self._file = await asyncio.to_thread(open, self._path, self._mode)
        cctx = zstd.ZstdCompressor(level=12)
        self._stream = cctx.stream_writer(self._file)
        return self

    def _closedown(self):
        if self._stream is None or  self._file is None:
            raise RuntimeError("Bad Objects")
        self._stream.flush(zstd.FLUSH_FRAME)
        self._stream.close()
        self._file.close()

    async def __aexit__(self, exc_type, exc, tb):
        await asyncio.to_thread( self._closedown )

    async def write(self, data: bytes):
        if self._stream is None:
            raise RuntimeError("Bad objects")
        await asyncio.to_thread(self._stream.write, data)

async def logtofile(obs: rx.AsyncObservable[str], path:Path):
    """
    A helper to push any observable out to a compressed file
    This uses internal buffering, and the context manager to flush.
    """
    zpath = path.with_name( path.name + ".zstd" )
    async with AsyncZstdLogWriter(zpath) as log, LazyIterSubscription(obs) as chunks:
            async for chunk in chunks:
                await log.write(chunk.encode())






