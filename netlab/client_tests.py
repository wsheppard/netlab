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

    async def _ping_clients(self, client1:WifiClient, client2:WifiClient, do_reverse=False):
        async with self.semaphore:

            #print(f"PING CLIENT: {client1} {client2}")

            src_ip = await client1.get_ip_address()
            dst_ip = await client2.get_ip_address()

            forward = None
            reverse = None
            src_ext = None
            dst_ext = None

            if src_ip:
                try:
                    src_ext = await client1.ping("1.1.1.1")
                except netutils.NUError:
                    pass

            if dst_ip:
                try:
                    dst_ext = await client2.ping("1.1.1.1")
                except netutils.NUError:
                    pass

            if src_ip and dst_ip:

                try:
                    forward = await client1.ping(client2)
                except netutils.NUError:
                    pass

                if do_reverse:
                    try:
                        reverse = await client1.ping(client2)
                    except netutils.NUError:
                        pass
            
            # Some of this info is redundant as it's in the dict
            ping_info = {
                "source": {
                    "client": client1.dict(),
                    "interface": client1.interface,
                    "ip": src_ip, 
                    "mac": await client1.get_mac(),
                    "ap_mac": await client1.get_ap_mac()
                },
                "destination": {
                    "client": client2.dict(),
                    "interface": client2.interface,
                    "ip": dst_ip, 
                    "mac": await client2.get_mac(),
                    "ap_mac": await client2.get_ap_mac()
                },
                "forward": forward,
                "reverse": reverse,
                "src_ext": src_ext,
                "dst_ext": dst_ext,
                }

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

