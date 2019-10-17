import re
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
import paramiko

# NOTE that in these comments, the OVS terminology is used when referring to port mirroring (Cisco terminology differs)

# Those interfaces are excluded for any port mirroring configuration.
exception_list = ["eno1", "eno2", "eno3"]

# The output ports of each mirror are pre-determined here. This dictionary cannot be empty!
output_ports = {"mgmt-ovs": "tap111i1",
                "han-ovs": "tap115i1",
                "ian-ovs": "tap116i1",
                "dmz-ovs": "tap117i1",
                "mgmt-aruba": "tap110i1",
                "han-aruba": "tap112i1",
                "ian-aruba": "tap113i1"}

# The destination ports to initialise for each mirror are determined here. You can left this dict entirely emtpy.
destination_ports = {
    "mgmt-ovs": ["tap100i0", "tap102i0", "tap101i0"],
    "han-ovs": ["tap115i1"],
    "ian-ovs": ["tap116i1"],
    "dmz-ovs": ["tap117i1"]
}

# The destination ports to initialise for each mirror are determined here. You can left this dict entirely emtpy.
source_ports = {
    "mgmt-aruba": ["eno4"],
    "han-aruba": ["ens4f1"],
    "ian-aruba": ["ens4f2"]
}

# This dictionary maps each mirror configuration to a bridge. BE SURE that the bridges names are correct
mirrors_bridges = {
    "vmbr0": "mgmt-ovs",
    "vmbr1": "han-ovs",
    "vmbr2": "ian-ovs",
    "vmbr3": "mgmt-aruba",
    "vmbr5": "han-aruba",
    "vmbr6": "ian-aruba",
    "vmbr99": "dmz-ovs"
}

# SSH initialisation to execute OVS-related commands. CHANGE the client.connect(..) with the credentials used by the
# host that runs the OVSDB server.
key = paramiko.RSAKey.from_private_key_file("/root/.ssh/proxmox-private.key")
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
            bridge = find_bridge(ev.datapath.id)
            iface = find_interface(bridge, ev.port_no)

            if iface not in exception_list:

                src_values = source_ports.values()
                dst_values = destination_ports.values()
                out_values = output_ports.values()

                # First check if the new iface is already a pre-determined source, destination or output port.
                if iface in [x for v in src_values for x in v if type(v) == list] or iface in src_values:
                    cmd = "ovs-vsctl -- --id=@src0 get Port " + iface + " -- add Mirror " + mirrors_bridges[
                        bridge] + "select-src-port @src0"
                    stdin, stdout, stderr = client.exec_command(cmd)

                elif iface in [x for v in dst_values for x in v if type(v) == list] or iface in dst_values:
                    cmd = "ovs-vsctl -- --id=@dst0 get Port " + iface + " -- add Mirror " + mirrors_bridges[
                        bridge] + "output-port @dst0"
                    stdin, stdout, stderr = client.exec_command(cmd)

                elif iface in [x for v in out_values for x in v if type(v) == list] or iface in out_values:
                    cmd = "ovs-vsctl -- --id=@out get Port " + iface + " -- add Mirror " + mirrors_bridges[
                        bridge] + "output-port @out"
                    stdin, stdout, stderr = client.exec_command(cmd)

                # If not, add the new port as dst-port (you can change this part at your own preference)
                else:
                    cmd = "ovs-vsctl -- --id=@dst0 get Port " + iface + " -- add Mirror " + mirrors_bridges[
                        bridge] + "select-dst-port @dst0"
                    stdin, stdout, stderr = client.exec_command(cmd)
