from collections import defaultdict
import json
from netlab import netutils
from netlab import libwifi
import asyncio
from netlab.commands import test_perms

import sys

async def get_ns_info():
    test_perms.main()
    nu = netutils.NetworkUtilities()
    netnss = await nu.list_ns()

    iwp = await asyncio.gather( *(libwifi.WifiInterface.from_iwp(netns) for netns in netnss ) )
    ret = defaultdict()
    for ns in iwp:
        for wifii in ns:
            ret[wifii.interface] = await wifii.summary()
    return ret

async def amain():
    ret = await get_ns_info()
    print( json.dumps(ret) )

if __name__ == "__main__":
    asyncio.run(amain())
