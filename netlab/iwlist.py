import json
import multiprocessing
import os
import os
import os
from pathlib import Path
import pickle
import pickle
from struct import pack_into
import sys

from pyroute2 import netlink
from pyroute2.iwutil import IW
from pyroute2.netlink import NLM_F_DUMP, NLM_F_REQUEST
from pyroute2.netlink.nl80211 import NL80211_NAMES, nl80211cmd
import pyudev
import requests

import logging

from .utils import async_cache_result, sync_cache_result, PythonBinaryChecker

log = logging.getLogger("iwlist")

class USBResolver:
    """
    Allows looking up USB devices synchronously.
    """
    USB_IDS_URL = 'http://www.linux-usb.org/usb.ids'
    _usb_ids = None  # Class attribute to store the USB IDs data

    @classmethod
    @sync_cache_result
    def _download_usb_ids(cls):
        print("Loading usbids synchronously...")
        response = requests.get(cls.USB_IDS_URL)
        response.raise_for_status()
        return response.text

    @property
    def usb_ids(cls):
        if not cls._usb_ids:
            cls.load_usb_ids()
        return cls._usb_ids 

    @classmethod
    def load_usb_ids(cls):
        usb_ids = {}
        usb_ids_content = cls._download_usb_ids()
        current_vendor = None

        for line in usb_ids_content.splitlines():
            if line.startswith('#') or not line.strip():
                continue
            if not line.startswith('\t'):
                parts = line.split()
                vendor_id = parts[0]
                vendor_name = ' '.join(parts[1:])
                usb_ids[vendor_id] = {'name': vendor_name, 'products': {}}
                current_vendor = vendor_id
            else:
                parts = line.split()
                product_id = parts[0]
                product_name = ' '.join(parts[1:])
                if current_vendor:
                    usb_ids[current_vendor]['products'][product_id] = product_name

        cls._usb_ids = usb_ids

    def resolve(self, vid, pid):
        if self.usb_ids is None:
            raise RuntimeError("USB IDs have not been loaded. Call USBResolver.load_usb_ids() first.")
        vendor_info = self.usb_ids.get(vid, {})
        vendor_name = vendor_info.get('name', 'Unknown Vendor')
        product_name = vendor_info.get('products', {}).get(pid, 'Unknown Product')
        return f'{vendor_name} - {product_name}'

_usb = USBResolver()

class IWList(IW):
    """
    Utility class to interact with nl80211 via pyroute2.
    Provides methods to fetch WiFi interfaces and wiphy details.

    NOTE: Network Namespaces need to be handled differently! 
    """
    
    IFMODES = [
        "unspecified", "IBSS", "managed", "AP", "AP/VLAN", "WDS",
        "monitor", "mesh point", "P2P-client", "P2P-GO", "P2P-device",
        "outside context of a BSS", "NAN"
    ]
    
    def _get_interfaces(self, ifname=None):
        '''
        Get interfaces dump
        '''
        msg = nl80211cmd()
        msg['cmd'] = NL80211_NAMES['NL80211_CMD_GET_INTERFACE']

        # This doesn't work, because it requires the INDEX not the NAME
        if ifname is not None:
            msg['attrs'].append(('NL80211_ATTR_IFNAME', ifname))

        ret = self.nlm_request(
            msg, msg_type=self.prid, msg_flags=NLM_F_REQUEST | NLM_F_DUMP
        )

        if ifname:
            for iface in ret:
                if iface.get_attr("NL80211_ATTR_IFNAME") == ifname:
                    return (iface,)
        else:
            return ret

    def _list_wiphy(self, wiphy_index=None):
        msg = nl80211cmd()
        msg['cmd'] = NL80211_NAMES['NL80211_CMD_GET_WIPHY']
        msg['attrs'].append(('NL80211_ATTR_SPLIT_WIPHY_DUMP', None))
        
        if wiphy_index is not None:
            msg['attrs'].append(('NL80211_ATTR_WIPHY', wiphy_index))  # Specify the WIPHY index
        
        return self.nlm_request(msg, msg_type=self.prid, msg_flags=(NLM_F_REQUEST | NLM_F_DUMP))

    def parse_wiphy_info(self, wiphy_index=None):
        """Retrieve and parse wiphy data into structured format."""
        wiphy_data = self._list_wiphy(wiphy_index)
        phys = {}
        for entry in wiphy_data:
            wiphy = entry.get_attr("NL80211_ATTR_WIPHY")
            if wiphy not in phys:
                phys[wiphy] = {'freqs': {}}
            bands = entry.get_attrs("NL80211_ATTR_WIPHY_BANDS")
            
            for band in bands:
                for attrs in band:
                    freqs = attrs.get_attrs("NL80211_BAND_ATTR_FREQS")
                    for freq in freqs:
                        for fr in freq:
                            val = fr.get_attr("NL80211_FREQUENCY_ATTR_FREQ")
                            if val not in phys[wiphy]['freqs']:
                                phys[wiphy]['freqs'][val] = {}
                            
                            for attr_key, attr_value in fr['attrs']:
                                if isinstance(attr_value, (netlink.nla_base, netlink.nla_slot, list)):
                                    continue
                                name = fr.nla2name(attr_key) or attr_key
                                phys[wiphy]['freqs'][val][name] = attr_value
        return phys
    
    def parse_interfaces(self, ifname=None):
        """Retrieve and parse interface data into structured format."""
        interface_data = self._get_interfaces(ifname)

        if not interface_data:
            return None

        # phyindex = interface_data.get_attr("NL80211_ATTR_WIPHY")
        # print(phyindex)

        # ... we might as well just pull them all in here why not.
        phys = self.parse_wiphy_info()
        ifaces = {}
        for entry in interface_data:
            ifname = entry.get_attr("NL80211_ATTR_IFNAME")
            ifname_data = ifaces.setdefault(ifname, {})
            
            for attr_key, attr_value in entry['attrs']:
                if isinstance(attr_value, (netlink.nla_base, netlink.nla_slot, list)):
                    continue
                name = entry.nla2name(attr_key) or attr_key
                
                if name == "iftype":
                    attr_value = self.IFMODES[attr_value]
                elif name == "wiphy":
                    attr_value = phys.get(attr_value, {})
                
                ifname_data[name] = attr_value

            # The pyudev stuff can fail because the namespaces that pyudev uses are Mount
            # not Network so the setns() call has no effect. Trying to do it with mounts
            # on sysfs and other such things are almost impossible. 
            try:
                sysfsinfo = self.get_info(ifname)
            except Exception:
                pass
            else:
                ifname_data.update(sysfsinfo)

        return ifaces

    def parse_all_wiphys(self):
        """Retrieve and parse all wiphy and interface data."""
        return self.parse_interfaces()

    @staticmethod
    def get_info(interface):
        context = pyudev.Context()
        # net_devices = context.list_devices(subsystem='net')
        net_device = pyudev.Devices.from_name(context, 'net', interface)
        parent = net_device.find_parent(subsystem='usb', device_type='usb_device')

        if parent is None:
            return

        vid = parent.properties.get('ID_VENDOR_ID')
        pid = parent.properties.get('ID_MODEL_ID')
        network_driver = net_device.properties.get('ID_NET_DRIVER')
        usbr = _usb.resolve(vid, pid)

        device_info = {
            'path': str(Path(f'/sys/class/net/{interface}/device').resolve()),
            'network_driver': network_driver,
            'vid_pid': f"{vid}:{pid}",
            'desc': usbr,
        }
        return device_info

    def enumerate_in_namespace(self, namespace):
        """
        Runs the Netlink enumeration in a separate process inside the specified namespace.
        - Spawns a new process.
        - Enters the namespace.
        - Runs `enumerate_all()`.
        - Returns the result as Pickle.
        """

        if not namespace:
            raise ValueError("Namespace is required.")

        parent_conn, child_conn = multiprocessing.Pipe()

        def worker(ns, conn, cls):
            """Child process that enters the namespace and runs Netlink enumeration."""
            try:
                # Enter the namespace
                ns_path = f"/var/run/netns/{ns}"
                if not os.path.exists(ns_path):
                    conn.send(pickle.dumps({"error": f"Namespace {ns} not found."}))
                    return

                ns_fd = os.open(ns_path, os.O_RDONLY)
                os.setns(ns_fd, os.CLONE_NEWNET)
                os.close(ns_fd)

                # Run Netlink queries inside a new instance of IWList (or derived class)
                iw = cls()
                result = iw.parse_all_wiphys()

                #import subprocess
                #from .utils import print_executable_and_caps
                #print_executable_and_caps()
                #try:
                #    # Unshare the mount namespace so our mount changes are private
                #    os.unshare(os.CLONE_NEWNS)
                #    # Make mounts private (optional but recommended)
                #    #subprocess.run(["mount", "--make-rprivate", "/"], check=True)
                #    # Unmount the inherited sysfs
                #    #subprocess.run(["umount", "/sys"], check=True)
                #    # Remount sysfs so that it reflects only this namespace's view
                #    ret = subprocess.run(["cat", "/proc/self/status"],
                #                   capture_output=True)
                #    print(ret)
                #    ret = subprocess.run(["mount", "-t", "sysfs", "sysfs", "/sys"],
                #                   capture_output=True)

                #    print(ret)

                #    if result:
                #        for iface in result:
                #            print("Hi there")
                #            print(iface)
                #            print("Calling it now...")
                #            print(iw.get_info(iface))
                # except Exception as e:
                #     log.exception("Problems")

                # Send pickled result
                conn.send(pickle.dumps(result))
            except Exception as e:
                log.exception(f"Issue in namespace: {ns}")
            finally:
                conn.close()

        # Spawn a separate process
        process = multiprocessing.Process(target=worker, args=(namespace, child_conn, self.__class__))
        process.start()
        process.join()

        # Receive result
        if parent_conn.poll():
            return pickle.loads(parent_conn.recv())
        return None  # No response

   

