# The /etc/netns/<name>/ dir - 

       For applications that are aware of network namespaces, the
       convention is to look for global network configuration files
       first in /etc/netns/NAME/ then in /etc/.  For example, if you
       want a different version of /etc/resolv.conf for a network
       namespace used to isolate your vpn you would name it
       /etc/netns/myvpn/resolv.conf.
