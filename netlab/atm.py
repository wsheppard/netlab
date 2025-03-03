import asyncio
import aiofiles
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Optional

import logging
from netlab.utils import BGTasksMixin

@dataclass
class LogEntry:
    """Stores a log message with a timestamp and source."""
    timestamp: datetime
    message: str
    source: str  # 'stdout' or 'stderr'

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
        self.log_buffer = deque(maxlen=buffer_size)

        self._process_start = asyncio.Event()
        self._process_started = asyncio.Event()
        self._process_stopped = asyncio.Event()
        self._process_done = asyncio.Event()
        self._return_code = None
        self._log_tasko = None

    def __repr__(self):
        return (f"ATM({self.args}, [{self.process}], "
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
        self.bgadd( self._monitor_task(), "atm-monitor" )
        self._log_tasko = self.bgadd( self._log_task(), "atm-log" )
        await self._run()
        if self.check_time:
            await self.check_process_alive(self.check_time)

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
        await self._process_started.wait()
        try:
            await self.process.wait()
        except asyncio.CancelledError:
            self.log.warning(f"Cancellation in ATM monitor: {self}")
        else:
            self._process_stopped.set()
            self.log.debug(f"Process {self} has exited with return code: {self.return_code}")
        finally:
            await self.shutdown()


    async def _log_task(self):
        """
        NOTE: If the log_task cancels, we're cancelling and so we don't guarantee any logs
        in any sane format.

        However, the monitor_task MUST wait for the log_task to complete if it completes
        properly because often the log pipes are fully buffered.
        """

        await self._process_started.wait()
    
        async def capture_stream(stream, source):
            """Capture a stream (stdout/stderr), store logs in a buffer, and write to a JSONL file if provided."""
            async for line in stream:
                log_line = line.decode(errors="replace").strip()
                timestamped_entry = LogEntry(timestamp=datetime.now(timezone.utc), message=log_line, source=source)

                self.log_buffer.append(timestamped_entry)

                if self.log_file:
                    async with aiofiles.open(self.log_file, 'a') as f:
                        await f.write(timestamped_entry.to_json() + '\n')


        await asyncio.gather(
            capture_stream(self.process.stdout, "stdout"),
            capture_stream(self.process.stderr, "stderr")
        )

    @property
    def is_running(self):
        return self._process_started.is_set() and not self._process_stopped.is_set()

    async def shutdown(self):
        if self.process and self.is_running:
            try:
                self.process.terminate()
                for n in range(2):
                        try:
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

        # We _must_ wait for logs to finish otherwise they will be incomplete
        # In normal shutdown, the streams should end when the managed process ends.
        if self._log_tasko:
            await self._log_tasko
        self._process_done.set()

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

    def get_recent_logs(self, line_count: int = 100):
        return list(self.log_buffer)[-line_count:]

    def get_str_logs(self):
        return list(log.message for log in self.log_buffer)

