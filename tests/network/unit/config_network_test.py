#
# Copyright 2012 IBM, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

import pytest

from vdsm.network import netinfo
from vdsm.network.link.iface import DEFAULT_MTU

from network.compat import mock

from vdsm.network import errors
from vdsm.network.configurators import ifcfg
from vdsm.network.canonicalize import canonicalize_networks
from vdsm.network import legacy_switch
from vdsm.network.models import Bond, Bridge, Nic, Vlan
from vdsm.network.netswitch import validator

NICS = [f'eth{i}' for i in range(11)]

BOND0 = 'bond00'
BOND0_SLAVES = ['eth5', 'eth6']
BONDS = [BOND0]


class BondMock:
    def __init__(self, name):
        pass

    @staticmethod
    def bonds():
        return BONDS

    @property
    def slaves(self):
        return set(BOND0_SLAVES)


def _raiseInvalidOpException(*args, **kwargs):
    return RuntimeError(
        'Attempted to apply network configuration during unit ' 'testing.'
    )


class TestConfigNetwork(object):
    def _addNetworkWithExc(self, netName, opts, errCode):
        fakeInfo = netinfo.cache.CachingNetInfo(FAKE_NETINFO)
        configurator = ifcfg.Ifcfg(fakeInfo)

        with pytest.raises(errors.ConfigNetworkError) as cneContext:
            canonicalize_networks({netName: opts})
            validator.validate_network_setup({netName: opts}, {}, FAKE_NETINFO)
            legacy_switch._add_network(
                netName, configurator, fakeInfo, None, **opts
            )
        assert cneContext.value.errCode == errCode

    # Monkey patch the real network detection from the netinfo module.
    @mock.patch.object(ifcfg, 'ifdown', _raiseInvalidOpException)
    @mock.patch.object(ifcfg, '_exec_ifup', _raiseInvalidOpException)
    @mock.patch.object(Bond, 'configure', _raiseInvalidOpException)
    @mock.patch.object(Bridge, 'configure', _raiseInvalidOpException)
    @mock.patch.object(Nic, 'configure', _raiseInvalidOpException)
    @mock.patch.object(Vlan, 'configure', _raiseInvalidOpException)
    @mock.patch.object(validator, 'nics', lambda: NICS)
    @mock.patch.object(validator, 'Bond', BondMock)
    def testAddNetworkValidation(self):

        # Test for already existing bridge.
        self._addNetworkWithExc(
            'fakebrnet',
            dict(nic='eth2', mtu=DEFAULT_MTU),
            errors.ERR_USED_BRIDGE,
        )

        # Test for already existing network.
        self._addNetworkWithExc(
            'fakent', dict(nic='eth2', mtu=DEFAULT_MTU), errors.ERR_USED_BRIDGE
        )

        # Test for non existing nic.
        self._addNetworkWithExc(
            'test', dict(nic='eth11', mtu=DEFAULT_MTU), errors.ERR_BAD_NIC
        )

        # Test for nic already in a bond.
        self._addNetworkWithExc(
            'test', dict(nic='eth6', mtu=DEFAULT_MTU), errors.ERR_USED_NIC
        )

    @mock.patch.object(validator, 'nics', lambda: [])
    @mock.patch.object(validator, 'Bond')
    def testValidateNetSetupRemoveParamValidation(self, bond_mock):
        bond_mock.return_value.bonds.return_value = []
        networks = {
            'test-network': {'nic': 'dummy', 'remove': True, 'bridged': True}
        }
        with pytest.raises(errors.ConfigNetworkError) as cneContext:
            validator.validate_network_setup(networks, {}, {'networks': {}})
        assert cneContext.value.errCode == errors.ERR_BAD_PARAMS


FAKE_NETINFO = {
    'networks': {
        'fakent': {
            'iface': 'fakeint',
            'bridged': False,
            'southbound': 'fakeint',
        },
        'fakebrnet': {
            'iface': 'fakebr',
            'bridged': True,
            'ports': ['eth0', 'eth1'],
            'southbound': 'eth0',
        },
        'fakebrnet1': {
            'iface': 'fakebr1',
            'bridged': True,
            'ports': ['bond00'],
            'southbound': 'bond00',
        },
        'fakebrnet2': {
            'iface': 'fakebr2',
            'bridged': True,
            'ports': ['eth7.1'],
            'southbound': 'eth7.1',
        },
        'fakebrnet3': {
            'iface': 'eth8',
            'bridged': False,
            'southbound': 'eth8',
        },
    },
    'vlans': {
        'eth3.2': {
            'iface': 'eth3',
            'addr': '10.10.10.10',
            'netmask': '255.255.0.0',
            'mtu': 1500,
        },
        'eth7.1': {
            'iface': 'eth7',
            'addr': '192.168.100.1',
            'netmask': '255.255.255.0',
            'mtu': 1500,
        },
    },
    'nics': NICS,
    'bridges': {
        'fakebr': {'ports': ['eth0', 'eth1']},
        'fakebr1': {'ports': ['bond00']},
        'fakebr2': {'ports': ['eth7.1']},
    },
    'bondings': {BOND0: {'slaves': BOND0_SLAVES}},
    'nameservers': [],
}
