You need to add things like

* setuid
* sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/dumpcap
* add wireshark group
* add to netdev group
