from itertools import count

from netlab.netutils import NetworkUtilities
from . import libwifi
import sys
import asyncio

def wifi_counter(prefix='wifi'):
    return (f"{prefix}{i}" for i in count(0))

async def amain():
    wifiis = await libwifi.WifiInterface.from_iwp()
    nu = NetworkUtilities()
    counter = wifi_counter()

    for wifii in wifiis:
        iface = wifii.interface
        mode = wifii['iftype']
        state = await wifii.state()

        if mode=="managed" and state=="DOWN":
            namespace = next(counter)
            while await nu.namespace_exists(namespace):
                namespace = next(counter)
            print(f"Moving {iface}  {mode} {state} to ->>> {namespace}")
            wifii = libwifi.WifiInterface(iface)
            wifii = await wifii.move_to_ns(namespace)


if __name__ == "__main__":
    asyncio.run(amain())
