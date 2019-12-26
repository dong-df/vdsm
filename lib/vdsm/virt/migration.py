#
# Copyright 2008-2019 Red Hat, Inc.
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

import io
import collections
import re
import threading
import time
import libvirt

from vdsm.common import concurrent
from vdsm.common import conv
from vdsm.common import logutils
from vdsm.common import response
from vdsm import sslutils
from vdsm import utils
from vdsm import jsonrpcvdscli
from vdsm.config import config
from vdsm.common import xmlutils
from vdsm.common.compat import pickle
from vdsm.common.define import NORMAL, Mbytes
from vdsm.common.network.address import normalize_literal_addr
from vdsm.virt.utils import DynamicBoundedSemaphore

from vdsm.virt import virdomain
from vdsm.virt import vmexitreason
from vdsm.virt import vmstatus


MODE_REMOTE = 'remote'
MODE_FILE = 'file'


METHOD_ONLINE = 'online'


incomingMigrations = DynamicBoundedSemaphore(
    max(1, config.getint('vars', 'max_incoming_migrations')))


CONVERGENCE_SCHEDULE_SET_DOWNTIME = "setDowntime"
CONVERGENCE_SCHEDULE_POST_COPY = "postcopy"
CONVERGENCE_SCHEDULE_SET_ABORT = "abort"


_MiB_IN_GiB = 1024


ADDRESS = '0'
PORT = 54321


class MigrationDestinationSetupError(RuntimeError):
    """
    Failed to create migration destination VM.
    """


class MigrationLimitExceeded(RuntimeError):
    """
    Cannot migrate right now: no resources on destination.
    """


class PostCopyPhase:
    NONE = 0
    REQUESTED = 1
    RUNNING = 2


class SourceThread(object):
    """
    A thread that takes care of migration on the source vdsm.
    """
    _RECOVERY_LOOP_PAUSE = 10

    ongoingMigrations = DynamicBoundedSemaphore(1)

    def __init__(self, vm, dst='', dstparams='',
                 mode=MODE_REMOTE, method=METHOD_ONLINE,
                 tunneled=False, dstqemu='', abortOnError=False,
                 consoleAddress=None, compressed=False,
                 autoConverge=False, recovery=False, encrypted=False,
                 **kwargs):
        self.log = vm.log
        self._vm = vm
        self._dst = dst
        self._mode = mode
        self._dstparams = dstparams
        self._enableGuestEvents = kwargs.get('enableGuestEvents', False)
        # TODO: conv.tobool shouldn't be used in this constructor, the
        # conversions should be handled properly in the API layer
        self._consoleAddress = consoleAddress
        self._dstqemu = dstqemu
        self._encrypted = encrypted
        self._maxBandwidth = int(
            kwargs.get('maxBandwidth') or
            config.getint('vars', 'migration_max_bandwidth')
        )
        self._incomingLimit = kwargs.get('incomingLimit')
        self._outgoingLimit = kwargs.get('outgoingLimit')
        self.status = {
            'status': {
                'code': 0,
                'message': 'Migration in progress'}}
        # we need to guard against concurrent updates only
        self._lock = threading.Lock()
        self._progress = 0
        self._thread = concurrent.thread(
            self.run, name='migsrc/' + self._vm.id[:8])
        self._preparingMigrationEvt = True
        self._migrationCanceledEvt = threading.Event()
        self._monitorThread = None
        self._destServer = None
        self._legacy_payload_path = None
        if 'convergenceSchedule' in kwargs:
            self._convergence_schedule = kwargs['convergenceSchedule']
        else:
            # Needed for Engine < 4.3 or when legacy migration is used
            # as a supposedly rare fallback in Engine >= 4.3.
            self._convergence_schedule = \
                self._legacy_convergence_schedule(kwargs.get('downtime'))
            self.log.info('using a computed convergence schedule for '
                          'a legacy migration: %s',
                          self._convergence_schedule)
        self.log.debug('convergence schedule set to: %s',
                       str(self._convergence_schedule))
        self._started = False
        self._failed = False
        self._recovery = recovery
        tunneled = conv.tobool(tunneled)
        abortOnError = conv.tobool(abortOnError)
        compressed = conv.tobool(compressed)
        autoConverge = conv.tobool(autoConverge)
        self._migration_flags = self._calculate_migration_flags(
            tunneled, abortOnError, compressed, autoConverge, encrypted
        )

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def migrating(self):
        """
        Return whether the thread currently manages a migration.

        That can be a migration directly supervised by the source thread and
        other threads (such as the downtime thread) or just an indirectly
        managed migration (detected on Vdsm recovery) without the threads
        actually running.
        """
        return ((self.is_alive() and not self._failed) or
                (self._recovery and
                 self._vm.lastStatus == vmstatus.MIGRATION_SOURCE))

    @property
    def started(self):
        return self._started

    @property
    def hibernating(self):
        return self._mode == MODE_FILE

    def _update_progress(self):
        if self._monitorThread is None:
            return

        # fetch migration status from the monitor thread
        if self._monitorThread.progress is not None:
            progress = self._monitorThread.progress.percentage
        else:
            progress = 0

        with self._lock:
            old_progress = self._progress
            if progress >= old_progress:
                self._progress = progress

        if progress < old_progress:
            self.log.info(
                'new computed progress %d < than old value %d, discarded',
                progress, old_progress)

    def getStat(self):
        """
        Get the status of the migration.
        """
        self._update_progress()
        self.status['progress'] = self._progress
        return self.status

    def _createClient(self, port):
        sslctx = sslutils.create_ssl_context()

        def is_ipv6_address(a):
            return (':' in a) and a.startswith('[') and a.endswith(']')

        if is_ipv6_address(self.remoteHost):
            host = self.remoteHost[1:-1]
        else:
            host = self.remoteHost

        client_socket = utils.create_connected_socket(host, int(port), sslctx)
        return self._vm.cif.createStompClient(client_socket)

    def _setupVdsConnection(self):
        if self.hibernating:
            return

        hostPort = _cannonize_host_port(
            self._dst,
            config.getint('addresses', 'management_port'))
        self.remoteHost, port = hostPort.rsplit(':', 1)

        client = self._createClient(port)
        requestQueues = config.get('addresses', 'request_queues')
        requestQueue = requestQueues.split(",")[0]
        self._destServer = jsonrpcvdscli.connect(requestQueue, client)
        self.log.debug('Initiating connection with destination')
        self._destServer.ping()

        self.log.debug('Destination server is: ' + hostPort)

    def _setupRemoteMachineParams(self):
        machineParams = self._vm.migration_parameters()
        machineParams['enableGuestEvents'] = self._enableGuestEvents
        if not self.hibernating:
            machineParams['migrationDest'] = 'libvirt'
        return machineParams

    def _prepareGuest(self):
        if self.hibernating:
            self.log.debug("Save State begins")
            if self._vm.guestAgent.isResponsive():
                lockTimeout = 30
            else:
                lockTimeout = 0
            self._vm.guestAgent.desktopLock()
            # wait for lock or timeout
            while lockTimeout:
                if self._vm.getStats()['session'] in ["Locked", "LoggedOff"]:
                    break
                time.sleep(1)
                lockTimeout -= 1
                if lockTimeout == 0:
                    self.log.warning('Agent ' + self._vm.id +
                                     ' unresponsive. Hiberanting without '
                                     'desktopLock.')
                    break
            self._vm.pause(vmstatus.SAVING_STATE)
        else:
            self.log.debug("Migration started")
            self._vm.lastStatus = vmstatus.MIGRATION_SOURCE

    def _recover(self, message):
        if not response.is_error(self.status):
            self.status = response.error('migrateErr')
        self.log.error(message)
        if not self.hibernating and self._destServer is not None:
            if self._vm.post_copy == PostCopyPhase.RUNNING:
                # We can't recover a VM after a failed post-copy migration.
                # And the destination takes care of the situation itself.
                self._vm.handle_failed_post_copy(clean_vm=True)
                return
            try:
                self._destServer.destroy(self._vm.id)
            except Exception:
                self.log.exception("Failed to destroy remote VM")
        # if the guest was stopped before migration, we need to cont it
        if self.hibernating:
            self._vm.cont(ignoreStatus=True)
            if self._enableGuestEvents:
                self._vm.guestAgent.events.after_hibernation_failure()
        elif self._enableGuestEvents:
            self._vm.guestAgent.events.after_migration_failure()
        # either way, migration has finished
        self._failed = True
        if self._recovery:
            self._vm.set_last_status(vmstatus.UP, vmstatus.MIGRATION_SOURCE)
            self._recovery = False
        else:
            self._vm.lastStatus = vmstatus.UP
        self._started = False
        self._vm.send_status_event()

    def _finishSuccessfully(self, machineParams):
        with self._lock:
            self._progress = 100
        if not self.hibernating:
            # TODO: We could use a timeout on the wait to be more robust
            # against "impossible" failures. But we don't have a good value to
            # use here now.
            self._vm.stopped_migrated_event_processed.wait()
            self._vm.setDownStatus(NORMAL, vmexitreason.MIGRATION_SUCCEEDED)
            self.status['status']['message'] = 'Migration done'
            if self._vm.post_copy == PostCopyPhase.RUNNING:
                self._vm.destroy()
        else:
            # don't pickle transient params
            for ignoreParam in ('displayIp', 'display', 'pid'):
                if ignoreParam in machineParams:
                    del machineParams[ignoreParam]

            fname = self._vm.cif.prepareVolumePath(self._dstparams)
            try:
                # Use r+ to avoid truncating the file, see BZ#1282239
                with io.open(fname, "r+b") as f:
                    # protocol=2 is needed for clusters < 4.4
                    # (for Python 2 host compatibility)
                    pickle.dump(machineParams, f, protocol=2)
            finally:
                self._vm.cif.teardownVolumePath(self._dstparams)

            self._vm.setDownStatus(NORMAL, vmexitreason.SAVE_STATE_SUCCEEDED)
            self.status['status']['message'] = 'SaveState done'

    @staticmethod
    def _raiseAbortError():
        e = libvirt.libvirtError(defmsg='')
        # we have to override the value to get what we want
        # err might be None
        e.err = (libvirt.VIR_ERR_OPERATION_ABORTED,  # error code
                 libvirt.VIR_FROM_QEMU,              # error domain
                 'operation aborted',                # error message
                 libvirt.VIR_ERR_WARNING,            # error level
                 '', '', '',                         # str1, str2, str3,
                 -1, -1)                             # int1, int2
        raise e

    def _update_outgoing_limit(self):
        if self._outgoingLimit:
            self.log.debug('Setting outgoing migration limit to %s',
                           self._outgoingLimit)
            SourceThread.ongoingMigrations.bound = self._outgoingLimit

    @property
    def recovery(self):
        """
        Return whether the source thread handles a recovered migration.

        This is when we detect the VM is migrating in Vdsm recovery and the
        source thread is not actually running.

        This serves to handle a possible already running migration detected
        during Vdsm recovery, for which no regular source thread exists.  We
        don't try to touch such a migration, but we still must ensure at least
        basic sanity:

        - Indication that the migration is running.
        - Canceling the migration.
        - Putting the VM into proper status after migration failure (in case
          the migration succeeds, we rely on the fact that the VM disappears
          and Vdsm detects that sooner or later).

        .. note::

           Just setting this flag doesn't mean that any migration is actually
           running, it just means that if a migration is running then the
           migration was started by another Vdsm instance.  When this flag is
           set then the VM may be actually migrating only if its status is
           `vmstatus.MIGRATION_SOURCE` or `vmstatus.WAIT_FOR_LAUNCH` (the
           latter is mostly irrelevant since we prevent most actions in that
           status).
        """
        return self._recovery

    def run(self):
        if self.recovery:
            self._recovery_run()
        else:
            self._regular_run()

    def _regular_run(self):
        self.log.debug("Starting migration source thread")
        self._recovery = False
        self._update_outgoing_limit()
        try:
            startTime = time.time()
            machineParams = self._setupRemoteMachineParams()
            self._setupVdsConnection()
            self._prepareGuest()

            while not self._started:
                try:
                    self.log.info("Migration semaphore: acquiring")
                    with SourceThread.ongoingMigrations:
                        self.log.info("Migration semaphore: acquired")
                        timeout = config.getint(
                            'vars', 'guest_lifecycle_event_reply_timeout')
                        if self.hibernating:
                            self._vm.guestAgent.events.before_hibernation(
                                wait_timeout=timeout)
                        elif self._enableGuestEvents:
                            self._vm.guestAgent.events.before_migration(
                                wait_timeout=timeout)
                        if self._migrationCanceledEvt.is_set():
                            self._raiseAbortError()
                        self.log.debug("migration semaphore acquired "
                                       "after %d seconds",
                                       time.time() - startTime)
                        self._startUnderlyingMigration(
                            time.time(), machineParams
                        )
                        self._finishSuccessfully(machineParams)
                except libvirt.libvirtError as e:
                    if e.get_error_code() == libvirt.VIR_ERR_OPERATION_ABORTED:
                        self.status = response.error(
                            'migCancelErr', message='Migration canceled')
                    raise
                except MigrationLimitExceeded:
                    retry_timeout = config.getint('vars',
                                                  'migration_retry_timeout')
                    self.log.debug("Migration destination busy. Initiating "
                                   "retry in %d seconds.", retry_timeout)
                    self._migrationCanceledEvt.wait(retry_timeout)
        except MigrationDestinationSetupError as e:
            self._recover(str(e))
            # we know what happened, no need to dump hollow stack trace
        except Exception as e:
            self._recover(str(e))
            self.log.exception("Failed to migrate")

    def _startUnderlyingMigration(self, startTime, machineParams):
        if self.hibernating:
            self._started = True
            self._vm.hibernate(self._dst)
        else:
            self._vm.prepare_migration()

            # Do not measure the time spent for creating the VM on the
            # destination. In some cases some expensive operations can cause
            # the migration to get cancelled right after the transfer started.
            destCreateStartTime = time.time()
            result = self._destServer.migrationCreate(machineParams,
                                                      self._incomingLimit)
            destCreationTime = time.time() - destCreateStartTime
            startTime += destCreationTime
            self.log.info('Creation of destination VM took: %d seconds',
                          destCreationTime)

            if response.is_error(result):
                self.status = result
                if response.is_error(result, 'migrateLimit'):
                    raise MigrationLimitExceeded()
                else:
                    raise MigrationDestinationSetupError(
                        'migration destination error: ' +
                        result['status']['message'])

            self._started = True

            # REQUIRED_FOR: destination Vdsm < 4.3
            if not self._vm.min_cluster_version(4, 3):
                payload_drives = self._vm.payload_drives()
                if payload_drives:
                    # Currently, only a single payload device may be present
                    payload_alias = payload_drives[0].alias
                    result = self._destServer.fullList(
                        vmList=(self._vm.id,)
                    )
                    vm_list = result.get('items')
                    remote_devices = vm_list[0].get('devices')
                    if remote_devices is not None:
                        payload_path = next(
                            (d['path'] for d in remote_devices
                             if d.get('alias') == payload_alias),
                            None
                        )
                        if payload_path is not None:
                            self._legacy_payload_path = \
                                (payload_alias, payload_path)

            if config.getboolean('vars', 'ssl'):
                transport = 'tls'
            else:
                transport = 'tcp'
            duri = 'qemu+{}://{}/system'.format(
                transport, normalize_literal_addr(self.remoteHost))

            if self._encrypted:
                # TODO: Stop using host names here and set the host
                # name based certificate verification parameter once
                # the corresponding functionality is available in
                # libvirt, see https://bugzilla.redhat.com/1754533
                #
                # When an encrypted migration is requested, we must
                # use the host name (stored in 'dst') rather than the
                # IP address (stored in 'dstqemu') in order to match
                # the target certificate.  That means that encrypted
                # migrations are incompatible with setups that require
                # an IP address to identify the host properly, such as
                # when a separate migration network should be used or
                # when using IPv4/IPv6 dual stack configurations.
                dstqemu = self.remoteHost
            else:
                dstqemu = self._dstqemu
            if dstqemu:
                muri = 'tcp://{}'.format(
                    normalize_literal_addr(dstqemu))
            else:
                muri = 'tcp://{}'.format(
                    normalize_literal_addr(self.remoteHost))

            self._vm.log.info('starting migration to %s '
                              'with miguri %s', duri, muri)
            self._monitorThread = MonitorThread(self._vm, startTime,
                                                self._convergence_schedule)
            self._perform_with_conv_schedule(duri, muri)
            self.log.info("migration took %d seconds to complete",
                          (time.time() - startTime) + destCreationTime)

    def _perform_migration(self, duri, muri):
        if self._vm.hasSpice and self._vm.conf.get('clientIp'):
            SPICE_MIGRATION_HANDOVER_TIME = 120
            self._vm._reviveTicket(SPICE_MIGRATION_HANDOVER_TIME)

        # FIXME: there still a race here with libvirt,
        # if we call stop() and libvirt migrateToURI3 didn't start
        # we may return migration stop but it will start at libvirt
        # side
        self._preparingMigrationEvt = False
        if not self._migrationCanceledEvt.is_set():
            self._vm._dom.migrateToURI3(duri,
                                        self._migration_params(muri),
                                        self._migration_flags)
        else:
            self._raiseAbortError()

    def _migration_params(self, muri):
        params = {libvirt.VIR_MIGRATE_PARAM_BANDWIDTH: self._maxBandwidth}
        if not self.tunneled:
            params[libvirt.VIR_MIGRATE_PARAM_URI] = str(muri)
        if self._consoleAddress:
            graphics = 'spice' if self._vm.hasSpice else 'vnc'
            params[libvirt.VIR_MIGRATE_PARAM_GRAPHICS_URI] = str(
                '%s://%s' % (graphics, self._consoleAddress)
            )
        # REQUIRED_FOR: destination Vdsm < 4.3
        if self._legacy_payload_path is not None:
            alias, path = self._legacy_payload_path
            dom = xmlutils.fromstring(self._vm.migratable_domain_xml())
            source = dom.find(".//alias[@name='%s']/../source" % (alias,))
            source.set('file', path)
            xml = xmlutils.tostring(dom)
            self._vm.log.debug("Migrating domain XML: %s", xml)
            params[libvirt.VIR_MIGRATE_PARAM_DEST_XML] = xml
        return params

    @property
    def tunneled(self):
        return self.migration_flags & libvirt.VIR_MIGRATE_TUNNELLED

    @property
    def migration_flags(self):
        return self._migration_flags

    def _calculate_migration_flags(self, tunneled, abort_on_error,
                                   compressed, auto_converge, encrypted):
        flags = libvirt.VIR_MIGRATE_LIVE | libvirt.VIR_MIGRATE_PEER2PEER
        if tunneled:
            flags |= libvirt.VIR_MIGRATE_TUNNELLED
        if abort_on_error:
            flags |= libvirt.VIR_MIGRATE_ABORT_ON_ERROR
        if compressed:
            flags |= libvirt.VIR_MIGRATE_COMPRESSED
        if auto_converge:
            flags |= libvirt.VIR_MIGRATE_AUTO_CONVERGE
        if encrypted:
            flags |= libvirt.VIR_MIGRATE_TLS
        if self._vm.min_cluster_version(4, 2):
            flags |= libvirt.VIR_MIGRATE_PERSIST_DEST
        # Migration may fail immediately when VIR_MIGRATE_POSTCOPY flag is
        # present in the following situations:
        # - The transport is not capable of full bidirectional
        #   connectivity: RDMA, tunnelled, pipe.
        # - Huge pages are used (doesn't apply to transparent huge pages).
        # - QEMU uses a file as a backing for memory.
        # - Perhaps non-shared block storage may cause some trouble.
        for stalling in self._convergence_schedule.get('stalling', []):
            action = stalling.get('action', {}).get('name')
            if action == CONVERGENCE_SCHEDULE_POST_COPY:
                flags |= libvirt.VIR_MIGRATE_POSTCOPY
                break
        return flags

    def _perform_with_conv_schedule(self, duri, muri):
        self._vm.log.debug('performing migration with conv schedule')
        with utils.running(self._monitorThread):
            self._perform_migration(duri, muri)
        self._monitorThread.join()

    def _legacy_convergence_schedule(self, max_downtime):
        # Simplified emulation of legacy non-scheduled migrations.
        if max_downtime is None:
            max_downtime = config.get('vars', 'migration_downtime')
        max_downtime = int(max_downtime)
        max_steps = config.getint('vars', 'migration_downtime_steps')
        downtimes = exponential_downtime(max_downtime, max_steps)

        def downtime_action(downtime):
            return {'params': [str(downtime)], 'name': 'setDowntime'}
        init = [downtime_action(next(downtimes))]
        stalling = []
        limit = 1
        for d in downtimes:
            stalling.append({'action': downtime_action(d), 'limit': limit})
            limit += 1
        stalling.append({'action': downtime_action(d), 'limit': 42})
        stalling.append({'action': {'params': [], 'name': 'abort'},
                         'limit': -1})
        return {'init': init, 'stalling': stalling}

    def set_max_bandwidth(self, bandwidth):
        self._vm.log.debug('setting migration max bandwidth to %d', bandwidth)
        self._maxBandwidth = bandwidth
        self._vm._dom.migrateSetMaxSpeed(bandwidth)

    def stop(self):
        # if its locks we are before the migrateToURI3()
        # call so no need to abortJob()
        try:
            self._migrationCanceledEvt.set()
            self._vm._dom.abortJob()
        except libvirt.libvirtError:
            if not self._preparingMigrationEvt:
                raise
        if self._recovery:
            self._recover("Migration stopped")

    def _recovery_run(self):
        self.log.debug("Starting migration recovery thread")
        while True:
            job_stats = self._vm._dom.jobStats()
            if not ongoing(job_stats):
                break
            time.sleep(self._RECOVERY_LOOP_PAUSE)
        self.log.debug("Recovered migration finished")
        # Successful migration is handled in VM.onJobCompleted, here we need
        # just to ensure that migration failures are detected and handled.
        if self._vm._dom.state(0)[0] == libvirt.VIR_DOMAIN_RUNNING:
            self.recovery_cleanup()

    def recovery_cleanup(self):
        """
        Finish and cleanup recovery migration if necessary.

        This is to handle the situation when we detect a failed migration
        outside the source thread.  The source thread usually handles failed
        migrations itself.  But the thread is not running after recovery so in
        such a case the source thread must be notified about the failed
        migration.  This is what this method serves for.
        """
        if self._recovery and \
           self._vm.lastStatus == vmstatus.MIGRATION_SOURCE:
            self._recover("Migration failed")


def exponential_downtime(downtime, steps):
    if steps > 1:
        offset = downtime / float(steps)
        base = (downtime - offset) ** (1 / float(steps - 1))

        for i in range(steps):
            yield int(offset + base ** i)
    else:
        yield downtime


class MonitorThread(object):
    _MIGRATION_MONITOR_INTERVAL = config.getint(
        'vars', 'migration_monitor_interval')  # seconds

    def __init__(self, vm, startTime, conv_schedule):
        super(MonitorThread, self).__init__()
        self._stop = threading.Event()
        self._vm = vm
        self._startTime = startTime
        self.daemon = True
        self.progress = None
        self._conv_schedule = conv_schedule
        self._thread = concurrent.thread(
            self.run, name='migmon/' + self._vm.id[:8])

    def start(self):
        self._thread.start()

    def join(self):
        self._thread.join()

    @property
    def enabled(self):
        return MonitorThread._MIGRATION_MONITOR_INTERVAL > 0

    @logutils.traceback()
    def run(self):
        if self.enabled:
            self._vm.log.debug('starting migration monitor thread')
            try:
                self.monitor_migration()
            except virdomain.NotConnectedError as e:
                # In case the VM is stopped during migration, there is a race
                # between domain disconnection and stopping the monitoring
                # thread. Then the domain may no longer be connected when
                # monitor_migration loop tries to access it. That's harmless
                # and shouldn't bubble up, let's just finish the thread.
                self._vm.log.debug('domain disconnected in monitor thread: %s',
                                   e)
            self._vm.log.debug('stopped migration monitor thread')
        else:
            self._vm.log.info('migration monitor thread disabled'
                              ' (monitoring interval set to 0)')

    def monitor_migration(self):
        lowmark = None
        initial_iteration = last_iteration = None

        self._execute_init(self._conv_schedule['init'])

        while not self._stop.isSet():
            stopped = self._stop.wait(self._MIGRATION_MONITOR_INTERVAL)
            if stopped:
                break

            job_stats = self._vm._dom.jobStats()
            # It may happen that the migration did not start yet
            # so we'll keep waiting
            if not ongoing(job_stats):
                continue

            progress = Progress.from_job_stats(job_stats)
            if initial_iteration is None:
                # The initial iteration number from libvirt is not
                # fixed, since it may include iterations from
                # previously cancelled migrations.
                initial_iteration = last_iteration = progress.mem_iteration

            self._vm.send_migration_status_event()

            if self._vm.post_copy != PostCopyPhase.NONE:
                # Post-copy mode is a final state of a migration -- it either
                # completes or fails and stops the VM, there is no way to
                # continue with the migration in either case.  So we won't
                # handle any further schedule actions once post-copy is
                # successfully started.  It's still recommended to put the
                # abort action after the post-copy action in the schedule, for
                # the case when it's not possible to switch to the post-copy
                # mode for some reason.
                if self._vm.post_copy == PostCopyPhase.RUNNING:
                    # If post-copy is not RUNNING then we are in the interim
                    # phase (which should be short) between initiating the
                    # post-copy migration and the actual start of the post-copy
                    # migration.  Nothing needs to be done in that case.
                    self._vm.log.debug(
                        'Post-copy migration still in progress: %d',
                        progress.data_remaining
                    )
            elif (lowmark is None) or (lowmark > progress.data_remaining):
                lowmark = progress.data_remaining
            else:
                self._vm.log.warn(
                    'Migration stalling: remaining (%sMiB)'
                    ' > lowmark (%sMiB).',
                    progress.data_remaining // Mbytes, lowmark // Mbytes)

            if not self._vm.post_copy and\
               progress.mem_iteration > last_iteration:
                last_iteration = progress.mem_iteration
                current_iteration = last_iteration - initial_iteration
                self._vm.log.debug('new iteration: %i', current_iteration)
                self._next_action(current_iteration)

            if self._stop.isSet():
                break

            self.progress = progress
            self._vm.log.info('%s', progress)

    def stop(self):
        self._vm.log.debug('stopping migration monitor thread')
        self._stop.set()

    def _next_action(self, stalling):
        head = self._conv_schedule['stalling'][0]

        self._vm.log.debug('Stalling for %d iterations, '
                           'checking to make next action: '
                           '%s', stalling, head)
        if head['limit'] < stalling:
            self._execute_action_with_params(head['action'])
            self._conv_schedule['stalling'].pop(0)
            self._vm.log.debug('setting conv schedule to: %s',
                               self._conv_schedule)

    def _execute_init(self, init_actions):
        for action_with_params in init_actions:
            self._execute_action_with_params(action_with_params)

    def _execute_action_with_params(self, action_with_params):
        action = str(action_with_params['name'])
        vm = self._vm
        if action == CONVERGENCE_SCHEDULE_SET_DOWNTIME:
            downtime = int(action_with_params['params'][0])
            vm.log.debug('Setting downtime to %d', downtime)
            vm._dom.migrateSetMaxDowntime(downtime, 0)
        elif action == CONVERGENCE_SCHEDULE_POST_COPY:
            if not self._vm.switch_migration_to_post_copy():
                # Do nothing for now; the next action will be invoked after a
                # while
                vm.log.warning('Failed to switch to post-copy migration')
        elif action == CONVERGENCE_SCHEDULE_SET_ABORT:
            vm.log.warning('Aborting migration')
            vm._dom.abortJob()
            self.stop()


_Progress = collections.namedtuple('_Progress', [
    'job_type', 'time_elapsed', 'data_total',
    'data_processed', 'data_remaining',
    'mem_total', 'mem_processed', 'mem_remaining',
    'mem_bps', 'mem_constant', 'compression_bytes',
    'dirty_rate', 'mem_iteration'
])


class Progress(_Progress):
    __slots__ = ()

    @classmethod
    def from_job_stats(cls, stats):
        return cls(
            stats['type'],
            stats[libvirt.VIR_DOMAIN_JOB_TIME_ELAPSED],
            stats[libvirt.VIR_DOMAIN_JOB_DATA_TOTAL],
            stats[libvirt.VIR_DOMAIN_JOB_DATA_PROCESSED],
            stats[libvirt.VIR_DOMAIN_JOB_DATA_REMAINING],
            stats[libvirt.VIR_DOMAIN_JOB_MEMORY_TOTAL],
            stats[libvirt.VIR_DOMAIN_JOB_MEMORY_PROCESSED],
            stats[libvirt.VIR_DOMAIN_JOB_MEMORY_REMAINING],
            stats.get(libvirt.VIR_DOMAIN_JOB_MEMORY_BPS, 0),
            stats.get(libvirt.VIR_DOMAIN_JOB_MEMORY_CONSTANT, 0),
            stats.get(libvirt.VIR_DOMAIN_JOB_COMPRESSION_BYTES, 0),
            # available since libvirt 1.3
            stats.get('memory_dirty_rate', -1),
            # available since libvirt 1.3
            stats.get('memory_iteration', -1),
        )

    def __str__(self):
        return (
            'Migration Progress: %s seconds elapsed,'
            ' %s%% of data processed,'
            ' total data: %iMB,'
            ' processed data: %iMB, remaining data: %iMB,'
            ' transfer speed %iMBps, zero pages: %iMB,'
            ' compressed: %iMB, dirty rate: %i,'
            ' memory iteration: %i' % (
                (self.time_elapsed / 1000),
                self.percentage,
                (self.data_total // Mbytes),
                (self.data_processed // Mbytes),
                (self.data_remaining // Mbytes),
                (self.mem_bps // Mbytes),
                self.mem_constant,
                (self.compression_bytes // Mbytes),
                self.dirty_rate,
                self.mem_iteration,
            )
        )

    @property
    def percentage(self):
        if self.data_remaining == 0 and self.data_total:
            return 100
        progress = 0
        if self.data_total:
            progress = 100 - 100 * self.data_remaining // self.data_total
        if progress < 100:
            return progress
        return 99


def ongoing(stats):
    try:
        job_type = stats['type']
    except KeyError:
        return False
    else:
        return (job_type != libvirt.VIR_DOMAIN_JOB_NONE and
                stats['operation'] ==
                libvirt.VIR_DOMAIN_JOB_OPERATION_MIGRATION_OUT)


def __guess_defaults():
    global ADDRESS, PORT
    try:
        from vdsm.config import config
        PORT = config.getint('addresses', 'management_port')
        ADDRESS = config.get('addresses', 'management_ip')
        if ADDRESS == '::':
            ADDRESS = 'localhost'
    except:
        pass


__guess_defaults()


def _cannonize_host_port(hostPort=None, port=PORT):
    if hostPort is None or hostPort == '0':
        addr = ADDRESS
        if ':' in addr:
            # __guess_defaults() might set an IPv6 address, cannonize it
            addr = '[%s]' % addr
    else:
        # hostPort is in rfc3986 'host [ ":" port ]' format
        hostPort = re.match(r'(?P<Host>.+?)(:(?P<Port>\d+))?$', hostPort)
        addr = hostPort.group('Host')
        if hostPort.group('Port'):
            port = int(hostPort.group('Port'))
    return '%s:%i' % (addr, port)
