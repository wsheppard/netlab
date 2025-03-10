import json
from netlab import netutils
from netlab import libwifi
import asyncio
from netlab.commands import test_perms

import sys

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

async def amain():
    test_perms.main()
    nu = netutils.NetworkUtilities()
    netnss = await nu.list_ns()

    ret = {}

    eprint("Start...")
    iwp = await asyncio.gather( *[libwifi.WifiInterface.from_iwp(netns) for netns in netnss ] )
    eprint("Stop..")

    tasks = []

    for netns,res in zip(netnss,iwp):
        async def doit(netns=netns, res=res):
            eprint(f"Doing, {netns}")
            wifiis = res
            ifaces = []
            ret[netns] = ifaces
            for wifii in wifiis:
                ifaces.append ( await wifii.summary() )
            eprint(f"Done, {netns}")
        tasks.append(doit())

    await asyncio.gather(*tasks)
    print( json.dumps(ret) )

if __name__ == "__main__":
    asyncio.run(amain())
