import asyncio
import aiofiles
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import json
from typing import Optional

import logging
from netlab.utils import BGTasksMixin

@dataclass
class LogEntry:
    """Stores a log message with a timestamp and source."""
    message: str
    source: str  
    timestamp: Optional[datetime] = None

    def __repr__(self):
        return f"[{self.timestamp}][{self.source}] {self.message}"

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    def to_json(self) -> str:
        """Convert LogEntry to a JSON string."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()  # Ensure datetime is JSON-compatible
        return json.dumps(data)


class AsyncTaskManager(BGTasksMixin):
    """
    Manages asynchronous subprocess tasks with log capture.
    Supports running commands within a specified network namespace.

    NOTE:
    Linux processes when piped, don't have line discipline. This is ok for quick programs
    where it runs, outputs, and that's the end of it. But for long-running daemons and
    such it won't quite be the same if it's fully buffered.

    wpa_supplicant, MIGHT actually output line-buffered because it seems to be configured
    to do that in its code ( with or without debug mode ). But not all programs will do
    this.

    The default state is that file-descriptors have a .flush() call - and CSTDLIB will
    automatically call that on every newline - IF IT DECIDES TO DO THAT - the underlying
    programs _can_ modify this beahviour but many don't.

    So whether this matters or not is up to the callers of this class.

    We _can_ implement ptys and all that nonsense, but it might not work - because they
    introduce a whole other raft of complexities.
    """
    log = logging.getLogger("ATM")

    def __init__(self, 
                 args: list, 
                 log_file: Optional[str] = None, 
                 buffer_size: int = 500, 
                 check_time: int = 0,
                 ssh: Optional[str] = None,
                 env: dict|None = None,
                 netns: Optional[str] = None):
        self.args = args
        self.log_file = log_file
        self.buffer_size = buffer_size
        self.netns = netns
        self.ssh = ssh
        self.check_time = check_time
        self.stop_grace_period = 5
        self.bgtasks = set()
        self.env = env

        self.process: Optional[asyncio.subprocess.Process] = None

        self._log_q = asyncio.Queue()
        self.log_buffer = deque(maxlen=buffer_size)

        self._process_start = asyncio.Event()
        self._process_started = asyncio.Event()
        self._process_stopped = asyncio.Event()
        self._process_done = asyncio.Event()
        self._return_code = None

    def __repr__(self):
        return (f"ATM({self.args}, [NS-{self.netns}], "
                f"running:{self.is_running}, rc:{self.return_code}, "
                f"loglen:{len(self.log_buffer)} )")

    def __await__(self):
        return self.run().__await__()

    async def run(self):
        if not self._process_start.is_set():
            await self.start()
            self._process_start.set()
        await self._process_done.wait()
        return self

    async def start(self):
        self.log.debug(f"Starting: {self}")
        if self._process_start.is_set():
            raise RuntimeError("Task is already running.")
        self._process_start.set()

        # Task hierachy is the way forward - only complete the outer event when the task
        # is actually done.
        self.monitor = asyncio.create_task(self._monitor_task(), name=f"atm-monitor[{self}]" )
        self.monitor.add_done_callback(self.mark_done)

        self.bgadd( self._log_task(), f"atm-logi[{self}]" )
        await self._run()
        if self.check_time:
            await self.check_process_alive(self.check_time)

    def mark_done(self,task):
        # We have to do this last, as this should be about everything being complete
        self._process_done.set()
    
    async def capture_stream(self, stream, source):
        """
        Capture a stream (stdout/stderr), store logs in a buffer, and write to a JSONL file if provided.
        Can be summarily cancelled
        """
        async for line in stream:
            log_line = line.decode(errors="replace").strip()
            await self.simple_log(log_line, source)

    async def _run(self):
        cmd = self.args
        if self.netns:
            cmd = ["ip", "netns", "exec", self.netns] + cmd
        elif self.ssh:
            # Important points,
            # We don't like ttys because they're weird.
            # If we run it like this, it behaves just like anything else.
            cmd = ["ssh", "-T", self.ssh] + cmd

        self.log.debug(f"Starting process: {cmd}")

        # If we don't explicitly set stding to /dev/null it'll send the calling processes
        # stdin which might be a tty. So really it's difficcult to know what to do here.
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env
        )

        if self.process is None:
            raise RuntimeError("Cannot start process!")
            
        self.bgadd(self.capture_stream(self.process.stdout, "stdout"))
        self.bgadd(self.capture_stream(self.process.stderr, "stderr"))
        self._process_started.set()

        self.log.debug(f"Created Process: {self.process}")


    @property
    def return_code(self):
        if self.process and self._return_code is None:
            self._return_code = self.process.returncode
        return self._return_code


    async def _monitor_task(self):
        """
        Monitor for exit of the underlying OR cancellation
        """
        try:
            await self.simple_log(f"ATM wait for start: {self}", source="atm")
            await self._process_started.wait()
            await self.simple_log(f"ATM started: {self}", source="atm")
            await self.process.wait()
        except asyncio.CancelledError:
            self.log.warning(f"Cancellation in ATM monitor: {self}")
        else:
            self._process_stopped.set()
            await self.simple_log(f"ATM stopped: {self}", source="atm")
            self.log.debug(f"Process {self} has exited with return code: {self.return_code}")
        finally:
            await self.shutdown()

    async def _write_to_log_file(self, entry: LogEntry):
        if not self.log_file:
            self.log.warning("Refuse to write to no file.")
            return

        async with aiofiles.open(self.log_file, 'ab') as f:
            # Compress the JSON line using gzip
            json_line = entry.to_json() + '\n'
            compressed_data = gzip.compress(json_line.encode('utf-8'))
            # Write the compressed data to the file
            await f.write(compressed_data)

    async def simple_log(self, message, source):
        entry = LogEntry(timestamp=datetime.now(timezone.utc),
                         message=message,
                         source=source)
        await self._log_q.put(entry)
    
    async def _log_task(self):
        try:
            while True:
                le = await self._log_q.get()
                self.log_buffer.append(le)
                if self.log_file:
                    await self._write_to_log_file(le)
        except asyncio.CancelledError:
            #drain logs from queue
            while not self._log_q.empty():
                self.log.debug(f"Drain: {self._log_q.qsize()}")
                le = self._log_q.get_nowait()
                self.log_buffer.append(le)
                if self.log_file:
                    await self._write_to_log_file(le)


    @property
    def is_running(self):
        return self._process_started.is_set() and not self._process_stopped.is_set()

    async def shutdown(self):
        self.log.debug(f"Start shutdown... {self}")
        if self.process and self.is_running:
            try:
                self.process.terminate()
                for n in range(2):
                        try:
                            self.log.debug(f"Waiting for process termination... {self}")
                            await asyncio.wait_for(self.process.wait(), timeout=self.stop_grace_period)
                        except asyncio.TimeoutError:
                            if n == 0:
                                self.log.warning("Process did not terminate gracefully, forcing kill.")
                                self.process.kill()
                            else:
                                self.log.error(f"Failed to kill process {self}")
                                raise RuntimeError(f"Process {self.process} won't shutdown..")
                        else:
                            break
            except ProcessLookupError:
                pass

        while not self._log_q.empty():
            self.log.debug("Waiting for log queue to empty")
            await asyncio.sleep(0.5)

        await self.bgcancel_and_wait()

        self.log.debug(f"Shutdown complete... {self}")

    async def check_process_alive(self, delay: int = 3):
        """Wait and verify if the process is still running, without interrupting wait."""

        self.log.info("Checking process is still alive...")
        await self._process_started.wait()

        try:
            await asyncio.wait_for(self._process_done.wait(), timeout=delay)
            logs = self.get_recent_logs(20)
            self.log.error(f"Process EXITED {self}")
            for log in logs:
                self.log.error(log.message)
            raise RuntimeError(f"Process {self.process} exited unexpectedly during check.")
        except asyncio.TimeoutError:
            self.log.info(f"Process {self.process} still running after {delay} seconds.")

    def logsource(self,source="stdout"):
        return ( log for log in self.log_buffer if log.source==source )

    def get_recent_logs(self, line_count: int = 100, source="stdout"):
        return list(self.logsource(source))[-line_count:]

    def get_str_logs(self):
        """
        Keep it simple and strip out the meta, just return a list of message strings.
        """
        return list(log.message for log in self.log_buffer)

