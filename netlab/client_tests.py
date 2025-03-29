import asyncio
import logging
from netlab.eventbus import EventBus, SimpleMessage
from netlab.utils import BGTasksMixin
from netlab.wpa_operations import WifiClient
from netlab import netutils

class WifiPingTester(BGTasksMixin):

    log = logging.getLogger("wifi-ping-tester")

    def __init__(self, clients, concurrency=10, eventbus:EventBus|None=None, external:str|None=None):
        self.clients = clients
        self.running = False
        self.bgtasks = set()
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)
        self.external = external or "1.1.1.1"
        if eventbus:
            self.eventbus = eventbus.child("WifiPingTest")
        else:
            self.eventbus = None


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
                src_ext = await client1.ping(self.external)

            if dst_ip:
                dst_ext = await client2.ping(self.external)

            if src_ip and dst_ip:
                forward = await client1.ping(client2)

                if do_reverse:
                    reverse = await client1.ping(client2)

            ping_info = {
                         "source": client1.wcstate(),
                         "destination": client2.wcstate(),
                         "forward": forward,
                         "reverse": reverse,
                         "src_ext": src_ext,
                         "dst_ext": dst_ext,
                         }

            return ping_info

    async def send_bus(self,msg):
        if self.eventbus:
            await self.eventbus.emit(SimpleMessage(message=msg))

    async def uni_ping_tests(self, source:WifiClient):
        """
        Uni-directional test from source to all configured clients.
        """
        clients = len(self.clients)
        await self.send_bus(f"Uni Ping test to {clients} clients")
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

