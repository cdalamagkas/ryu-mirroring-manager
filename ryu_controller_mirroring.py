import re
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
import paramiko

# NOTE that in these comments, the OVS terminology is used when referring to port mirroring (Cisco terminology differs)

# TODO: Import this configuration from a yaml or json file
# Those interfaces are excluded for any port mirroring configuration.
exception_list = ["eno1", "eno2", "eno3"]

# TODO: Import this configuration from a yaml or json file
# The output ports of each mirror are pre-determined here. This dictionary CANNOT be empty!
output_ports = {"mgmt-ovs": "tap114i1",
                "han-ovs": "tap113i1",
                "ian-ovs": "tap111i1",
                "mgmt-aruba": "tap114i2",
                "han-aruba": "tap113i2",
                "ian-aruba": "tap111i3"}

# TODO: Import this configuration from a yaml or json file
# The source ports (cisco: ingress source) to initialise for each mirror are determined here.
# You can left this dict entirely emtpy. Example: source_ports = {"mgmt-ovs": ["1", "2"], ...}
source_ports = {}

# TODO: Import this configuration from a yaml or json file
# The destination ports (cisco: egress source) to initialise for each mirror are determined here.
# You can left this dict entirely emtpy.
destination_ports = {}

# TODO: Import this configuration from a yaml or json file
# This dictionary maps each mirror configuration to a bridge. BE SURE that the bridges names are correct
mirrors_bridges = {
    "vmbr0": "mgmt-ovs",
    "vmbr1": "han-ovs",
    "vmbr2": "ian-ovs",
    "vmbr3": "mgmt-aruba",
    "vmbr5": "han-aruba",
    "vmbr6": "ian-aruba"}

# TODO: Import this configuration from a yaml or json file
# SSH initialisation to execute OVS-related commands. CHANGE the client.connect(..) with the credentials used by the
# host that runs the OVSDB server.
key = paramiko.RSAKey.from_private_key_file("proxmox-private.key")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('example.com', username='root', pkey=key)

# This command returns the name of a bridge, given its datapath id.
def find_bridge(dpid):
    cmd = "ovs-vsctl -f table --columns=name find Bridge datapath_id=0000" + hex(dpid)[2:]
    stdin, stdout, stderr = client.exec_command(cmd)
    bridge = stdout.read().decode('ascii').strip("\n").split("\n")[2].replace("\"", '')
    return bridge


# This command returns the name of an interface, given its OpenFlow port number (ofport) and the bridge's name.
def find_interface(br, ofpt):
    cmd = "ovs-ofctl dump-ports-desc " + br
    stdin, stdout, stderr = client.exec_command(cmd)
    port = stdout.read().decode('ascii').strip("\n")
    port = re.findall("\d+\(.*\)", port)
    for item in port:
        checking_port = int(item.split("(")[0])
        if checking_port == ofpt:
            port = item.split("(")[1].split(")")[0]
    return port



def refresh_mirrors(self, current_bridge, default_src_port=True):
    # 1. Send the command to get all interfaces of the specific bridge
    cmd = "ovs-vsctl list-ifaces " + current_bridge
    stdin, stdout, stderr = client.exec_command(cmd)
    ifaces = stdout.read().decode('ascii').strip(" ")
    ifaces_temp = ifaces.split()

    ifaces_src = []
    ifaces_dst = []

    # 2. Iterate through each interface of the list and check if it is in the exception list. If no, then check if the
    # iface has been pre-determined as src or dst port. If its role is not pre-determined, then do what the flag
    # instructs
    for iface in ifaces_temp:
        if iface not in exception_list and iface not in output_ports[mirrors_bridges[current_bridge]]:
            if iface in source_ports:
                ifaces_src.append(iface)
            elif iface in destination_ports:
                ifaces_dst.append(iface)
            else:
                if default_src_port is True:
                    ifaces_src.append(iface)
                else:
                    ifaces_dst.append(iface)

    # 3. Prepare the new port mirroring command
    cmd = "ovs-vsctl -- set Bridge " + current_bridge + " mirrors=@m "

    # 3.1 Insert the src ports
    counter_src = 0
    for iface in ifaces_src:
        cmd = cmd + " -- --id=@src" + str(counter_src) + " get Port " + iface
        counter_src = counter_src + 1
        
    # This is needed if you want also to monitor the host management interface
    if current_bridge == "vmbr0":
        cmd = cmd + " -- --id=@src" + str(counter_src) + " get Port vmbr0"
        counter_src = counter_src + 1
    
    # 3.2 Insert the dst ports
    counter_dst = 0
    for iface in ifaces_dst:
        cmd = cmd + " -- --id=@dst" + str(counter_dst) + " get Port " + iface
        counter_dst = counter_dst + 1

    # 3.3 Each mirror MUST have a single output port
    cmd = cmd + " -- --id=@out get Port " + output_ports[mirrors_bridges[current_bridge]]

    # 3.4 Create the mirror object
    cmd = cmd + " -- --id=@m create Mirror name=" + mirrors_bridges[current_bridge]

    # 3.5 Select the src ports, if any
    if ifaces_src:
        cmd = cmd + " select-src-port="
        for i in range(0, counter_src):
            cmd = cmd + "@src" + str(i) + ","
        cmd = cmd[:-1]  # Delete the last comma

    # 3.6 Select the dst ports, if any
    if ifaces_dst:
        cmd = cmd + " select-dst-port="
        for i in range(0, counter_dst):
            cmd = cmd + "@dst" + str(i) + ","
        cmd = cmd[:-1]  # Delete the last comma

    # 3.7 Last but not least, add the output port
    cmd = cmd + " output-port=@out"

    # 4. Clear the previous mirror
    stdin, stdout, stderr = client.exec_command("ovs-vsctl clear Bridge " + current_bridge + " mirrors")

    # 5. Fire up!
    stdin, stdout, stderr = client.exec_command(cmd)
    self.logger.info("RyuMirroringManager: " + cmd)


class RyuMirrorManager(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RyuMirrorManager, self).__init__(*args, **kwargs)

        # Issue mirror command for each bridge
        for current_bridge, current_mirror in mirrors_bridges.items():
            refresh_mirrors(self, current_bridge)


    @set_ev_cls(ofp_event.EventOFPPortStateChange, MAIN_DISPATCHER)
    def update_mirror(self, ev):
        self.logger.info("RyuMirroringApp: Port change detected!")
        if ev.reason == 0:  # this means that a port has been added, therefore, a mirroring session should be updated.
            # Send the commands to find bridge and interface
            current_bridge = find_bridge(ev.datapath.id)

            iface = find_interface(current_bridge, ev.port_no)

            if iface not in exception_list and current_bridge in mirrors_bridges:
                refresh_mirrors(self, current_bridge)
