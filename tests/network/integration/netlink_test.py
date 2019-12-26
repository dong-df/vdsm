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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import
from __future__ import division

from collections import deque
import os
import threading
import time

import pytest

from vdsm.common.time import monotonic_time

from ..nettestlib import Dummy
from vdsm.network.netlink import NLSocketPool
from vdsm.network.netlink import monitor
from vdsm.network.sysctl import is_disabled_ipv6

IP_ADDRESS = '192.0.2.1'
IP_CIDR = '24'


running_on_ovirt_ci = 'OVIRT_CI' in os.environ


class TestNetlinkEventMonitor(object):

    TIMEOUT = 5

    def test_iterate_after_events(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy = Dummy()
            dummy_name = dummy.create()
            dummy.remove()
            for event in mon:
                if event.get('name') == dummy_name:
                    break

    def test_iterate_while_events(self):
        """Tests if monitor is able to catch event while iterating. Before the
        iteration we start _set_and_remove_device, which is delayed for .2
        seconds. Then iteration starts and wait for new dummy.
        """
        dummy = Dummy()
        dummy_name = dummy.create()

        def _set_and_remove_device():
            time.sleep(0.2)
            dummy.up()
            dummy.remove()

        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            add_device_thread = _start_thread(_set_and_remove_device)
            for event in mon:
                if event.get('name') == dummy_name:
                    break
            add_device_thread.join()

    def test_stopped(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy = Dummy()
            dummy_name = dummy.create()
            dummy.remove()

        found = any(event.get('name') == dummy_name for event in mon)
        assert found, 'Expected event was not caught.'

    def test_event_groups(self):
        with monitor.Monitor(
            timeout=self.TIMEOUT, groups=('ipv4-ifaddr',)
        ) as mon_a:
            with monitor.Monitor(
                timeout=self.TIMEOUT, groups=('link', 'ipv4-route')
            ) as mon_l_r:
                dummy = Dummy()
                dummy.create()
                dummy.set_ip(IP_ADDRESS, IP_CIDR)
                dummy.up()
                dummy.remove()

        for event in mon_a:
            assert '_addr' in event['event'], (
                "Caught event '%s' is not "
                "related to address." % event['event']
            )

        for event in mon_l_r:
            link_or_route = (
                '_link' in event['event'] or '_route' in event['event']
            )
            assert link_or_route, (
                "Caught event '%s' is not related "
                "to link or route." % event['event']
            )

    def test_iteration(self):
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            iterator = iter(mon)

            # Generate events to avoid blocking
            dummy = Dummy()
            dummy.create()
            next(iterator)

            dummy.remove()
            next(iterator)

        with pytest.raises(StopIteration):
            while True:
                next(iterator)

    @pytest.mark.xfail(
        condition=running_on_ovirt_ci,
        raises=AssertionError,
        reason='Sometimes we miss some events on CI',
        strict=False,
    )
    def test_events_keys(self):
        def _simplify_event(event):
            """ Strips event keys except event, address, name, destination,
            family.
            """
            allow = set(['event', 'address', 'name', 'destination', 'family'])
            return {k: v for (k, v) in event.items() if k in allow}

        def _expected_events(nic, address, cidr):
            events_add = [
                {'event': 'new_link', 'name': nic},
                {'event': 'new_addr', 'address': address + '/' + cidr},
                {'event': 'new_link', 'name': nic},
            ]
            events_del = [
                {'address': address + '/' + cidr, 'event': 'del_addr'},
                {'destination': address, 'event': 'del_route'},
                {'event': 'del_link', 'name': nic},
            ]
            events_ipv6 = [
                {'event': 'new_addr', 'family': 'inet6'},
                {'event': 'del_addr', 'family': 'inet6'},
            ]
            if is_disabled_ipv6():
                return deque(events_add + events_del)
            else:
                return deque(events_add + events_ipv6 + events_del)

        with monitor.Monitor(timeout=self.TIMEOUT, silent_timeout=True) as mon:
            dummy = Dummy()
            dummy_name = dummy.create()
            dummy.set_ip(IP_ADDRESS, IP_CIDR)
            dummy.up()
            dummy.remove()

            expected_events = _expected_events(dummy_name, IP_ADDRESS, IP_CIDR)
            _expected = list(expected_events)
            _caught = []

            expected = expected_events.popleft()
            for event in mon:
                _caught.append(event)
                if _is_subdict(expected, event):
                    expected = expected_events.popleft()
                    if len(expected_events) == 0:
                        break

        assert 0 == len(expected_events), (
            'Expected events have not '
            'been caught (in the right order).\n'
            'Expected:\n%s.\nCaught:\n%s.'
            % (
                '\n'.join([str(d) for d in _expected]),
                '\n'.join([str(_simplify_event(d)) for d in _caught]),
            ),
        )

    def test_timeout(self):
        with pytest.raises(monitor.MonitorError):
            try:
                with monitor.Monitor(timeout=0.01) as mon:
                    for event in mon:
                        pass
            except monitor.MonitorError as e:
                assert e.args[0] == monitor.E_TIMEOUT
                raise

        assert mon.is_stopped()

    def test_timeout_silent(self):
        with monitor.Monitor(timeout=0.01, silent_timeout=True) as mon:
            for event in mon:
                pass

        assert mon.is_stopped()

    def test_timeout_not_triggered(self):
        time_start = monotonic_time()
        with monitor.Monitor(timeout=self.TIMEOUT) as mon:
            dummy = Dummy()
            dummy.create()
            dummy.remove()

            for event in mon:
                break

        assert (monotonic_time() - time_start) <= self.TIMEOUT
        assert mon.is_stopped()

    def test_passing_invalid_groups(self):
        with pytest.raises(AttributeError):
            monitor.Monitor(groups=('blablabla',))
        monitor.Monitor(groups=('link',))


class TestSocketPool(object):
    def test_reuse_socket_per_thread(self):
        # The same thread should always get the same socket. Otherwise any
        # recusion in the code will lead to a deadlock.
        pool = NLSocketPool(3)
        with pool.socket() as s1:
            with pool.socket() as s2:
                assert s1 is s2


def _start_thread(func, *args, **kwargs):
    t = threading.Thread(target=func, args=args, kwargs=kwargs)
    t.daemon = True
    t.start()
    return t


def _is_subdict(subset, superset):
    return all(item in superset.items() for item in subset.items())
