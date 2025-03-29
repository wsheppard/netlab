import json
from pathlib import Path
import random
from typing import Dict, List, Optional
import ipaddress

import logging

from .atm import AsyncTaskManager

log = logging.getLogger("NU")

class NUError(Exception):
    def __init__(self, message, task):
        super().__init__(message)
        self.task = task

    def __str__(self):
        base_message = super().__str__()
        extra_info = (f" RC: {self.task.return_code} " 
                      + str(self.task.get_recent_logs(source='stderr')))
        return base_message + extra_info

class NetworkUtilities:
    """
    It's worth knowing what you can do as a regular user in linux - 

    If you have GROUP netdev assigned to your user, you can do quite a lot without sudo

    FOr other things, there's setuid
    """

    def __init__(self, netns: Optional[str] = None):
        self.netns = netns

    async def _run_command(self, command: List[str]):
        log.debug(f"[{self.netns}] {command}")
        task = await AsyncTaskManager(args=command, netns=self.netns)
        if task.return_code:
            #for loge in task.get_recent_logs(20):
            #    print(loge.message)
            raise NUError(f"Command failed [{command}]", task)
        return task

    async def namespace_exists(self, ns: str|None = None) -> bool:
        """Return True if the specified network namespace exists, False otherwise."""
        namespaces = await self.list_ns()
        return (ns or self.netns) in namespaces

    async def list_ns(self):
        """
        Return a list of iproute2 managed network namespaces
        """
        ret = await self._run_command(["ip","-json", "netns", "list"])
        ret = ret.get_recent_logs(1)
        if ret:
            ret = json.loads(ret[0].message)
            return list( ns['name'] for ns in ret )
        else:
            return []

    async def move_wifi_ns(self, dev: str, namespace: str) -> None:
        """
        Move a WIFI interface into a ns, create if needed
        """
        # Read the physical device name from sysfs (synchronous I/O is acceptable here)
        try:
            with open(f"/sys/class/net/{dev}/phy80211/name", "r") as f:
                phy = f.read().strip()
        except Exception as exc:
            raise NUError(f"Failed to read phy name for device {dev}", exc)

        log.info(f"** Creating wifi NS[{namespace}] dev[{dev}] phy[{phy}] **")

        # Delete the namespace if it exists; ignore errors if it doesn't
        try:
            await self._run_command(["ip", "netns", "delete", namespace])
        except NUError:
            pass

        # Define paths using pathlib
        netns_path = Path(f"/etc/netns/{namespace}")
        resolv_conf_path = netns_path / "resolv.conf"

        # Ensure /etc/netns/{namespace} directory exists
        netns_path.mkdir(parents=True, exist_ok=True)

        # Ensure resolv.conf exists
        resolv_conf_path.touch(exist_ok=True)

        # List of commands to create and configure the network namespace
        commands = [
            # Add the namespace
            ["ip", "netns", "add", namespace],
            # Ensure that lo is UP in the namespace
            ["ip", "netns", "exec", namespace, "ip", "link", "set", "lo", "up"],
            # # Ensure the mounted dir is available
            # ["mkdir", "-p", f"/etc/netns/{namespace}"],
            # # Populate that with a plain resolv conf ( to be filled by dhclient )
            # ["touch", f"/etc/netns/{namespace}/resolv.conf"],
            # Now move the interface
            ["iw", phy, "set", "netns", "name", namespace],
            # Also, rename the interface in the namespace to the namespace
            ["ip", "netns", "exec", namespace, "ip", "link", "set", dev, "name", namespace],
        ]

        for cmd in commands:
            await self._run_command(cmd)

    async def ping(self, host: str, count: int = 5) -> Dict[str, str]:
        task = await self._run_command(["ping", "-c", str(count), host])
        return self._parse_ping_output("\n".join( log.message for log in
                                                 task.get_recent_logs() ))

    async def randomize_mac_macchanger(self, interface: str):
        """
        Will require setuid
        """
        await self._run_command(["macchanger", "-r", interface])
        return await self.get_ip4(interface)

    async def flush_neighbour(self, interface: str):
        await self._run_command(["ip", "neigh", "flush", interface])

    async def randomize_mac(self, interface: str):
        def generate_random_mac():
            return ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))

        new_mac = generate_random_mac()
        await self._run_command(["ip", "link", "set", interface, "down"])
        await self._run_command(["ip", "link", "set", interface, "address", new_mac])
        await self._run_command(["ip", "link", "set", interface, "up"])

        return new_mac


    async def get_ip4(self, interface):
        result = await self.ipa("show", "dev", interface)
        if result:
            for addr_info in result:
                if 'addr_info' in addr_info:
                    for addr in addr_info['addr_info']:
                        if addr['family'] == 'inet':
                            return addr['local']
        # ipa = await self.ipa() 
        # ipl = await self.ipl() 
        #print(f"**** Could NOT FIND address on interface: {interface} *****")
        # print("IPAddress:")
        # print(json.dumps(ipa, indent=4))
        # print("IPLink:")
        # print(json.dumps(ipl, indent=4))
        return None

    async def get_routing_table(self) -> List[Dict[str, str]]:
        return await self.ipr() or []

    async def get_interface_info(self):
        ret = await self.ipl()
        if ret:
            ret = { a['ifname']: a for a in ret }
            return ret
        return {}
    
    async def ipl(self, *args: str):
        task = await self._run_command(["ip", "-json", "link"] + list(args))
        if task.return_code:
            log.warning(f"IPA command failed - {args}")
            return None

        logs = task.get_recent_logs(1)
        if logs:
            return json.loads(logs[0].message)


    async def ipa(self, *args: str):
        task = await self._run_command(["ip", "-json", "addr"] + list(args))
        if task.return_code:
            log.warning(f"IPA command failed - {args}")
            return None
        logs = task.get_recent_logs(1)
        if logs:
            return json.loads(logs[0].message)

    async def ipr(self, *args: str):
        task = await self._run_command(["ip", "-json", "route"] + list(args))
        logs = task.get_recent_logs(1)
        if logs:
            return json.loads(logs[0].message)

    async def set_ip_address(self, interface: str, ip_address: str, subnet_mask: str) -> None:
        # log.warning(f"{interface} - Setting ip address: {ip_address}/{subnet_mask}")
        await self.ipa("add", f"{ip_address}/{subnet_mask}", "dev", interface)

    async def set_gateway(self, gateway_ip: str) -> None:
        await self.ipr("add", "default", "via", gateway_ip)

    async def flush_ip_address(self, interface: str) -> None:
        await self.ipa("flush", "dev", interface)

    async def flush_routes(self, interface: str) -> None:
        await self.ipr("flush", "dev", interface)

    def _parse_ping_output(self, output: str) -> Dict[str, str]:
        result = {}
        lines = output.strip().split("\n")
        for line in lines:
            if "packets transmitted" in line:
                parts = line.split(",")
                result["packets_transmitted"] = parts[0].split()[0]
                result["packets_received"] = parts[1].split()[0]
                result["packet_loss"] = parts[2].strip()
            elif "rtt min/avg/max/mdev" in line:
                values = line.split("=")[1].strip().split("/")
                result["rtt_min"] = values[0] + " ms"
                result["rtt_avg"] = values[1] + " ms"
                result["rtt_max"] = values[2] + " ms"
                result["rtt_mdev"] = values[3] + " ms"
        return result






