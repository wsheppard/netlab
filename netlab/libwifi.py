import asyncio
from itertools import count
import json
import os
from pathlib import Path
import pickle
from re import I
import subprocess
import time
import time
import pyudev

from . import netutils
from .iwlist import IWList



class IWParse(dict):
    def __init__(self, namespace=None):
        self.namespace = namespace
        self.iwl = IWList()

    @property
    def ifaces(self):
        return list(self.keys())

    async def init(self):
        # These guys can take a bit of time.
        if self.namespace:
            iwdata = await asyncio.to_thread( self.iwl.enumerate_in_namespace, self.namespace )
        else:
            iwdata = await asyncio.to_thread( self.iwl.parse_all_wiphys )

        if iwdata:
            self.update(iwdata)
        return self


# def wifi_counter(prefix='wifi'):
#     return (f"{prefix}{i}" for i in count(0))

# def create_all():
#     nsgen = wifi_counter()
#     for iface in list_wifi_interfaces():
#         ns = next(nsgen)
#         while check_ns_exists(ns).returncode == 0:
#             ns = next(nsgen)
#         create_wifi(ns,iface)

class WifiInterface(dict):

    def __init__(self, interface: str, namespace: str|None = None, data: dict|None = None):
        self.interface = interface
        self.namespace = namespace
        self.net_utils = netutils.NetworkUtilities(netns=namespace)
        try:
            if data:
                self.update(data)
        except Exception:
            print("Couldnt update data with provided data - ")
            print(data)


    def __repr__(self):
        nsfield = f", ns={self.namespace}" if self.namespace else ""
        return f'Wifii({self.interface}{nsfield})'

    @classmethod
    async def from_iwp(cls, namespace=None) -> list["WifiInterface"]:
        """
        Return all interfaces using the nl80211 calls, specifing namespace
        """
        iwp = await IWParse(namespace).init()
        ret = []
        for iface,data in iwp.items():
            obj = cls( iface, namespace, data )
            ret.append(obj)
        return ret 

    async def move_to_ns(self, namespace):
        """
        We just use the interface name as the namespace name
        """
        await self.net_utils.move_wifi_ns(self.interface, namespace)

    async def get_interface_state(self) -> str|None:
        interface = self.interface
        result = await self.net_utils.ipl('show', 'dev', interface)
        if not result:
            return "UNKNOWN"
        if "UP" in result[0]['flags']:
            return "UP"
        else:
            return "DOWN"

    async def state(self):
        return await self.get_interface_state()

    @property
    def has5g(self):
        return 5180 in self.get_freqs()

    @property
    def has2g(self):
        return 2412 in self.get_freqs()

    async def summary(self):
        return { 
               'iface': self.interface, 
               'driver': self.get('network_driver'),
               'desc': self.get('desc'),
               'state': await self.state(),
               'iftype': self['iftype'],
               '5g': 5180 in self.get_freqs(),
               '2g': 2412 in self.get_freqs(),
               #'freqs': ",".join( map(str,wifii['wiphy']['freqs']) )
               }

    def disabled_freqs(self):
        wiphy = self['wiphy']
        freqs = wiphy['freqs']
        freqs = ( freq for freq,fmeta in freqs.items() if fmeta.get("disabled") )
        ret = list(map(int,freqs))
        return ret

    def get_freqs(self):
        wiphy = self['wiphy']
        freqs = wiphy['freqs']
        freqs = ( freq for freq,fmeta in freqs.items() if not fmeta.get("disabled") )
        ret = list(map(int,freqs))
        return ret

    def get_freq(self,freq):
        freqs = self['wiphy']['freqs']
        return freqs[freq]


