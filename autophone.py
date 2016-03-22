# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import Queue
import SocketServer
import datetime
import errno
import inspect
import json
import logging
import logging.handlers
import multiprocessing
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import traceback

from manifestparser import TestManifest

import builds
import buildserver
import jobs
import utils

from adb import ADBHost
from adb_android import ADBAndroid
from autophonepulsemonitor import AutophonePulseMonitor
from autophonetreeherder import AutophoneTreeherder
from mailer import Mailer
from options import AutophoneOptions
from phonestatus import PhoneStatus
from phonetest import PhoneTest
from process_states import ProcessStates
from sensitivedatafilter import SensitiveDataFilter
from worker import PhoneWorker

logger = None
console_logger = None

class PhoneData(object):
    def __init__(self, phoneid, serial, machinetype, osver, abi, sdk, ipaddr):
        self.id = phoneid
        self.serial = serial
        self.machinetype = machinetype
        self.osver = osver
        self.abi = abi
        self.sdk = sdk
        self.host_ip = ipaddr

    @property
    def architecture(self):
        abi = self.abi
        if 'armeabi-v7a' in abi:
            abi = 'armv7'
        elif 'arm64-v8a' in abi:
            abi = 'armv8'
        return abi

    @property
    def os(self):
        return 'android-{0!s}'.format('-'.join(self.osver.split('.')[:2]))

    @property
    def platform(self):
        if self.architecture == 'x86':
            return '{0!s}-x86'.format(self.os)
        return '{0!s}-{1!s}-{2!s}'.format(self.os,
                             self.architecture,
                             ''.join(self.sdk.split('-')))

    def __str__(self):
        return '{0!s}'.format(self.__dict__)


class AutoPhone(object):

    class CmdTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):

        allow_reuse_address = True
        daemon_threads = True
        cmd_cb = None

    class CmdTCPHandler(SocketServer.BaseRequestHandler):
        def handle(self):
            buffer = ''
            self.request.send('Hello? Yes this is Autophone.\n')
            while True:
                try:
                    data = self.request.recv(1024)
                except socket.error, e:
                    if e.errno == errno.ECONNRESET:
                        break
                    raise e
                if not data:
                    break
                buffer += data
                while buffer:
                    line, nl, rest = buffer.partition('\n')
                    if not nl:
                        break
                    buffer = rest
                    line = line.strip()
                    if not line:
                        continue
                    if line == 'quit' or line == 'exit':
                        return
                    response = self.server.cmd_cb(line)
                    self.request.send(response + '\n')

    def __init__(self, loglevel, options):
        self.state = ProcessStates.STARTING
        self.unrecoverable_error = False
        self.options = options
        self.loglevel = loglevel
        self.mailer = Mailer(options.emailcfg, '[autophone] ')

        self._next_worker_num = 0
        self.jobs = jobs.Jobs(self.mailer,
                              allow_duplicates=options.allow_duplicate_jobs)
        self.phone_workers = {}  # indexed by phone id
        self.lock = threading.RLock()
        self.shared_lock = multiprocessing.Lock()
        self._tests = []
        self._devices = {} # dict indexed by device names found in devices ini file
        self.server = None
        self.server_thread = None
        self.treeherder_thread = None
        self.pulse_monitor = None
        self.restart_workers = {}
        self.treeherder = AutophoneTreeherder(None,
                                              self.options,
                                              self.jobs,
                                              mailer=self.mailer,
                                              shared_lock=self.shared_lock)

        console_logger.info('Starting autophone.')

        # Queue for listening to status updates from
        # PhoneWorkerSubProcess workers.
        self.queue = multiprocessing.Queue()

        console_logger.info('Loading tests.')
        self.read_tests()

        console_logger.info('Initializing devices.')

        self.read_devices()

        self.state = ProcessStates.RUNNING
        for worker in self.phone_workers.values():
            worker.start()

        # We must wait to start the pulse monitor until after the
        # workers have started in order to make certain that the
        # shared_lock is passed to the worker subprocesses in an
        # unlocked state.
        if options.enable_pulse:
            self.pulse_monitor = AutophonePulseMonitor(
                userid=options.pulse_user,
                password=options.pulse_password,
                jobaction_exchange_name=options.pulse_jobactions_exchange,
                build_callback=self.on_build,
                jobaction_callback=self.on_jobaction,
                treeherder_url=self.options.treeherder_url,
                trees=options.repos,
                platforms=['android',
                           'android-api-9',
                           'android-api-10',
                           'android-api-11',
                           'android-api-15',
                           'android-x86'],
                buildtypes=options.buildtypes,
                durable_queues=self.options.pulse_durable_queue,
                shared_lock=self.shared_lock,
                verbose=options.verbose)
            self.pulse_monitor.start()

        logger.debug('autophone_options: {0!s}'.format(self.options))

        console_logger.info('Autophone started.')
        if self.options.reboot_on_error:
            msg_subj = 'Starting {0!s}'.format(utils.host())
            msg_body = ('Hello, this is Autophone. '
                        'Just to let you know, I have started running. '
                        'Wish me luck and check on me from time to time. '
                        'I will send you emails if I have any problems.\n\n')
            self.mailer.send(msg_subj, msg_body)

    def _get_frames(self):
        """Return the stack to the current location"""
        frames = traceback.format_list(traceback.extract_stack())
        return ''.join(frames[:-2])

    def lock_acquire(self, data=None):
        if logger.getEffectiveLevel() == logging.DEBUG:
            if self.options.verbose:
                logger.debug('lock_acquire: {0!s}\n{1!s}'.format(data, self._get_frames()))
            else:
                logger.debug('lock_acquire: {0!s}'.format(data))
        self.lock.acquire()

    def lock_release(self, data=None):
        if logger.getEffectiveLevel() == logging.DEBUG:
            if self.options.verbose:
                logger.debug('lock_release: {0!s}\n{1!s}'.format(data, self._get_frames()))
            else:
                logger.debug('lock_release: {0!s}'.format(data))
        self.lock.release()

    @property
    def next_worker_num(self):
        n = self._next_worker_num
        self._next_worker_num += 1
        return n

    def run(self):
        self.server = self.CmdTCPServer(('0.0.0.0', self.options.port),
                                        self.CmdTCPHandler)
        self.server.cmd_cb = self.route_cmd
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                              name='CmdTCPThread')
        self.server_thread.daemon = True
        self.server_thread.start()

        if self.options.treeherder_url:
            self.treeherder_thread = threading.Thread(
                target=self.treeherder.serve_forever,
                name='TreeherderThread')
            self.treeherder_thread.daemon = True
            self.treeherder_thread.start()

        self.worker_msg_loop()

    def check_for_dead_workers(self):
        if self.state != ProcessStates.RUNNING:
            return
        workers = self.phone_workers.values()
        for worker in workers:
            if not worker.is_alive():
                phoneid = worker.phone.id
                logger.debug('Worker {0!s} {1!s} is not alive'.format(phoneid, worker.state))
                if phoneid in self.restart_workers:
                    initial_state = PhoneStatus.IDLE
                    logger.info('Worker %s exited; restarting with new '
                                 'values.' % phoneid)
                elif worker.state == ProcessStates.STOPPING:
                    # The device will be removed and not restarted.
                    initial_state = None
                elif worker.state == ProcessStates.RESTARTING:
                    initial_state = PhoneStatus.IDLE
                else:
                    console_logger.error('Worker {0!s} died!'.format(phoneid))
                    msg_subj = '{0!s} worker {1!s} died'.format(utils.host(), phoneid)
                    msg_body = ('Hello, this is Autophone. '
                                'Just to let you know, '
                                'the worker process '
                                'for phone %s died.\n' %
                                phoneid)
                    if worker.crashes.too_many_crashes():
                        initial_state = PhoneStatus.DISABLED
                        msg_subj += ' and was disabled'
                        msg_body += (
                            'It looks really crashy, so I disabled it. '
                            'Sorry about that.\n\n')
                    else:
                        initial_state = PhoneStatus.DISCONNECTED
                    logger.info('Sending notification...')
                    self.mailer.send(msg_subj, msg_body)

                # Have to remove the tests for the worker prior to
                # removing or recreating it in order to remove it from
                # the PhoneTest.instances.
                while worker.tests:
                    t = worker.tests.pop()
                    t.remove()

                # Do we need to worry about a race between the pulse
                # monitor locking the shared lock?

                if worker.state == ProcessStates.STOPPING:
                    console_logger.info('Worker {0!s} stopped'.format(phoneid))
                    del self.phone_workers[phoneid]
                else:
                    if worker.state == ProcessStates.RESTARTING:
                        # The device is being restarted with a
                        # potentially changed test manifest and
                        # changed test configurations. The changes to
                        # the test configuration files will be
                        # automatically picked up when the tests are
                        # recreated for the worker, but we must
                        # reparse the test manifest in order for the
                        # worker to pick up test manifest changes. We
                        # re-read the tests here, to update
                        # self._tests which will be incorporated into
                        # the new worker instance. If a worker dies
                        # and is restarted, it will automatically pick
                        # up these changes as well.
                        self.read_tests()

                    # We can not re-use the original worker instance
                    # since we need to recreate the
                    # multiprocessing.Process object before we can
                    # call start on it again.
                    console_logger.info('Worker {0!s} restarting'.format(phoneid))
                    # Save the record of crashes before recreating the
                    # Worker, then restore it afterwards.
                    crashes = worker.crashes
                    try:
                        new_worker = self.create_worker(worker.phone)
                        new_worker.crashes = crashes
                        new_worker.start(initial_state)
                    except Exception, e:
                        console_logger.info('Worker {0!s} failed to restart'.format(
                                            phoneid))
                        msg_subj = ('{0!s} worker {1!s} failed to restart'.format(utils.host(), phoneid))
                        msg_body = ('Hello, this is Autophone. '
                                    'Just to let you know, '
                                    'the worker process '
                                    'for phone %s '
                                    'failed to restart due to %s.\n' %
                                    (phoneid, e))
                        self.mailer.send(msg_subj, msg_body)
                        self.purge_worker(phoneid)

    def check_for_unrecoverable_errors(self):
        """Set the property unrecoverable_error to True if any devices have
        lost usb connectivity or not updated their status within the
        maximum allowed heartbeat time period. Forcefully stop any
        workers which have exceeded the maximum heartbeat time.
        """
        for worker in self.phone_workers.values():
            if not worker.last_status_msg:
                continue

            if worker.last_status_msg.phone_status == PhoneStatus.DISCONNECTED:
                self.unrecoverable_error = True

            # Do not check the last timestamp of a worker that
            # is currently downloading a build due to the size
            # of the downloads and the unknown network speed.
            elapsed = datetime.datetime.now() - worker.last_status_msg.timestamp
            if (worker.last_status_msg.phone_status != PhoneStatus.FETCHING and
                elapsed > datetime.timedelta(seconds=self.options.maximum_heartbeat)):
                self.unrecoverable_error = True
                worker.stop()

    def worker_msg_loop(self):
        self.lock_acquire()
        try:
            while self.phone_workers and self.state != ProcessStates.STOPPING:
                if self.options.reboot_on_error:
                    self.check_for_unrecoverable_errors()
                    if (self.unrecoverable_error and
                        self.state != ProcessStates.SHUTTINGDOWN):
                        self.shutdown()
                self.check_for_dead_workers()
                if (self.state == ProcessStates.RUNNING and
                    self.pulse_monitor and not self.pulse_monitor.is_alive()):
                    self.pulse_monitor.start()
                # Temporarily release the lock while we are waiting
                # for a message from the workers.
                self.lock_release()
                try:
                    msg = self.queue.get(timeout=5)
                except Queue.Empty:
                    continue
                except IOError, e:
                    if e.errno == errno.EINTR:
                        continue
                finally:
                    # Reacquire the lock.
                    self.lock_acquire()
                if msg.phone.id not in self.phone_workers:
                    logger.warning('Received message %s '
                                   'from Non-existent worker' % msg)
                    continue
                self.phone_workers[msg.phone.id].process_msg(msg)
                if msg.phone_status == PhoneStatus.SHUTDOWN:
                    # Have to remove the tests for the worker prior to
                    # removing it in order to remove it from the
                    # PhoneTest.instances so that it will not appear
                    # in future PhoneTest.match results.
                    worker = self.phone_workers[msg.phone.id]
                    while worker.tests:
                        t = worker.tests.pop()
                        t.remove()
                    if worker.state == ProcessStates.SHUTTINGDOWN:
                        # We are completely shutting down the device
                        # so we delete it from the phone_workers
                        # dictionary. Otherwise, the phone will be
                        # detected as dead and will be restarted.
                        del self.phone_workers[msg.phone.id]
                    console_logger.info('Worker {0!s} shutdown'.format(msg.phone.id))
        except KeyboardInterrupt:
            pass
        finally:
            if self.pulse_monitor:
                self.pulse_monitor.stop()
                self.pulse_monitor = None
            if self.server:
                self.server.shutdown()
            if self.server_thread:
                self.server_thread.join()
            if self.options.treeherder_url:
                self.treeherder.shutdown()
                if self.treeherder_thread:
                    self.treeherder_thread.join()
            for p in self.phone_workers.values():
                p.stop()
            self.lock_release()

        if self.unrecoverable_error and self.options.reboot_on_error:
            console_logger.info('Rebooting due to unrecoverable errors')
            msg_subj = 'Rebooting {0!s} due to unrecoverable errors'.format(utils.host())
            msg_body = ('Hello, this is Autophone. '
                        'Just to let you know, I have experienced '
                        'unrecoverable device errors and am rebooting in '
                        'the hope of resolving them.\n\n'
                        'Please check on me.\n')
            self.mailer.send(msg_subj, msg_body)
            subprocess.call('sudo reboot', shell=True)

        if self.state == ProcessStates.RESTARTING:
            # Lifted from Sisyphus/Bughunter...
            newargv = sys.argv
            newargv.insert(0, sys.executable)

            # Set all open file handlers to close on exec.  Use 64K as
            # the limit to check as that is the max on Windows XP. The
            # performance issue of doing this is negligible since it
            # is only run during a program reload.
            from fcntl import fcntl, F_GETFD, F_SETFD, FD_CLOEXEC
            for fd in xrange(0x3, 0x10000):
                try:
                    fcntl(fd, F_SETFD, fcntl(fd, F_GETFD) | FD_CLOEXEC)
                except KeyboardInterrupt:
                    raise
                except:
                    pass

            while True:
                try:
                    (pid, status, resource) = os.wait3(os.WNOHANG)
                    logger.debug('Reaped {0!s} {1!s}'.format(pid, status))
                except OSError:
                    break

            os.execvp(sys.executable, newargv)

    # Start the phones for testing
    def new_job(self, job_data):
        logger.debug('new_job: {0!s}'.format(job_data))
        build_url = job_data['build']
        tests = job_data['tests']

        build_data = utils.get_build_data(build_url)
        logger.debug('new_job: build_data {0!s}'.format(build_data))

        if not build_data:
            logger.warning('new_job: Could not find build_data for {0!s}'.format(
                                build_url))
            return

        revision_hash = utils.get_treeherder_revision_hash(
            self.options.treeherder_url,
            build_data['repo'],
            build_data['revision'])

        logger.debug('new_job: revision_hash {0!s}'.format(revision_hash))

        phoneids = set([test.phone.id for test in tests])
        for phoneid in phoneids:
            p = self.phone_workers[phoneid]
            logger.debug('new_job: worker phoneid {0!s}'.format(phoneid))
            # Determine if we will test this build, which tests to run and if we
            # need to enable unittests.
            runnable_tests = PhoneTest.match(tests=tests, phoneid=phoneid)
            if not runnable_tests:
                logger.debug('new_job: Ignoring build {0!s} for phone {1!s}'.format(build_url, phoneid))
                continue
            enable_unittests = False
            for t in runnable_tests:
                enable_unittests = enable_unittests or t.enable_unittests

            new_tests = self.jobs.new_job(build_url,
                                          build_id=build_data['id'],
                                          changeset=build_data['changeset'],
                                          tree=build_data['repo'],
                                          revision=build_data['revision'],
                                          revision_hash=revision_hash,
                                          tests=runnable_tests,
                                          enable_unittests=enable_unittests,
                                          device=phoneid)
            if new_tests:
                self.treeherder.submit_pending(phoneid,
                                               build_url,
                                               build_data['repo'],
                                               revision_hash,
                                               tests=new_tests)
                logger.info('new_job: Notifying device %s of new job '
                                 '%s for tests %s, enable_unittests=%s.' %
                                 (phoneid, build_url, runnable_tests,
                                  enable_unittests))
                p.new_job()

    def route_cmd(self, data):
        response = ''
        self.lock_acquire(data=data)
        try:
            response = self._route_cmd(data)
        finally:
            self.lock_release(data=data)
        return response

    def _route_cmd(self, data):
        # There is not currently any way to get proper responses for commands
        # that interact with workers, since communication between the main
        # process and the worker processes is asynchronous.
        # It would be possible but nontrivial for the workers to put responses
        # onto a queue and have them routed to the proper connection by using
        # request IDs or something like that.
        logger.debug('route_cmd: {0!s}'.format(data))
        data = data.strip()
        cmd, space, params = data.partition(' ')
        cmd = cmd.lower()
        response = 'ok'

        if cmd.startswith('device-'):
            # Device commands have prefix device- and are mapped into
            # PhoneWorker methods by stripping the leading 'device-'
            # from the command.  The device id is the first parameter.
            valid_cmds = ('is_alive', 'stop', 'shutdown', 'reboot', 'disable',
                          'enable', 'ping', 'status', 'restart')
            cmd = cmd.replace('device-', '').replace('-', '_')
            if cmd not in valid_cmds:
                response = 'Unknown command device-{0!s}'.format(cmd)
            else:
                phoneid, space, params = params.partition(' ')
                response = 'error: phone not found'
                for worker in self.phone_workers.values():
                    if (phoneid.lower() == 'all' or
                        worker.phone.serial == phoneid or
                        worker.phone.id == phoneid):
                        f = getattr(worker, cmd)
                        if params:
                            value = f(params)
                        else:
                            value = f()
                        if value is not None:
                            response = '{0!s}\n'.format(value)
                        else:
                            response = ''
                        response += 'ok'
        elif cmd == 'autophone-add-device':
            phoneid, space, serialno = params.partition(' ')
            if phoneid in self.phone_workers:
                response = 'device {0!s} already exists'.format(phoneid)
                console_logger.warning(response)
            else:
                self.read_devices(new_device_name=phoneid)
                self.phone_workers[phoneid].start()
        elif cmd == 'autophone-restart':
            self.state = ProcessStates.RESTARTING
            console_logger.info('Restarting Autophone...')
            for worker in self.phone_workers.values():
                worker.shutdown()
        elif cmd == 'autophone-stop':
            console_logger.info('Stopping Autophone...')
            self.stop()
        elif cmd == 'autophone-shutdown':
            console_logger.info('Shutting down Autophone...')
            self.shutdown()
        elif cmd == 'autophone-log':
            logger.info(params)
        elif cmd == 'autophone-triggerjobs':
            response = self.trigger_jobs(params)
        elif cmd == 'autophone-status':
            response = 'state: {0!s}\n'.format(self.state)
            phoneids = self.phone_workers.keys()
            phoneids.sort()
            for i in phoneids:
                response += self.phone_workers[i].status()
            response += 'ok'
        elif cmd == 'autophone-help':
            response = '''
Autophone command help:

autophone-help
    Generate this message.

autophone-add-device <devicename> <serialno>
    Adds a new device to the active workers. <devicename> refers to
    the name given to the device in the devices.ini file while
    <serialno> is its adb serial number.

autophone-restart
    Shutdown each worker after its current test, then restart
    autophone.

autophone-shutdown
    Shutdown each worker after its current test, then
    shutdown autophone.

autophone-status
    Generate a status report for each device.

autophone-stop
    Immediately stop autophone and all worker processes; may be
    delayed by pending download.

device-disable <devicename>
   Disable the device's worker and cancel its pending jobs.

device-enable <devicename>
   Enable a disabled device's worker.

device-is-alive <devicename>
   Check if the device's worker process is alive, report to log.

device-ping <devicename>
   Issue a ping command to the device's worker which checks the sdcard
   availability.

device-reboot  <devicename>
   Reboot the device.

device-restart <devicename>
   Shutdown the device's worker process after the current test, then
   restart the worker picking up test manifest and test configuration
   changes.

device-status <devicename>
   Generate a status report for the device's worker.

device-shutdown  <devicename>
   Shutdown the device's worker process after the current test. The
   device's worker process will not be restarted and will be removed
   from the active list of workers.

device-stop <devicename>
   Immediately stop the device's worker process and remove it from the
   list of active workers.

ok
'''
        else:
            response = 'Unknown command "{0!s}"\n'.format(cmd)
        return response

    def create_worker(self, phone):
        logger.info('Creating worker for {0!s}: {1!s}.'.format(phone, self.options))
        dm = self._devices[phone.id]['dm']
        tests = []
        for test_class, config_file, test_devices_repos in self._tests:
            logger.debug('create_worker: {0!s} {1!s} {2!s}'.format(
                test_class, config_file, test_devices_repos))
            skip_test = True
            if not test_devices_repos:
                # There is no restriction on this test being run by
                # specific devices.
                repos = []
                skip_test = False
            elif phone.id in test_devices_repos:
                # This test is to be run by this device on
                # the repos test_devices_repos[phone.id]
                repos = test_devices_repos[phone.id]
                skip_test = False
            if not skip_test:
                test = test_class(dm=dm,
                                  phone=phone,
                                  options=self.options,
                                  config_file=config_file,
                                  repos=repos)
                tests.append(test)
                for chunk in range(2, test.chunks+1):
                    logger.debug('Creating chunk {0:d}/{1:d}'.format(chunk, test.chunks))
                    tests.append(test_class(dm=dm,
                                            phone=phone,
                                            options=self.options,
                                            config_file=config_file,
                                            chunk=chunk,
                                            repos=repos))
        if not tests:
            logger.warning('Not creating worker: No tests defined for '
                                'worker for %s: %s.' %
                                (phone, self.options))
            return
        logfile_prefix = os.path.splitext(self.options.logfile)[0]
        worker = PhoneWorker(dm, self.next_worker_num,
                             tests, phone, self.options,
                             self.queue,
                             '{0!s}-{1!s}'.format(logfile_prefix, phone.id),
                             self.loglevel, self.mailer, self.shared_lock)
        self.phone_workers[phone.id] = worker
        return worker

    def purge_worker(self, phoneid):
        """Remove worker and its tests from cached locations."""
        if phoneid in self.phone_workers:
            del self.phone_workers[phoneid]
        if phoneid in self.restart_workers:
            del self.restart_workers[phoneid]
        for t in PhoneTest.match(phoneid=phoneid):
            t.remove()

    def register_cmd(self, data):
        # Map MAC Address to ip and user name for phone
        # The configparser does odd things with the :'s so remove them.
        phoneid = data['device_name']
        phone = PhoneData(
            phoneid,
            data['serialno'],
            data['hardware'],
            data['osver'],
            data['abi'],
            data['sdk'],
            self.options.ipaddr) # XXX IPADDR no longer needed?
        if logger.getEffectiveLevel() == logging.DEBUG:
            logger.debug('register_cmd: phone: {0!s}'.format(phone))
        if phoneid in self.phone_workers:
            logger.debug('Received registration message for known phone '
                              '%s.' % phoneid)
            worker = self.phone_workers[phoneid]
            if worker.phone.__dict__ != phone.__dict__:
                # This won't update the subprocess, but it will allow
                # us to write out the updated values right away.
                worker.phone = phone
                logger.info('Registration info has changed; restarting '
                                 'worker.')
                if phoneid in self.restart_workers:
                    logger.info('Phone worker is already scheduled to be '
                                 'restarted!')
                else:
                    self.restart_workers[phoneid] = phone
                    worker.stop()
        else:
            try:
                self.create_worker(phone)
                logger.info('Registered phone {0!s}.'.format(phone.id))
            except Exception:
                console_logger.info('Worker {0!s} failed to register'.format(phoneid))
                self.purge_worker(phoneid)
                raise

    def read_devices(self, new_device_name=None):
        """Read the devices.ini file and create a corresponding ADBAndroid dm
        instance to manage each of the devices listed.

        When called without a new_device_name argument, read_devices()
        will register all devices currently specified in the
        devices.ini file.

        When called with a new_device_name argument specifying the
        name of a device, read_devices(new_device_name="devicename")
        will register only that device and will reload the tests from
        the test manifest in order to pick up the tests for the newly
        added device.
        """
        cfg = ConfigParser.RawConfigParser()
        cfg.read(self.options.devicescfg)

        if new_device_name:
            devices = [new_device_name]
        else:
            devices = cfg.sections()

        for device_name in devices:
            # failure for a device to have a serialno option is fatal.
            serialno = cfg.get(device_name, 'serialno')
            if cfg.has_option(device_name, 'test_root'):
                test_root = cfg.get(device_name, 'test_root')
            else:
                test_root = self.options.device_test_root

            console_logger.info("Initializing device name={0!s}, serialno={1!s}".format(device_name, serialno))
            try:
                dm = ADBAndroid(
                    device=serialno,
                    device_ready_retry_wait=self.options.device_ready_retry_wait,
                    device_ready_retry_attempts=self.options.device_ready_retry_attempts,
                    verbose=self.options.verbose,
                    test_root=test_root)
                dm.power_on()
                device = {"device_name": device_name,
                          "serialno": serialno,
                          "dm" : dm}
                device['osver'] = dm.get_prop('ro.build.version.release')
                device['hardware'] = dm.get_prop('ro.product.model')
                device['abi'] = dm.get_prop('ro.product.cpu.abi')
                try:
                    sdk = int(dm.get_prop('ro.build.version.sdk'))
                    if sdk <= 10:
                        device['sdk'] = 'api-9'
                    elif sdk < 15:
                        device['sdk'] = 'api-11'
                    else:
                        device['sdk'] = 'api-15'
                except ValueError:
                    device['sdk'] = 'api-9'
                self._devices[device_name] = device
                if new_device_name:
                    self.read_tests()
                self.register_cmd(device)
            except Exception, e:
                console_logger.error('Unable to initialize device {0!s} due to {1!s}.'.format(device_name, e))
                msg_subj = '{0!s} unable to initialize device {1!s}'.format(utils.host(),
                                                                  device_name)
                msg_body = ('Hello, this is Autophone. '
                            'Just to let you know, '
                            'phone %s '
                            'failed to initialize due to %s.\n' %
                            (device_name, e))
                self.mailer.send(msg_subj, msg_body)
                self.purge_worker(device_name)

    def read_tests(self):
        self._tests = []
        manifest = TestManifest()
        manifest.read(self.options.test_path)
        tests_info = manifest.get()
        for t in tests_info:
            # Remove test section suffix.
            t['name'] = t['name'].split()[0]
            if not t['here'] in sys.path:
                sys.path.append(t['here'])
            if t['name'].endswith('.py'):
                t['name'] = t['name'][:-3]
            # add all classes in module that are derived from PhoneTest to
            # the test list
            tests = []
            for member_name, member_value in inspect.getmembers(__import__(t['name']),
                                                                inspect.isclass):
                if (member_name != 'PhoneTest' and
                    member_name != 'PerfTest' and
                    issubclass(member_value, PhoneTest)):
                    config = t.get('config', '')
                    # config is a space separated list of config
                    # files.  The test will be instantiated for each
                    # of the config files allowing tests such as the
                    # runremotetests.py to handle more than one unit
                    # test at a time.
                    #
                    # Each config file can contain additional options
                    # for a test.
                    #
                    # Other options are:
                    #
                    # <device> = <repo-list>
                    #
                    # which determines the devices which should
                    # run the test. If no devices are listed, then
                    # all devices will run the test.

                    devices = [device for device in t if device not in
                               ('name', 'here', 'manifest', 'path', 'config',
                                'relpath', 'unittests', 'subsuite')]
                    logger.debug('read_tests: test: %s, class: %s, '
                                      'config: %s, devices: %s' % (
                                          member_name,
                                          member_value,
                                          config,
                                          devices))
                    test_devices_repos = {}
                    for device in devices:
                        test_devices_repos[device] = t[device].split()
                    configs = config.split()
                    for config_file in configs:
                        config_file = os.path.normpath(
                            os.path.join(t['here'], config_file))
                        tests.append((member_value,
                                      config_file, test_devices_repos))

            self._tests.extend(tests)


    def trigger_jobs(self, data):
        logger.info('Received user-specified job: {0!s}'.format(data))
        trigger_data = json.loads(data)
        if 'build' not in trigger_data:
            return 'invalid args'
        build_url = trigger_data['build']
        tests = []
        test_names = trigger_data['test_names']
        if not test_names:
            # No test names specified, force PhoneTest.match
            # to return tests with any name.
            test_names = [None]
        devices = trigger_data['devices']
        if not devices:
            # No devices specified, force PhoneTest.match
            # to return tests for any device.
            devices = [None]
        for test_name in test_names:
            for device in devices:
                tests.extend(PhoneTest.match(test_name=test_name,
                                             phoneid=device,
                                             build_url=build_url))
        if tests:
            job_data = {
                'build': build_url,
                'tests': tests,
            }
            self.new_job(job_data)
        return 'ok'

    def reset_phones(self):
        logger.info('Resetting phones...')
        for phoneid, phone in self.phone_workers.iteritems():
            phone.reboot()

    def on_build(self, msg):
        self.lock_acquire()
        try:
            if self.state != ProcessStates.RUNNING:
                return
            logger.debug('PULSE BUILD FOUND {0!s}'.format(msg))
            build_url = msg['packageUrl']
            if msg['branch'] != 'try':
                tests = PhoneTest.match(build_url=build_url)
            else:
                # Autophone try builds will have a comment of the form:
                # try: -b o -p android-api-9,android-api-15 -u autophone-smoke,autophone-s1s2 -t none
                # Do not allow global selection of tests
                # since Autophone can not handle the load.
                tests = []
                reTests = re.compile('try:.* -u (.*) -t.*')
                match = reTests.match(msg['comments'])
                if match:
                    test_names = [t for t in match.group(1).split(',')
                                  if t.startswith('autophone-') and
                                  t != 'autophone-tests']
                    for test_name in test_names:
                        tests.extend(PhoneTest.match(test_name=test_name,
                                                     build_url=build_url))
            job_data = {'build': build_url, 'tests': tests}
            self.new_job(job_data)
        finally:
            self.lock_release()

    def on_jobaction(self, job_action):
        self.lock_acquire()
        try:
            if (self.state != ProcessStates.RUNNING or
                job_action['job_group_name'] != 'Autophone'):
                return
            machine_name = job_action['machine_name']
            if machine_name not in self.phone_workers:
                logger.warning('on_jobaction: unknown device {0!s}'.format(machine_name))
                return
            logger.debug('on_jobaction: found {0!s}'.format(json.dumps(
                job_action, sort_keys=True, indent=4)))

            p = self.phone_workers[machine_name]
            if job_action['action'] == 'cancel':
                request = (job_action['job_guid'],)
                p.cancel_test(request)
            elif job_action['action'] == 'retrigger':
                test = PhoneTest.lookup(
                    machine_name,
                    job_action['config_file'],
                    job_action['chunk'])
                if not test:
                    logger.warning(
                        'on_jobaction: No test found for {0!s}'.format(
                        json.dumps(job_action, sort_keys=True, indent=4)))
                else:
                    job_data = {
                        'build': job_action['build_url'],
                        'tests': [test],
                    }
                    self.new_job(job_data)
            else:
                logger.warning('on_jobaction: unknown action {0!s}'.format(
                                    job_action['action']))
        finally:
            self.lock_release()

    def stop(self):
        self.state = ProcessStates.STOPPING

    def shutdown(self):
        logger.debug('AutoPhone.shutdown: enter')
        self.state = ProcessStates.SHUTTINGDOWN
        if self.pulse_monitor:
            logger.debug('AutoPhone.shutdown: stopping pulse monitor')
            self.pulse_monitor.stop()
            self.pulse_monitor = None
        logger.debug('AutoPhone.shutdown: shutting down workers')
        for p in self.phone_workers.values():
            logger.debug('AutoPhone.shutdown: shutting down worker {0!s}'.format(p.phone.id))
            p.shutdown()
        logger.debug('AutoPhone.shutdown: exit')

def load_autophone_options(cmd_options):
    options = AutophoneOptions()
    option_tuples = [(option_name, type(option_value))
                     for option_name, option_value in inspect.getmembers(options)
                     if not option_name.startswith('_')]
    getter_map = {str: 'get', int: 'getint', bool: 'getboolean', list: 'get'}

    for option_name, option_type in option_tuples:
        try:
            value = getattr(cmd_options, option_name)
            if value is not None:
                value = option_type(value)
            setattr(options, option_name, value)
        except AttributeError:
            pass

    cfg = ConfigParser.RawConfigParser()
    if cmd_options.autophonecfg:
        cfg.read(cmd_options.autophonecfg)
        if cfg.has_option('settings', 'credentials_file'):
            cfg.read(cfg.get('settings', 'credentials_file'))
    if cmd_options.credentials_file:
        cfg.read(cmd_options.credentials_file)

    if cmd_options.autophonecfg or cmd_options.credentials_file:
        for option_name, option_type in option_tuples:
            try:
                getter = getattr(ConfigParser.RawConfigParser,
                                 getter_map[option_type])
                value = getter(cfg, 'settings', option_name)
                if option_type == list:
                    value = value.split()
                setattr(options, option_name, option_type(value))
            except ConfigParser.NoOptionError:
                pass

    # record sensitive data that should be filtered from logs.
    options.sensitive_data = []
    options.sensitive_data.append(options.phonedash_password)
    options.sensitive_data.append(options.pulse_password)
    options.sensitive_data.append(options.aws_access_key_id)
    options.sensitive_data.append(options.aws_access_key)
    options.sensitive_data.append(options.treeherder_client_id)
    options.sensitive_data.append(options.treeherder_secret)
    return options


def main(options):
    global logger, console_logger

    def sigterm_handler(signum, frame):
        autophone.stop()

    loglevel = e = None
    try:
        loglevel = getattr(logging, options.loglevel)
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != options.loglevel:
            print 'Invalid log level {0!s}'.format(options.loglevel)
            return errno.EINVAL

    sensitive_data_filter = SensitiveDataFilter(options.sensitive_data)
    logging.captureWarnings(True)

    logger = logging.getLogger()
    logger.addFilter(sensitive_data_filter)
    logger.setLevel(loglevel)

    filehandler = logging.handlers.TimedRotatingFileHandler(options.logfile,
                                                            when='midnight',
                                                            backupCount=7)
    fileformatstring = ('%(asctime)s|%(process)d|%(threadName)s|%(name)s|'
                        '%(levelname)s|%(message)s')
    fileformatter = logging.Formatter(fileformatstring)
    filehandler.setFormatter(fileformatter)
    logger.addHandler(filehandler)

    console_logger = logging.getLogger('console')
    console_logger.setLevel(loglevel)
    streamhandler = logging.StreamHandler(stream=sys.stderr)
    streamformatstring = ('%(asctime)s|%(process)d|%(threadName)s|%(name)s|'
                          '%(levelname)s|%(message)s')
    streamformatter = logging.Formatter(streamformatstring)
    streamhandler.setFormatter(streamformatter)
    console_logger.addHandler(streamhandler)

    for other_logger_name, other_logger in logger.manager.loggerDict.iteritems():
        if ((other_logger_name == 'root' or other_logger_name == 'console')
            or not hasattr(other_logger, 'handlers')):
            continue
        other_logger.addFilter(sensitive_data_filter)
        for other_handler in other_logger.handlers:
            other_handler.flush()
            other_handler.close()
            other_logger.removeHandler(other_handler)
        other_logger.addHandler(logging.NullHandler())
        logger.debug('Library logger {0!s}'.format(other_logger_name))
        if options.verbose:
            other_logger.setLevel(loglevel)

    console_logger.info('Starting server on port {0:d}.'.format(options.port))
    console_logger.info('Starting build-cache server on port {0:d}.'.format(
                        options.build_cache_port))

    # By starting adb server before the build cache, we prevent adb
    # from listening to the build cache client port, thus preventing
    # restart without first killing adb.
    adbhost = ADBHost()
    adbhost.start_server()


    product = 'fennec'
    build_platforms = ['android',
                       'android-api-9',
                       'android-api-10',
                       'android-api-11',
                       'android-api-15',
                       'android-x86']
    buildfile_ext = '.apk'
    try:
        build_cache = builds.BuildCache(
            options.repos,
            options.buildtypes,
            product,
            build_platforms,
            buildfile_ext,
            cache_dir=options.cache_dir,
            override_build_dir=options.override_build_dir,
            build_cache_size=options.build_cache_size,
            build_cache_expires=options.build_cache_expires,
            treeherder_url=options.treeherder_url)
    except builds.BuildCacheException, e:
        print '''{0!s}

When specifying --override-build-dir, the directory must already exist
and contain a build.apk package file to be tested.

In addition, if you have specified --enable-unittests, the override
build directory must also contain a tests directory containing the
unpacked tests package for the build.

        '''.format(e)
        raise

    build_cache_server = buildserver.BuildCacheServer(
        ('127.0.0.1', options.build_cache_port),
        buildserver.BuildCacheHandler)
    build_cache_server.build_cache = build_cache
    build_cache_server_thread = threading.Thread(
        target=build_cache_server.serve_forever,
        name='BuildCacheThread')
    build_cache_server_thread.daemon = True
    build_cache_server_thread.start()

    autophone = AutoPhone(loglevel, options)

    signal.signal(signal.SIGTERM, sigterm_handler)
    autophone.run()
    # Drop pending messages and commands to prevent hangs on shutdown.
    while True:
        try:
            msg = autophone.queue.get_nowait()
            logger.debug('Dropping autphone.queue: {0!s}'.format(msg))
        except Queue.Empty:
            break

    for phoneid in autophone.phone_workers:
        worker = autophone.phone_workers[phoneid]
        while True:
            try:
                msg = worker.queue.get_nowait()
                logger.debug('Dropping phone {0!s} worker.queue: {1!s}'.format(phoneid, msg))
            except Queue.Empty:
                break

    while True:
        try:
            (pid, status, resource) = os.wait3(os.WNOHANG)
            logger.debug('Reaped {0!s} {1!s}'.format(pid, status))
        except OSError:
            break

    console_logger.info('AutoPhone terminated.')
    console_logger.info('Shutting down build-cache server...')
    build_cache_server.shutdown()
    build_cache_server_thread.join()
    console_logger.info('Done.')
    return 0


if __name__ == '__main__':
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option('--ipaddr', action='store', type='string', dest='ipaddr',
                      default=None, help='IP address of interface to use for '
                      'phone callbacks, e.g. after rebooting. If not given, '
                      'it will be guessed.')
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=28001,
                      help='Port to listen for incoming connections, defaults '
                      'to 28001')
    parser.add_option('--logfile', action='store', type='string',
                      dest='logfile', default='autophone.log',
                      help='Log file to store logging from entire system. '
                      'Individual phone worker logs will use '
                      '<logfile>-<phoneid>[.<ext>]. Default: autophone.log')
    parser.add_option('--loglevel', action='store', type='string',
                      dest='loglevel', default='INFO',
                      help='Log level - ERROR, WARNING, DEBUG, or INFO, '
                      'defaults to INFO')
    parser.add_option('-t', '--test-path', action='store', type='string',
                      dest='test_path', default='tests/manifest.ini',
                      help='path to test manifest')
    parser.add_option('--minidump-stackwalk', action='store', type='string',
                      dest='minidump_stackwalk', default='/usr/local/bin/minidump_stackwalk',
                      help='Path to minidump_stackwalk executable; defaults to /usr/local/bin/minidump_stackwalk.')
    parser.add_option('--emailcfg', action='store', type='string',
                      dest='emailcfg', default='',
                      help='config file for email settings; defaults to none')
    parser.add_option('--phonedash-url', action='store', type='string',
                      dest='phonedash_url', default='',
                      help='Url to Phonedash server. If not set, results for '
                      'each device will be written to comma delimited files in '
                      'the form: autophone-results-<deviceid>.csv.')
    parser.add_option('--phonedash-user', action='store', type='string',
                      dest='phonedash_user', default='',
                      help='user id for connecting to Phonedash server')
    parser.add_option('--phonedash-password', action='store', type='string',
                      dest='phonedash_password', default='',
                      help='password for connecting to Phonedash server')
    parser.add_option('--webserver-url', action='store', type='string',
                      dest='webserver_url', default='',
                      help='Url to web server for remote tests.')
    parser.add_option('--enable-pulse', action='store_true',
                      dest="enable_pulse", default=False,
                      help='Enable connecting to Pulse to look for new builds. '
                      'If specified, --pulse-user and --pulse-password must also '
                      'be specified.')
    parser.add_option('--pulse-durable-queue', action='store_true',
                      dest="pulse_durable_queue", default=False,
                      help='Use a durable queue when connecting to Pulse.')
    parser.add_option('--pulse-user', action='store', type='string',
                      dest='pulse_user', default='',
                      help='user id for connecting to PulseGuardian')
    parser.add_option('--pulse-password', action='store', type='string',
                      dest='pulse_password', default='',
                      help='password for connecting to PulseGuardian')
    parser.add_option('--pulse-jobactions-exchange', action='store', type='string',
                      dest='pulse_jobactions_exchange',
                      default='exchange/treeherder/v1/job-actions',
                      help='Exchange for Pulse Job Actions queue; '
                      'defaults to exchange/treeherder/v1/job-actions.')
    parser.add_option('--cache-dir', type='string',
                      dest='cache_dir', default='builds',
                      help='Use the specified directory as the build '
                      'cache directory; defaults to builds.')
    parser.add_option('--override-build-dir', type='string',
                      dest='override_build_dir', default=None,
                      help='Use the specified directory as the current build '
                      'cache directory without attempting to download a build '
                      'or test package.')
    parser.add_option('--allow-duplicate-jobs', action='store_true',
                      dest='allow_duplicate_jobs', default=False,
                      help='Allow duplicate jobs to be queued. This is useful '
                      'when testing intermittent failures. Defaults to False.')
    parser.add_option('--repo',
                      dest='repos',
                      action='append',
                      default=['mozilla-central'],
                      help='The repos to test. '
                      'One of b2g-inbound, fx-team, mozilla-aurora, '
                      'mozilla-beta, mozilla-central, mozilla-inbound, '
                      'mozilla-release, try. To specify multiple '
                      'repos, specify them with additional --repo options. '
                      'Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      default=['opt'],
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')
    parser.add_option('--lifo',
                      dest='lifo',
                      action='store_true',
                      default=False,
                      help='Process jobs in LIFO order. Default of False '
                      'implies FIFO order.')
    parser.add_option('--build-cache-port',
                      dest='build_cache_port',
                      action='store',
                      type='int',
                      default=buildserver.DEFAULT_PORT,
                      help='Port for build-cache server. If you are running '
                      'multiple instances of autophone, this will have to be '
                      'different in each. Defaults to %d.' %
                      buildserver.DEFAULT_PORT)
    parser.add_option('--devices',
                      dest='devicescfg',
                      action='store',
                      type='string',
                      default='devices.ini',
                      help='Devices configuration ini file. '
                      'Each device is listed by name in the sections of the ini file.')
    parser.add_option('--config',
                      dest='autophonecfg',
                      action='store',
                      type='string',
                      default=None,
                      help='Optional autophone.py configuration ini file. '
                      'The values of the settings in the ini file override '
                      'any settings set on the command line. '
                      'autophone.ini.example contains all of the currently '
                      'available settings.')
    parser.add_option('--credentials-file',
                      dest='credentials_file',
                      action='store',
                      type='string',
                      default=None,
                      help='Optional autophone.py configuration ini file '
                      'which is to be loaded in addition to that specified '
                      'by the --config option. It is intended to contain '
                      'sensitive options such as credentials which should not '
                      'be checked into the source repository. '
                      'The values of the settings in the ini file override '
                      'any settings set on the command line. '
                      'autophone.ini.example contains all of the currently '
                      'available settings.')
    parser.add_option('--verbose', action='store_true',
                      dest='verbose', default=False,
                      help='Include output from ADBAndroid command_output and '
                      'shell_output commands when loglevel is DEBUG. '
                      'Defaults to False.')
    parser.add_option('--treeherder-url',
                      dest='treeherder_url',
                      action='store',
                      type='string',
                      default=None,
                      help='Url of the treeherder server where test results '
                      'are reported. If specified, --treeherder-client-id and '
                      '--treeherder-secret must also be specified. '
                      'Defaults to None.')
    parser.add_option('--treeherder-client-id',
                      dest='treeherder_client_id',
                      action='store',
                      type='string',
                      default=None,
                      help='Treeherder client id. If specified, '
                      '--treeherder-url and --treeherder-secret must also '
                      'be specified. Defaults to None.')
    parser.add_option('--treeherder-secret',
                      dest='treeherder_secret',
                      action='store',
                      type='string',
                      default=None,
                      help='Treeherder secret. If specified, --treeherder-url '
                      'and --treeherder-client-id must also be specified. '
                      'Defaults to None.')
    parser.add_option('--treeherder-tier',
                      dest='treeherder_tier',
                      action='store',
                      type='int',
                      default=3,
                      help='Integer specifying Treeherder Job Tier. '
                      'Defaults to 3.')
    parser.add_option('--treeherder-retry-wait',
                      dest='treeherder_retry_wait',
                      action='store',
                      type='int',
                      default=300,
                      help='Number of seconds to wait between attempts '
                      'to send data to Treeherder. Defaults to 300.')
    parser.add_option('--s3-upload-bucket',
                      dest='s3_upload_bucket',
                      action='store',
                      type='string',
                      default=None,
                      help='AWS S3 bucket name used to store logs. '
                      'Defaults to None. If specified, --aws-access-key-id '
                      'and --aws-secret-access-key must also be specified.')
    parser.add_option('--aws-access-key-id',
                      dest='aws_access_key_id',
                      action='store',
                      type='string',
                      default=None,
                      help='AWS Access Key ID used to access AWS S3. '
                      'Defaults to None. If specified, --s3-upload-bucket '
                      'and --aws-secret-access-key must also be specified.')
    parser.add_option('--aws-access-key',
                      dest='aws_access_key',
                      action='store',
                      type='string',
                      default=None,
                      help='AWS Access Key used to access AWS S3. '
                      'Defaults to None. If specified, --s3-upload-bucket '
                      'and --aws-secret-access-key-id must also be specified.')
    parser.add_option('--reboot-on-error', action='store_true',
                      dest='verbose', default=False,
                      help='Reboot host in the event of an unrecoverable error.'
                      'Defaults to False.')
    parser.add_option('--maximum-heartbeat',
                      dest='maximum_heartbeat',
                      action='store',
                      type='int',
                      default=900,
                      help='Maximum heartbeat in seconds before worker is '
                      'considered to be hung. Defaults to 900.')
    parser.add_option('--device-test-root',
                      dest='device_test_root',
                      action='store',
                      type='string',
                      default='',
                      help='Device directory to be used as the test root. '
                      'Defaults to an empty string which will defer selection '
                      'of the test root to ADBAndroid. Can be overridden '
                      'via a test_root option for a device in the devices.ini '
                      'file.')

    (cmd_options, args) = parser.parse_args()
    options = load_autophone_options(cmd_options)
    if (options.treeherder_url or
        options.treeherder_client_id or
        options.treeherder_secret):
        if (not options.treeherder_url or
            not options.treeherder_client_id or
            not options.treeherder_secret):
            raise Exception('Inconsistent treeherder options')
    if ((options.s3_upload_bucket or
         options.aws_access_key_id or
         options.aws_access_key) and (
             not options.s3_upload_bucket or
             not options.aws_access_key_id or
             not options.aws_access_key)):
        raise Exception('--s3-upload-bucket, --aws-access-key-id, '
                        '--aws-access-key must be specified together')

    exit_code = main(options)

    sys.exit(exit_code)
