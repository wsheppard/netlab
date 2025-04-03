import asyncio
import asyncio
import logging
import os
from pathlib import Path
import socket
from typing import Optional, Tuple
from typing import Optional
from uuid import uuid4
import weakref

from pydantic import BaseModel

from netlab.eventbus import EventBus
from netlab.utils import BGTasksMixin


log = logging.getLogger("BaseSocket")


class WpaBaseSocket(BGTasksMixin):
    """
    Basic Unix DGRAM socket with async queue support.
    """

    def __init__(self, path):
        self.path = Path(path)
        ranid = uuid4()
        self.local_path = Path(f"/tmp/netlab/wpa_ctrl_{ranid}")
        self.bgtasks = set()
        self.sock = None
        self.queue = asyncio.Queue()
        self._reader_task = None
        self._started = False


    async def start(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        if self._started:
            return
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        log.debug(f"Connecting sockets: {self.local_path} {self.path}")

        loop = asyncio.get_running_loop()

        # Check main socket exists - be nice.
        for _ in range(5):
            if self.path.exists():
                break
            log.warning(f"WPA Socket {self.path} doesn't exist yet.. waiting..>")
            await asyncio.sleep(2)
        else:
            raise RuntimeError(f"Socket {self.path} never appeared")

        try:
            self.sock.bind(str(self.local_path))
            await loop.sock_connect(self.sock, str(self.path))
        except:
            self._cleanup()
            raise
        self._reader_task = self.bgadd(self._read_loop())
        self._started = True
        log.debug(f"Connected sockets!: {self.local_path} {self.path}")

    async def _read_loop(self):
        loop = asyncio.get_running_loop()
        while self.sock:
            try:
                data = await loop.sock_recv(self.sock, 4096)
                if not data:
                    break
                await self.queue.put(data)
            except (asyncio.CancelledError, OSError):
                break

    async def send_raw(self, data: bytes):
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self.sock, data)

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


class WpaEvent(BaseModel):
    data: str


class WpaCtrl:
    """
    WpaCtrl manages both control and event communication with wpa_supplicant.
    """
    def __init__(self, interface, eventbus: Optional[EventBus] = None):
        self.interface = interface

        # Control socket
        path = f"/run/wpa_supplicant/{interface}"
        self.ctrl_sock = WpaBaseSocket(path)

        # Event socket (lazy init)
        self.event_sock: Optional[WpaBaseSocket] = None
        self._event_task = None
        self._subscribers = []

        if eventbus:
            self.eventbus = eventbus.child(f"wpactrl-{interface}")
        else:
            self.eventbus = EventBus(f"wpactrl-{interface}")

    async def start(self):
        await self.ctrl_sock.start()

        self.event_sock = WpaBaseSocket(path=f"/run/wpa_supplicant/{self.interface}")
        await self.event_sock.start()
        await self.event_sock.send_raw(b"ATTACH")
        # Start broadcasting to subscribers
        self._event_task = asyncio.create_task(self._broadcast_events())

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

    async def _broadcast_events(self):
        while self.event_sock:
            try:
                data: bytes = await self.event_sock.queue.get()
                log.debug(f"Event: {data}")
                record = WpaEvent(data=data.decode())
                await self.eventbus.emit(record)
            except asyncio.CancelledError:
                break

    def _dump_q(self,ref):
        log.warning(f"DUMP-Q: {ref}")
        self._subscribers.remove(ref)

    
