import asyncio
import asyncio
import logging
import os
from pathlib import Path
import socket
from typing import Optional, Tuple
from typing import Optional
import weakref

from netlab.utils import BGTasksMixin

from .utils import EventQ, EventRecord

log = logging.getLogger("BaseSocket")


class WpaBaseSocket(BGTasksMixin):
    """
    Basic Unix DGRAM socket with async queue support.
    """
    _counter = 0

    def __init__(self, path=None, loop=None):
        WpaBaseSocket._counter += 1
        self.path = Path(path or "/run/wpa_supplicant/wlan0")
        self.loop = loop or asyncio.get_event_loop()
        self.local_path = f"/tmp/wpa_ctrl_{os.getpid()}_{WpaBaseSocket._counter}"
        self.bgtasks = set()
        self.sock = None
        self.queue = asyncio.Queue()
        self._reader_task = None
        self._started = False

    async def start(self):
        if self._started:
            return
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        log.debug(f"Connecting sockets: {self.local_path} {self.path}")

        # Check main socket exists - be nice.
        for _ in range(5):
            if self.path.exists():
                break
            log.warning(f"WPA Socket {self.path} doesn't exist yet.. waiting..>")
            await asyncio.sleep(2)
        else:
            raise RuntimeError(f"Socket {self.path} never appeared")

        try:
            self.sock.bind(self.local_path)
            await self.loop.sock_connect(self.sock, str(self.path))
        except:
            self._cleanup()
            raise
        self._reader_task = self.bgadd(self._read_loop())
        self._started = True
        log.debug(f"Connected sockets!: {self.local_path} {self.path}")

    async def _read_loop(self):
        while self.sock:
            try:
                data = await self.loop.sock_recv(self.sock, 4096)
                if not data:
                    break
                await self.queue.put(data)
            except (asyncio.CancelledError, OSError):
                break

    async def send_raw(self, data: bytes):
        await self.loop.sock_sendall(self.sock, data)

    async def close(self):
        if not self._started:
            return
        self.bgcleanup()
        self._cleanup()
        self._started = False

    def _cleanup(self):
        if self.sock:
            self.sock.close()
        if os.path.exists(self.local_path):
            os.unlink(self.local_path)

class WpaCtrl:
    """
    WpaCtrl manages both control and event communication with wpa_supplicant.
    """
    def __init__(self, interface, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.interface = interface

        # Control socket
        path = f"/run/wpa_supplicant/{interface}"
        self.ctrl_sock = WpaBaseSocket(path, loop=self.loop)

        # Event socket (lazy init)
        self.event_sock: Optional[WpaBaseSocket] = None
        self._event_task = None
        self._subscribers = []

    async def start(self):
        await self.ctrl_sock.start()

    async def close(self):
        await self.ctrl_sock.close()
        if self.event_sock:
            await self.event_sock.send_raw(b"DETACH")
            await self.event_sock.close()
            if self._event_task:
                self._event_task.cancel()

    async def request(self, cmd: str, timeout=5) -> bytes:
        """
        Send a command over the control socket (no locks needed).
        Yes we have a 5 second sanity timeout for requests...
        """
        await self.ctrl_sock.send_raw(cmd.encode())
        return await asyncio.wait_for(self.ctrl_sock.queue.get(), timeout=timeout)

    async def _start_event_listener(self):
        """
        Lazy-init the event listener and ATTACH.
        """
        if self.event_sock:
            return

        self.event_sock = WpaBaseSocket(path=f"/run/wpa_supplicant/{self.interface}", loop=self.loop)
        await self.event_sock.start()
        await self.event_sock.send_raw(b"ATTACH")

        # Start broadcasting to subscribers
        self._event_task = asyncio.create_task(self._broadcast_events())

    async def _broadcast_events(self):
        while self.event_sock:
            try:
                data: bytes = await self.event_sock.queue.get()
                log.debug(f"Event: {data}")
                record = EventRecord(data=data.decode())
                for subref in self._subscribers:
                    sub = subref()
                    if sub:
                        sub.put_nowait(record)
            except asyncio.CancelledError:
                break

    def _dump_q(self,ref):
        log.warning(f"DUMP-Q: {ref}")
        self._subscribers.remove(ref)

    async def subscribe_events(self) -> Tuple[asyncio.Queue, weakref.ref]:
        """Creates a new subscriber queue and starts the event listener if needed."""
        await self._start_event_listener()
        q = asyncio.Queue()
        q_ref = weakref.ref(q, self._dump_q)
        self._subscribers.append(q_ref)
        return q, q_ref

    class EventSubscription:
        """Context manager for event queue cleanup."""
        def __init__(self, event_manager):
            self.event_manager = event_manager
            self.queue = None
            self.queue_ref = None

        async def __aenter__(self):
            self.queue, self.queue_ref = await self.event_manager.subscribe_events()
            return self.queue

        async def __aexit__(self, exc_type, exc, tb):
            if self.queue_ref in self.event_manager._subscribers:
                self.event_manager._subscribers.remove(self.queue_ref)
            self.queue = None
            self.queue_ref = None
            return True

    def event_subscription(self):
        return self.EventSubscription(self)
    
    async def _wait_for_event(self, pattern: str) -> Optional[EventRecord]:
        async with self.event_subscription() as sub_q:
            while True:
                record: EventRecord = await sub_q.get()
                if pattern in record.data:
                    return record

    async def wait_for_event(self, pattern: str, timeout=30) -> Optional[EventRecord]:
        return await asyncio.wait_for( self._wait_for_event(pattern), timeout )
    
