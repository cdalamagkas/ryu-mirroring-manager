import re
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
import paramiko

# NOTE that in these comments, the OVS terminology is used when referring to port mirroring (Cisco terminology differs)

# Those interfaces are excluded for any port mirroring configuration.
exception_list = ["eno1", "eno2", "eno3", "eno4", "tap104i0", "tap108i0"]

# The output ports of each mirror are pre-determined here. This dictionary cannot be empty!
output_ports = {"mgmt-ovs": "tap111i1",
                "han-ovs": "tap115i1",
                "ian-ovs": "tap116i1",
                "dmz-ovs": "tap117i1",
                "mgmt-aruba": "tap110i1",
                "han-aruba": "tap112i1",
                "ian-aruba": "tap113i1"}

# The source ports (cisco: ingress source) to initialise for each mirror are determined here.
# You can left this dict entirely emtpy.
source_ports = {
    "mgmt-ovs": ["tap100i0", "tap102i0", "veth103i0"],
    "han-ovs": ["tap100i1", "tap101i0"],
    "ian-ovs": ["tap100i2", "tap107i0"],
    "mgmt-aruba": ["eno4"],
    "han-aruba": ["ens4f1"],
    "ian-aruba": ["ens4f2"]
}

# The destination ports (cisco: egress source) to initialise for each mirror are determined here.
# You can left this dict entirely emtpy.
destination_ports = {}

# This dictionary maps each mirror configuration to a bridge. BE SURE that the bridges names are correct
mirrors_bridges = {
    "vmbr0": "mgmt-ovs",
    "vmbr1": "han-ovs",
    "vmbr2": "ian-ovs",
    "vmbr3": "mgmt-aruba",
    "vmbr5": "han-aruba",
    "vmbr6": "ian-aruba"}

# SSH initialisation to execute OVS-related commands. CHANGE the client.connect(..) with the credentials used by the
# host that runs the OVSDB server.
key = paramiko.RSAKey.from_private_key_file("/root/.ssh/proxmox-private.key")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('ryu.trsc.net', username='root', pkey=key)


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


class RyuMirrorManager(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RyuMirrorManager, self).__init__(*args, **kwargs)

        bridges = list(mirrors_bridges)

        # Clear all mirrors before issuing new ones
        for current_bridge in bridges:
            stdin, stdout, stderr = client.exec_command("ovs-vsctl clear Bridge " + current_bridge + " mirrors")

        # Issue mirror command for each bridge
        for current_bridge, current_mirror in mirrors_bridges.items():
            cmd = "ovs-vsctl -- set Bridge " + current_bridge + " mirrors=@m"

            # if the current mirror has source ports, then add them
            temp_notations_src = temp_notations_dst = []

            if current_mirror in source_ports:
                i = 0
                for current_port in source_ports[current_mirror]:
                    notation = "@src" + str(i)
                    cmd = cmd + " -- --id=" + notation + " get Port " + str(current_port)
                    i += 1
                    temp_notations_src.append(notation)

            # if the current mirror has destination ports, then add them
            if current_mirror in destination_ports:
                i = 0
                for current_port in destination_ports[current_mirror]:
                    notation = "@dst" + str(i)
                    cmd = cmd + " -- --id=" + notation + " get Port " + str(current_port)
                    i += 1
                    temp_notations_dst.append(notation)

            # Each mirror MUST have a single output port
            cmd = cmd + " -- --id=@out get Port " + output_ports[current_mirror]

            # Now start creating the mirror
            cmd = cmd + " -- --id=@m create Mirror name=" + current_mirror

            # Check again if the mirror has source and destination ports. If yes, add them by their notation
            if current_mirror in source_ports:
                cmd = cmd + " select-src-port="
                for item in temp_notations_src:
                    cmd = cmd + item + ","
                cmd = cmd[:-1]  # Delete the last comma

            if current_mirror in destination_ports:
                cmd = cmd + " select-dst-port="
                for item in temp_notations_dst:
                    cmd = cmd + item + ","
                cmd = cmd[:-1]  # Delete the last comma

            # Last but not least, add the output port
            cmd = cmd + " output-port=@out"

            stdin, stdout, stderr = client.exec_command(cmd)

    @set_ev_cls(ofp_event.EventOFPPortStateChange, MAIN_DISPATCHER)
    def update_mirror(self, ev):

        if ev.reason == 0:  # this means that a port has been added, therefore, a mirroring session should be updated.

            # Send the commands to find bridge and interface
            current_bridge = find_bridge(ev.datapath.id)
            iface = find_interface(current_bridge, ev.port_no)

            if iface not in exception_list:

                default_src_port = True

                # 1. Send the command to get all interfaces of the specific bridge
                cmd = "ovs-vsctl list-ifaces " + current_bridge
                stdin, stdout, stderr = client.exec_command(cmd)
                ifaces = stdout.read().decode('ascii').strip(" ")
                ifaces_temp = ifaces.split()

                ifaces_src = []
                ifaces_dst = []

                # 2. Iterate through each interface of the list and check if it is in the exception list. If no, then
                # check if the iface has been pre-determined as src or dst port. If its role is not pre-determined, then
                # do what the flag instructs
                for iface in ifaces_temp:
                    if iface not in exception_list:
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
                    cmd = cmd + " select-src-ports="
                for i in range(0, counter_src-1):
                    cmd = cmd + "@src" + str(i) + ","
                cmd = cmd[:-1]  # Delete the last comma

                # 3.6 Select the dst ports, if any
                cmd = cmd + "select-dst-ports="
                for i in range(0, counter_dst-1):
                    cmd = cmd + "@dst" + str(i) + ","
                cmd = cmd[:-1]  # Delete the last comma

                # 3.7 Last but not least, add the output port
                cmd = cmd + " output-port=@out"

                # 4. Clear the previous mirror
                stdin, stdout, stderr = client.exec_command("ovs-vsctl clear Bridge " + current_bridge + " mirrors")

                # 5. Fire up!
                stdin, stdout, stderr = client.exec_command(cmd)
