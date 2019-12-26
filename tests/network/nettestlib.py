# Copyright 2015-2019 Red Hat, Inc.
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
import fcntl
import os
import shutil
import signal
import struct
import time
from contextlib import contextmanager
from multiprocessing import Process
import logging

import pytest

from vdsm.common import cpuarch
from vdsm.network import cmd
from vdsm.network import nmstate
from vdsm.network.ip import address
from vdsm.network.ip import dhclient
from vdsm.network.ipwrapper import (
    addrAdd,
    linkSet,
    linkAdd,
    linkDel,
    IPRoute2Error,
)
from vdsm.network.link import iface as linkiface, bond as linkbond
from vdsm.network.link.iface import random_iface_name
from vdsm.network.lldpad import lldptool
from vdsm.network.netinfo import routes
from vdsm.network.netlink import monitor
from vdsm.common.cache import memoized
from vdsm.common.proc import pgrep

from . import dhcp
from . import firewall


EXT_IP = "/sbin/ip"


class Interface(object):
    def __init__(self, prefix='vdsm-', max_length=11):
        self.devName = random_iface_name(prefix, max_length)

    def up(self):
        linkSet(self.devName, ['up'])

    def _down(self):
        with monitor.Monitor(groups=('link',), timeout=2) as mon:
            linkSet(self.devName, ['down'])
            for event in mon:
                if (
                    event.get('name') == self.devName
                    and event.get('state') == 'down'
                ):
                    return

    def __repr__(self):
        return "<{0} {1!r}>".format(self.__class__.__name__, self.devName)


class Vlan(Interface):
    def __init__(self, backing_device_name, tag):
        self.tag = tag
        self.backing_device_name = backing_device_name
        vlan_name = '{}.{}'.format(backing_device_name, tag)
        super(Vlan, self).__init__(vlan_name, len(vlan_name))

    def addDevice(self):
        linkAdd(
            self.devName,
            'vlan',
            link=self.backing_device_name,
            args=['id', str(self.tag)],
        )
        self.up()

    def delDevice(self):
        self._down()
        linkDel(self.devName)
        if nmstate.is_nmstate_backend():
            cmd.exec_sync(['nmcli', 'con', 'del', self.devName])


@contextmanager
def vlan_device(link, tag=16):
    vlan = Vlan(link, tag)
    vlan.addDevice()
    try:
        yield vlan
    finally:
        try:
            vlan.delDevice()
        except IPRoute2Error:
            # if the underlying device was removed beforehand, the vlan device
            # would be gone by now.
            pass


def _listenOnDevice(fd, icmp):
    while True:
        packet = os.read(fd, 2048)
        # check if it is an IP packet
        if packet[12:14] == chr(0x08) + chr(0x00):
            if packet == icmp:
                return


class Tap(Interface):

    _IFF_TAP = 0x0002
    _IFF_NO_PI = 0x1000
    arch = cpuarch.real()
    if arch in (cpuarch.X86_64, cpuarch.S390X):
        _TUNSETIFF = 0x400454CA
    elif cpuarch.is_ppc(arch):
        _TUNSETIFF = 0x800454CA
    else:
        pytest.skip("Unsupported Architecture %s" % arch)

    _deviceListener = None

    def addDevice(self):
        self._cloneDevice = open('/dev/net/tun', 'r+b', buffering=0)
        ifr = struct.pack(
            b'16sH', self.devName.encode(), self._IFF_TAP | self._IFF_NO_PI
        )
        fcntl.ioctl(self._cloneDevice, self._TUNSETIFF, ifr)
        self.up()

    def delDevice(self):
        self._down()
        self._cloneDevice.close()

    def startListener(self, icmp):
        self._deviceListener = Process(
            target=_listenOnDevice, args=(self._cloneDevice.fileno(), icmp)
        )
        self._deviceListener.start()

    def isListenerAlive(self):
        if self._deviceListener:
            return self._deviceListener.is_alive()
        else:
            return False

    def stopListener(self):
        if self._deviceListener:
            os.kill(self._deviceListener.pid, signal.SIGKILL)
            self._deviceListener.join()

    def writeToDevice(self, icmp):
        os.write(self._cloneDevice.fileno(), icmp)


class Dummy(Interface):
    """
    Create a dummy interface with a pseudo-random suffix, e.g. dummy_ilXaYiSn7.
    Limit the name to 11 characters to make room for VLAN IDs. This assumes
    root privileges.
    """

    def __init__(self, prefix='dummy_', max_length=11):
        super(Dummy, self).__init__(prefix, max_length)

    def create(self):
        try:
            linkAdd(self.devName, linkType='dummy')
            if nmstate.is_nmstate_backend():
                cmd.exec_sync(
                    ['nmcli', 'dev', 'set', self.devName, 'managed', 'yes']
                )
        except IPRoute2Error as e:
            pytest.skip(
                'Failed to create a dummy interface %s: %s' % (self.devName, e)
            )
        else:
            return self.devName

    def remove(self):
        """
        Remove the dummy interface. This assumes root privileges.
        """
        try:
            linkDel(self.devName)
        except IPRoute2Error as e:
            pytest.skip(
                "Unable to delete the dummy interface %s: %s"
                % (self.devName, e)
            )
        finally:
            if nmstate.is_nmstate_backend():
                cmd.exec_sync(['nmcli', 'con', 'del', self.devName])

    def set_ip(self, ipaddr, netmask, family=4):
        try:
            addrAdd(self.devName, ipaddr, netmask, family)
        except IPRoute2Error as e:
            message = (
                'Failed to add the IPv%s address %s/%s to device %s: %s'
                % (family, ipaddr, netmask, self.devName, e)
            )
            if family == 6:
                message += (
                    "; NetworkManager may have set the sysctl "
                    "disable_ipv6 flag on the device, please see e.g. "
                    "RH BZ #1102064"
                )
            pytest.skip(message)


@contextmanager
def dummy_device(prefix='dummy_', max_length=11):
    dummy_interface = Dummy(prefix, max_length)
    dummy_name = dummy_interface.create()
    try:
        linkiface.iface(dummy_name).up()
        yield dummy_name
    finally:
        dummy_interface.remove()


@contextmanager
def dummy_devices(amount, prefix='dummy_', max_length=11):
    dummy_interfaces = [Dummy(prefix, max_length) for _ in range(amount)]
    created = []
    try:
        for iface in dummy_interfaces:
            iface.create()
            created.append(iface)
        yield [iface.devName for iface in created]
    finally:
        for iface in created:
            iface.remove()


@contextmanager
def bond_device(slaves, prefix='bond', max_length=11):
    check_sysfs_bond_permission()
    name = random_iface_name(prefix, max_length)
    with linkbond.Bond(name, slaves) as bond:
        bond.create()
        yield bond.master
    bond.destroy()


@contextmanager
def veth_pair(prefix='veth_', max_length=15):
    """
    Yield a pair of veth devices. This assumes root privileges (currently
    required by all tests anyway).

    Both sides of the pair have a pseudo-random suffix (e.g. veth_m6Lz7uMK9c).
    """
    left_side = random_iface_name(prefix, max_length)
    right_side = random_iface_name(prefix, max_length)
    try:
        linkAdd(left_side, linkType='veth', args=('peer', 'name', right_side))
        if nmstate.is_nmstate_backend():
            cmd.exec_sync(['nmcli', 'dev', 'set', left_side, 'managed', 'yes'])
            cmd.exec_sync(
                ['nmcli', 'dev', 'set', right_side, 'managed', 'yes']
            )
    except IPRoute2Error as e:
        pytest.skip('Failed to create a veth pair: %s', e)
    try:
        yield left_side, right_side
    finally:
        # the peer device is removed by the kernel
        linkDel(left_side)
        if nmstate.is_nmstate_backend():
            cmd.exec_sync(['nmcli', 'con', 'del', left_side])
            cmd.exec_sync(['nmcli', 'con', 'del', right_side])


@contextmanager
def enable_lldp_on_ifaces(ifaces, rx_only):
    for interface in ifaces:
        lldptool.enable_lldp_on_iface(interface, rx_only)
    # We must give a chance for the LLDP messages to be received.
    time.sleep(2)
    try:
        yield
    finally:
        for interface in ifaces:
            lldptool.disable_lldp_on_iface(interface)


def nm_is_running():
    return len(pgrep('NetworkManager')) > 0


@contextmanager
def dnsmasq_run(
    interface,
    dhcp_range_from=None,
    dhcp_range_to=None,
    dhcpv6_range_from=None,
    dhcpv6_range_to=None,
    router=None,
    ipv6_slaac_prefix=None,
):
    """Manages the life cycle of dnsmasq as a DHCP/RA server."""
    server = dhcp.Dnsmasq()
    server.start(
        interface,
        dhcp_range_from,
        dhcp_range_to,
        dhcpv6_range_from,
        dhcpv6_range_to,
        router,
        ipv6_slaac_prefix,
    )

    try:
        with firewall.allow_dhcp(interface):
            try:
                yield
            finally:
                server.stop()
    except firewall.FirewallError as e:
        pytest.skip('Failed to allow DHCP traffic in firewall: %s' % e)


@contextmanager
def wait_for_ipv6(iface, wait_for_scopes=None):
    """Wait for iface to get their IPv6 addresses with netlink Monitor"""
    logevents = []
    if not wait_for_scopes:
        wait_for_scopes = ['global', 'link']
    try:
        with monitor.Monitor(groups=('ipv6-ifaddr',), timeout=20) as mon:
            yield
            for event in mon:
                logevents.append(event)
                dev_name = event.get('label')
                if (
                    dev_name == iface
                    and event.get('event') == 'new_addr'
                    and event.get('scope') in wait_for_scopes
                ):

                    wait_for_scopes.remove(event.get('scope'))
                    if not wait_for_scopes:
                        return

    except monitor.MonitorError as e:
        if e.args[0] == monitor.E_TIMEOUT:
            raise Exception(
                'IPv6 addresses has not been caught within 20sec.\n'
                'Event log: {}\n'.format(logevents)
            )
        else:
            raise


@contextmanager
def dhclient_run(iface, family=4):
    dhclient.run(iface, family, blocking_dhcp=True)
    try:
        yield
    finally:
        dhclient.stop(iface, family)


@contextmanager
def dhcp_client_run(iface, family=4):
    dhcp_client = (
        dhcp_nm_client if nmstate.is_nmstate_backend() else dhclient_run
    )
    with dhcp_client(iface, family):
        yield


@contextmanager
def dhcp_nm_client(iface, family=4):
    cmd.exec_sync(
        [
            'nmcli',
            'con',
            'modify',
            iface,
            'ipv{}.method'.format(family),
            'auto',
        ]
    )
    cmd.exec_sync(['nmcli', 'con', 'up', iface])
    try:
        yield
    finally:
        cmd.exec_sync(
            [
                'nmcli',
                'con',
                'modify',
                iface,
                'ipv{}.method'.format(family),
                'disabled',
            ]
        )
        cmd.exec_sync(['nmcli', 'con', 'up', iface])


@contextmanager
def restore_resolv_conf():
    RESOLV_CONF = '/etc/resolv.conf'
    RESOLV_CONF_BACKUP = '/etc/resolv.conf.test-backup'
    shutil.copy2(RESOLV_CONF, RESOLV_CONF_BACKUP)
    try:
        yield
    finally:
        shutil.copy2(RESOLV_CONF_BACKUP, RESOLV_CONF)


def check_sysfs_bond_permission():
    if not has_sysfs_bond_permission():
        pytest.skip('This test requires sysfs bond write access')


@contextmanager
def preserve_default_route():
    ipv4_dg_data = routes.getDefaultGateway()
    ipv4_gateway = ipv4_dg_data.via if ipv4_dg_data else None
    ipv4_device = ipv4_dg_data.device if ipv4_dg_data else None

    ipv6_dg_data = routes.ipv6_default_gateway()
    ipv6_gateway = ipv6_dg_data.via if ipv6_dg_data else None
    ipv6_device = ipv6_dg_data.device if ipv6_dg_data else None

    try:
        yield
    finally:
        if ipv4_gateway and not routes.is_default_route(
            ipv4_gateway, routes.get_routes()
        ):
            address.set_default_route(ipv4_gateway, family=4, dev=ipv4_device)
        if ipv6_gateway and not routes.is_ipv6_default_route(ipv6_gateway):
            address.set_default_route(ipv6_gateway, family=6, dev=ipv6_device)


@contextmanager
def running(runnable):
    runnable.start()
    try:
        yield runnable
    finally:
        runnable.stop()


@memoized
def has_sysfs_bond_permission():
    BondSysFS = linkbond.sysfs_driver.BondSysFS
    bond = BondSysFS(random_iface_name('check_', max_length=11))
    try:
        bond.create()
        bond.destroy()
    except IOError:
        return False
    return True


class KernelModule(object):
    SYSFS_MODULE_PATH = '/sys/module'
    CMD_MODPROBE = 'modprobe'

    def __init__(self, name):
        self._name = name

    def exists(self):
        return os.path.exists(
            os.path.join(KernelModule.SYSFS_MODULE_PATH, self._name)
        )

    def load(self):
        if not self.exists():
            ret, out, err = cmd.exec_sync(
                [KernelModule.CMD_MODPROBE, self._name]
            )
            if ret != 0:
                logging.warning(
                    'Unable to load %s module, out=%s, err=%s',
                    self._name,
                    out,
                    err,
                )


def running_on_centos():
    with open('/etc/redhat-release') as f:
        return 'CentOS Linux release' in f.readline()


def running_on_fedora(ver=''):
    with open('/etc/redhat-release') as f:
        return 'Fedora release {}'.format(ver) in f.readline()


def running_on_travis_ci():
    return 'TRAVIS_CI' in os.environ
