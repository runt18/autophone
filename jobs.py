# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import json
import logging
import os
import sqlite3
import time
import traceback

import utils

# Set the logger globally in the file, but this must be reset when
# used in a child process.
logger = logging.getLogger()

class Jobs(object):

    MAX_ATTEMPTS = 3
    SQL_RETRY_DELAY = 60
    SQL_MAX_RETRIES = 10

    def __init__(self, mailer, default_device=None, allow_duplicates=False):
        self.mailer = mailer
        self.default_device = default_device
        self.filename = 'jobs.sqlite'
        self.allow_duplicates = allow_duplicates

        if not os.path.exists(self.filename):
            conn = self._conn()
            conn.execute('create table jobs ('
                         'id integer primary key, '
                         'created text, '
                         'last_attempt text, '
                         'build_url text, '
                         'build_id text, '
                         'changeset text, '
                         'tree text, '
                         'revision text, '
                         'revision_hash, '
                         'enable_unittests int, '
                         'attempts int, '
                         'device text)')
            conn.execute('create table tests ('
                         'id integer primary key, '
                         'name text, '
                         'config_file text, '
                         'chunk int, '
                         'guid text, '
                         'repos text, '
                         'jobid integer)')
            conn.execute('create table treeherder ('
                         'id integer primary key, '
                         'attempts int, '
                         'last_attempt text, '
                         'machine text,'
                         'project text,'
                         'job_collection text)')
            conn.commit()
            conn.close()

    def report_sql_error(self, attempt, email_sent, sql, values):
        message = '{0!s} {1!s}'.format(sql, values)
        logger.exception(message)
        if attempt > self.SQL_MAX_RETRIES and not email_sent:
            email_sent = True
            email_subject = '{0!s} jobs SQL Error'.format(utils.host())
            email_body = (
                'Attempt %d to execute %s failed.\n'
                '%s'
                'Waiting for %d seconds.' %
                (attempt, message, traceback.format_exc(), self.SQL_RETRY_DELAY))
            self.mailer.send(email_subject, email_body)
            logger.info('Sent mail notification about jobs database sql error.')
            time.sleep(self.SQL_RETRY_DELAY)
        return email_sent

    def _conn(self):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn = sqlite3.connect(self.filename)
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    'connect({0!s})'.format(self.filename),
                    None)
        return conn

    def _commit_connection(self, conn):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn.commit()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    '_commit_connection({0!s})'.format(self.filename),
                    None)

    def _close_connection(self, conn):
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                conn.close()
                break
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(
                    attempt, email_sent,
                    '_close_connection({0!s})'.format(self.filename),
                    None)

    def _execute_sql(self, conn, sql, values=()):
        """Execute sql statement.

        Returns the cursor which executed the statement if no error
        occured, otherwise it keeps trying until it succeeds.
        """
        attempt = 0
        email_sent = False
        while True:
            attempt += 1
            try:
                return conn.execute(sql, values)
            except sqlite3.OperationalError:
                email_sent = self.report_sql_error(attempt, email_sent,
                                                   sql, values)

    def clear_all(self):
        conn = self._conn()
        self._execute_sql(conn, 'delete from tests')
        self._execute_sql(conn, 'delete from jobs')
        self._execute_sql(conn, 'delete from treeherder')
        self._commit_connection(conn)
        self._close_connection(conn)

    def new_job(self, build_url, build_id=None, changeset=None, tree=None,
                revision=None, revision_hash=None, tests=None,
                enable_unittests=False, device=None,
                attempts=0):
        logger.debug('jobs.new_job: {0!s} {1!s} {2!s} {3!s} {4!s} {5!s} {6!s} {7!s} {8!s} {9!s}'.format(
            build_url, build_id, changeset, tree, revision, revision_hash,
            tests, enable_unittests, device, attempts))
        if not device:
            device = self.default_device
        now = datetime.datetime.now().isoformat()

        conn = self._conn()
        job_id = None
        if not self.allow_duplicates:
            job_cursor = self._execute_sql(
                conn,
                'select id from jobs where device=? and build_url=?',
                values=(device, build_url))

            job = job_cursor.fetchone()
            job_cursor.close()
            if job:
                job_id = job[0]
        if not job_id:
            job_cursor = self._execute_sql(
                conn,
                'insert into jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                values=(None, now, None, build_url, build_id, changeset, tree,
                        revision, revision_hash, enable_unittests, attempts, device))
            job_id = job_cursor.lastrowid
            job_cursor.close()

        new_tests = []
        for test in tests:
            repos = json.dumps(test.repos)
            if not self.allow_duplicates:
                test_cursor = self._execute_sql(
                    conn,
                    'select * from tests where '
                    'name=? and config_file=? and chunk=? and repos=? and jobid=?',
                    values=(test.name, test.config_file, test.chunk, repos, job_id))
                test_row = test_cursor.fetchone()
                test_cursor.close()
                if test_row:
                    logger.warning(
                        'jobs.new_job: duplicate test: %s, device: %s, '
                        'name: %s, config_file: %s, chunk: %s, repos: %s' % (
                            build_url, device, test.name, test.config_file,
                            test.chunk, repos))
                    continue
            new_tests.append(test)
            test.generate_guid()
            self._execute_sql(
                conn,
                'insert into tests values (?, ?, ?, ?, ?, ?, ?)',
                values=(None, test.name, test.config_file, test.chunk,
                        test.job_guid, repos, job_id))
        self._commit_connection(conn)
        self._close_connection(conn)

        return new_tests

    def jobs_pending(self, device=None):
        conn = self._conn()
        if not device:
            device = self.default_device
        cursor = self._execute_sql(
            conn,
            'select count(id) from jobs where device=?',
            values=(device,))
        count = cursor.fetchone()[0]
        cursor.close()
        self._close_connection(conn)
        return count

    def set_job_attempts(self, jobid, attempts):
        conn = self._conn()

        self._execute_sql(
            conn,
            'update jobs set attempts=? where id=?',
            values=(attempts, jobid))
        self._commit_connection(conn)

    def get_next_job(self, lifo=False, device=None, worker=None):
        if not device:
            device = self.default_device
        order = 'desc' if lifo else 'asc'

        conn = self._conn()

        # Find the ids of the jobs whose attempts exceed the maximum.
        # First delete the associated tests, then the jobs.
        job_cursor = self._execute_sql(
            conn,
            'select id from jobs where device=? and attempts>=?',
            values=(device, self.MAX_ATTEMPTS))
        job_ids = [job[0] for job in job_cursor]
        for job_id in job_ids:
            self._execute_sql(conn, 'delete from tests where jobid=?',
                              values=(job_id,))
        job_cursor.close()
        self._execute_sql(
            conn,
            'delete from jobs where device=? and attempts>=?',
            values=(device, self.MAX_ATTEMPTS))

        self._commit_connection(conn)

        job_cursor = self._execute_sql(
            conn,
            'select id,created,last_attempt,build_url,'
            'build_id,changeset,tree,revision,revision_hash,'
            'enable_unittests,attempts,instr(build_url,"try") as istry '
            'from jobs where device=? order by istry desc, '
            'created %s' % order,
            values=(device,))

        job_row = job_cursor.fetchone()
        job_cursor.close()
        if not job_row:
            self._close_connection(conn)
            return None

        job = {'id': job_row[0],
               'created': job_row[1],
               'last_attempt': job_row[2],
               'build_url': job_row[3],
               'build_id': job_row[4],
               'changeset': job_row[5],
               'tree': job_row[6],
               'revision': job_row[7],
               'revision_hash': job_row[8],
               'enable_unittests': job_row[9],
               'attempts': job_row[10],
               'istry': job_row[11]}
        job['attempts'] += 1
        job['last_attempt'] = datetime.datetime.now().isoformat()

        self._execute_sql(
            conn,
            'update jobs set attempts=?, last_attempt=? where id=?',
            values=(job['attempts'], job['last_attempt'],
                    job['id']))

        job['tests'] = []
        test_cursor = self._execute_sql(
            conn,
            'select name, config_file, chunk, repos, guid '
            'from tests where jobid=?', values=(job['id'],))

        test_rows = [
            {
                'name': test_row[0],
                'config_file': test_row[1],
                'chunk': test_row[2],
                'repos' : json.loads(test_row[3]),
                'guid': test_row[4]
            }
            for test_row in test_cursor
        ]
        test_cursor.close()

        for test_row in test_rows:
            # Generate the list of tests to be executed for this job
            test_row['repos'].sort()
            for test in worker.tests:
                if (test.name == test_row['name'] and
                    test.config_file == test_row['config_file'] and
                    test.chunk == test_row['chunk'] and
                    test.repos == test_row['repos']):
                    test.job_guid = test_row['guid']
                    job['tests'].append(test)
        logger.debug('jobs.get_next_job: {0!s}'.format(job))
        self._commit_connection(conn)
        self._close_connection(conn)
        return job

    def cancel_test(self, test_guid, device=None):
        logger.debug('jobs.cancel_test: test {0!s} device {1!s}'.format(
            test_guid, device))
        if not device:
            device = self.default_device

        conn = self._conn()

        # Get the jobid for this test.
        job_ids = [
            test_row[0]
            for test_row in self._execute_sql(
                    conn,
                    'select jobid from tests '
                    'where guid=?', values=(test_guid,))
        ]

        if not job_ids:
            logger.debug('jobs.cancel_test: test %s for device %s '
                         'already deleted' % (test_guid, device))
            self._close_connection(conn)
            return

        job_id = job_ids[0]

        # Delete the test.
        self._execute_sql(
            conn,
            'delete from tests where guid=?',
            values=(test_guid,))

        # Get the number of remaining tests for this job and delete
        # the job if there are no more remaining tests for the job.
        test_cursor = self._execute_sql(
            conn,
            'select count(id) from tests '
            'where jobid=?', values=(job_id,))
        count = test_cursor.fetchone()[0]
        test_cursor.close()
        if count == 0:
            logger.debug('jobs.cancel_test: delete job_id {0!s} device {1!s}'.format(
                job_id,
                device))
            self._execute_sql(
                conn,
                'delete from jobs where id=?',
                values=(job_id,))
        self._commit_connection(conn)
        self._close_connection(conn)

    def new_treeherder_job(self, machine, project, job_collection):
        logger.debug('jobs.new_treeherder_job: {0!s} {1!s} {2!s}'.format(machine, project, job_collection.__dict__))
        attempts = 0
        now = datetime.datetime.now().isoformat()
        conn = self._conn()
        job_cursor = self._execute_sql(
            conn,
            'insert into treeherder values (?, ?, ?, ?, ?, ?)',
            values=(None, attempts, now, machine, project, job_collection.to_json()))
        job_cursor.close()
        self._commit_connection(conn)
        self._close_connection(conn)

    def get_next_treeherder_job(self):
        conn = self._conn()

        job_cursor = self._execute_sql(
            conn,
            'select id,attempts,last_attempt,machine,project,job_collection from treeherder')

        job_row = job_cursor.fetchone()
        job_cursor.close()
        if not job_row:
            self._close_connection(conn)
            return None

        job = {'id': job_row[0],
               'attempts': job_row[1],
               'last_attempt': job_row[2],
               'machine': job_row[3],
               'project': job_row[4],
               'job_collection': json.loads(job_row[5])}
        job['attempts'] += 1
        job['last_attempt'] = datetime.datetime.now().isoformat()
        self._execute_sql(
            conn,
            'update treeherder set attempts=?, last_attempt=? where id=?',
            values=(job['attempts'], job['last_attempt'],
                    job['id']))

        logger.debug('jobs.get_next_treeherder_job: {0!s}'.format(job))
        self._commit_connection(conn)
        self._close_connection(conn)
        return job

    def treeherder_job_completed(self, id):
        logger.debug('jobs.treeherder_job_completed: {0!s}'.format(id))
        conn = self._conn()
        self._execute_sql(conn, 'delete from treeherder where id=?', values=(id,))
        self._commit_connection(conn)
        self._close_connection(conn)

    def test_completed(self, test_guid):
        logger.debug('jobs.test_completed: {0!s}'.format(test_guid))
        conn = self._conn()
        self._execute_sql(conn, 'delete from tests where guid=?', values=(test_guid,))
        self._commit_connection(conn)
        self._close_connection(conn)

    def job_completed(self, job_id):
        logger.debug('jobs.job_completed: {0!s}'.format(job_id))
        conn = self._conn()
        self._execute_sql(conn, 'delete from tests where jobid=?', values=(job_id,))
        self._execute_sql(conn, 'delete from jobs where id=?', values=(job_id,))
        self._commit_connection(conn)
        self._close_connection(conn)
