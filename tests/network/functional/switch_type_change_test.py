#
# Copyright 2016-2019 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

from copy import deepcopy
import six

import pytest

from vdsm.network import api as net_api
from vdsm.network import errors as ne
from vdsm.network.initializer import init_unpriviliged_dhclient_monitor_ctx
from vdsm.network.ipwrapper import linkSet, addrAdd

from network.nettestlib import dummy_device
from network.nettestlib import dummy_devices
from network.nettestlib import veth_pair
from network.nettestlib import dnsmasq_run
from network.nettestlib import running_on_fedora

from .netfunctestlib import NetFuncTestAdapter, SetupNetworksError, NOCHK


NET1_NAME = 'test-network1'
NET2_NAME = 'test-network2'
VLAN = 10
BOND_NAME = 'bond10'

IPv4_ADDRESS = '192.0.3.1'
IPv4_NETMASK = '255.255.255.0'
IPv4_PREFIX_LEN = '24'
IPv6_ADDRESS = 'fdb3:84e5:4ff4:55e3::1'
IPv6_PREFIX_LEN = '64'

DHCPv4_RANGE_FROM = '192.0.3.2'
DHCPv4_RANGE_TO = '192.0.3.253'

adapter = None


pytestmark = pytest.mark.ovs_switch

parametrize_switch_change = pytest.mark.parametrize(
    'sw_src, sw_dst', [('legacy', 'ovs'), ('ovs', 'legacy')]
)


class FakeNotifier:
    def notify(self, event_id, params=None):
        pass


@pytest.fixture(scope='module', autouse=True)
def create_adapter(target):
    global adapter
    adapter = NetFuncTestAdapter(target)


@pytest.fixture(scope='module', autouse=True)
def dhclient_monitor():
    event_sink = FakeNotifier()
    with init_unpriviliged_dhclient_monitor_ctx(event_sink, net_api):
        yield


@parametrize_switch_change
class TestBasicSwitchChange(object):
    def test_switch_change_basic_network(self, sw_src, sw_dst):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {NET1_NAME: {'nic': nic, 'switch': sw_src}}
            NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                adapter.setupNetworks(NETSETUP_TARGET, {}, NOCHK)
                adapter.assertNetwork(NET1_NAME, NETSETUP_TARGET[NET1_NAME])

    def test_switch_change_basic_vlaned_network(self, sw_src, sw_dst):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {
                NET1_NAME: {'nic': nic, 'vlan': VLAN, 'switch': sw_src}
            }
            NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                adapter.setupNetworks(NETSETUP_TARGET, {}, NOCHK)
                adapter.assertNetwork(NET1_NAME, NETSETUP_TARGET[NET1_NAME])

    def test_switch_change_bonded_network(self, sw_src, sw_dst):
        with dummy_devices(2) as (nic1, nic2):
            NETSETUP_SOURCE = {
                NET1_NAME: {'bonding': BOND_NAME, 'switch': sw_src}
            }
            NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)
            BONDSETUP_SOURCE = {
                BOND_NAME: {'nics': [nic1, nic2], 'switch': sw_src}
            }
            BONDSETUP_TARGET = _change_switch_type(BONDSETUP_SOURCE, sw_dst)

            with adapter.setupNetworks(
                NETSETUP_SOURCE, BONDSETUP_SOURCE, NOCHK
            ):
                adapter.setupNetworks(NETSETUP_TARGET, BONDSETUP_TARGET, NOCHK)
                adapter.assertNetwork(NET1_NAME, NETSETUP_TARGET[NET1_NAME])
                adapter.assertBond(BOND_NAME, BONDSETUP_TARGET[BOND_NAME])


@parametrize_switch_change
class TestIpSwitch(object):
    def test_switch_change_bonded_network_with_static_ip(self, sw_src, sw_dst):
        with dummy_devices(2) as (nic1, nic2):
            NETSETUP_SOURCE = {
                NET1_NAME: {
                    'bonding': BOND_NAME,
                    'ipaddr': IPv4_ADDRESS,
                    'netmask': IPv4_NETMASK,
                    'ipv6addr': IPv6_ADDRESS + '/' + IPv6_PREFIX_LEN,
                    'switch': sw_src,
                }
            }
            NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)
            BONDSETUP_SOURCE = {
                BOND_NAME: {'nics': [nic1, nic2], 'switch': sw_src}
            }
            BONDSETUP_TARGET = _change_switch_type(BONDSETUP_SOURCE, sw_dst)

            with adapter.setupNetworks(
                NETSETUP_SOURCE, BONDSETUP_SOURCE, NOCHK
            ):
                adapter.setupNetworks(NETSETUP_TARGET, BONDSETUP_TARGET, NOCHK)
                adapter.assertNetwork(NET1_NAME, NETSETUP_TARGET[NET1_NAME])
                adapter.assertBond(BOND_NAME, BONDSETUP_TARGET[BOND_NAME])

    def test_switch_change_bonded_network_with_dhclient(self, sw_src, sw_dst):
        if sw_src == 'ovs' and sw_dst == 'legacy' and running_on_fedora(29):
            pytest.xfail('Fails on Fedora 29')
        with veth_pair() as (server, nic1):
            with dummy_device() as nic2:
                NETSETUP_SOURCE = {
                    NET1_NAME: {
                        'bonding': BOND_NAME,
                        'bootproto': 'dhcp',
                        'blockingdhcp': True,
                        'switch': sw_src,
                    }
                }
                NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)
                BONDSETUP_SOURCE = {
                    BOND_NAME: {'nics': [nic1, nic2], 'switch': sw_src}
                }
                BONDSETUP_TARGET = _change_switch_type(
                    BONDSETUP_SOURCE, sw_dst
                )

                addrAdd(server, IPv4_ADDRESS, IPv4_PREFIX_LEN)
                linkSet(server, ['up'])

                with dnsmasq_run(
                    server,
                    DHCPv4_RANGE_FROM,
                    DHCPv4_RANGE_TO,
                    router=IPv4_ADDRESS,
                ):
                    with adapter.setupNetworks(
                        NETSETUP_SOURCE, BONDSETUP_SOURCE, NOCHK
                    ):
                        adapter.setupNetworks(
                            NETSETUP_TARGET, BONDSETUP_TARGET, NOCHK
                        )
                        adapter.assertNetwork(
                            NET1_NAME, NETSETUP_TARGET[NET1_NAME]
                        )
                        adapter.assertBond(
                            BOND_NAME, BONDSETUP_TARGET[BOND_NAME]
                        )


@parametrize_switch_change
class TestSwitchRollback(object):
    def test_rollback_target_configuration_with_invalid_ip(
        self, sw_src, sw_dst
    ):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {NET1_NAME: {'nic': nic, 'switch': sw_src}}
            NETSETUP_TARGET = {
                NET1_NAME: {
                    'nic': nic,
                    'ipaddr': '300.300.300.300',  # invalid
                    'netmask': IPv4_NETMASK,
                    'switch': sw_dst,
                }
            }

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                with pytest.raises(SetupNetworksError) as e:
                    adapter.setupNetworks(NETSETUP_TARGET, {}, NOCHK)
                assert e.value.status == ne.ERR_BAD_ADDR
                adapter.assertNetwork(NET1_NAME, NETSETUP_SOURCE[NET1_NAME])

    def test_rollback_target_bond_configuration_with_invalid_ip(
        self, sw_src, sw_dst
    ):
        with dummy_devices(3) as (nic1, nic2, nic3):
            NETSETUP_SOURCE = {NET1_NAME: {'nic': nic1, 'switch': sw_src}}
            BONDSETUP_SOURCE = {
                BOND_NAME: {'nics': [nic2, nic3], 'switch': sw_src}
            }
            NETSETUP_TARGET = {
                NET1_NAME: {
                    'nic': nic1,
                    'ipaddr': '300.300.300.300',  # invalid
                    'netmask': IPv4_NETMASK,
                    'switch': sw_dst,
                }
            }
            BONDSETUP_TARGET = {
                BOND_NAME: {'nics': [nic2, nic3], 'switch': sw_dst}
            }

            with adapter.setupNetworks(
                NETSETUP_SOURCE, BONDSETUP_SOURCE, NOCHK
            ):
                with pytest.raises(SetupNetworksError) as e:
                    adapter.setupNetworks(
                        NETSETUP_TARGET, BONDSETUP_TARGET, NOCHK
                    )
                assert e.value.status == ne.ERR_BAD_ADDR
                adapter.assertNetwork(NET1_NAME, NETSETUP_SOURCE[NET1_NAME])
                adapter.assertBond(BOND_NAME, BONDSETUP_SOURCE[BOND_NAME])

    def test_rollback_target_configuration_failed_connectivity_check(
        self, sw_src, sw_dst
    ):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {
                NET1_NAME: {'nic': nic, 'switch': sw_src},
                NET2_NAME: {'nic': nic, 'vlan': VLAN, 'switch': sw_src},
            }
            NETSETUP_TARGET = _change_switch_type(NETSETUP_SOURCE, sw_dst)

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                with pytest.raises(SetupNetworksError) as e:
                    adapter.setupNetworks(
                        NETSETUP_TARGET,
                        {},
                        {
                            'connectivityCheck': True,
                            'connectivityTimeout': 0.1,
                        },
                    )
                assert e.value.status == ne.ERR_LOST_CONNECTION
                adapter.assertNetwork(NET1_NAME, NETSETUP_SOURCE[NET1_NAME])
                adapter.assertNetwork(NET2_NAME, NETSETUP_SOURCE[NET2_NAME])


@parametrize_switch_change
class TestSwitchValidation(object):
    def test_switch_change_with_not_all_existing_networks_specified(
        self, sw_src, sw_dst
    ):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {
                NET1_NAME: {'nic': nic, 'switch': sw_src},
                NET2_NAME: {'nic': nic, 'vlan': VLAN, 'switch': sw_src},
            }
            NETSETUP_TARGET = {NET1_NAME: {'nic': nic, 'switch': sw_dst}}

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                with pytest.raises(SetupNetworksError) as e:
                    adapter.setupNetworks(NETSETUP_TARGET, {}, NOCHK)
                assert e.value.status == ne.ERR_BAD_PARAMS
                adapter.assertNetwork(NET1_NAME, NETSETUP_SOURCE[NET1_NAME])
                adapter.assertNetwork(NET2_NAME, NETSETUP_SOURCE[NET2_NAME])

    def test_switch_change_setup_includes_a_network_removal(
        self, sw_src, sw_dst
    ):
        with dummy_device() as nic:
            NETSETUP_SOURCE = {
                NET1_NAME: {'nic': nic, 'switch': sw_src},
                NET2_NAME: {'nic': nic, 'vlan': VLAN, 'switch': sw_src},
            }
            NETSETUP_TARGET = {
                NET1_NAME: {'nic': nic, 'switch': sw_dst},
                NET2_NAME: {'remove': True},
            }

            with adapter.setupNetworks(NETSETUP_SOURCE, {}, NOCHK):
                with pytest.raises(SetupNetworksError) as e:
                    adapter.setupNetworks(NETSETUP_TARGET, {}, NOCHK)
                assert e.value.status == ne.ERR_BAD_PARAMS
                adapter.assertNetwork(NET1_NAME, NETSETUP_SOURCE[NET1_NAME])
                adapter.assertNetwork(NET2_NAME, NETSETUP_SOURCE[NET2_NAME])


def _change_switch_type(requests, target_switch):
    changed_requests = deepcopy(requests)
    for attrs in six.itervalues(changed_requests):
        attrs['switch'] = target_switch
    return changed_requests
