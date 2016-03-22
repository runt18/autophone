# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import glob
import json
import logging
import os
import re
import tempfile
import time
import urlparse

from thclient import (TreeherderClient, TreeherderJobCollection, TreeherderJob)

import utils

from s3 import S3Error

LEAK_RE = re.compile('\d+ bytes leaked \((.+)\)$')
CRASH_RE = re.compile('.+ application crashed \[@ (.+)\]$')

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

def timestamp_now():
    return int(time.mktime(datetime.datetime.now().timetuple()))


class TestState(object):
    COMPLETED = 'completed'
    PENDING = 'pending'
    RUNNING = 'running'


class AutophoneTreeherder(object):

    def __init__(self, worker_subprocess, options, jobs, s3_bucket=None,
                 mailer=None, shared_lock=None):
        assert options, "options is required."
        assert shared_lock, "shared_lock is required."

        self.options = options
        self.jobs = jobs
        self.s3_bucket = s3_bucket
        self.mailer = mailer
        self.shared_lock = shared_lock
        self.worker = worker_subprocess
        self.shutdown_requested = False
        logger.debug('AutophoneTreeherder')

        self.url = self.options.treeherder_url
        if not self.url:
            logger.debug('AutophoneTreeherder: no treeherder url')
            return

        self.server = self.options.treeherder_server
        self.protocol = self.options.treeherder_protocol
        self.host = self.options.treeherder_server
        self.client_id = self.options.treeherder_client_id
        self.secret = self.options.treeherder_secret
        self.retry_wait = self.options.treeherder_retry_wait
        self.bugscache_uri = '{0!s}/api/bugscache/'.format(self.url)

        logger.debug('AutophoneTreeherder: {0!s}'.format(self))

    def __str__(self):
        # Do not publish sensitive information
        whitelist = ('url',
                     'server',
                     'protocol',
                     'host',
                     'retry_wait',
                     'bugscache_uri')
        d = {}
        for attr in whitelist:
            d[attr] = getattr(self, attr)
        return '{0!s}'.format(d)

    def post_request(self, machine, project, job_collection, attempts, last_attempt):
        logger.debug('AutophoneTreeherder.post_request: {0!s}, attempt={1:d}, last={2!s}'.format(job_collection.__dict__, attempts, last_attempt))
        client = TreeherderClient(protocol=self.protocol,
                                  host=self.server,
                                  client_id=self.client_id,
                                  secret=self.secret)

        try:
            client.post_collection(project, job_collection)
            return True
        except Exception, e:
            logger.exception('Error submitting request to Treeherder, attempt={0:d}, last={1!s}'.format(attempts, last_attempt))
            if self.mailer:
                if e.response:
                    response_json = json.dumps(e.response.json(),
                                               indent=2, sort_keys=True)
                else:
                    response_json = None
                self.mailer.send(
                    '{0!s} attempt {1:d} Error submitting request to Treeherder'.format(utils.host(), attempts),
                    'Phone: %s\n'
                    'TreeherderClientError: %s\n'
                    'Last attempt: %s\n'
                    'Response: %s\n' % (
                        machine,
                        e,
                        response_json,
                        last_attempt))
        return False

    def queue_request(self, machine, project, job_collection):
        logger.debug('AutophoneTreeherder.queue_request: {0!s}'.format(job_collection.__dict__))
        logger.debug('AutophoneTreeherder shared_lock.acquire')
        self.shared_lock.acquire()
        try:
            self.jobs.new_treeherder_job(machine, project, job_collection)
        finally:
            logger.debug('AutophoneTreeherder shared_lock.release')
            self.shared_lock.release()

    def submit_pending(self, machine, build_url, project, revision_hash, tests=[]):
        """Submit tests pending notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_pending: {0!s}'.format(tests))
        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_pending: no url/revision hash')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            t.message = None
            t.submit_timestamp = timestamp_now()
            t.job_details = []

            logger.info('creating Treeherder job %s for %s %s, '
                        'revision_hash: %s' % (
                            t.job_guid, t.name, project,
                            revision_hash))

            logger.debug('AutophoneTreeherder.submit_pending: '
                         'test config_file=%s, config sections=%s' % (
                             t.config_file, t.cfg.sections()))

            tj = tjc.get_job()
            tj.add_tier(self.options.treeherder_tier)
            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.PENDING)
            tj.add_submit_timestamp(t.submit_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_start_timestamp(0)
            tj.add_end_timestamp(0)
            #
            tj.add_machine(machine)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            # Fake the buildername from buildbot...
            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})
            # Create a 'privatebuild' artifact for storing information
            # regarding the build.
            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_pending: tjc: {0!s}'.format((
            tjc.to_json())))

        self.queue_request(machine, project, tjc)

    def submit_running(self, machine, build_url, project, revision_hash, tests=[]):
        """Submit tests running notifications to Treeherder

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_running: {0!s}'.format(tests))
        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_running: no url/revision hash')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_running: '
                         'for %s %s' % (t.name, project))

            t.submit_timestamp = timestamp_now()
            t.start_timestamp = timestamp_now()

            tj = tjc.get_job()
            tj.add_tier(self.options.treeherder_tier)
            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.RUNNING)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            # XXX need to send these until Bug 1066346 fixed.
            tj.add_end_timestamp(0)
            #
            tj.add_machine(machine)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})
            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})
            tjc.add(tj)

        logger.debug('AutophoneTreeherder.submit_running: tjc: {0!s}'.format(
                     tjc.to_json()))

        self.queue_request(machine, project, tjc)

    def submit_complete(self, machine, build_url, project, revision_hash,
                        tests=None):
        """Submit test results for the worker's current job to Treeherder.

        :param machine: machine id
        :param build_url: url to build being tested.
        :param project: repository of build.
        :param revision_hash: Treeherder revision hash of build.
        :param tests: Lists of tests to be reported.
        """
        logger.debug('AutophoneTreeherder.submit_complete: {0!s}'.format(tests))

        if not self.url or not revision_hash:
            logger.debug('AutophoneTreeherder.submit_complete: no url/revision hash')
            return

        tjc = TreeherderJobCollection()

        for t in tests:
            logger.debug('AutophoneTreeherder.submit_complete '
                         'for %s %s' % (t.name, project))

            t.job_details.append({
                'value': os.path.basename(t.config_file),
                'content_type': 'text',
                'title': 'Config'})
            t.job_details.append({
                'url': build_url,
                'value': os.path.basename(build_url),
                'content_type': 'link',
                'title': 'Build'})
            t.job_details.append({
                'value': utils.host(),
                'content_type': 'text',
                'title': 'Host'})

            t.end_timestamp = timestamp_now()
            # A usercancelled job may not have a start_timestamp
            # since it may have been cancelled before it started.
            if not t.start_timestamp:
                t.start_timestamp = t.end_timestamp

            if t.test_result.failed == 0:
                failed = '0'
            else:
                failed = '<em class="testfail">{0!s}</em>'.format(t.test_result.failed)

            t.job_details.append({
                'value': "{0!s}/{1!s}/{2!s}".format(t.test_result.passed, failed, t.test_result.todo),
                'content_type': 'raw_html',
                'title': "{0!s}-{1!s}".format(t.job_name, t.job_symbol)
            })

            if hasattr(t, 'phonedash_url'):
                t.job_details.append({
                    'url': t.phonedash_url,
                    'value': 'graph',
                    'content_type': 'link',
                    'title': 'phonedash'
                    })

            if (hasattr(t, 'perfherder_artifact') and
                hasattr(t, 'perfherder_signature')):
                perfherder_url = ('https://{0!s}/perf.html#/graphs?series'.format(
                                  self.server))
                url = "{0!s}=[{1!s},{2!s},1]".format(perfherder_url, project, t.perfherder_signature)
                t.job_details.append({
                    'url': url,
                    'value': 'graph',
                    'content_type': 'link',
                    'title': 'perfherder'
                    })

            tj = tjc.get_job()

            # Attach logs, ANRs, tombstones, etc.

            logurl = None
            logname = None
            if self.s3_bucket:
                # We must make certain that S3 keys for uploaded files
                # are unique. We can create a unique log_identifier as
                # follows: For Unittests, t.unittest_logpath's
                # basename contains a unique name based on the actual
                # Unittest name, chunk and machine id. For
                # Non-Unittests, the test classname, chunk and machine
                # id can be used.

                if t.unittest_logpath:
                    log_identifier = os.path.splitext(os.path.basename(
                        t.unittest_logpath))[0]
                else:
                    log_identifier = "{0!s}-{1!s}-{2!s}-{3!s}".format(
                        t.name, os.path.basename(t.config_file), t.chunk,
                        machine)
                # We must make certain the key is unique even in the
                # event of retries.
                log_identifier = '{0!s}-{1!s}'.format(log_identifier, t.job_guid)

                key_prefix = os.path.dirname(
                    urlparse.urlparse(build_url).path)
                key_prefix = re.sub('/tmp$', '', key_prefix)

                # Logcat
                fname = '{0!s}-logcat.log'.format(log_identifier)
                lname = 'logcat'
                key = "{0!s}/{1!s}".format(key_prefix, fname)
                with tempfile.NamedTemporaryFile(suffix='logcat.txt') as f:
                    try:
                        if self.worker.is_ok():
                            for line in t.logcat.get(full=True):
                                f.write('{0!s}\n'.format(line.encode('UTF-8',
                                                             errors='replace')))
                            t.logcat.reset()
                        else:
                            # Device is in an error state so we can't
                            # get the full logcat but we can output
                            # any logcat output we accumulated
                            # previously.
                            for line in t.logcat._accumulated_logcat:
                                f.write('{0!s}\n'.format(line.encode('UTF-8',
                                                             errors='replace')))
                    except Exception, e:
                        logger.exception('Error reading logcat {0!s}'.format(fname))
                        t.job_details.append({
                            'value': 'Failed to read {0!s}: {1!s}'.format(fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                    try:
                        url = self.s3_bucket.upload(f.name, key)
                        t.job_details.append({
                            'url': url,
                            'value': lname,
                            'content_type': 'link',
                            'title': 'artifact uploaded'})
                    except S3Error, e:
                        logger.exception('Error uploading logcat {0!s}'.format(fname))
                        t.job_details.append({
                            'value': 'Failed to upload {0!s}: {1!s}'.format(fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                # Upload directory containing ANRs, tombstones and other items
                # to be uploaded.
                if t.upload_dir:
                    for f in glob.glob(os.path.join(t.upload_dir, '*')):
                        try:
                            lname = os.path.basename(f)
                            fname = '{0!s}-{1!s}'.format(log_identifier, lname)
                            url = self.s3_bucket.upload(f, "{0!s}/{1!s}".format(
                                key_prefix, fname))
                            t.job_details.append({
                                'url': url,
                                'value': lname,
                                'content_type': 'link',
                                'title': 'artifact uploaded'})
                        except S3Error, e:
                            logger.exception('Error uploading artifact {0!s}'.format(fname))
                            t.job_details.append({
                                'value': 'Failed to upload artifact {0!s}: {1!s}'.format(fname, e),
                                'content_type': 'text',
                                'title': 'Error'})

                # Bug 1113264 - Multiple job logs push action buttons outside
                # the job details navbar
                #
                # Due to the way Treeherder UI displays log buttons in the
                # Job Info panel, it is important to only specify one log
                # file to prevent the multiple log buttons from hiding the
                # retrigger button. If the test is a Unit Test, its log
                # will marked as the log file. Otherwise, the Autophone
                # log will be marked as the log file.

                # UnitTest Log
                if t.unittest_logpath and os.path.exists(t.unittest_logpath):
                    fname = '{0!s}.log'.format(log_identifier)
                    logname = os.path.basename(t.unittest_logpath)
                    key = "{0!s}/{1!s}".format(key_prefix, fname)
                    try:
                        logurl = self.s3_bucket.upload(t.unittest_logpath, key)
                        tj.add_log_reference(fname, logurl,
                                             parse_status='parsed')
                        t.job_details.append({
                            'url': logurl,
                            'value': logname,
                            'content_type': 'link',
                            'title': 'artifact uploaded'})
                    except Exception, e:
                        logger.exception('Error {0!s} uploading log {1!s}'.format(
                            e, fname))
                        t.job_details.append({
                            'value': 'Failed to upload log {0!s}: {1!s}'.format(fname, e),
                            'content_type': 'text',
                            'title': 'Error'})
                # Autophone Log
                # Since we are submitting results to Treeherder, we flush
                # the worker's log before uploading the log to
                # Treeherder. When we upload the log, it will contain
                # results for a single test run with possibly an error
                # message from the previous test if the previous log
                # upload failed.
                if t.test_logfile:
                    try:
                        t.test_logfilehandler.flush()
                        fname = '{0!s}-autophone.log'.format(log_identifier)
                        lname = 'Autophone Log'
                        key = "{0!s}/{1!s}".format(key_prefix, fname)
                        url = self.s3_bucket.upload(t.test_logfile, key)
                        t.job_details.append({
                            'url': url,
                            'value': lname,
                            'content_type': 'link',
                            'title': 'artifact uploaded'})
                        if not logurl:
                            tj.add_log_reference(fname, url,
                                                 parse_status='parsed')
                            logurl = url
                            logname = fname
                    except Exception, e:
                        logger.exception('Error {0!s} uploading {1!s}'.format(
                            e, fname))
                        t.job_details.append({
                            'value': 'Failed to upload Autophone log: {0!s}'.format(e),
                            'content_type': 'text',
                            'title': 'Error'})

            tj.add_tier(self.options.treeherder_tier)
            tj.add_revision_hash(revision_hash)
            tj.add_project(project)
            tj.add_job_guid(t.job_guid)
            tj.add_job_name(t.job_name)
            tj.add_job_symbol(t.job_symbol)
            tj.add_group_name(t.group_name)
            tj.add_group_symbol(t.group_symbol)
            tj.add_product_name('fennec')
            tj.add_state(TestState.COMPLETED)
            tj.add_result(t.test_result.status)
            tj.add_submit_timestamp(t.submit_timestamp)
            tj.add_start_timestamp(t.start_timestamp)
            tj.add_end_timestamp(t.end_timestamp)
            tj.add_machine(machine)
            tj.add_build_info('android', t.phone.platform, t.phone.architecture)
            tj.add_machine_info('android',t.phone.platform, t.phone.architecture)
            tj.add_option_collection({'opt': True})

            error_lines = []
            for failure in t.test_result.failures:
                line = ''
                status = failure['status']
                test = failure['test']
                text = failure['text']
                if not (status or test or text):
                    continue
                if status and test and text:
                    line = '{0!s} | {1!s} | {2!s}'.format(status, test, text)
                elif test and text:
                    line = '{0!s} | {1!s}'.format(test, text)
                elif text:
                    line = text
                # XXX Need to update the log parser to return the line
                # numbers of the errors.
                if line:
                    error_lines.append({"line": line, "linenumber": 1})

            text_log_summary = {
                'header': {
                    'slave': machine,
                    'revision': revision_hash
                },
                'step_data': {
                    'all_errors': error_lines,
                    'steps': [
                        {
                            'name': 'step',
                            'started_linenumber': 1,
                            'finished_linenumber': 1,
                            'duration': t.end_timestamp - t.start_timestamp,
                            'finished': '{0!s}'.format(datetime.datetime.fromtimestamp(t.end_timestamp)),
                            'errors': error_lines,
                            'error_count': len(error_lines),
                            'order': 0,
                            'result': t.test_result.status
                        },
                    ],
                    'errors_truncated': False
                    },
                'logurl': logurl,
                'logname': logname
                }

            tj.add_artifact('text_log_summary', 'json', json.dumps(text_log_summary))
            logger.debug('AutophoneTreeherder.submit_complete: text_log_summary: {0!s}'.format(json.dumps(text_log_summary)))

            tj.add_artifact('Job Info', 'json', {'job_details': t.job_details})
            tj.add_artifact('buildapi', 'json', {
                'buildername': t.get_buildername(project)})

            tj.add_artifact('privatebuild', 'json', {
                'build_url': build_url,
                'config_file': t.config_file,
                'chunk': t.chunk})

            if hasattr(t, 'perfherder_artifact') and t.perfherder_artifact:
                jsondata = json.dumps({'performance_data': t.perfherder_artifact})
                logger.debug("AutophoneTreeherder.submit_complete: perfherder_artifact: {0!s}".format(jsondata))
                tj.add_artifact('performance_data', 'json', jsondata)

            tjc.add(tj)
            message = 'TestResult: {0!s} {1!s} {2!s}'.format(t.test_result.status, t.name, build_url)
            if t.message:
                message += ', {0!s}'.format(t.message)
            logger.info(message)

        logger.debug('AutophoneTreeherder.submit_completed: tjc: {0!s}'.format(
                     tjc.to_json()))

        self.queue_request(machine, project, tjc)

    def serve_forever(self):
        while not self.shutdown_requested:
            wait = True
            job = self.jobs.get_next_treeherder_job()
            if job:
                tjc = TreeherderJobCollection()
                for data in job['job_collection']:
                    tj = TreeherderJob(data)
                    tjc.add(tj)
                if self.post_request(job['machine'], job['project'], tjc, job['attempts'], job['last_attempt']):
                    self.jobs.treeherder_job_completed(job['id'])
                    wait = False
            if wait:
                for i in range(self.retry_wait):
                    if self.shutdown_requested:
                        break
                    time.sleep(1)

    def shutdown(self):
        self.shutdown_requested = True
