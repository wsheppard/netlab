import asyncio
import logging
from netlab.utils import BGTasksMixin
from netlab.wpa_operations import WifiClient
from netlab import netutils

class WifiPingTester(BGTasksMixin):

    log = logging.getLogger("wifi-ping-tester")

    def __init__(self, clients, concurrency=10):
        self.clients = clients
        self.running = False
        self.bgtasks = set()
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _ping_clients(self, client1:WifiClient, client2:WifiClient):
        async with self.semaphore:

            #print(f"PING CLIENT: {client1} {client2}")

            ip1 = await client1.get_ip_address()
            ip2 = await client2.get_ip_address()
            mac1 = await client1.get_status()
            mac2 = await client2.get_status()

            ping_info = {
                "source": {
                    "client": client1.dict(),
                    "interface": client1.interface,
                    "ip": ip1,
                    "mac": mac1['mac'],
                    "ap_mac": mac1['ap_mac']
                },
                "destination": {
                    "client": client2.dict(),
                    "interface": client2.interface,
                    "ip": ip2,
                    "mac": mac2['mac'],
                    "ap_mac": mac2['ap_mac']
                },
                "internal": None,
                "external": None
                }

            if ip1 and ip2:

                # OK client 1 pings client 2
                try:
                    internal = await client1.ping(client2)
                except netutils.NUError:
                    self.log.debug(f"PING FAILED:{client1} -> {client2}")
                else:
                    self.log.debug(f"PING OK:{client1} -> {client2}")
                    ping_info["internal"] = internal

                # OK client 1 pings WAN
                try:
                    external = await client1.ping("1.1.1.1")
                except netutils.NUError:
                    self.log.debug(f"EXTERNAL PING FAILED:{client1} -> 1.1.1.1")
                else:
                    self.log.debug(f"EXTERNAL PING OK:{client1} -> 1.1.1.1")
                    ping_info["external"] = external

            return ping_info

    async def uni_ping_tests(self, source:WifiClient):
        """
        Uni-directional test from source to all configured clients.
        """
        clients = len(self.clients)
        self.log.info(f"Uni Ping test to {clients} clients")
        tests = [self._ping_clients(source, client) for client in self.clients]
        return await asyncio.gather(*tests, return_exceptions=True)

    async def run_ping_tests(self):
        """
        All combinations of clients.
        """

        tests = []
        for client1 in self.clients:
            for client2 in self.clients:
                if client1 != client2:
                    tests.append(self._ping_clients(client1, client2))
        return await asyncio.gather( *tests, return_exceptions=True )

