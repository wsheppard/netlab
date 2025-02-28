from netlab.libwifi import WifiInterface
import json
import asyncio

async def amain():
    wifiis = await WifiInterface.from_iwp()
    for wifii in wifiis:
        summary = await wifii.summary()
        print(json.dumps(summary))
    return

if __name__ == "__main__":
    asyncio.run(amain())



