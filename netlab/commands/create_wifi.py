from . import libwifi
import sys
import asyncio

async def amain():
    iface = sys.argv[1]
    namespace = sys.argv[2]
    iwp = await libwifi.IWParse().init()

    if iface not in iwp:
        raise RuntimeError("Can't find iface..")

    print(f"Found interface:  {iface}")
    # Grab a class for the interface
    wifii = libwifi.WifiInterface(iface)
    wifii = await wifii.move_to_ns(namespace)

    iwp = await libwifi.IWParse(namespace).init()
    print(iwp)


if __name__ == "__main__":
    asyncio.run(amain())
