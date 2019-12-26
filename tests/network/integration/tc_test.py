#
# Copyright 2012 Roman Fenkhuber.
# Copyright 2012-2019 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

from binascii import unhexlify
from collections import namedtuple
import os
import sys
import time

import pytest
import six

from network.compat import mock

from ..nettestlib import Tap
from ..nettestlib import dummy_device
from ..nettestlib import veth_pair
from ..nettestlib import vlan_device
from .iperf import IperfServer
from .iperf import IperfClient
from .iperf import requires_iperf3
from .netintegtestlib import Bridge
from .netintegtestlib import bridge_device
from .netintegtestlib import network_namespace
from ..nettestlib import running

from vdsm.network import cmd
from vdsm.network import tc
from vdsm.network.configurators import qos
from vdsm.network.ipwrapper import addrAdd, linkSet, netns_exec, link_set_netns
from vdsm.network.netinfo.qos import DEFAULT_CLASSID


EXT_TC = "/sbin/tc"


@pytest.fixture
def bridge_dev():
    with bridge_device() as dev:
        yield dev


@pytest.fixture
def requires_tc(bridge_dev):
    cmds = [EXT_TC, 'qdisc', 'add', 'dev', bridge_dev.devName, 'ingress']
    rc, out, err = cmd.exec_sync(cmds)
    if rc != 0:
        pytest.skip(
            '%r has failed: %s\nDo you have Traffic Control kernel '
            'modules installed?' % (EXT_TC, err)
        )


class TestQdisc(object):
    def _showQdisc(self, bridge):
        _, out, _ = cmd.exec_sync(
            [EXT_TC, "qdisc", "show", "dev", bridge.devName]
        )
        return out

    def _addIngress(self, bridge):
        tc._qdisc_replace_ingress(bridge.devName)
        assert 'qdisc ingress' in self._showQdisc(bridge)

    def testToggleIngress(self, bridge_dev):
        self._addIngress(bridge_dev)
        tc._qdisc_del(bridge_dev.devName, 'ingress')
        assert 'qdisc ingress' not in self._showQdisc(bridge_dev)

    def testQdiscsOfDevice(self, bridge_dev):
        self._addIngress(bridge_dev)
        assert ('ffff:',) == tuple(tc._qdiscs_of_device(bridge_dev.devName))

    def testReplacePrio(self, bridge_dev):
        self._addIngress(bridge_dev)
        tc.qdisc.replace(bridge_dev.devName, 'prio', parent=None)
        assert 'root' in self._showQdisc(bridge_dev)

    def testException(self):
        pytest.raises(
            tc.TrafficControlException,
            tc._qdisc_del,
            "__nosuchiface__",
            'ingress',
        )


@pytest.mark.skipif(
    not os.path.exists('/dev/net/tun'), reason='No tun device available'
)
class TestPortMirror(object):

    """
    Tests port mirroring of IP traffic between two bridges.

    This test brings up two tap devices and attaches every device to a
    separate bridge. Then mirroring of IP packets between the two bridges is
    enabled. If sent through _tap0 the packet _ICMP arrives on _tap1 the test
    succeeds. The tap devices are needed because the tc filter rules only
    become active when the bridge is ready, and the bridge only becomes ready
    when it is attached to an active device.
    """

    #  [ Ethernet ]
    #  dst       = 00:1c:c0:d0:44:dc
    #  src       = 00:21:5c:4d:42:75
    #  type      = IPv4
    #     [ IP ]
    #     version   = 4L
    #     ihl       = 5L
    #     tos       = 0x0
    #     len       = 33
    #     id        = 1
    #     flags     =
    #     frag      = 0L
    #     ttl       = 64
    #     proto     = icmp
    #     chksum    = 0xf953
    #     src       = 192.168.0.52
    #     dst       = 192.168.0.3
    #     \options   \
    #        [ ICMP ]
    #        type      = echo-request
    #        code      = 0
    #        chksum    = 0x2875
    #        id        = 0x0
    #        seq       = 0x0
    #           [ Raw ]
    #           load      = '\x01#Eg\x89'
    _ICMP = unhexlify(
        '001cc0d044dc'
        '00215c4d4275'
        '0800'  # Ethernet
        '45000021000100004001f953'
        'c0a80034'
        'c0a80003'  # IP
        '080028750000000'  # ICMP
        '00123456789'
    )  # Payload

    @pytest.fixture(autouse=True)
    def setup(self, requires_tc):
        self._tap0 = Tap()
        self._tap1 = Tap()
        self._tap2 = Tap()
        self._bridge0 = Bridge('src-')
        self._bridge1 = Bridge('target-')
        self._bridge2 = Bridge('target2-')
        self._devices = [
            self._tap0,
            self._tap1,
            self._tap2,
            self._bridge0,
            self._bridge1,
            self._bridge2,
        ]
        # If setUp raise, teardown is not called, so we should either succeed,
        # or fail without leaving junk around.
        cleanup = []
        try:
            for iface in self._devices:
                iface.addDevice()
                cleanup.append(iface)
            self._bridge0.addIf(self._tap0.devName)
            self._bridge1.addIf(self._tap1.devName)
            self._bridge2.addIf(self._tap2.devName)
        except:
            t, v, tb = sys.exc_info()
            for iface in cleanup:
                try:
                    iface.delDevice()
                except Exception:
                    self.log.exception("Error removing device %s" % iface)
            six.reraise(t, v, tb)

        yield

        failed = False
        for iface in self._devices:
            try:
                iface.delDevice()
            except Exception:
                self.log.exception("Error removing device %s" % iface)
                failed = True
        if failed:
            raise RuntimeError("Error tearing down interfaces")

    def _sendPing(self):
        self._tap1.startListener(self._ICMP)
        self._tap0.writeToDevice(self._ICMP)
        # Attention: sleep is bad programming practice! Never use it for
        # synchronization in productive code!
        time.sleep(0.1)
        if self._tap1.isListenerAlive():
            self._tap1.stopListener()
            return False
        else:
            return True

    @pytest.mark.skipif(six.PY3, reason='needs porting to python 3')
    def testMirroring(self):
        tc.setPortMirroring(self._bridge0.devName, self._bridge1.devName)
        assert self._sendPing(), 'Bridge received no mirrored ping requests.'

        tc.unsetPortMirroring(self._bridge0.devName, self._bridge1.devName)
        assert not self._sendPing(), (
            'Bridge received mirrored ping '
            'requests, but mirroring is unset.'
        )

    @pytest.mark.skipif(six.PY3, reason='needs porting to python 3')
    def testMirroringWithDistraction(self):
        "setting another mirror action should not obstract the first one"
        tc.setPortMirroring(self._bridge0.devName, self._bridge2.devName)
        self.testMirroring()
        tc.unsetPortMirroring(self._bridge0.devName, self._bridge2.devName)


HOST_QOS_OUTBOUND = {
    'ls': {
        'm1': 4 * 1000 ** 2,  # 4Mbit/s
        'd': 100 * 1000,  # 100 microseconds
        'm2': 3 * 1000 ** 2,
    },  # 3Mbit/s
    'ul': {'m2': 8 * 1000 ** 2},
}  # 8Mbit/s

TcClasses = namedtuple('TcClasses', 'classes, default_class, root_class')
TcQdiscs = namedtuple('TcQdiscs', 'leaf_qdiscs, ingress_qdisc, root_qdisc')
TcFilters = namedtuple('TcFilters', 'untagged_filters, tagged_filters')


@pytest.fixture
def dummy():
    with dummy_device() as dev_name:
        yield dev_name


class TestConfigureOutbound(object):
    # TODO:
    # test remove_outbound

    def test_single_non_vlan(self, dummy):
        qos.configure_outbound(HOST_QOS_OUTBOUND, dummy, None)
        tc_entities = self._analyse_qos_and_general_assertions(dummy)
        tc_classes, tc_filters, tc_qdiscs = tc_entities
        assert tc_classes.classes == []

        assert len(tc_qdiscs.leaf_qdiscs) == 1
        assert self._non_vlan_qdisc(tc_qdiscs.leaf_qdiscs) is not None
        self._assert_parent(tc_qdiscs.leaf_qdiscs, tc_classes.default_class)

        assert len(tc_filters.tagged_filters) == 0

    @pytest.mark.parametrize('repeating_calls', [1, 2])
    @mock.patch('vdsm.network.netinfo.bonding.permanent_address', lambda: {})
    def test_single_vlan(self, dummy, repeating_calls):
        with vlan_device(dummy) as vlan:
            for _ in range(repeating_calls):
                qos.configure_outbound(HOST_QOS_OUTBOUND, dummy, vlan.tag)
            tc_entities = self._analyse_qos_and_general_assertions(dummy)
            tc_classes, tc_filters, tc_qdiscs = tc_entities
            assert len(tc_classes.classes) == 1

            assert len(tc_qdiscs.leaf_qdiscs) == 2
            vlan_qdisc = self._vlan_qdisc(tc_qdiscs.leaf_qdiscs, vlan.tag)
            vlan_class = self._vlan_class(tc_classes.classes, vlan.tag)
            self._assert_parent([vlan_qdisc], vlan_class)

            tag_filters = tc_filters.tagged_filters
            assert len(tag_filters) == 1
            assert int(tag_filters[0]['basic']['value']) == vlan.tag

    @mock.patch('vdsm.network.netinfo.bonding.permanent_address', lambda: {})
    def test_multiple_vlans(self, dummy):
        with vlan_device(dummy, tag=16) as vlan1:
            with vlan_device(dummy, tag=17) as vlan2:
                for v in (vlan1, vlan2):
                    qos.configure_outbound(HOST_QOS_OUTBOUND, dummy, v.tag)

                tc_entities = self._analyse_qos_and_general_assertions(dummy)
                tc_classes, tc_filters, tc_qdiscs = tc_entities
                assert len(tc_classes.classes) == 2

                assert len(tc_qdiscs.leaf_qdiscs) == 3
                v1_qdisc = self._vlan_qdisc(tc_qdiscs.leaf_qdiscs, vlan1.tag)
                v2_qdisc = self._vlan_qdisc(tc_qdiscs.leaf_qdiscs, vlan2.tag)
                v1_class = self._vlan_class(tc_classes.classes, vlan1.tag)
                v2_class = self._vlan_class(tc_classes.classes, vlan2.tag)
                self._assert_parent([v1_qdisc], v1_class)
                self._assert_parent([v2_qdisc], v2_class)

                assert len(tc_filters.tagged_filters) == 2
                current_tagged_filters_flow_id = set(
                    f['basic']['flowid'] for f in tc_filters.tagged_filters
                )
                expected_flow_ids = set(
                    '%s%x' % (qos._ROOT_QDISC_HANDLE, v.tag)
                    for v in (vlan1, vlan2)
                )
                assert current_tagged_filters_flow_id == expected_flow_ids

    @requires_iperf3
    @pytest.mark.xfail(reason='Not maintained stress test', run=False)
    def test_iperf_upper_limit(self, requires_tc):
        # Upper limit is not an accurate measure. This is because it converges
        # over time and depends on current machine hardware (CPU).
        # Hence, it is hard to make hard assertions on it. The test should run
        # at least 60 seconds (the longer the better) and the user should
        # inspect the computed average rate and optionally the additional
        # traffic data that was collected in client.out in order to be
        # convinced QOS is working properly.
        limit_kbps = 1000  # 1 Mbps (in kbps)
        server_ip = '192.0.2.1'
        client_ip = '192.0.2.10'
        qos_out = {'ul': {'m2': limit_kbps}, 'ls': {'m2': limit_kbps}}
        # using a network namespace is essential since otherwise the kernel
        # short-circuits the traffic and bypasses the veth devices and the
        # classfull qdisc.
        with network_namespace(
            'server_ns'
        ) as ns, bridge_device() as bridge, veth_pair() as (
            server_peer,
            server_dev,
        ), veth_pair() as (
            client_dev,
            client_peer,
        ):
            linkSet(server_peer, ['up'])
            linkSet(client_peer, ['up'])
            # iperf server and its veth peer lie in a separate network
            # namespace
            link_set_netns(server_dev, ns)
            bridge.addIf(server_peer)
            bridge.addIf(client_peer)
            linkSet(client_dev, ['up'])
            netns_exec(ns, ['ip', 'link', 'set', 'dev', server_dev, 'up'])
            addrAdd(client_dev, client_ip, 24)
            netns_exec(
                ns,
                [
                    'ip',
                    '-4',
                    'addr',
                    'add',
                    'dev',
                    server_dev,
                    '%s/24' % server_ip,
                ],
            )
            qos.configure_outbound(qos_out, client_peer, None)
            with running(IperfServer(server_ip, network_ns=ns)):
                client = IperfClient(server_ip, client_ip, test_time=60)
                client.start()
                max_rate = max(
                    [
                        float(interval['streams'][0]['bits_per_second'])
                        // (2 ** 10)
                        for interval in client.out['intervals']
                    ]
                )
                assert 0 < max_rate < limit_kbps * 1.5

    def _analyse_qos_and_general_assertions(self, device_name):
        tc_classes = self._analyse_classes(device_name)
        tc_qdiscs = self._analyse_qdiscs(device_name)
        tc_filters = self._analyse_filters(device_name)
        self._assertions_on_classes(
            tc_classes.classes, tc_classes.default_class, tc_classes.root_class
        )
        self._assertions_on_qdiscs(
            tc_qdiscs.ingress_qdisc, tc_qdiscs.root_qdisc
        )
        self._assertions_on_filters(
            tc_filters.untagged_filters, tc_filters.tagged_filters
        )
        return tc_classes, tc_filters, tc_qdiscs

    def _analyse_classes(self, device_name):
        all_classes = list(tc.classes(device_name))
        root_class = self._root_class(all_classes)
        default_class = self._default_class(all_classes)
        all_classes.remove(root_class)
        all_classes.remove(default_class)
        return TcClasses(all_classes, default_class, root_class)

    def _analyse_qdiscs(self, device_name):
        all_qdiscs = list(tc.qdiscs(device_name))
        ingress_qdisc = self._ingress_qdisc(all_qdiscs)
        root_qdisc = self._root_qdisc(all_qdiscs)
        leaf_qdiscs = self._leaf_qdiscs(all_qdiscs)
        assert len(leaf_qdiscs) + 2 == len(all_qdiscs)
        return TcQdiscs(leaf_qdiscs, ingress_qdisc, root_qdisc)

    def _analyse_filters(self, device_name):
        filters = list(tc._filters(device_name))
        untagged_filters = self._untagged_filters(filters)
        tagged_filters = self._tagged_filters(filters)
        return TcFilters(untagged_filters, tagged_filters)

    def _assertions_on_classes(self, all_classes, default_class, root_class):
        assert all(
            cls.get('kind') == qos._SHAPING_QDISC_KIND for cls in all_classes
        ), str(all_classes)

        self._assertions_on_root_class(root_class)

        self._assertions_on_default_class(default_class)

        if not all_classes:  # only a default class
            self._assert_upper_limit(default_class)
        else:
            for cls in all_classes:
                self._assert_upper_limit(cls)

    def _assertions_on_qdiscs(self, ingress_qdisc, root_qdisc):
        assert root_qdisc['kind'] == qos._SHAPING_QDISC_KIND
        self._assert_root_handle(root_qdisc)
        assert ingress_qdisc['handle'] == tc.QDISC_INGRESS

    def _assertions_on_filters(self, untagged_filters, tagged_filters):
        assert all(f['protocol'] == 'all' for f in tagged_filters)
        self._assert_parent_handle(
            tagged_filters + untagged_filters, qos._ROOT_QDISC_HANDLE
        )
        assert len(untagged_filters) == 1, untagged_filters
        assert untagged_filters[0]['protocol'] == 'all'

    def _assert_upper_limit(self, default_class):
        dclass = default_class[qos._SHAPING_QDISC_KIND]['ul']['m2']
        assert dclass == HOST_QOS_OUTBOUND['ul']['m2']

    def _assertions_on_default_class(self, default_class):
        self._assert_parent_handle([default_class], qos._ROOT_QDISC_HANDLE)
        assert default_class['leaf'] == DEFAULT_CLASSID + ':'
        dclass = default_class[qos._SHAPING_QDISC_KIND]['ls']
        assert dclass == HOST_QOS_OUTBOUND['ls']

    def _assertions_on_root_class(self, root_class):
        assert root_class is not None
        self._assert_root_handle(root_class)

    def _assert_root_handle(self, entity):
        assert entity['handle'] == qos._ROOT_QDISC_HANDLE

    def _assert_parent(self, entities, parent):
        assert all(e['parent'] == parent['handle'] for e in entities)

    def _assert_parent_handle(self, entities, parent_handle):
        assert all(e['parent'] == parent_handle for e in entities)

    def _root_class(self, classes):
        return _find_entity(lambda c: c.get('root'), classes)

    def _default_class(self, classes):
        default_cls_handle = qos._ROOT_QDISC_HANDLE + DEFAULT_CLASSID
        return _find_entity(
            lambda c: c['handle'] == default_cls_handle, classes
        )

    def _ingress_qdisc(self, qdiscs):
        return _find_entity(lambda q: q['kind'] == 'ingress', qdiscs)

    def _root_qdisc(self, qdiscs):
        return _find_entity(lambda q: q.get('root'), qdiscs)

    def _leaf_qdiscs(self, qdiscs):
        return [
            qdisc for qdisc in qdiscs if qdisc['kind'] == qos._FAIR_QDISC_KIND
        ]

    def _untagged_filters(self, filters):
        predicate = lambda f: f.get('u32', {}).get('match', {}) == {
            'mask': 0,
            'value': 0,
            'offset': 0,
        }
        return list(f for f in filters if predicate(f))

    def _tagged_filters(self, filters):
        def tagged(f):
            return f.get('basic', {}).get('object') == 'vlan'

        return list(f for f in filters if tagged(f))

    def _vlan_qdisc(self, qdiscs, vlan_tag):
        handle = '%x:' % vlan_tag
        return _find_entity(lambda q: q['handle'] == handle, qdiscs)

    def _vlan_class(self, classes, vlan_tag):
        handle = qos._ROOT_QDISC_HANDLE + '%x' % vlan_tag
        return _find_entity(lambda c: c['handle'] == handle, classes)

    def _non_vlan_qdisc(self, qdiscs):
        handle = DEFAULT_CLASSID + ':'
        return _find_entity(lambda q: q['handle'] == handle, qdiscs)


def _find_entity(predicate, entities):
    for ent in entities:
        if predicate(ent):
            return ent
