# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
from time import sleep

from mozprofile import FirefoxProfile

from phonetest import PhoneTest, PhoneTestResult


class SmokeTest(PhoneTest):

    @property
    def name(self):
        return 'autophone-smoketest{0!s}'.format(self.name_suffix)

    def run_job(self):
        self.update_status(message='Running smoketest')

        # Read our config file which gives us our number of
        # iterations and urls that we will be testing
        self.prepare_phone()

        # Clear logcat
        self.logcat.clear()

        # Run test
        self.loggerdeco.debug('running fennec')
        self.run_fennec_with_profile(self.build.app_name, 'about:fennec')

        is_test_completed = True
        command = None
        fennec_launched = self.dm.process_exist(self.build.app_name)
        found_throbber = False
        start = datetime.datetime.now()
        while (not fennec_launched and (datetime.datetime.now() - start
                                        <= datetime.timedelta(seconds=60))):
            command = self.worker_subprocess.process_autophone_cmd(test=self)
            if command['interrupt']:
                break
            sleep(3)
            fennec_launched = self.dm.process_exist(self.build.app_name)

        if fennec_launched:
            found_throbber = self.check_throbber()
            while (not found_throbber and (datetime.datetime.now() - start
                                           <= datetime.timedelta(seconds=60))):
                command = self.worker_subprocess.process_autophone_cmd(test=self)
                if command['interrupt']:
                    break
                sleep(3)
                found_throbber = self.check_throbber()

        if command and command['interrupt']:
            is_test_completed = False
            self.handle_test_interrupt(command['reason'],
                                       command['test_result'])
        elif self.fennec_crashed:
            pass # Handle the crash in teardown_job
        elif not fennec_launched:
            self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                              'Failed to launch Fennec',
                              PhoneTestResult.BUSTED)
        elif not found_throbber:
            self.test_failure(self.name, 'TEST_UNEXPECTED_FAIL',
                              'Failed to find Throbber',
                              PhoneTestResult.TESTFAILED)
        else:
            self.test_pass(self.name)

        if fennec_launched:
            self.loggerdeco.debug('killing fennec')
            self.dm.pkill(self.build.app_name, root=True)

        self.loggerdeco.debug('removing sessionstore files')
        self.remove_sessionstore_files()
        return is_test_completed

    def prepare_phone(self):
        profile = FirefoxProfile(preferences=self.preferences)
        self.install_profile(profile)

    def check_throbber(self):
        buf = self.logcat.get()

        for line in buf:
            line = line.strip()
            self.loggerdeco.debug('check_throbber: {0!s}'.format(line))
            if 'Throbber stop' in line:
                return True
        return False

