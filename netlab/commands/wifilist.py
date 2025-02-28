#!/usr/bin/env python3
from netlab import libwifi

ifs = libwifi.list_wifi_interfaces()
for iface in ifs:
    print(iface)
