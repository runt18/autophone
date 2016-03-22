# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import with_statement

import Queue
import datetime
import logging
import logging.handlers
import multiprocessing
import os
import posixpath
import re
import sys
import tempfile
import time
import traceback

import buildserver
import jobs
# The following direct imports are necessary in order to reference the
# modules when we reset their global loggers:
import autophonetreeherder
import builds
import mailer
import phonetest
import s3
import utils
from adb import ADBError, ADBTimeoutError
from autophonetreeherder import AutophoneTreeherder
from builds import BuildMetadata
from logdecorator import LogDecorator
from phonestatus import PhoneStatus
from phonetest import PhoneTest, PhoneTestResult, FLASH_PACKAGE
from process_states import ProcessStates
from s3 import S3Bucket
from sensitivedatafilter import SensitiveDataFilter

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

class Crashes(object):

    CRASH_WINDOW = 30
    CRASH_LIMIT = 5

    def __init__(self, crash_window=CRASH_WINDOW, crash_limit=CRASH_LIMIT):
        self.crash_times = []
        self.crash_window = datetime.timedelta(seconds=crash_window)
        self.crash_limit = crash_limit

    def add_crash(self):
        self.crash_times.append(datetime.datetime.now())
        self.crash_times = [x for x in self.crash_times
                            if self.crash_times[-1] - x <= self.crash_window]

    def too_many_crashes(self):
        return len(self.crash_times) >= self.crash_limit


class PhoneTestMessage(object):

    def __init__(self, phone, build=None, phone_status=None,
                 message=None):
        self.phone = phone
        self.build = build
        self.phone_status = phone_status
        self.message = message
        self.timestamp = datetime.datetime.now().replace(microsecond=0)

    def __str__(self):
        s = '<{0!s}> {1!s} ({2!s})'.format(self.timestamp.isoformat(), self.phone.id,
                              self.phone_status)
        if self.message:
            s += ': {0!s}'.format(self.message)
        return s

    def short_desc(self):
        s = self.phone_status
        if self.message:
            s += ': {0!s}'.format(self.message)
        return s


class PhoneWorker(object):

    """Runs tests on a single phone in a separate process.
    This is the interface to the subprocess, accessible by the main
    process."""

    DEVICE_READY_RETRY_WAIT = 20
    DEVICE_READY_RETRY_ATTEMPTS = 3
    DEVICE_BATTERY_MIN = 90
    DEVICE_BATTERY_MAX = 95
    PHONE_RETRY_LIMIT = 2
    PHONE_RETRY_WAIT = 15
    PHONE_MAX_REBOOTS = 3
    PHONE_PING_INTERVAL = 15*60
    PHONE_COMMAND_QUEUE_TIMEOUT = 10

    def __init__(self, dm, worker_num, tests, phone, options,
                 autophone_queue, logfile_prefix, loglevel, mailer,
                 shared_lock):

        self.state = ProcessStates.STARTING
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        self.worker_num = worker_num
        self.last_status_msg = None
        self.first_status_of_type = None
        self.last_status_of_previous_type = None
        self.crashes = Crashes(crash_window=options.phone_crash_window,
                               crash_limit=options.phone_crash_limit)
        # Messages are passed to the PhoneWorkerSubProcess worker from
        # the main process by PhoneWorker which puts messages into
        # PhoneWorker.queue. PhoneWorkerSubProcess is given a
        # reference to this queue and gets messages from the main
        # process via this queue.
        self.queue = multiprocessing.Queue()
        self.lock = multiprocessing.Lock()
        self.shared_lock = shared_lock
        self.subprocess = PhoneWorkerSubProcess(dm,
                                                self.worker_num,
                                                tests,
                                                phone, options,
                                                autophone_queue,
                                                self.queue, logfile_prefix,
                                                loglevel, mailer,
                                                shared_lock)
        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id},
                                       '%(phoneid)s|%(message)s')
        self.loggerdeco.debug('PhoneWorker:__init__')

    def is_alive(self):
        return self.subprocess.is_alive()

    def start(self, phone_status=PhoneStatus.IDLE):
        self.loggerdeco.debug('PhoneWorker:start')
        self.state = ProcessStates.RUNNING
        self.subprocess.start(phone_status)

    def stop(self):
        self.loggerdeco.debug('PhoneWorker:stop')
        self.state = ProcessStates.STOPPING
        self.subprocess.stop()

    def shutdown(self):
        self.loggerdeco.debug('PhoneWorker:shutdown')
        self.state = ProcessStates.SHUTTINGDOWN
        self.queue.put_nowait(('shutdown', None))

    def restart(self):
        """We tell the PhoneWorkerSubProcess to shut down cleanly, but mark
        the PhoneWorker state as restarting. AutoPhone will use this
        information to not remove the device when it has completed
        shutting down and will restart it.
        """
        self.loggerdeco.debug('PhoneWorker:restart')
        self.state = ProcessStates.RESTARTING
        self.queue.put_nowait(('shutdown', None))

    def new_job(self):
        self.loggerdeco.debug('PhoneWorker:new_job')
        self.queue.put_nowait(('job', None))

    def reboot(self):
        self.loggerdeco.debug('PhoneWorker:reboot')
        self.queue.put_nowait(('reboot', None))

    def disable(self):
        self.loggerdeco.debug('PhoneWorker:disable')
        self.queue.put_nowait(('disable', None))

    def enable(self):
        self.loggerdeco.debug('PhoneWorker:enable')
        self.queue.put_nowait(('enable', None))

    def cancel_test(self, request):
        self.loggerdeco.debug('PhoneWorker:cancel_test')
        self.queue.put_nowait(('cancel_test', request))

    def ping(self):
        self.loggerdeco.debug('PhoneWorker:ping')
        self.queue.put_nowait(('ping', None))

    def process_msg(self, msg):
        """These are status messages routed back from the autophone_queue
        listener in the main AutoPhone class. There is probably a bit
        clearer way to do this..."""
        if (not self.last_status_msg or
            msg.phone_status != self.last_status_msg.phone_status):
            self.last_status_of_previous_type = self.last_status_msg
            self.first_status_of_type = msg
        if msg.message == 'Heartbeat':
            self.last_status_msg.timestamp = msg.timestamp
        else:
            self.loggerdeco.debug('PhoneWorker:process_msg: {0!s}'.format(msg))
            self.last_status_msg = msg

    def status(self):
        response = ''
        now = datetime.datetime.now().replace(microsecond=0)
        response += 'phone {0!s} ({1!s}):\n'.format(self.phone.id, self.phone.serial)
        response += '  state {0!s}\n'.format(self.state)
        response += '  debug level {0:d}\n'.format(self.options.debug)
        if not self.last_status_msg:
            response += '  no updates\n'
        else:
            if self.last_status_msg.build and self.last_status_msg.build.id:
                d = self.last_status_msg.build.id
                d = '{0!s}-{1!s}-{2!s} {3!s}:{4!s}:{5!s}'.format(d[0:4], d[4:6], d[6:8],
                                           d[8:10], d[10:12], d[12:14])
                response += '  current build: {0!s} {1!s}\n'.format(
                    d,
                    self.last_status_msg.build.tree)
            else:
                response += '  no build loaded\n'
            response += '  last update {0!s} ago:\n    {1!s}\n'.format(
                now - self.last_status_msg.timestamp,
                self.last_status_msg.short_desc())
            response += '  {0!s} for {1!s}\n'.format(
                self.last_status_msg.phone_status,
                now - self.first_status_of_type.timestamp)
            if self.last_status_of_previous_type:
                response += '  previous state {0!s} ago:\n    {1!s}\n'.format(
                    now - self.last_status_of_previous_type.timestamp,
                    self.last_status_of_previous_type.short_desc())
        return response

class PhoneWorkerSubProcess(object):

    """Worker subprocess.

    FIXME: Would be nice to have test results uploaded outside of the
    test objects, and to have them queued (and cached) if the results
    server is unavailable for some reason.  Might be best to communicate
    this back to the main AutoPhone process.
    """

    def __init__(self, dm, worker_num, tests, phone, options,
                 autophone_queue, queue, logfile_prefix, loglevel, mailer,
                 shared_lock):
        global logger

        self.state = ProcessStates.RUNNING
        self.worker_num = worker_num
        self.tests = tests
        self.dm = dm
        self.phone = phone
        self.options = options
        # PhoneWorkerSubProcess.autophone_queue is used to pass
        # messages back to the main Autophone process while
        # PhoneWorkerSubProcess.queue is used to get messages from the
        # main process.
        self.autophone_queue = autophone_queue
        self.queue = queue
        self.logfile_prefix = logfile_prefix
        self.logfile = logfile_prefix + '.log'
        self.outfile = logfile_prefix + '.out'
        self.test_logfile = None
        self.loglevel = loglevel
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.p = None
        self.jobs = None
        self.build = None
        self.last_ping = None
        self.phone_status = None
        self.filehandler = None
        self.s3_bucket = None
        self.treeherder = None

    def is_alive(self):
        """Call from main process."""
        try:
            if self.options.verbose:
                logger.debug('is_alive: PhoneWorkerSubProcess.p {0!s}, pid {1!s}'.format(
                    self.p, self.p.pid if self.p else None))
            return self.p and self.p.is_alive()
        except Exception:
            logger.exception('is_alive: PhoneWorkerSubProcess.p {0!s}, pid {1!s}'.format(
                self.p, self.p.pid if self.p else None))
        return False

    def start(self, phone_status=None):
        """Call from main process."""
        logger.debug('PhoneWorkerSubProcess:starting: {0!s} {1!s}'.format(self.phone.id,
                                                                phone_status))
        if self.p:
            if self.is_alive():
                logger.debug('PhoneWorkerSubProcess:start - {0!s} already alive'.format(
                             self.phone.id))
                return
            del self.p
        self.phone_status = phone_status
        self.p = multiprocessing.Process(target=self.run, name=self.phone.id)
        self.p.start()
        logger.debug('PhoneWorkerSubProcess:started: {0!s} {1!s}'.format(self.phone.id,
                                                               self.p.pid))

    def stop(self):
        """Call from main process."""
        logger.debug('PhoneWorkerSubProcess:stopping {0!s}'.format(self.phone.id))
        if self.is_alive():
            logger.debug('PhoneWorkerSubProcess:stop p.terminate() {0!s} {1!s} {2!s}'.format(self.phone.id, self.p, self.p.pid))
            self.p.terminate()
            logger.debug('PhoneWorkerSubProcess:stop p.join() {0!s} {1!s} {2!s}'.format(self.phone.id, self.p, self.p.pid))
            self.p.join(self.options.phone_command_queue_timeout*2)
            if self.p.is_alive():
                logger.debug('PhoneWorkerSubProcess:stop killing %s %s '
                             'stuck process %s' %
                             (self.phone.id, self.p, self.p.pid))
                os.kill(self.p.pid, 9)

    def is_ok(self):
        return (self.phone_status != PhoneStatus.DISCONNECTED and
                self.phone_status != PhoneStatus.ERROR)

    def is_disabled(self):
        return self.phone_status == PhoneStatus.DISABLED

    def update_status(self, build=None, phone_status=None,
                      message=None):
        if phone_status:
            self.phone_status = phone_status
        phone_message = PhoneTestMessage(self.phone, build=build,
                                         phone_status=self.phone_status,
                                         message=message)
        if message != 'Heartbeat':
            self.loggerdeco.info(str(phone_message))
        try:
            self.autophone_queue.put_nowait(phone_message)
        except Queue.Full:
            self.loggerdeco.warning('Autophone queue is full!')

    def heartbeat(self):
        self.update_status(message='Heartbeat')

    def _check_path(self, path):
        self.loggerdeco.debug('Checking path {0!s}.'.format(path))
        success = True
        try:
            d = posixpath.join(path, 'autophone_check_path')
            self.dm.rm(d, recursive=True, force=True, root=True)
            self.dm.mkdir(d, parents=True, root=True)
            self.dm.chmod(d, recursive=True, root=True)
            with tempfile.NamedTemporaryFile() as tmp:
                tmp.write('autophone test\n')
                tmp.flush()
                self.dm.push(tmp.name,
                             posixpath.join(d, 'path_check'))
            self.dm.rm(d, recursive=True, root=True)
        except (ADBError, ADBTimeoutError):
            self.loggerdeco.exception('Exception while checking path {0!s}'.format(path))
            success = False
        return success

    def reboot(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:reboot')
        self.update_status(phone_status=PhoneStatus.REBOOTING)
        self.dm.reboot()
        # Setting svc power stayon true after rebooting is necessary
        # since the setting does not survice reboots.
        self.dm.power_on()
        self.ping()

    def disable_phone(self, errmsg, send_email=True):
        """Completely disable phone. No further attempts to recover it will
        be performed unless initiated by the user."""
        self.loggerdeco.info('Disabling phone: {0!s}.'.format(errmsg))
        if errmsg and send_email:
            self.loggerdeco.info('Sending notification...')
            self.mailer.send('{0!s} {1!s} was disabled'.format(utils.host(),
                                                     self.phone.id),
                             'Phone %s has been disabled:\n'
                             '\n'
                             '%s\n'
                             '\n'
                             'I gave up on it. Sorry about that. '
                             'You can manually re-enable it with '
                             'the "enable" command.' %
                             (self.phone.id, errmsg))
        self.update_status(phone_status=PhoneStatus.DISABLED,
                           message=errmsg)

    def ping(self, test=None, require_ip_address=False):
        """Checks if the device is accessible via adb and that its sdcard and
        /data/local/tmp are accessible. If the device is accessible
        via adb but the sdcard or /data/local/tmp are not accessible,
        the device is rebooted in an attempt to recover.
        """
        for attempt in range(1, self.options.phone_retry_limit+1):
            self.loggerdeco.debug('Pinging phone attempt {0:d}'.format(attempt))
            msg = 'Phone OK'
            phone_status = PhoneStatus.OK
            try:
                state = self.dm.get_state(timeout=60)
            except (ADBError, ADBTimeoutError):
                state = 'missing'
            try:
                if state != 'device':
                    msg = 'Attempt: {0:d}, ping state: {1!s}'.format(attempt, state)
                    phone_status = PhoneStatus.DISCONNECTED
                elif (self.dm.selinux and
                      self.dm.shell_output('getenforce') != 'Permissive'):
                    msg = 'Attempt: {0:d}, SELinux is not permissive'.format(attempt)
                    phone_status = PhoneStatus.ERROR
                    self.dm.shell_output("setenforce Permissive", root=True)
                elif not self._check_path('/data/local/tmp'):
                    msg = 'Attempt: {0:d}, ping path: {1!s}'.format(attempt, '/data/local/tmp')
                    phone_status = PhoneStatus.ERROR
                elif not self._check_path(self.dm.test_root):
                    msg = 'Attempt: {0:d}, ping path: {1!s}'.format(attempt, self.dm.test_root)
                    phone_status = PhoneStatus.ERROR
                elif require_ip_address:
                    try:
                        ip_address = self.dm.get_ip_address()
                    except (ADBError, ADBTimeoutError):
                        ip_address = None
                    if not ip_address:
                        msg = 'Device network offline'
                        phone_status = PhoneStatus.ERROR
                        # If a backup wpa_supplicant.conf is available
                        # in /data/local/tmp/, attempt to recover by
                        # turning off wifi, copying the backup
                        # wpa_supplicant.conf to /data/misc/wifi/,
                        # then turning wifi back on.
                        source_wpa = '/data/local/tmp/wpa_supplicant.conf'
                        dest_wpa = '/data/misc/wifi/wpa_supplicant.conf'
                        if self.dm.exists(source_wpa):
                            self.loggerdeco.info('Resetting wpa_supplicant')
                            self.dm.shell_output('svc wifi disable', root=True)
                            self.dm.shell_output('dd if={0!s} of={1!s}'.format(
                                source_wpa, dest_wpa), root=True)
                            try:
                                # First, attempt to use older chown syntax
                                # chown user.group FILE.
                                self.loggerdeco.debug('attempting chown wifi.wifi')
                                self.dm.shell_output(
                                    'chown wifi.wifi {0!s}'.format(dest_wpa), root=True)
                            except ADBError, e1:
                                if 'No such user' not in e1.message:
                                    # The error is not a chown syntax
                                    # compatibility issue.
                                    raise
                                self.loggerdeco.debug('attempting chown wifi:wifi')
                                # The error was due to a chown
                                # user.group syntax compatibilty
                                # issue, re-attempt to use the newer
                                # chown syntax chown user:group FILE.
                                self.dm.shell_output('chown wifi:wifi {0!s}'.format(
                                                     dest_wpa), root=True)
                            self.dm.shell_output('svc wifi enable', root=True)
                if phone_status == PhoneStatus.OK:
                    break
            except (ADBError, ADBTimeoutError):
                msg = 'Exception pinging device: {0!s}'.format(traceback.format_exc())
                phone_status = PhoneStatus.ERROR
            self.loggerdeco.warning(msg)
            time.sleep(self.options.phone_retry_wait)
            if self.is_ok() and phone_status == PhoneStatus.ERROR:
                # Only reboot if the previous state was ok.
                self.loggerdeco.warning('Rebooting due to ping failure.')
                try:
                    self.dm.reboot()
                except (ADBError, ADBTimeoutError):
                    msg2 = 'Exception rebooting device: {0!s}'.format(traceback.format_exc())
                    self.loggerdeco.warning(msg2)
                    msg += '\n\n' + msg2

        if test:
            test_msg = 'during {0!s} {1!s}\n'.format(test.name, os.path.basename(test.config_file))
        else:
            test_msg = ''

        if self.is_disabled():
            self.heartbeat()
        elif phone_status == PhoneStatus.ERROR:
            # The phone is in an error state related to its storage or
            # networking and requires user intervention.
            self.loggerdeco.warning('Phone is in an error state {0!s} {1!s}.'.format(phone_status, msg))
            if self.is_ok():
                msg_subject = ('{0!s} {1!s} is in an error state {2!s}'.format(utils.host(), self.phone.id, phone_status))
                msg_body = ("Phone {0!s} requires intervention:\n\n{1!s}\n\n{2!s}\n".format(self.phone.id, msg, test_msg))
                msg_body += ("I'll keep trying to ping it periodically "
                             "in case it reappears.")
                self.mailer.send(msg_subject, msg_body)
            self.update_status(phone_status=phone_status)
        elif phone_status == PhoneStatus.DISCONNECTED:
            # If the phone is disconnected, there is nothing we can do
            # to recover except reboot the host.
            self.loggerdeco.warning('Phone is in an error state {0!s} {1!s}.'.format(phone_status, msg))
            if self.is_ok():
                msg_subject = ('{0!s} {1!s} is in an error state {2!s}'.format(utils.host(), self.phone.id, phone_status))
                msg_body = ("Phone {0!s} is unusable:\n\n{1!s}\n\n{2!s}\n".format(self.phone.id, msg, test_msg))
                if self.options.reboot_on_error:
                    msg_body += ("I'll reboot after shutting down cleanly "
                                 "which will hopefully recover.")
                else:
                    msg_body += ("I'll keep trying to ping it periodically "
                                 "in case it reappears.")
                self.mailer.send(msg_subject, msg_body)
            self.update_status(phone_status=phone_status)
        elif not self.is_ok():
            # The phone has recovered and is usable again.
            self.loggerdeco.warning('Phone has recovered.')
            self.mailer.send('{0!s} {1!s} has recovered'.format(utils.host(),
                                                      self.phone.id),
                             'Phone {0!s} is now usable.'.format(self.phone.id))
            self.update_status(phone_status=PhoneStatus.OK)

        self.last_ping = datetime.datetime.now()
        return msg

    def check_battery(self, test):
        if self.dm.get_battery_percentage() < self.options.device_battery_min:
            while self.dm.get_battery_percentage() < self.options.device_battery_max:
                self.update_status(phone_status=PhoneStatus.CHARGING,
                                   build=self.build)
                command = self.process_autophone_cmd(test=test, wait_time=60)
                if command['interrupt']:
                    return command
                if self.state == ProcessStates.SHUTTINGDOWN:
                    return {'interrupt': True,
                            'reason': 'Shutdown while charging',
                            'test_result': PhoneTestResult.RETRY}

        return {'interrupt': False, 'reason': '', 'test_result': None}

    def cancel_test(self, test_guid):
        """Cancel a job.

        If the test is currently queued up in run_tests(), mark it as
        canceled, then delete the test from the entry in the jobs
        database and we are done. There is no need to notify
        treeherder as it will handle marking the job as cancelled.

        """
        self.loggerdeco.debug('cancel_test: test.job_guid {0!s}'.format(test_guid))
        tests = PhoneTest.match(job_guid=test_guid)
        if tests:
            assert len(tests) == 1, "test.job_guid {0!s} is not unique".format(test_guid)
            for test in tests:
                test.test_result.status = PhoneTestResult.USERCANCEL
        self.jobs.cancel_test(test_guid, device=self.phone.id)

    def install_build(self, job):
        ### Why are we retrying here? is it helpful at all?
        """Install the build for this job.

        returns {success: Boolean, message: ''}
        """
        self.update_status(phone_status=PhoneStatus.INSTALLING,
                           build=self.build,
                           message='{0!s} {1!s}'.format(job['tree'], job['build_id']))
        self.loggerdeco.info('Installing build {0!s}.'.format(self.build.id))
        # Record start time for the install so can track how long this takes.
        start_time = datetime.datetime.now()
        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            uninstalled = False
            if not self.is_ok():
                break
            try:
                # Uninstall all org.mozilla.(fennec|firefox) packages
                # to make sure there are no previous installations of
                # different versions of fennec which may interfere
                # with the test.
                mozilla_packages = [
                    p.replace('package:', '') for p in
                    self.dm.shell_output("pm list package org.mozilla").split()
                    if re.match('package:.*(fennec|firefox)', p)]
                for p in mozilla_packages:
                    self.dm.uninstall_app(p)
                if self.dm.is_app_installed(FLASH_PACKAGE):
                    self.dm.uninstall_app(FLASH_PACKAGE)
                self.reboot()
                uninstalled = True
                break
            except ADBError, e:
                if e.message.find('Failure') != -1:
                    # Failure indicates the failure was due to the
                    # app not being installed.
                    uninstalled = True
                break
                message = 'Exception uninstalling fennec attempt {0:d}!\n\n{1!s}'.format(
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception uninstalling fennec '
                                          'attempt %d' % attempt)
                self.ping()
            except ADBTimeoutError, e:
                message = 'Timed out uninstalling fennec attempt {0:d}!\n\n{1!s}'.format(
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout uninstalling fennec '
                                          'attempt %d' % attempt)
                self.ping()
            time.sleep(self.options.phone_retry_wait)

        if not uninstalled:
            self.loggerdeco.warning('Failed to uninstall fennec.')
            return {'success': False, 'message': message}

        message = ''
        for attempt in range(1, self.options.phone_retry_limit+1):
            if not self.is_ok():
                break
            try:
                self.dm.install_app(os.path.join(self.build.dir,
                                                'build.apk'))
                stop_time = datetime.datetime.now()
                self.loggerdeco.info('Install build {0!s} elapsed time: {1!s}'.format(*(
                    (job['build_url'], stop_time - start_time))))
                return {'success': True, 'message': ''}
            except ADBError, e:
                message = 'Exception installing fennec attempt {0:d}!\n\n{1!s}'.format(
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Exception installing fennec '
                                          'attempt %d' % attempt)
                self.ping()
            except ADBTimeoutError, e:
                message = 'Timed out installing fennec attempt {0:d}!\n\n{1!s}'.format(
                    attempt, traceback.format_exc())
                self.loggerdeco.exception('Timedout installing fennec '
                                          'attempt %d' % attempt)
                self.ping()
            time.sleep(self.options.phone_retry_wait)

        self.loggerdeco.warning('Failed to uninstall fennec.')
        return {'success': False, 'message': message}

    def run_tests(self, job):
        """Install build, run tests, report results and uninstall build.
        Returns True if the caller should call job_completed to remove
        the job from the jobs database.

        If an individual test fails to complete, it is re-inserted
        into the jobs database with a new job row but the same number
        of attempts as the original. It will be retried for up to
        jobs.Jobs.MAX_ATTEMPTS times. Therefore even if individual
        tests fail to complete but all of the tests are actually
        attempted to run, we return True to delete the original job.

        In cases where the test run is interrupted by a command or a
        device failure, we will return False to the caller where the
        caller will not delete the original job and will decrement its
        attempts so that jobs are not deleted due to device errors.
        """
        command = self.process_autophone_cmd(test=None)
        if (command['interrupt'] or
            self.state == ProcessStates.SHUTTINGDOWN or
            self.is_disabled()):
            return False
        install_status = self.install_build(job)
        if not install_status['success']:
            self.loggerdeco.info('Not running tests due to {0!s}'.format((
                install_status['message'])))
            return False

        self.loggerdeco.info('Running tests for job {0!s}'.format(job))
        for t in job['tests']:
            if t.test_result.status == PhoneTestResult.USERCANCEL:
                self.loggerdeco.info('Skipping Cancelled test {0!s}'.format(t.name))
                continue
            command = self.process_autophone_cmd(test=t)
            if (command['interrupt'] or
                self.state == ProcessStates.SHUTTINGDOWN or
                self.is_disabled()):
                return False
            if (self.state == ProcessStates.SHUTTINGDOWN or
                self.is_disabled() or not self.is_ok()):
                self.loggerdeco.info('Skipping test {0!s}'.format(t.name))
                job['attempts'] -= 1
                self.jobs.set_job_attempts(job['id'], job['attempts'])
                return False
            self.loggerdeco.info('Running test {0!s}'.format(t.name))
            is_test_completed = False
            # Save the test's job_quid since it will be reset during
            # the test's tear_down and we will need it to complete the
            # test.
            test_job_guid = t.job_guid
            try:
                t.setup_job()
                # Note that check_battery calls process_autophone_cmd
                # which can receive commands to cancel the currently
                # running test.
                command = self.check_battery(t)
                if command['interrupt']:
                    t.handle_test_interrupt(command['reason'],
                                            command['test_result'])
                else:
                    try:
                        if self.is_ok() and not self.is_disabled():
                            is_test_completed = t.run_job()
                    except (ADBError, ADBTimeoutError):
                        self.loggerdeco.exception('device error during '
                                                  '%s.run_job' % t.name)
                        message = ('Uncaught device error during {0!s}.run_job\n\n{1!s}'.format(
                                   t.name, traceback.format_exc()))
                        t.test_failure(
                            t.name,
                            'TEST-UNEXPECTED-FAIL',
                            message,
                            PhoneTestResult.EXCEPTION)
                        self.ping(test=t)
            except:
                self.loggerdeco.exception('device error during '
                                          '%s.setup_job.' % t.name)
                message = ('Uncaught device error during {0!s}.setup_job.\n\n{1!s}'.format(
                           t.name, traceback.format_exc()))
                t.test_failure(t.name, 'TEST-UNEXPECTED-FAIL',
                               message, PhoneTestResult.EXCEPTION)
                self.ping(test=t)

            if (t.test_result.status != PhoneTestResult.USERCANCEL and
                not is_test_completed and
                job['attempts'] < jobs.Jobs.MAX_ATTEMPTS):
                # This test did not run successfully and we have not
                # exceeded the maximum number of attempts, therefore
                # mark this attempt as a RETRY.
                t.test_result.status = PhoneTestResult.RETRY
            try:
                t.teardown_job()
            except:
                self.loggerdeco.exception('device error during '
                                          '%s.teardown_job' % t.name)
                message = ('Uncaught device error during {0!s}.teardown_job\n\n{1!s}'.format(
                           t.name, traceback.format_exc()))
                t.test_failure(t.name, 'TEST-UNEXPECTED-FAIL',
                               message, PhoneTestResult.EXCEPTION)
            # Remove this test from the jobs database whether or not it
            # ran successfully.
            self.jobs.test_completed(test_job_guid)
            if (t.test_result.status != PhoneTestResult.USERCANCEL and
                not is_test_completed and
                job['attempts'] < jobs.Jobs.MAX_ATTEMPTS):
                # This test did not run successfully and we have not
                # exceeded the maximum number of attempts, therefore
                # re-add this test with a new guid so that Treeherder
                # will generate a new job for the next attempt.
                #
                # We must do this after tearing down the job since the
                # t.guid will change as a result of the call to
                # self.jobs.new_job.
                self.jobs.new_job(job['build_url'],
                                  build_id=job['build_id'],
                                  changeset=job['changeset'],
                                  tree=job['tree'],
                                  revision=job['revision'],
                                  revision_hash=job['revision_hash'],
                                  tests=[t],
                                  enable_unittests=job['enable_unittests'],
                                  device=self.phone.id,
                                  attempts=job['attempts'])
                self.treeherder.submit_pending(self.phone.id,
                                               job['build_url'],
                                               job['tree'],
                                               job['revision_hash'],
                                               tests=[t])

        try:
            if self.is_ok():
                self.dm.uninstall_app(self.build.app_name)
        except:
            self.loggerdeco.exception('device error during '
                                      'uninstall_app %s' % self.build.app_name)
        return True

    def handle_timeout(self):
        if (not self.is_disabled() and
            (not self.last_ping or
             (datetime.datetime.now() - self.last_ping >
              datetime.timedelta(seconds=self.options.phone_ping_interval)))):
            self.ping()

    def handle_job(self, job):
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_job: {0!s}, {1!s}'.format(
            self.phone, job))
        self.loggerdeco.info('Checking job {0!s}.'.format(job['build_url']))
        client = buildserver.BuildCacheClient(port=self.options.build_cache_port)
        self.update_status(phone_status=PhoneStatus.FETCHING,
                           message='{0!s} {1!s}'.format(job['tree'], job['build_id']))
        test_package_names = set()
        for t in job['tests']:
            test_package_names.update(t.get_test_package_names())
        cache_response = client.get(
            job['build_url'],
            enable_unittests=job['enable_unittests'],
            test_package_names=test_package_names)
        client.close()
        if not cache_response['success']:
            self.loggerdeco.warning('Errors occured getting build {0!s}: {1!s}'.format(job['build_url'], cache_response['error']))
            return
        self.build = BuildMetadata().from_json(cache_response['metadata'])
        self.loggerdeco.info('Starting job {0!s}.'.format(job['build_url']))
        starttime = datetime.datetime.now()
        if self.run_tests(job):
            self.loggerdeco.info('Job completed.')
            self.jobs.job_completed(job['id'])
        else:
            # Decrement the job attempts so that the remaining
            # tests aren't dropped simply due to a device error or
            # user command.
            job['attempts'] -= 1
            self.loggerdeco.debug(
                'Shutting down... Reset job id {0:d} attempts to {1:d}.'.format(job['id'], job['attempts']))
            self.jobs.set_job_attempts(job['id'], job['attempts'])
        for t in self.tests:
            if t.test_result.status == PhoneTestResult.USERCANCEL:
                self.loggerdeco.warning(
                    'Job %s, Cancelled Test: %s was not reset after '
                    'the Job completed' % (job, t))
                t.test_result.status = PhoneTestResult.SUCCESS
        if self.is_ok() and not self.is_disabled():
            self.update_status(phone_status=PhoneStatus.IDLE,
                               build=self.build)
        stoptime = datetime.datetime.now()
        self.loggerdeco.info('Job elapsed time: {0!s}'.format((stoptime - starttime)))

    def handle_cmd(self, request, current_test=None):
        """Execute the command dispatched from the Autophone process.

        handle_cmd is used in the worker's main_loop method and in a
        test's run_job method to process pending Autophone
        commands. It returns a dict which is used by tests to
        determine if the currently running test should be terminated
        as a result of the command.

        :param request: tuple containing the command name and
            necessary argument values.

        :param current_test: currently running test. Defaults to
            None. A running test will pass this parameter which will
            be used to determine if a cancel_test request pertains to
            the currently running test and thus should be terminated.

        :returns: {'interrupt': boolean, True if current activity should be aborted
                   'reason': message to be used to indicate reason for interruption,
                   'test_result': PhoneTestResult to be used for the test result}
        """
        self.loggerdeco.debug('PhoneWorkerSubProcess:handle_cmd')
        command = {'interrupt': False, 'reason': '', 'test_result': None}
        if not request:
            self.loggerdeco.debug('handle_cmd: No request')
        elif request[0] == 'shutdown':
            self.loggerdeco.info('Shutting down at user\'s request...')
            self.state = ProcessStates.SHUTTINGDOWN
        elif request[0] == 'job':
            # This is just a notification that breaks us from waiting on the
            # command queue; it's not essential, since jobs are stored in
            # a db, but it allows the worker to react quickly to a request if
            # it isn't doing anything else.
            self.loggerdeco.debug('Received job command request...')
        elif request[0] == 'reboot':
            self.loggerdeco.info("Rebooting at user's request...")
            self.reboot()
            command['interrupt'] = True
            command['reason'] = 'Worker rebooted by administrator'
            command['test_result'] = PhoneTestResult.RETRY
        elif request[0] == 'disable':
            self.disable_phone("Disabled at user's request", False)
            command['interrupt'] = True
            command['reason'] = 'Worker disabled by administrator'
            command['test_result'] = PhoneTestResult.USERCANCEL
        elif request[0] == 'enable':
            self.loggerdeco.info("Enabling phone at user's request...")
            if self.is_disabled():
                self.update_status(phone_status=PhoneStatus.IDLE)
                self.last_ping = None
        elif request[0] == 'cancel_test':
            self.loggerdeco.info('Received cancel_test request {0!s}'.format(list(request)))
            (test_guid,) = request[1]
            self.cancel_test(test_guid)
            if current_test and current_test.job_guid == test_guid:
                command['interrupt'] = True
                command['reason'] = 'Running Job Canceled'
                command['test_result'] = PhoneTestResult.USERCANCEL
        elif request[0] == 'ping':
            self.loggerdeco.info("Pinging at user's request...")
            self.ping()
        else:
            self.loggerdeco.debug('handle_cmd: Unknown request {0!s}'.format(request[0]))
        return command

    def process_autophone_cmd(self, test=None, wait_time=1, require_ip_address=False):
        """Process any outstanding commands received from the main process,
        then check on the phone's status to see if the device is healthy
        enough to continue testing.
        """
        while True:
            try:
                self.heartbeat()
                request = self.queue.get(True, wait_time)
                command = self.handle_cmd(request, current_test=test)
                if command['interrupt']:
                    return command
            except Queue.Empty:
                reason = self.ping(test=test, require_ip_address=require_ip_address)
                if self.is_ok():
                    return {'interrupt': False,
                            'reason': '',
                            'test_result': None}
                else:
                    return {'interrupt': True,
                            'reason': reason,
                            'test_result': PhoneTestResult.RETRY}

    def main_loop(self):
        self.loggerdeco.debug('PhoneWorkerSubProcess:main_loop')
        # Commands take higher priority than jobs, so we deal with all
        # immediately available commands, then start the next job, if there is
        # one.  If neither a job nor a command is currently available,
        # block on the command queue for PhoneWorker.PHONE_COMMAND_QUEUE_TIMEOUT seconds.
        request = None
        while True:
            while True:
                try:
                    (pid, status, resource) = os.wait3(os.WNOHANG)
                    logger.debug('Reaped {0!s} {1!s}'.format(pid, status))
                except OSError:
                    break
            try:
                self.heartbeat()
                if self.state == ProcessStates.SHUTTINGDOWN:
                    self.update_status(phone_status=PhoneStatus.SHUTDOWN)
                    return
                if not request:
                    request = self.queue.get_nowait()
                self.handle_cmd(request)
                request = None
            except Queue.Empty:
                request = None
                if not self.is_ok():
                    self.ping()
                if self.is_ok():
                    job = self.jobs.get_next_job(lifo=self.options.lifo, worker=self)
                    if job:
                        if not self.is_disabled():
                            self.handle_job(job)
                        else:
                            self.loggerdeco.info('Job skipped because device is disabled: {0!s}'.format(job))
                            for t in job['tests']:
                                if t.test_result.status != PhoneTestResult.USERCANCEL:
                                    t.test_failure(
                                        t.name,
                                        'TEST_UNEXPECTED_FAIL',
                                        'Worker disabled by administrator',
                                        PhoneTestResult.USERCANCEL)
                                self.treeherder.submit_complete(
                                    t.phone.id,
                                    job['build_url'],
                                    job['tree'],
                                    job['revision_hash'],
                                    tests=[t])
                            self.jobs.job_completed(job['id'])
                    else:
                        try:
                            request = self.queue.get(
                                timeout=self.options.phone_command_queue_timeout)
                        except Queue.Empty:
                            request = None
                            self.handle_timeout()

        while True:
            try:
                (pid, status, resource) = os.wait3(os.WNOHANG)
                logger.debug('Reaped {0!s} {1!s}'.format(pid, status))
            except OSError:
                break

    def run(self):
        global logger

        self.state = ProcessStates.RUNNING

        sys.stdout = file(self.outfile, 'a', 0)
        sys.stderr = sys.stdout
        # Complete initialization of PhoneWorkerSubProcess in the new
        # process.
        sensitive_data_filter = SensitiveDataFilter(self.options.sensitive_data)
        logger = logging.getLogger()
        logger.addFilter(sensitive_data_filter)
        logger.propagate = False
        logger.setLevel(self.loglevel)
        # Remove any handlers inherited from the main process.  This
        # prevents these handlers from causing the main process to log
        # the same messages.
        for handler in logger.handlers:
            handler.flush()
            handler.close()
            logger.removeHandler(handler)
        for other_logger_name, other_logger in logger.manager.loggerDict.iteritems():
            if not hasattr(other_logger, 'handlers'):
                continue
            other_logger.addFilter(sensitive_data_filter)
            for other_handler in other_logger.handlers:
                other_handler.flush()
                other_handler.close()
                other_logger.removeHandler(other_handler)
            other_logger.addHandler(logging.NullHandler())

        self.filehandler = logging.handlers.TimedRotatingFileHandler(
            self.logfile,
            when='midnight',
            backupCount=7)
        fileformatstring = ('%(asctime)s|%(process)d|%(threadName)s|%(name)s|'
                            '%(levelname)s|%(message)s')
        fileformatter = logging.Formatter(fileformatstring)
        self.filehandler.setFormatter(fileformatter)
        logger.addHandler(self.filehandler)

        self.loggerdeco = LogDecorator(logger,
                                       {'phoneid': self.phone.id},
                                       '%(phoneid)s|%(message)s')
        # Set the loggers for the imported modules
        for module in (autophonetreeherder, builds, jobs, mailer, phonetest,
                       s3, utils):
            module.logger = logger
        self.loggerdeco.info('Worker: Connecting to {0!s}...'.format(self.phone.id))
        # Override mozlog.logger
        self.dm._logger = self.loggerdeco

        self.jobs = jobs.Jobs(self.mailer,
                              default_device=self.phone.id,
                              allow_duplicates=self.options.allow_duplicate_jobs)

        self.loggerdeco.info('Worker: Connected.')

        for t in self.tests:
            t.loggerdeco_original = None
            t.dm_logger_original = None
            t.loggerdeco = self.loggerdeco
            t.worker_subprocess = self
            t.dm = self.dm
            t.update_status_cb = self.update_status
        if self.options.s3_upload_bucket:
            self.s3_bucket = S3Bucket(self.options.s3_upload_bucket,
                                      self.options.aws_access_key_id,
                                      self.options.aws_access_key)
        self.treeherder = AutophoneTreeherder(self,
                                              self.options,
                                              self.jobs,
                                              s3_bucket=self.s3_bucket,
                                              mailer=self.mailer,
                                              shared_lock=self.shared_lock)
        self.update_status(phone_status=PhoneStatus.IDLE)
        self.ping()
        self.main_loop()

