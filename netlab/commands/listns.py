import json
from netlab import netutils
from netlab import libwifi
import asyncio
from netlab.commands import test_perms

async def amain():
    test_perms.main()
    nu = netutils.NetworkUtilities()
    netnss = await nu.list_ns()

    ret = {}
    for netns in netnss:
        wifiis = await libwifi.WifiInterface.from_iwp(netns)
        ifaces = []
        ret[netns] = ifaces
        for wifii in wifiis:
            ifaces.append ( await wifii.summary() )

    print( json.dumps(ret) )

if __name__ == "__main__":
    asyncio.run(amain())
