import asyncio
import getpass
import grp
import os
import pathlib
import pickle
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Coroutine, Deque, TypeAlias
import logging
import weakref

log = logging.getLogger("netlab.utils")

class BGTasksMixin:
    
    # This is designed to break to force override
    bgtasks: set|None = None
    # You should call bgcleanup somewhere otherwise these tasks will be
    # around forever!!

    def bgtask_complete(self, task):
        log = getattr(self, "log", None) or logging.getLogger("bgtasks")
        name = task.get_name()
        try:
            result = task.result()
        except asyncio.CancelledError:
            # We're expecting cancellation here and as we're in the bgtasks callback
            # we can just absorb it.
            log.debug(f"[{name}] Completed with Cancellation")
        except asyncio.TimeoutError:
            log.warning(f"Completed with Timeout")
        except Exception:
            log.exception(f"Completed with Exception")
        else:
            log.debug(f"[{name}] Completed without exception: [{result}]")
        finally:
            self.bgtasks.discard(task)

    def bgadd(self, coro:Coroutine, name:str|None=None) -> asyncio.Task:
        if not isinstance(coro, asyncio.Task):
            tsk = asyncio.create_task(coro, name=name)
        else:
            tsk = coro
        self.bgtasks.add(tsk)
        tsk.add_done_callback(self.bgtask_complete)
        return tsk

    def bgcleanup(self):
        log = getattr(self, "log") or logging.getLogger("bgtasks")
        if not self.bgtasks:
            return
        for task in self.bgtasks:
            if task is asyncio.current_task():
                log.debug(f"BgCleanup[{self}]: Skipping Current Task\n{task}")
                continue
            log.debug(f"BgCleanup[{self}]: Cancelling Task\n{task}")
            task.cancel()

    async def bgcancel_and_wait(self):
        """
        Cancel all background tasks except the current one, then wait for them to finish.
        This ensures that when a master task decides to cancel the rest, it properly waits
        for cleanup before returning.
        """
        log = getattr(self, "log", None) or logging.getLogger("bgtasks")
        current = asyncio.current_task()
        tasks_to_cancel = {task for task in self.bgtasks if task is not current}
        if not tasks_to_cancel:
            log.debug("No tasks to cancel")
            return

        for task in tasks_to_cancel:
            log.debug(f"bgcancel_and_wait: Cancelling task {task.get_name()}")
            task.cancel()

        # Wait for all cancelled tasks to finish, collecting exceptions.
        return await asyncio.gather(*tasks_to_cancel, return_exceptions=True)


    async def bgwait(self):
        log = getattr(self, "log", None) or logging.getLogger("bgtasks")
        log.debug("Waiting on completion...")
        if not self.bgtasks:
            log.debug("No tasks to wait on, early exit")
            return
        while True:
            comp, pend = await asyncio.wait(self.bgtasks, timeout=5)

            if comp:
                log.debug(f"Tasks result: {comp}")

            if pend:
                this = asyncio.current_task()
                thisname = this.get_name()
                log.debug(f"Tasks still running after 5 seconds This: {thisname}")
                for t in self.bgtasks:
                    name = t.get_name()
                    log.debug(name)
            else:
                break
        return


class PythonBinaryChecker:
    """
    I don't like running as root.
    So here we look to try to add caps to the current python binary and sequester it away
    from other users with o700

    This class will check if the caller has all the permissions required to run the netlab
    utilities
    """

    # To ensure only the user has access to python 
    REQUIRED_FILE_MODE = 0o700

    # Needed for raw sockets and interacting with wifi devices
    # Capability bits: CAP_NET_RAW (bit 13), CAP_NET_ADMIN (bit 12), CAP_SYS_ADMIN (bit 21)
    REQUIRED_CAPS = {
        "cap_net_raw": 1 << 13,
        "cap_net_admin": 1 << 12,
        "cap_sys_admin": 1 << 21,
    }

    # This allows for various operations to users with it as membership
    NETDEV_GROUP = "netdev"

    # For namespace use - mostly for storing the resolv.conf
    ETC_NETNS_PATH = "/etc/netns"

    def __init__(self, executable_path=None):
        self.executable = pathlib.Path(executable_path or sys.executable).resolve()
        self._file_mode = self.executable.stat().st_mode & 0o777

    @property
    def file_mode_ok(self):
        """Check that the Python executable has mode 700."""
        return self._file_mode == self.REQUIRED_FILE_MODE

    @property
    def capabilities_missing(self):
        """Return a list of missing capabilities based on /proc/self/status."""
        cap_eff = 0
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("CapEff:"):
                        cap_eff = int(line.split()[1], 16)
                        break
        except Exception:
            # If we can't read capabilities, assume all are missing.
            return list(self.REQUIRED_CAPS.keys())
        return [cap for cap, bit in self.REQUIRED_CAPS.items() if not (cap_eff & bit)]

    @property
    def capabilities_ok(self):
        """Return True if all required capabilities are present."""
        return not self.capabilities_missing

    @property
    def in_netdev_group(self):
        """Check if the current user is in the netdev group."""
        try:
            netdev_gid = grp.getgrnam(self.NETDEV_GROUP).gr_gid
        except KeyError:
            return False
        return netdev_gid in os.getgroups()

    @property
    def etc_netns_group_writable(self):
        """
        Check if /etc/netns exists, is owned by the netdev group,
        and is group-writable.
        """
        try:
            st = os.stat(self.ETC_NETNS_PATH)
        except FileNotFoundError:
            return False
        try:
            netdev_gid = grp.getgrnam(self.NETDEV_GROUP).gr_gid
        except KeyError:
            return False
        if st.st_gid != netdev_gid:
            return False
        return bool(st.st_mode & stat.S_IWGRP)

    @property
    def all_checks_ok(self):
        """Return True only if all individual checks pass."""
        return (self.file_mode_ok and
                self.capabilities_ok and
                self.in_netdev_group and
                self.etc_netns_group_writable)

    def diagnose(self):
        """Print diagnostic messages if any check fails."""
        if self.all_checks_ok:
            return

        print(f"Python executable: {self.executable}")

        if not self.file_mode_ok:
            print(f"File mode is {oct(self._file_mode)}, expected {oct(self.REQUIRED_FILE_MODE)}.")
            print(f"To set the file mode, run:\n  sudo chmod {oct(self.REQUIRED_FILE_MODE)[2:]} {self.executable}")

        if not self.capabilities_ok:
            missing = ",".join(self.capabilities_missing)
            print("Missing capabilities detected.")
            print(f"To add the missing capabilities, run:\n  sudo setcap {missing}+ep {self.executable}")

        print(f"User is in netdev group: {self.in_netdev_group}")
        if not self.in_netdev_group:
            username = getpass.getuser()
            print(f"To add your user to the netdev group, run:\n  sudo usermod -aG {self.NETDEV_GROUP} {username}")

        print(f"/etc/netns is group-writable by netdev: {self.etc_netns_group_writable}")
        if not self.etc_netns_group_writable:
            print("Ensure /etc/netns is owned by the netdev group and is group-writable.")


class CancellableQueue(asyncio.Queue):
    """
    This class allows queues to cancel waiters on them
    """
    _SENTINEL = object()  # Unique sentinel for cancellation

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cancelled = False
        self._waiters = set()

    async def get(self):
        if self._cancelled:
            raise asyncio.CancelledError("Queue has been cancelled")
        
        item = await super().get()
        
        if item is self._SENTINEL:
            raise asyncio.CancelledError("Queue was cancelled while waiting")
        
        return item

    def cancel(self):
        """Cancel all current and future waiters by inserting a sentinel."""
        self._cancelled = True
        self.put_nowait(self._SENTINEL)  # Unblock waiters with sentinel
        
    def reset(self):
        """Allow the queue to be used again after cancellation."""
        self._cancelled = False

def async_cache_result(func):
    """
    ASYNC Decorator which will simply cache the result of the wrapped function for an hour
    """
    async def wrapper(*args, **kwargs):
        cache_file = f"/tmp/{func.__name__}.pkl"
        use_cache = False
        if os.path.isfile(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 3600:  # less than 1 hour old
                use_cache = True
        if use_cache:
            with open(cache_file, "rb") as f:
                result = pickle.load(f)
        else:
            if os.path.isfile(cache_file):
                log.debug(f"Cache expired: {cache_file}")
            else:
                log.debug(f"Cache Miss: {cache_file}")
            result = await func(*args, **kwargs)
            with open(cache_file, "wb") as f:
                pickle.dump(result, f)
        return result
    return wrapper

def sync_cache_result(func):
    """
    SYNC Decorator which will simply cache the result of the wrapped function for an hour
    """
    def wrapper(*args, **kwargs):
        cache_file = f"/tmp/{func.__name__}.pkl"
        use_cache = False
        if os.path.isfile(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 3600:  # less than 1 hour old
                use_cache = True
        if use_cache:
            with open(cache_file, "rb") as f:
                result = pickle.load(f)
        else:
            if os.path.isfile(cache_file):
                log.debug(f"Cache expired: {cache_file}")
            else:
                log.debug(f"Cache Miss: {cache_file}")
            result = func(*args, **kwargs)
            with open(cache_file, "wb") as f:
                pickle.dump(result, f)
        return result
    return wrapper
