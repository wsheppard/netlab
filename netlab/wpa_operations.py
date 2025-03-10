import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, TypeAlias
import json

import logging

from netlab import libwifi
from netlab.utils import EventBus, EventRecord

from .atm import AsyncTaskManager
from .dhcpman import DHCPManager
from .netutils import NetworkUtilities
from .wpa_base import WpaCtrl
import re
import datetime
from dataclasses import dataclass, field

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

WPA_DISCONNECT_REASONS = {
    0: "Unspecified reason",
    1: "Previous authentication no longer valid",
    2: "Deauthenticated due to inactivity",
    3: "Deauthenticated because AP is unable to handle all stations",
    4: "Class 2 frame received from non-authenticated station",
    5: "Class 3 frame received from non-associated station",
    6: "Station has left the BSS",
    7: "Station requesting (re)association is not authenticated",
    8: "Disassociated due to inactivity",
    9: "Disassociated because AP is unable to handle all stations",
    10: "Class 2 frame received from non-authenticated station",
    11: "Class 3 frame received from non-associated station",
    12: "Disassociated, leaving due to excessive retries",
    13: "Disassociated, reason unspecified",
    14: "Disassociated due to missing PMKSA cache entry",
    15: "Disassociated due to 4-way handshake timeout",
    16: "Disassociated due to invalid group cipher",
    17: "Disassociated due to authentication timeout",
    18: "Disassociated due to reason outside the standard list",
    34: "Disassociated because AP requested reassociation",
    35: "Disassociated due to network congestion",
    36: "Disassociated due to security policy violation",
    37: "Disassociated due to protocol timeout",
    39: "Disassociated due to regulatory reasons",
    43: "Disassociated due to excessive retries in reassociation",
    45: "Disassociated due to AP handoff",
}

@dataclass
class WpaEventRecord(EventRecord):
    event_type: str|None = None
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        self._parse_data()

    @classmethod
    def from_event_record(cls, record: EventRecord) -> "WpaEventRecord":
        return cls(data=record.data, timestamp=record.timestamp)

    def _parse_data(self):
        event_patterns = [
            ("SCAN_RESULTS", r"<3>CTRL-EVENT-SCAN-RESULTS"),
            ("AP_AVAILABLE", r"<3>WPS-AP-AVAILABLE"),
            ("AUTH_ATTEMPT", r"<3>SME: Trying to authenticate with (?P<mac>[0-9a-fA-F:]+) \(SSID='(?P<ssid>[^']+)' freq=(?P<freq>\d+) MHz\)"),
            ("ASSOCIATE", r"<3>Trying to associate with (?P<mac>[0-9a-fA-F:]+) \(SSID='(?P<ssid>[^']+)' freq=(?P<freq>\d+) MHz\)"),
            ("ASSOCIATED", r"<3>Associated with (?P<mac>[0-9a-fA-F:]+)"),
            ("SUBNET_STATUS", r"<3>CTRL-EVENT-SUBNET-STATUS-UPDATE status=(?P<status>\d+)"),
            ("KEY_NEGOTIATION", r"<3>WPA: Key negotiation completed with (?P<mac>[0-9a-fA-F:]+) \[PTK=(?P<ptk>\w+) GTK=(?P<gtk>\w+)\]"),
            ("CONNECTED", r"<3>CTRL-EVENT-CONNECTED - Connection to (?P<mac>[0-9a-fA-F:]+) completed \[id=(?P<id>\d+).*?\]"),
            ("DISCONNECTED", r"<3>CTRL-EVENT-DISCONNECTED reason=(?P<reason>\d+)"),
            ("SSID_TEMP_DISABLED", r"<3>CTRL-EVENT-SSID-TEMP-DISABLED id=(?P<id>\d+) ssid=\"(?P<ssid>[^\"]+)\" auth_failures=(?P<failures>\d+) duration=(?P<duration>\d+)"),
            ("SSID_REENABLED", r"<3>CTRL-EVENT-SSID-REENABLED id=(?P<id>\d+) ssid=\"(?P<ssid>[^\"]+)\""),
            ("EAP_START", r"<3>CTRL-EVENT-EAP-STARTED (?:.*)"),
            ("EAP_SUCCESS", r"<3>CTRL-EVENT-EAP-SUCCESS"),
            ("EAP_FAILURE", r"<3>CTRL-EVENT-EAP-FAILURE"),
            ("BSS_ADDED", r"<3>CTRL-EVENT-BSS-ADDED (?P<bss_id>\d+) (?P<mac>[0-9a-fA-F:]+)"),
            ("BSS_REMOVED", r"<3>CTRL-EVENT-BSS-REMOVED (?P<bss_id>\d+) (?P<mac>[0-9a-fA-F:]+)"),
            ("REGDOM_CHANGE", r"<3>CTRL-EVENT-REGDOM-CHANGE (?P<change_type>\w+) (?P<country>\w+)"),
            ("CHANNEL_SWITCH", r"<3>CTRL-EVENT-CHANNEL-SWITCH freq=(?P<freq>\d+) width=(?P<width>\d+)"),
            ("ALARM", r"<3>CTRL-EVENT-ALARM (?P<message>.+)"),
        ]

        for etype, pattern in event_patterns:
            match = re.search(pattern, self.data)
            if match:
                self.event_type = etype
                self.details = match.groupdict()

                # If it's a disconnection event, add reason description
                if etype == "DISCONNECTED" and "reason" in self.details:
                    reason_code = int(self.details["reason"])
                    self.details["reason_desc"] = WPA_DISCONNECT_REASONS.get(reason_code, "Unknown reason")
                break

    def get_event(self):
        """Returns the parsed event type and details."""
        return self.event_type, self.details

WpaEventDeq: TypeAlias = Deque[WpaEventRecord]

class WpaOperations:
    """
    High-level, ephemeral Wi-Fi operations using WpaCtrl.
    No configurations are saved; all settings are temporary.
    """

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface
        self.wpa_ctrl = WpaCtrl(interface)
        self.connected_network_id: Optional[int] = None
        self.events_task = None
        self.event_bus = EventBus()
        # Keep a history of recent events
        self.wpa_events:WpaEventDeq = deque(maxlen=128)
    
    async def _wait_for_event(self, matcher:Callable[[WpaEventRecord],bool]) -> Optional[WpaEventRecord]:
        async with self.event_bus as sub_q:
            while True:
                record: WpaEventRecord = await sub_q.get()
                if matcher(record):
                    return record

    async def wait_for_event(self, event: str, timeout=30) -> Optional[WpaEventRecord]:
        # Simple event name matcher
        def match_event(new_event:WpaEventRecord):
            if new_event.event_type:
                return event in new_event.event_type
            return False

        return await asyncio.wait_for( self._wait_for_event(match_event), timeout )
    

    async def _events_task(self):
        """
        Here we convert the raw event stream to our json-friendly one
        """
        async with self.wpa_ctrl.event_subscription() as sub_q:
            while True:
                record: EventRecord = await sub_q.get()
                parsed = WpaEventRecord.from_event_record(record)
                self.event_bus.put(parsed)
                self.wpa_events.append(parsed)

    async def start(self):
        """Start the underlying WpaCtrl instance."""
        await self.wpa_ctrl.start()
        self.events_task = asyncio.create_task( self._events_task() )

    async def stop(self):
        """Close the control connection cleanly."""
        if self.events_task:
            self.events_task.cancel()
            await self.events_task
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

    async def connect_to_ap(self, config: NetworkConfig):
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

    async def wait_for_connection(self):
        # Wait for connection event
        connected = await self.wait_for_event("CONNECTED")
        if connected:
            log.debug(f"[{self.interface}] WPA_Supplicant connected!")
        else:
            await self.disconnect()
            raise FailedToConnect(
                    f"[{self.interface}] Failed to connect in 30 secs")
        return True

    async def disconnect(self):
        """Disconnect and remove the network configuration."""
        if self.connected_network_id is not None:
            await self.wpa_ctrl.request(f"DISABLE_NETWORK {self.connected_network_id}")
            await self.wpa_ctrl.request(f"REMOVE_NETWORK {self.connected_network_id}")
            self.connected_network_id = None

    async def is_connected(self) -> bool:
        return False

    async def status(self) -> str:
        """Retrieve the current connection status."""
        response = await self.wpa_ctrl.request("STATUS")
        return response.decode(errors='replace')

    async def get_status(self):
        response = await self.status()
        return dict(line.split("=", 1) for line in response.strip().split("\n") if "=" in line)


class WifiClient:
    """
    The top-level class that manages a wifi interface inside a network namespace

    Specify the interface to work with.
    Can also specify if that interface is inside a net-namespace.
    Also you can specify the log_file name to record all supplicant debug output
    """
    def __init__(self, interface, netns=None, log_file=None):
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
               
        # TODO: Should we always have a log, they can take up a lot of space
        self.log_file = log_file or f"{self.interface}.log.jsonl"

        # This doesn't start it
        self.supplicant = AsyncTaskManager(
                self.wpa_args,
                netns=self.netns,
                log_file=self.log_file,
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

   
        self._setup_done = asyncio.Event()

        self._generate_wpa_supplicant_config()

        self.event_bus_wpa = self.ops.event_bus
        self.event_bus_dhcp = self.dhcp.event_bus

    @property
    def setup_done(self):
        return self._setup_done.is_set()

    async def setup(self):
        """
        Start supplicant, operations and clear dhcp
        """

        if not self._setup_done.is_set():
            # The mac _probably_ won't change. So should be ok
            self.mac = await self.get_mac()

            await self.supplicant.start()
            await self.ops.start()
            self.events_task = asyncio.create_task( self._events_task() )
            await self.full_release()
            self._setup_done.set()


    def _generate_wpa_supplicant_config(self):
        with open(self.config_path, 'w') as conf_file:
            conf_file.write(f"ctrl_interface=DIR={self.ctrl_interface} GROUP=netdev\n")
            if self.mac_addr_randomization:
                conf_file.write("mac_addr=2\n")

    def dict(self):
        """
        Return a representation of this class as a dict
        """
        data = {
            "mac": self.mac,
            "iface": self.interface,
            "bound": self.bound,
            "ip": self.ipaddr if self.bound else None,
            "ns": self.netns if self.netns else None,
            "vci": self.vci if self.vci else None
        }

        # Remove None values for cleaner output
        return {k: v for k, v in data.items() if v is not None}

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

    async def connect_and_dhcp(self, config: NetworkConfig):
        await self.connect(config)
        await self.ops.wait_for_connection()
        await self.start_dhcp()
        await self.wait_for_bind()
        return self

    async def full_release(self):
        await self.dhcp.release()
        await self.disconnect()
        await self.flush()

    async def _events_task(self):
        """
        Here we convert the raw event stream to our json-friendly one
        """
        async with self.ops.wpa_ctrl.event_subscription() as sub_q:
            while True:
                record: EventRecord = await sub_q.get()
                parsed = WpaEventRecord.from_event_record(record)
                self.event_bus.put(parsed)
                self.wpa_events.append(parsed)

    async def wifii(self):
        wifiis = await libwifi.WifiInterface.from_iwp(self.netns)
        return next( wifii for wifii in wifiis 
                    if wifii.interface == self.interface)

    @property
    def bound(self):
        return self.dhcp.bound_event.is_set()

    @property
    def connected(self):
        return 

    async def start_dhcp(self):
        await self.dhcp.start_dhcp()

    async def connect(self, config: NetworkConfig):
        return await self.ops.connect_to_ap(config)

    async def randomize_mac(self):
        return await self.nu.randomize_mac_macchanger(self.interface)

    async def wait_for_bind(self, timeout=20):
        while True:
            self.ipaddr = None
            try:
                ret = await asyncio.wait_for( self.dhcp.wait_for_bind(), timeout )
            except asyncio.TimeoutError:
                status = await self.get_status()
                apmac = status['ap_mac']
                raise FailedToBind(f"[{self.interface}==>{apmac}] DHCP Timeout [{timeout}]")
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


