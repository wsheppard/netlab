import asyncio
from collections import deque
import json
import os
from pathlib import Path
from typing import Deque, Optional, Sequence
import ipaddress

from netlab.utils import BGTasksMixin
import logging
from .eventbus import EventBus

from .atm import AsyncTaskManager
from .netutils import NUError, NetworkUtilities
from netlab.eventbus import EventBus, Event, BaseModel, subscribe


class DHCPEvent(BaseModel):
    reason: str
    message: dict
    iface: str

class DHCPMan(BaseModel):
    iface: str
    message: str

class DHCPManager(BGTasksMixin):

    log = logging.getLogger("DHCPM")

    WORK_DIR = Path("/tmp/netlab/dhcpmanager")

    INCLUDE_KEYS = [ 
                    "reason", "interface", "new_ip_address",
                    "new_routers","new_domain_name_servers", "new_domain_name",
                    "new_vendor_class_identifier",
                    "new_subnet_mask", "new_dhcp_lease_time", "new_dhcp_server_identifier"
                    ]

    def __init__(self, interface: str, netns: Optional[str] = None, 
                 eventbus: Optional[EventBus] = None):
        self.interface = interface
        self.netns = netns
        self.lease_file = self.WORK_DIR / f"{interface}.lease"
        self.pid_file = self.WORK_DIR / f"{interface}.pid"
        self.script_file = self.WORK_DIR / f"{interface}_script.py"
        self.socket_file = self.WORK_DIR / f"{interface}.sock"
        self.WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.args = args = [
            "dhclient", "-d", "-v",
            "-sf", str(self.script_file),
            "-lf", str(self.lease_file),
            "-pf", str(self.pid_file),
            self.interface
        ]
        self._dhcptask = None 
        self._socktask = None
        self.bound_event = asyncio.Event()
        self._msg_lock = asyncio.Lock()
        self.bgtasks = set()
        self.server = None
        self._quit = asyncio.Event()

        if eventbus:
            self.eventbus = eventbus.child(f"dhcp-{interface}")
        else:
            self.eventbus = EventBus(name=f"dhcp-{interface}")

        #self.net_utils = NetworkUtilities(netns=netns,eventbus=self.eventbus)
        self.net_utils = NetworkUtilities(netns=netns)
        # Let's keep track of the last 32 events
        self.eventq: Deque[DHCPEvent] = deque(maxlen=32)
  
    def _generate_script(self):
        """
        This script is generated and called by dhclient on every event. It pipes data back
        into our application via sockets - yes, it uses python, yes we could use C, no I
        can't be bothered.
        """
        script_content = f"""#!/usr/bin/env python3
import os
import socket
import json

SOCKET_PATH = "{self.socket_file}"

# Specify the environment variables to include
INCLUDE_KEYS = {self.INCLUDE_KEYS}

def send_update():
    env = dict(os.environ)
    filtered_env = {{key: env[key] for key in INCLUDE_KEYS if key in env}}
    
    data = json.dumps(filtered_env)
   
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCKET_PATH)
            s.sendall(data.encode())
            s.close()
    except Exception as e:
        print("Exception.")
        print(type(e))
        print(str(e))
    else:
        print("******** SUCCESSFULLY SEND *************")


if __name__ == "__main__":
    send_update()
        """
        self.script_file.write_text(script_content)
        self.script_file.chmod(0o755)

    async def _listen_for_events(self):
        if self.socket_file.exists():
            self.socket_file.unlink()
        self.server = await asyncio.start_unix_server(self._handle_dhcp_message, str(self.socket_file))
        await self.server.start_serving()
        try:
            await self._quit.wait()
        except asyncio.CancelledError:
            pass
        finally:
            self.server.close()
            await self.server.wait_closed()

    async def release(self,removeleasefile=True):
        self.bound_event.clear()
        if self._dhcptask:
            await self._dhcptask.shutdown()
            await self._dhcptask
        args = [
            "dhclient", "-d", "-v", "-r",
            "-sf", str(self.script_file),
            "-lf", str(self.lease_file),
            "-pf", str(self.pid_file),
            self.interface
        ]
        ret = await AsyncTaskManager(args, netns=self.netns,
                                     check_time=0,eventbus=self.eventbus)
        self.log.debug(f"DHCP Release: {ret}")
        if removeleasefile:
            if self.lease_file.exists():
                self.lease_file.unlink()
                self.lease_file.touch()

    async def send_bus(self, msg:str):
        await self.eventbus.emit( DHCPMan(
        iface = self.interface,
            message=msg) )

    async def _process_message(self, event: Event[DHCPEvent]):
        """
        This is where our events get to eventually
        """
        devent = event.data
        message = devent.message

        async with self._msg_lock:

            self.log.debug(f"Process message: {message}")

            if devent.reason in ["BOUND", "REBOOT"]:
                ip,sm = message["new_ip_address"], message["new_subnet_mask"]
                # TODO: These can be multiple..
                gw,dns = message["new_routers"], message["new_domain_name_servers"] 
                await self.send_bus(f"Setting ip address to {ip}/{sm}")
                await self.net_utils.set_ip_address(self.interface,ip, sm)
                await self.send_bus(f"Setting gateway address to {gw}")
                await self.net_utils.set_gateway(gw)
                await self.send_bus(f"Setting dns to {dns}")
                await self.send_bus("Bound event complete")
                self.bound_event.set()
            elif devent.reason in ["RELEASE", "EXPIRE", "PREINIT"]:
                await self.send_bus("Flushing addresses and routes....")
                await self.net_utils.flush_ip_address(message["interface"])
                await self.net_utils.flush_routes(message["interface"])
                await self.send_bus("Clearing bound event...")
                self.bound_event.clear()


    async def _handle_dhcp_message(self, reader, writer):
        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                message = json.loads(data.decode())
                self.log.debug(f"Received DHCP event: {message}")
                reason = message.pop("reason")
                event = DHCPEvent(reason=reason, message=message, iface=self.interface)
                await self.eventbus.emit(event)
                self.eventq.append(event)
        except Exception:
            self.log.exception("DHCP HANDLE EXCEPTION")
        finally:
            # This is required otherwise it'll hold up..
            writer.close()

    async def wait_for_bind(self):
        await self.bound_event.wait()

    async def start_dhcp(self):
        self.bound_event.clear()
        # Should await the closing of the older task
        if self._dhcptask:
            await self._dhcptask
        await self._stop_previous()

        self._generate_script()
        await self.send_bus(f"Starting dhclient on {self.interface} with script {self.script_file}...")

        # with open(self.lease_file) as f:
        #     for line in f:
        #         print(line)

        await self.eventbus.subscribe_type( DHCPEvent, self._process_message )

        self._dhcptask = AsyncTaskManager(self.args, netns=self.netns, check_time=0,
                                          eventbus=self.eventbus)
        await self._dhcptask.start()
        if not self._socktask:
            self._socktask = self.bgadd(self._listen_for_events(),"dhcp_listener")


    async def _stop_previous(self):
        self.log.debug(f"Stopping any existing dhclient on {self.interface}...")
        if self.pid_file.exists():
            with self.pid_file.open() as f:
                pid = f.read().strip()
                if pid:
                    try:
                        os.kill(int(pid), 15)
                        self.pid_file.unlink()
                    except ProcessLookupError:
                        pass
        await self.net_utils.flush_ip_address(self.interface)
        await self.net_utils.flush_routes(self.interface)

    async def shutdown(self):
        if self._dhcptask:
            await self._dhcptask.shutdown()
        await self.eventbus.destroy()

    async def manage_dhcp(self):
        await self.start_dhcp()
        await self.wait_for_bind()
        self.log.debug(f"Interface {self.interface} successfully bound.")

