#! /usr/bin/python

# @author: Kyle Benson
# (c) Kyle Benson 2017
import os

import errno
import re

from subprocess import Popen

from smart_campus_experiment import SmartCampusExperiment, DISTANCE_METRIC

import logging as log
import json
import argparse
import time
import signal
import ipaddress

from mininet.net import Mininet
from mininet.node import RemoteController, Host, OVSKernelSwitch
from mininet.node import Switch, Link, Node  # these just used for types in docstrings
from mininet.cli import CLI
from mininet.link import TCLink, Intf

from topology_manager.networkx_sdn_topology import NetworkxSdnTopology
from topology_manager.test_sdn_topology import mac_for_host  # used for manual MAC assignment

EXPERIMENT_DURATION = 35  # in seconds
# EXPERIMENT_DURATION = 10  # for testing
SEISMIC_EVENT_DELAY = 25  # seconds before the 'earthquake happens', i.e. sensors start sending data
# SEISMIC_EVENT_DELAY = 5  # for testing
IPERF_BASE_PORT = 5000  # background traffic generators open several iperf connections starting at this port number
OPENFLOW_CONTROLLER_PORT = 6653  # we assume the controller will always be at the default port
# subnet for all hosts (if you change this, update the __get_ip_for_host() function!)
# NOTE: we do /9 so as to avoid problems with addressing e.g. the controller on the local machine
# (vagrant uses 10.0.2.* for VM's IP address).
IP_SUBNET = '10.128.0.0/9'
WITH_LOGS = True  # output seismic client/server stdout to a log file
# HACK: rather than some ugly hacking at Mininet's lack of API for allocating the next IP address,
# we just put the NAT/server interfaces in a hard-coded subnet.
NAT_SERVER_IP_ADDRESS = '11.0.0.%d/24'
# TODO: use a different address base...
MULTICAST_ADDRESS_BASE = u'224.0.0.1'  # must be unicode!
# When True, runs host processes with -00 command for optimized python code
OPTIMISED_PYTHON = False
SLEEP_TIME_BETWEEN_RUNS = 5  # give Mininet/OVS/ONOS a chance to reconverge after cleanup

# Default values
DEFAULT_TREE_CHOOSING_HEURISTIC = 'importance'
DEFAULT_TOPOLOGY_ADAPTER = 'onos'

class MininetSmartCampusExperiment(SmartCampusExperiment):
    """
    Version of SmartCampusExperiment that runs the experiment in Mininet emulation.
    This includes some background traffic and....

    It outputs the following files (where * is a string representing a summary of experiment parameters):
      - results_*.json : the results file output by this experiment that contains all of the parameters
          and information about publishers/subscribers/the following output locations for each experimental run
      - outputs_*/client_events_{$HOST_ID}.json : contains events sent/recvd by seismic client
      - logs_*/{$HOST_ID} : log files storing seismic client/server's stdout/stderr
      NOTE: the folder hierarchy is important as the results_*.json file contains relative paths pointing
          to the other files from its containing directory.
    """

    def __init__(self, controller_ip='127.0.0.1', controller_port=8181,
                 # need to save these two params to pass to RideD
                 tree_choosing_heuristic=DEFAULT_TREE_CHOOSING_HEURISTIC,
                 topology_adapter=DEFAULT_TOPOLOGY_ADAPTER,
                 n_traffic_generators=0, traffic_generator_bandwidth=10,
                 show_cli=False, comparison=None,
                 *args, **kwargs):
        """
        Mininet and the SdnTopology adapter will be started by this constructor.
        NOTE: you must start the remote SDN controller before constructing/running the experiment!
        :param controller_ip: IP address of SDN controller that we point RideD towards: it must be accessible by the server Mininet host!
        :param controller_port: REST API port of SDN controller
        :param tree_choosing_heuristic: explicit in this version since we are running an
         actual emulation and so cannot check all the heuristics at once
        :param topology_adapter: type of REST API topology adapter we use: one of 'onos', 'floodlight'
        :param n_traffic_generators: number of background traffic generators to run iperf on
        :param traffic_generator_bandwidth: bandwidth (in Mbps; using UDP) to set the iperf traffic generators to
        :param show_cli: display the Mininet CLI in between each run (useful for debugging)
        :param comparison: disable RIDE-D and use specified comparison strategy (unicast or oracle)
        :param args: see args of superclass
        :param kwargs: see kwargs of superclass
        """

        # We want this parameter overwritten in results file for the proper configuration.
        self.comparison = comparison
        if comparison is not None:
            assert comparison in ('oracle', 'unicast'), "Uncrecognized comparison method: %s" % comparison
            kwargs['tree_construction_algorithm'] = (comparison,)

        super(MininetSmartCampusExperiment, self).__init__(*args, **kwargs)
        # save any additional parameters the Mininet version adds
        self.results['params']['experiment_type'] = 'mininet'
        self.results['params']['tree_choosing_heuristic'] = self.tree_choosing_heuristic = tree_choosing_heuristic
        self.results['params']['n_traffic_generators'] = self.n_traffic_generators = n_traffic_generators
        self.results['params']['traffic_generator_bandwidth'] = self.traffic_generator_bandwidth = traffic_generator_bandwidth

        self.controller_ip = controller_ip
        self.controller_port = controller_port
        self.topology_adapter_type = topology_adapter
        # set later as it needs resetting between runs and must be created after the network starts up
        self.topology_adapter = None
        # This gets passed to seismic hosts
        self.debug_level = kwargs.get('debug', 'error')

        # These will all be filled in by calling setup_mininet()
        #TODO: do we actually need all these???
        self.hosts = []
        self.switches = []
        self.links = []
        self.net = None
        self.controller = None
        self.nat = None

        self.server = None
        self.server_switch = None
        # Save Popen objects to later ensure procs terminate before exiting Mininet
        # or we'll end up with hanging procs.
        self.popens = []
        # Need to save client/server iperf procs separately as we need to terminate the server ones directly.
        self.client_iperfs = []
        self.server_iperfs = []

        # We'll drop to a CLI after the experiment completes for
        # further poking around if we're only doing a single run.
        self.show_cli = self.nruns == 1 or show_cli

        # HACK: We just manually allocate IP addresses rather than adding a controller API to request them.
        base_addr = ipaddress.IPv4Address(MULTICAST_ADDRESS_BASE)
        self.mcast_address_pool = [str(base_addr + i) for i in range(kwargs['ntrees'])]

    @classmethod
    def get_arg_parser(cls, parents=(SmartCampusExperiment.get_arg_parser(),), add_help=True):
        """
        Argument parser that can be combined with others when this class is used in a script.
        Need to not add help options to use that feature, though.
        :param tuple[argparse.ArgumentParser] parents:
        :param add_help: if True, adds help command (set to False if using this arg_parser as a parent)
        :return argparse.ArgumentParser arg_parser:
        """

        # argument parser that can be combined with others when this class is used in a script
        # need to not add help options to use that feature, though
        # TODO: document some behavior that changes with the Mininet version:
        # -- pubs/subs are actual client processes
        arg_parser = argparse.ArgumentParser(parents=parents, add_help=add_help)
        # experimental treatment parameters: all taken from parents
        # background traffic generation
        arg_parser.add_argument('--ngenerators', '-g', default=0, dest='n_traffic_generators', type=int,
                                help='''number of hosts that generate random traffic to cause congestion (default=%(default)s)''')
        arg_parser.add_argument('--generator-bandwidth', '-bw', default=10, dest='traffic_generator_bandwidth', type=float,
                                help='''bandwidth (in Mbps) of iperf for congestion traffic generating hosts (default=%(default)s)''')
        arg_parser.add_argument('--cli', '-cli', dest='show_cli', action='store_true',
                                help='''force displaying the Mininet CLI after running the experiment. Normally it is
                                 only displayed iff nruns==1. This is useful for debugging problems as it prevents
                                the OVS/controller state from being wiped after the experiment.''')
        arg_parser.add_argument('--comparison', default=None,
                                help='''use the specified comparison strategy rather than RIDE-D.  Can be one of:
                                 unicast (send individual unicast packets to each subscriber),
                                 oracle (modifies experiment duration to allow server to retransmit aggregated
                                 packets enough times that the SDN controller should detect failures and recover paths).''')

        return arg_parser

    @classmethod
    def build_default_results_file_name(cls, args, dirname='results'):
        """
        :param args: argparse object (or plain dict) with all args info (not specifying ALL args is okay)
        :param dirname: directory name to place the results files in
        :return: string representing the output_filename containing a parameter summary for easy identification
        """
        # HACK: we need to add the additional parameters this experiment version bring in
        output_filename = super(MininetSmartCampusExperiment, cls).build_default_results_file_name(args, dirname)
        if isinstance(args, argparse.Namespace):
            choosing_heuristic = args.tree_choosing_heuristic
        else:
            choosing_heuristic = args.get('tree_choosing_heuristic', DEFAULT_TREE_CHOOSING_HEURISTIC)
        replacement = '_%s.json' % choosing_heuristic
        output_filename = output_filename.replace('.json', replacement)
        return output_filename

    def set_interrupt_signal(self):
        # ignore it so we can terminate Mininet commands without killing Mininet
        # TODO: something else?
        return

    def setup_topology(self):
        """
        Builds the Mininet network, including all hosts, servers, switches, links, and NATs.
        This relies on reading the topology file using a NetworkxSdnTopology helper.

        NOTE: we assume that the topology file was generated by (or follows the same naming conventions as)
        the campus_topo_gen.py module.  In particular, the naming conventions is used to identify different
        types of hosts/switches as well as to assign MAC/IP addresses in a more legible manner.  i.e.
        Hosts are assigned IP addresses with the format "10.[131/200 for major/minor buildings respectively].building#.host#".
        Switch DPIDs (MAC addresses) are assigned with first letter being type (minor buildings are 'a' and
         the server switch is 'e') and the last digits being its #.
        :param str topology_file: file name of topology to import
        """
        self.net = Mininet(topo=None,
                           build=False,
                           ipBase=IP_SUBNET,
                           autoSetMacs=True,
                           # autoStaticArp=True
                           )

        log.info('*** Adding controller')
        self.controller = self.net.addController(name='c0',
                                         controller=RemoteController,
                                         ip=self.controller_ip,
                                         port=OPENFLOW_CONTROLLER_PORT,
                                         )

        # import the switches, hosts, and server(s) from our specified file
        self.topo = NetworkxSdnTopology(self.topology_filename)

        def __get_mac_for_switch(switch):
            # BUGFIX: need to manually specify the mac to set DPID properly or Mininet
            # will just use the number at the end of the name, causing overlaps.
            # HACK: slice off the single letter at start of name, which we assume it has;
            # then convert the number to a MAC.
            mac = mac_for_host(int(switch[1:]))
            # Disambiguate one switch type from another by setting the first letter
            # to be a unique one corresponding to switch type and add in the other 0's.
            first_letter = switch[0]
            if first_letter == 'm':
                first_letter = 'a'
            # rest fit in hex except for rack switches
            mac = first_letter + '0:00:00' + mac[3:]
            return str(mac)

        for switch in self.topo.get_switches():
            mac = __get_mac_for_switch(switch)
            s = self.net.addSwitch(switch, dpid=mac, cls=OVSKernelSwitch)
            log.debug("adding switch %s at DPID %s" % (switch, s.dpid))
            self.switches.append(s)

        def __get_ip_for_host(host):
            # See note in docstring about host format
            host_num, building_type, building_num = re.match('h(\d+)-([mb])(\d+)', host).groups()
            return "10.%d.%s.%s" % (131 if building_type == 'b' else 200, building_num, host_num)

        for host in self.topo.get_hosts():
            h = self.net.addHost(host, ip=__get_ip_for_host(host))
            self.hosts.append(h)

        for server in self.topo.get_servers():
            # HACK: we actually add a switch in case the server is multi-homed since it's very
            # difficult to work with multiple interfaces on a host (e.g. ONOS can only handle
            # a single MAC address per host).
            server_switch_name = server.replace('s', 'e')
            server_switch_dpid = __get_mac_for_switch(server_switch_name)
            # Keep server name for switch so that the proper links will be added later.
            self.server_switch = self.net.addSwitch(server, dpid=server_switch_dpid, cls=OVSKernelSwitch)
            s = self.net.addHost('h' + server)
            self.server = s
            self.net.addLink(self.server_switch, self.server)

        for link in self.topo.get_links():
            from_link = link[0]
            to_link = link[1]
            log.debug("adding link from %s to %s" % (from_link, to_link))

            # Get link attributes for configuring realistic traffic control settings
            # For configuration options, see mininet.link.TCIntf.config()
            attributes = link[2]
            _bw = attributes.get('bw', 10)  # in Mbps
            _delay = '%fms' % attributes.get('latency', 10)
            _jitter = '1ms'
            _loss = self.error_rate

            l = self.net.addLink(self.net.get(from_link), self.net.get(to_link),
                                 cls=TCLink, bw=_bw, delay=_delay, jitter=_jitter, loss=_loss
                                 )
            self.links.append(l)

        # add NAT so the server can communicate with SDN controller's REST API
        # NOTE: because we didn't add it to the actual SdnTopology, we don't need
        # to worry about it getting failed.  However, we do need to ensure it
        # connects directly to the server to avoid failures disconnecting it.
        # HACK: directly connect NAT to the server, set a route for it, and
        # handle this hacky IP address configuration
        nat_ip = NAT_SERVER_IP_ADDRESS % 2
        srv_ip = NAT_SERVER_IP_ADDRESS % 3
        self.nat = self.net.addNAT(connect=self.server)
        self.nat.configDefault(ip=nat_ip)

        # Now we set the IP address for the server's new interface.
        # NOTE: we have to set the default route after starting Mininet it seems...
        srv_iface = sorted(self.server.intfNames())[-1]
        self.server.intf(srv_iface).setIP(srv_ip)

    # HACK: because self.topo stores nodes by just their string name, we need to
    # convert them into actual Mininet hosts for use by this experiment.

    def _get_mininet_nodes(self, nodes):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be subscribers.
        :param List[str] nodes:
        :return List[Node] mininet_nodes:
        """
        return [self.net.get(n) for n in nodes]

    def choose_publishers(self):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be publishers.
        :return List[Host] publishers:
        """
        return self._get_mininet_nodes(super(MininetSmartCampusExperiment, self).choose_publishers())

    def choose_subscribers(self):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be subscribers.
        :return List[Host] subscribers:
        """
        return self._get_mininet_nodes(super(MininetSmartCampusExperiment, self).choose_subscribers())

    def choose_server(self):
        """
        Choose the actual Mininet Host (rather than just strings) that will
        be the server.
        :return Host server:
        """
        # HACK: call the super version of this so that we increment the random number generator correctly
        super(MininetSmartCampusExperiment, self).choose_server()
        return self.server

    def get_failed_nodes_links(self):
        fnodes, flinks = super(MininetSmartCampusExperiment, self).get_failed_nodes_links()
        # NOTE: we can just pass the links as strings
        return self._get_mininet_nodes(fnodes), flinks

    def run_experiment(self, failed_nodes, failed_links, server, publishers, subscribers):
        """
        Configures all appropriate settings, runs the experiment, and
        finally tears it down before returning the results.
        (Assumes Mininet has already been started).

        Returned results is a dict containing the 'logs_dir' and 'outputs_dir' for
        this run as well as lists of 'subscribers' and 'publishers' (their app IDs
        (Mininet node names), which will appear in the name of their output file).

        :param List[Node] failed_nodes:
        :param List[str] failed_links:
        :param Host server:
        :param List[Host] publishers:
        :param List[Host] subscribers:
        :rtype dict:
        """

        log.info('*** Starting network')
        self.net.build()
        self.net.start()
        self.net.waitConnected()  # ensure switches connect

        # give controller time to converge topology so pingall works
        time.sleep(5)

        # setting the server's default route for controller access needs to be
        # done after the network starts up
        nat_ip = self.nat.IP()
        srv_iface = self.server.intfNames()[-1]
        self.server.setDefaultRoute('via %s dev %s' % (nat_ip, srv_iface))

        # We also have to manually configure the routes for the multicast addresses
        # the server will use.
        for a in self.mcast_address_pool:
            self.server.setHostRoute(a, self.server.intf().name)

        # this needs to come after starting network or no interfaces/IP addresses will be present
        log.debug("\n".join("added host %s at IP %s" % (host.name, host.IP()) for host in self.net.hosts))
        log.debug('links: %s' % [(l.intf1.name, l.intf2.name) for l in self.net.links])

        log.info('*** Pinging hosts so controller can gather IP addresses...')
        # don't want the NAT involved as hosts won't get a route to it
        # TODO: could just ping the server from each host as we don't do any host-to-host
        # comms and the whole point of this is really just to establish the hosts in the
        # controller's topology.  ALSO: we need to either modify this or call ping manually
        # because having error_rate > 0 leads to ping loss, which could results in a host
        # not being known!
        loss = self.net.ping(hosts=[h for h in self.net.hosts if h != self.nat], timeout=2)
        if loss > 0:
            log.warning("ping had a loss of %f" % loss)

        # This needs to occur AFTER pingAll as the exchange of ARP messages
        # is used by the controller (ONOS) to learn hosts' IP addresses
        self.net.staticArp()

        self.setup_topology_manager()

        log.info('*** Network set up!\n*** Configuring experiment...')

        self.setup_traffic_generators()
        # NOTE: it takes a second or two for the clients to actually start up!
        # log.debug('*** Starting clients at time %s' % time.time())
        logs_dir, outputs_dir = self.setup_seismic_test(publishers, subscribers, server)
        # log.debug('*** Done starting clients at time %s' % time.time())

        # Apply actual failure model: we schedule these to fail when the earthquake hits
        # so there isn't time for the topology to update on the controller,
        # which would skew the results incorrectly. Since it may take a few cycles
        # to fail a lot of nodes/links, we schedule the failures for a second before.
        # ENCHANCE: instead of just 1 sec before, should try to figure out how long
        # it'll take for different machines/configurations and time it better...
        log.info('*** Configuration done!  Waiting for earthquake to start...')
        time.sleep(SEISMIC_EVENT_DELAY - 1)
        log.info('*** Earthquake at %s!  Applying failure model...' % time.time())
        for link in failed_links:
            self.net.configLinkStatus(link[0], link[1], 'down')
        for node in failed_nodes:
            node.stop(deleteIntfs=False)

        # log.debug('*** Failure model finished applying at %s' % time.time())

        log.info("*** Waiting for experiment to complete...")

        time.sleep(EXPERIMENT_DURATION - SEISMIC_EVENT_DELAY)

        return {'outputs_dir': outputs_dir, 'logs_dir': logs_dir,
                'publishers': [p.name for p in publishers],
                'subscribers': [s.name for s in subscribers]}

    def setup_topology_manager(self):
        """
        Starts a SdnTopology for the given controller (topology_manager) type.  Used for setting
        routes, clearing flows, etc.
        :return:
        """
        SdnTopologyAdapter = None
        if self.topology_adapter_type == 'onos':
            from topology_manager.onos_sdn_topology import OnosSdnTopology as SdnTopologyAdapter
        elif self.topology_adapter_type == 'floodlight':
            from topology_manager.floodlight_sdn_topology import FloodlightSdnTopology as SdnTopologyAdapter
        else:
            log.error("Unrecognized topology_adapter_type type %s.  Can't reset controller between runs or manipulate flows properly!")
            exit(102)

        if SdnTopologyAdapter is not None:
            self.topology_adapter = SdnTopologyAdapter(ip=self.controller_ip, port=self.controller_port)

    def setup_traffic_generators(self):
        """Each traffic generating host starts an iperf process aimed at
        (one of) the server(s) in order to generate random traffic and create
        congestion in the experiment.  Traffic is all UDP because it sets the bandwidth.

        NOTE: iperf v2 added the capability to tell the server when to exit after some time.
        However, we explicitly terminate the server anyway to avoid incompatibility issues."""

        generators = self._get_mininet_nodes(self._choose_random_hosts(self.n_traffic_generators))

        # TODO: include the cloud_server as a possible traffic generation/reception
        # point here?  could also use other hosts as destinations...
        srv = self.server

        log.info("*** Starting background traffic generators")
        # We enumerate the generators to fill the range of ports so that the server
        # can listen for each iperf client.
        for n, g in enumerate(generators):
            log.info("iperf from %s to %s" % (g, srv))
            # can't do self.net.iperf([g,s]) as there's no option to put it in the background
            i = g.popen('iperf -p %d -t %d -u -b %dM -c %s &' % (IPERF_BASE_PORT + n, EXPERIMENT_DURATION,
                                                                 self.traffic_generator_bandwidth, srv.IP()))
            self.client_iperfs.append(i)
            i = srv.popen('iperf -p %d -t %d -u -s &' % (IPERF_BASE_PORT + n, EXPERIMENT_DURATION))
            self.server_iperfs.append(i)


    def setup_seismic_test(self, sensors, subscribers, server):
        """
        Sets up the seismic sensing test scenario in which each sensor reports
        a sensor reading to the server, which will aggregate them together and
        multicast the result back out to each subscriber.  The server uses RIDE-D:
        a reliable multicast method in which several maximally-disjoint multicast
        trees (MDMTs) are installed in the SDN topology and intelligently
        choosen from at alert-time based on various heuristics.
        :param List[Host] sensors:
        :param List[Host] subscribers:
        :param Host server:
        :returns logs_dir, outputs_dir: the directories (relative to the experiment output
         file) in which the logs and output files, respectively, are stored for this run
        """

        delay = SEISMIC_EVENT_DELAY  # seconds before sensors start picking
        quit_time = EXPERIMENT_DURATION

        # The logs and output files go in nested directories rooted
        # at the same level as the whole experiment's output file.
        # We typically name the output file as results_$PARAMS.json, so cut off the front and extension
        root_dir = os.path.dirname(self.output_filename)
        base_dirname = os.path.splitext(os.path.basename(self.output_filename))[0]
        if base_dirname.startswith('results_'):
            base_dirname = base_dirname[8:]
        if WITH_LOGS:
            logs_dir = os.path.join(root_dir, 'logs_%s' % base_dirname, 'run%d' % self.current_run_number)
            try:
                os.makedirs(logs_dir)
            except OSError:
                pass
        else:
            logs_dir = None
        outputs_dir =  os.path.join(root_dir, 'outputs_%s' % base_dirname, 'run%d' % self.current_run_number)
        try:
            os.makedirs(outputs_dir)
        except OSError:
            pass

        ####################
        ### SETUP SERVER
        ####################

        log.info("Seismic server on host %s" % server.name)

        # First, we need to set static unicast routes to subscribers for unicast comparison config.
        # This HACK avoids the controller recovering failed paths too quickly due to Mininet's zero latency
        # control plane network.
        # NOTE: because we only set static routes when not using RideD multicast, this shouldn't
        # interfere with other routes.
        if self.comparison is not None and self.comparison == 'unicast':
            for sub in subscribers:
                try:
                    # HACK: we get the route from the NetworkxTopology in order to have the same
                    # as other experiments, but then need to convert these paths into one
                    # recognizable by the actual SDN Controller Topology manager.
                    # HACK: since self.server is a new Mininet Host not in original topo, we do this:
                    original_server_name = self.topo.get_servers()[0]
                    route = self.topo.get_path(original_server_name, sub.name, weight=DISTANCE_METRIC)
                    # Next, convert the NetworkxTopology nodes to the proper ID
                    route = self._get_mininet_nodes(route)
                    route = [self.get_node_dpid(n) for n in route]
                    # Then we need to modify the route to account for the real Mininet server 'hs0'
                    route.insert(0, self.get_host_dpid(self.server))
                    log.debug("Installing static route for subscriber %s: %s" % (sub, route))

                    flow_rules = self.topology_adapter.build_flow_rules_from_path(route)
                    for r in flow_rules:
                        self.topology_adapter.install_flow_rule(r)
                except Exception as e:
                    log.error("Error installing flow rules for static subscriber routes: %s" % e)
                    raise e

        cmd = "python %s seismic_warning_test/seismic_server.py -a %s --quit_time %d --debug %s" % \
              ("-O" if OPTIMISED_PYTHON else "", ' '.join(self.mcast_address_pool), quit_time, self.debug_level)

        # HACK: we pass the static lists of publishers/subscribers via cmd line so as to avoid having to build an
        # API server for RideD to pull this info from.  ENHANCE: integrate a pub-sub broker agent API on controller.
        # NOTE: we pass the subscribers' DPIDs and the server will handle converting them to appropriate IDs (e.g. IP address)
        subs = ' '.join(self.get_host_dpid(h) for h in subscribers)
        pubs = ' '.join(self.get_host_dpid(h) for h in sensors)
        if self.comparison:
            cmd += " --no-ride "
        cmd += " --subs %s --pubs %s" % (subs, pubs)

        # Add RideD arguments to the server command.
        cmd += " --ntrees %d --mcast-construction-algorithm %s --choosing-heuristic %s --dpid %s --ip %s --port %d"\
               % (self.ntrees, ' '.join(self.tree_construction_algorithm), self.tree_choosing_heuristic,
                  self.get_host_dpid(self.server), self.controller_ip, self.controller_port)

        if WITH_LOGS:
            cmd += " > %s 2>&1" % os.path.join(logs_dir, 'srv')

        log.debug(cmd)
        # HACK: Need to set PYTHONPATH since we don't install our Python modules directly and running Mininet
        # as root strips this variable from our environment.
        env = os.environ.copy()
        if 'PYTHONPATH' not in env:
            env['PYTHONPATH'] = os.path.dirname(os.path.abspath(__file__))
        p = server.popen(cmd, shell=True, env=env)
        self.popens.append(p)

        ####################
        ###  SETUP CLIENTS
        ####################

        sensors = set(sensors)
        subscribers = set(subscribers)

        log.info("Running seismic test client on %d subscribers and %d sensors" % (len(subscribers), len(sensors)))
        server_ip = server.IP()
        assert server_ip != '127.0.0.1', "ERROR: server.IP() returns localhost!"
        for client in sensors.union(subscribers):
            client_id = client.name
            cmd = "python %s seismic_warning_test/seismic_client.py --id %s --delay %d --quit_time %d --debug %s --file %s" % \
                  ("-O" if OPTIMISED_PYTHON else "", client_id, delay, quit_time, self.debug_level,
                   os.path.join(outputs_dir, 'client_events'))  # the client appends its ID automatically to the file name
            if client in sensors:
                cmd += ' -a %s' % server_ip
            if client in subscribers:
                cmd += ' -l'
            if WITH_LOGS:
                cmd += " > %s 2>&1" % os.path.join(logs_dir, client_id)

            # the node.sendCmd option in mininet only allows a single
            # outstanding command at a time and cancels any current
            # ones when net.CLI is called.  Hence, we need popen.
            log.debug(cmd)
            p = client.popen(cmd, shell=True, env=env)
            self.popens.append(p)

        # make the paths relative to the root directory in which the whole experiment output file is stored
        # as otherwise the paths are dependent on where the cwd is
        logs_dir = os.path.relpath(logs_dir, root_dir)
        outputs_dir = os.path.relpath(outputs_dir, root_dir)
        return logs_dir, outputs_dir

    def teardown_experiment(self):
        log.info("*** Experiment complete! Waiting for all host procs to exit...")

        # need to check if the programs have finished before we exit mininet!
        # First, we check the server to see if it even ran properly.
        ret = self.popens[0].wait()
        if ret != 0:
            from seismic_warning_test.seismic_server import SeismicServer
            if ret == SeismicServer.EXIT_CODE_NO_SUBSCRIBERS:
                log.error("Server proc exited due to no reachable subscribers: this experiment is a wash!")
                # TODO: handle this error appropriately: mark results as junk?
            else:
                log.error("Server proc exited with code %d" % self.popens[0].returncode)
        for p in self.popens[1:]:
            ret = p.wait()
            while ret is None:
                ret = p.wait()
            if ret != 0:
                if ret == errno.ENETUNREACH:
                    # TODO: handle this error appropriately: record failed clients in results?
                    log.error("Client proc failed due to unreachable network!")
                else:
                    log.error("Client proc exited with code %d" % p.returncode)
        # Clients should terminate automatically, but the server won't do so unless
        # a high enough version of iperf is used so we just do it explicitly.
        for p in self.client_iperfs:
            p.wait()
        for p in self.server_iperfs:
            try:
                p.kill()
                p.wait()
            except OSError:
                pass  # must have already terminated
        self.popens = []
        self.server_iperfs = []
        self.client_iperfs = []

        log.debug("*** All processes exited!  Cleaning up Mininet...")

        if self.show_cli:
            CLI(self.net)

        # Clear out all the flows/groups from controller
        if self.topology_adapter is not None:
            log.debug("Removing groups and flows via REST API.  This could take a while while we wait for the transactions to commit...")
            self.topology_adapter.remove_all_flow_rules()

            # We loop over doing this because we want to make sure the groups have been fully removed
            # before continuing to the next run or we'll have serious problems.
            # NOTE: some flows will still be present so we'd have to check them after
            # filtering only those added by REST API, hence only looping over groups for now...
            ngroups = 1
            while ngroups > 0:
                self.topology_adapter.remove_all_groups()
                time.sleep(1)
                leftover_groups = self.topology_adapter.get_groups()
                ngroups = len(leftover_groups)
                # len(leftover_groups) == 0, "Not all groups were cleared after experiment! Still left: %s" % leftover_groups

        # BUG: This might error if a process (e.g. iperf) didn't finish exiting.
        try:
            self.net.stop()
        except OSError as e:
            log.error("Stopping Mininet failed, but we'll keep going.  Reason: %s" % e)

        # We seem to still have process leakage even after the previous call to stop Mininet,
        # so let's do an explicit clean between each run.
        p = Popen('sudo mn -c > /dev/null 2>&1', shell=True)
        p.wait()

        # Sleep for a bit so the controller/OVS can finish resetting
        time.sleep(SLEEP_TIME_BETWEEN_RUNS)

    def get_host_dpid(self, host):
        """
        Returns the data plane ID for the given host that is recognized by the
        particular SDN controller currently in use.
        :param Host host:
        :return:
        """
        if self.topology_adapter_type == 'onos':
            # TODO: verify this vibes with ONOS properly; might need VLAN??
            dpid = host.defaultIntf().MAC().upper() + '/None'
        elif self.topology_adapter_type == 'floodlight':
            dpid = host.IP()
        else:
            raise ValueError("Unrecognized topology adapter type %s" % self.topology_adapter_type)
        return dpid

    def get_switch_dpid(self, switch):
        """
        Returns the data plane ID for the given switch that is recognized by the
        particular SDN controller currently in use.
        :param Switch switch:
        :return:
        """
        if self.topology_adapter_type == 'onos':
            dpid = 'of:' + switch.dpid
        elif self.topology_adapter_type == 'floodlight':
            raise NotImplementedError()
        else:
            raise ValueError("Unrecognized topology adapter type %s" % self.topology_adapter_type)
        return dpid

    def get_node_dpid(self, node):
        """
        Returns the data plane ID for the given node by determining whether it's a
        Switch or Host first.
        :param node:
        :return:
        """
        if isinstance(node, Switch):
            return self.get_switch_dpid(node)
        elif isinstance(node, Host):
            return self.get_host_dpid(node)
        else:
            raise TypeError("Unrecognized node type for: %s" % node)

if __name__ == "__main__":
    import sys
    exp = MininetSmartCampusExperiment.build_from_args(sys.argv[1:])
    exp.run_all_experiments()

