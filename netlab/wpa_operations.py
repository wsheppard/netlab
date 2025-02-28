import asyncio
from dataclasses import dataclass
from typing import Optional

import logging

from .atm import AsyncTaskManager
from .dhcpman import DHCPManager
from .netutils import NetworkUtilities
from .wpa_base import WpaCtrl

log = logging.getLogger("WpaOperations")

class FailedToConnect(Exception):
    pass

class FailedToBind(Exception):
    pass

class LostConnection(Exception):
    pass

@dataclass
class NetworkConfig:
    ssid: str
    psk: Optional[str] = None
    bssid: Optional[str] = None

class WpaOperations:
    """
    High-level, ephemeral Wi-Fi operations using WpaCtrl.
    No configurations are saved; all settings are temporary.
    """

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self.wpa_ctrl = WpaCtrl(interface)
        self.connected_network_id: Optional[int] = None

    async def start(self):
        """Start the underlying WpaCtrl instance."""
        await self.wpa_ctrl.start()

    async def stop(self):
        """Close the control connection cleanly."""
        await self.disconnect()  # Ensure clean disconnection
        await self.wpa_ctrl.close()

    async def flush_networks(self):
        """Remove all configured networks (ephemeral cleanup)."""
        await self.wpa_ctrl.request("DISABLE all")
        await self.wpa_ctrl.request("FLUSH")

    async def add_network(self) -> int:
        """Add a temporary network and return its network ID."""
        response = await self.wpa_ctrl.request("ADD_NETWORK")
        try:
            network_id = int(response.strip())
            return network_id
        except ValueError:
            raise Exception(f"Failed to add network: {response.decode()}")

    async def set_network_param(self, network_id: int, param: str, value: str, quoted:
                                bool=True):
        """Set network parameters like SSID or PSK."""
        if quoted:
            value = f'"{value}"'
        cmd = f'SET_NETWORK {network_id} {param} {value}'
        response = await self.wpa_ctrl.request(cmd)
        if b"OK" not in response:
            raise Exception(f"Failed to set {param}: {response.decode()}")

    async def connect_to_ap(self, config: NetworkConfig, timeout: int = 30):
        """
        Connect to a Wi-Fi access point without persisting config.
        """
        log.info(f"Connecting to network [{config.ssid}] [{config.psk}]  [{config.bssid or ""}]")
        self.connected_network_id = None

        await self.wpa_ctrl.request("DISCONNECT")
        await self.flush_networks()
        await asyncio.sleep(1)
        network_id = await self.add_network()

        # Set SSID
        await self.set_network_param(network_id, "ssid", config.ssid)

        # Set PSK if provided
        if config.psk:
            await self.set_network_param(network_id, "psk", config.psk)
        else:
            await self.set_network_param(network_id, "key_mgmt", "NONE")

        # Set BSSID if provided
        if config.bssid:
            await self.set_network_param(network_id, "bssid", config.bssid, quoted=False)

        await self.wpa_ctrl.request(f"RECONNECT")

        # Enable the network (no config saving)
        await self.wpa_ctrl.request(f"ENABLE_NETWORK {network_id}")

        # Wait for connection event
        connected = await self.wpa_ctrl.wait_for_event("CTRL-EVENT-CONNECTED")
        if connected:
            log.debug(f"[{self.interface}] WPA_Supplicant connected!")
            self.connected_network_id = network_id
        else:
            await self.disconnect()
            raise FailedToConnect(f"[{self.interface}] Failed to connect to {config.ssid} within {timeout} seconds")

        return True

    async def disconnect(self):
        """Disconnect and remove the network configuration."""
        if self.connected_network_id is not None:
            await self.wpa_ctrl.request(f"DISABLE_NETWORK {self.connected_network_id}")
            await self.wpa_ctrl.request(f"REMOVE_NETWORK {self.connected_network_id}")
            self.connected_network_id = None

    async def status(self) -> str:
        """Retrieve the current connection status."""
        response = await self.wpa_ctrl.request("STATUS")
        return response.decode(errors='replace')

    async def get_status(self):
        response = await self.status()
        return dict(line.split("=", 1) for line in response.strip().split("\n") if "=" in line)


class WifiClient:
    """
    The top-level class that should bring all the other bits together
    """
    def __init__(self, interface, netns=None):
        self.interface = interface
        self.netns = netns if netns else interface
        self.config_path = f"/tmp/{self.interface}_wpasup.conf"
        self.ctrl_interface = "/var/run/wpa_supplicant"

        # This adds an option to the supplicant config
        self.mac_addr_randomization = False
        self.wpa_args = [
            "wpa_supplicant",
            "-i", self.interface,
            "-c", self.config_path,
            "-d"
        ]

        # This doesn't start it
        self.supplicant = AsyncTaskManager(
                self.wpa_args,
                netns=self.netns,
                log_file=f"{self.interface}.log",
                check_time=3
                )

        # Operations dont usually care about namespaces because the unix socket is in the
        # filesystem context which remains the same
        self.ops = WpaOperations(self.interface)
        self.dhcp = DHCPManager(self.interface, netns=self.netns)
        self.nu = NetworkUtilities(netns=self.netns)
        self.ipaddr = None
        self.mac = None

        # Vendor Class ID from dhcp often this is None
        self.vci = None

        self._generate_wpa_supplicant_config()

    def _generate_wpa_supplicant_config(self):
        with open(self.config_path, 'w') as conf_file:
            conf_file.write(f"ctrl_interface=DIR={self.ctrl_interface} GROUP=netdev\n")
            if self.mac_addr_randomization:
                conf_file.write("mac_addr=2\n")

    def __repr__(self):

        vci =  f"vci={self.vci}" if self.vci else ""
        bound = f"bound={self.bound}"
        ns = f"ns={self.netns}" if self.netns else ""
        ip = f"ip={self.ipaddr}" if self.bound else ""
        
        others = ", ".join( filter(bool, [bound,ip,ns,vci]) ) or ""
        main = f"WifiClient(mac={self.mac}, iface={self.interface}, {others})"
        return main

    async def ex_on_disconnect(self):
        """
        A utility for callers to wait on for disconnection
        """
        while True:
            wifistat = await self.ops.get_status()
            if "COMPLETE" not in wifistat['wpa_state']:
                raise LostConnection(f"Disconnected on  {self}")
            await asyncio.sleep(5)

    async def connect_and_dhcp(self,config: NetworkConfig):
        await self.connect(config)
        await self.start_dhcp()
        await self.wait_for_bind()
        return self

    async def full_release(self):
        await self.dhcp.release()
        await self.disconnect()
        await self.flush()

    async def setup(self):
        # The mac _probably_ won't change. So should be ok
        self.mac = await self.get_mac()

        await self.supplicant.start()
        await self.ops.start()
        await self.full_release()

    @property
    def bound(self):
        return self.dhcp.bound_event.is_set()

    async def start_dhcp(self):
        await self.dhcp.start_dhcp()

    async def connect(self, config: NetworkConfig):
        return await self.ops.connect_to_ap(config)

    async def randomize_mac(self):
        return await self.nu.randomize_mac_macchanger(self.interface)

    async def wait_for_bind(self, timeout=15):
        while True:
            self.ipaddr = None
            try:
                ret = await asyncio.wait_for( self.dhcp.wait_for_bind(), timeout )
            except asyncio.TimeoutError:
                status = await self.get_status()
                apmac = status['ap_mac']
                raise FailedToBind(f"[{self.interface}==>{apmac}] DHCP Timeout")
            else:
                if not self.dhcp.bound_event.is_set():
                    continue
                self.ipaddr = await self.get_ip_address()
                le = self.dhcp.eventq[-1]
                if le:
                    self.vci = le.data.get("new_vendor_class_identifier")
                return True

    async def disconnect(self):
        await self.ops.wpa_ctrl.request("DISCONNECT")

    async def reconnect(self):
        await self.ops.wpa_ctrl.request("RECONNECT")

    async def ping(self, target):
        if isinstance(target, WifiClient):
            des_ip = await target.get_ip_address()
            ns = target.netns
        else:
            des_ip = target
            ns = ""

        if des_ip:
            src_ip = await self.get_ip_address()
            log.debug(f"PING:{self.netns}[{src_ip}] -> {ns}[{des_ip}]")
            return await self.nu.ping(des_ip)
        else:
            raise ValueError("Cannot ping nothing...")

    async def get_status(self):
        status_dict = await self.ops.get_status()
        return {
            "mac": status_dict.get("address"),
            "ap_mac": status_dict.get("bssid"),
            "freq": status_dict.get("freq")
        }

    async def get_ap_mac(self):
        stat = await self.get_status()
        return stat['ap_mac']

    async def get_ip_address(self):
        return await self.nu.get_ip4(self.interface)

    async def get_mac(self):
        ifaceinfo = await self.nu.get_interface_info()
        ifaces = ifaceinfo.get(self.interface)
        if ifaces:
            mac = ifaces['address']
            return mac

    async def flush(self):
        self.ipaddr = None
        await self.nu.flush_ip_address(self.interface)
        await self.nu.flush_routes(self.interface)

    async def static_station(self,config):
        """
        Meant as a task, to keep a station connected - and release and retry on failure.
        """
        while True:
            await self.full_release()
            try:
                await self.connect_and_dhcp(config)
                await self.ex_on_disconnect()
            except Exception as e:
                log.warning(f"{self} Failed - try again {e}")


