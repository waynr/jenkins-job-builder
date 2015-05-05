#!/usr/bin/env python
# Copyright (C) 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import io
from six.moves import configparser, StringIO, input
import fnmatch
import logging
import os
import platform
import sys
import yaml

from jenkins_jobs.builder import Builder
from jenkins_jobs.errors import JenkinsJobsException

import jenkins_jobs.cli

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

DEFAULT_CONF = """
[job_builder]
keep_descriptions=False
ignore_cache=False
recursive=False
exclude=.*
allow_duplicates=False
allow_empty_variables=False

[jenkins]
url=http://localhost:8080/
query_plugins_info=True

[hipchat]
authtoken=dummy
send-as=Jenkins
"""


def confirm(question):
    answer = input('%s (Y/N): ' % question).upper().strip()
    if not answer == 'Y':
        sys.exit('Aborted')


def recurse_path(root, excludes=None):
    if excludes is None:
        excludes = []

    basepath = os.path.realpath(root)
    pathlist = [basepath]

    patterns = [e for e in excludes if os.path.sep not in e]
    absolute = [e for e in excludes if os.path.isabs(e)]
    relative = [e for e in excludes if os.path.sep in e and
                not os.path.isabs(e)]
    for root, dirs, files in os.walk(basepath, topdown=True):
        dirs[:] = [
            d for d in dirs
            if not any([fnmatch.fnmatch(d, pattern) for pattern in patterns])
            if not any([fnmatch.fnmatch(os.path.abspath(os.path.join(root, d)),
                                        path)
                        for path in absolute])
            if not any([fnmatch.fnmatch(os.path.relpath(os.path.join(root, d)),
                                        path)
                        for path in relative])
        ]
        pathlist.extend([os.path.join(root, path) for path in dirs])

    return pathlist


def setup_config_settings(options):

    conf = '/etc/jenkins_jobs/jenkins_jobs.ini'
    if options.conf:
        conf = options.conf
    else:
        # Fallback to script directory
        localconf = os.path.join(os.path.dirname(__file__),
                                 'jenkins_jobs.ini')
        if os.path.isfile(localconf):
            conf = localconf
    config = configparser.ConfigParser()
    # Load default config always
    config.readfp(StringIO(DEFAULT_CONF))
    if os.path.isfile(conf):
        options.conf = conf  # remember file we read from
        logger.debug("Reading config from {0}".format(conf))
        conffp = io.open(conf, 'r', encoding='utf-8')
        config.readfp(conffp)
    elif options.command == 'test':
        logger.debug("Not requiring config for test output generation")
    else:
        raise JenkinsJobsException(
            "A valid configuration file is required."
            "\n{0} is not valid.".format(conf))

    return config


def execute(options, config):
    logger.debug("Config: {0}".format(config))

    # check the ignore_cache setting: first from command line,
    # if not present check from ini file
    ignore_cache = False
    if options.ignore_cache:
        ignore_cache = options.ignore_cache
    elif config.has_option('jenkins', 'ignore_cache'):
        logging.warn('ignore_cache option should be moved to the [job_builder]'
                     ' section in the config file, the one specified in the '
                     '[jenkins] section will be ignored in the future')
        ignore_cache = config.getboolean('jenkins', 'ignore_cache')
    elif config.has_option('job_builder', 'ignore_cache'):
        ignore_cache = config.getboolean('job_builder', 'ignore_cache')

    # Jenkins supports access as an anonymous user, which can be used to
    # ensure read-only behaviour when querying the version of plugins
    # installed for test mode to generate XML output matching what will be
    # uploaded. To enable must pass 'None' as the value for user and password
    # to python-jenkins
    #
    # catching 'TypeError' is a workaround for python 2.6 interpolation error
    # https://bugs.launchpad.net/openstack-ci/+bug/1259631
    try:
        user = config.get('jenkins', 'user')
    except (TypeError, configparser.NoOptionError):
        user = None

    try:
        password = config.get('jenkins', 'password')
    except (TypeError, configparser.NoOptionError):
        password = None

    # Inform the user as to what is likely to happen, as they may specify
    # a real jenkins instance in test mode to get the plugin info to check
    # the XML generated.
    if user is None and password is None:
        logger.info("Will use anonymous access to Jenkins if needed.")
    elif (user is not None and password is None) or (
            user is None and password is not None):
        raise JenkinsJobsException(
            "Cannot authenticate to Jenkins with only one of User and "
            "Password provided, please check your configuration."
        )

    # None -- no timeout, blocking mode; same as setblocking(True)
    # 0.0 -- non-blocking mode; same as setblocking(False) <--- default
    # > 0 -- timeout mode; operations time out after timeout seconds
    # < 0 -- illegal; raises an exception
    # to retain the default must use
    # "timeout=jenkins_jobs.builder._DEFAULT_TIMEOUT" or not set timeout at
    # all.
    timeout = jenkins_jobs.builder._DEFAULT_TIMEOUT
    try:
        timeout = config.getfloat('jenkins', 'timeout')
    except (ValueError):
        raise JenkinsJobsException("Jenkins timeout config is invalid")
    except (TypeError, configparser.NoOptionError):
        pass

    plugins_info = None

    if getattr(options, 'plugins_info_path', None) is not None:
        with io.open(options.plugins_info_path, 'r',
                     encoding='utf-8') as yaml_file:
            plugins_info = yaml.load(yaml_file)
        if not isinstance(plugins_info, list):
            raise JenkinsJobsException("{0} must contain a Yaml list!"
                                       .format(options.plugins_info_path))
    elif (not options.conf or not
          config.getboolean("jenkins", "query_plugins_info")):
        logger.debug("Skipping plugin info retrieval")
        plugins_info = {}

    if options.allow_empty_variables is not None:
        config.set('job_builder',
                   'allow_empty_variables',
                   str(options.allow_empty_variables))

    builder = Builder(config.get('jenkins', 'url'),
                      user,
                      password,
                      config,
                      jenkins_timeout=timeout,
                      ignore_cache=ignore_cache,
                      flush_cache=options.flush_cache,
                      plugins_list=plugins_info)

    if getattr(options, 'path', None):
        if hasattr(options.path, 'read'):
            logger.debug("Input file is stdin")
            if options.path.isatty():
                key = 'CTRL+Z' if platform.system() == 'Windows' else 'CTRL+D'
                logger.warn(
                    "Reading configuration from STDIN. Press %s to end input.",
                    key)
        else:
            # take list of paths
            options.path = options.path.split(os.pathsep)

            do_recurse = (getattr(options, 'recursive', False) or
                          config.getboolean('job_builder', 'recursive'))

            excludes = [e for elist in options.exclude
                        for e in elist.split(os.pathsep)] or \
                config.get('job_builder', 'exclude').split(os.pathsep)
            paths = []
            for path in options.path:
                if do_recurse and os.path.isdir(path):
                    paths.extend(recurse_path(path, excludes))
                else:
                    paths.append(path)
            options.path = paths

    if options.command == 'delete':
        for job in options.name:
            builder.delete_job(job, options.path)
    elif options.command == 'delete-all':
        confirm('Sure you want to delete *ALL* jobs from Jenkins server?\n'
                '(including those not managed by Jenkins Job Builder)')
        logger.info("Deleting all jobs")
        builder.delete_all_jobs()
    elif options.command == 'update':
        if options.n_workers < 0:
            raise JenkinsJobsException(
                'Number of workers must be equal or greater than 0')

        logger.info("Updating jobs in {0} ({1})".format(
            options.path, options.names))
        jobs, num_updated_jobs = builder.update_jobs(
            options.path, options.names,
            n_workers=options.n_workers)
        logger.info("Number of jobs updated: %d", num_updated_jobs)
        if options.delete_old:
            num_deleted_jobs = builder.delete_old_managed()
            logger.info("Number of jobs deleted: %d", num_deleted_jobs)
    elif options.command == 'test':
        builder.update_jobs(options.path, options.name,
                            output=options.output_dir,
                            n_workers=1)
