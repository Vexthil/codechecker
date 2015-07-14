# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
'''
handle command line arguments
'''
import os
import sys
import glob
import time
import json
import pickle
import signal
import subprocess
import multiprocessing

import shared
from viewer_server import client_db_access_server

from codechecker_lib import client
from codechecker_lib import generic_package_context
from codechecker_lib import analyzer
from codechecker_lib import log_parser
from codechecker_lib import util
from codechecker_lib import debug_reporter
from codechecker_lib import logger
from codechecker_lib import analyzer_env
from codechecker_lib import host_check
from codechecker_lib import database_handler
from codechecker_lib import generic_package_suppress_handler

LOG = logger.get_new_logger('ARGHANDLER')

#===-----------------------------------------------------------------------===#
def perform_build_command(logfile, command, context):
    """ Build the project and create a log file. """
    LOG.info("Build has started..")


    try:
        original_env_file = os.path.join(context.package_root,'config/original_env.pickle')

        with open(original_env_file, 'rb') as env_file:
            original_env = pickle.load(env_file)
    except Exception as ex:
        LOG.warning(str(ex))
        LOG.warning('Failed to get saved original_env using a current copy for logging')
        original_env = os.environ.copy()

    return_code = 0
    # Run user's commands in shell
    log_env = analyzer_env.get_log_env(logfile, context, original_env)
    LOG.debug(log_env)
    try:
        proc = subprocess.Popen(command,
                                bufsize=-1,
                                env=log_env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                shell=True)
        while True:
            line = proc.stdout.readline()
            print line,
            if line == '' and proc.poll() is not None: break

        return_code = proc.returncode

        if return_code == 0:
            LOG.info("Build finished successfully.")
            LOG.info("The logfile is: " + logfile)
        else:
            LOG.info("Build failed.")
            sys.exit(1)
    except Exception as ex:
            LOG.error("Calling original build command failed")
            LOG.error(str(ex))
            sys.exit(1)


#===-----------------------------------------------------------------------===#
def worker_result_handler(results):
    LOG.info("----==== Summary ====----")
    LOG.info("All/successed build actions: " + str(len(results)) + "/" + str(len(filter(lambda x : x == 0, results))))

#===-----------------------------------------------------------------------===#
def check((static_analyzer, action, context)):
    """ Invoke clang with an action which called by processes. """
    try:
        LOG.info("Processing action %s." % action.id)
        result = analyzer.run(static_analyzer, action)
        #LOG.info("Action %s is done." % a.id)
        return result
    except Exception as e:
        LOG.debug(str(e))

#===-----------------------------------------------------------------------===#
def start_workers(sa, actions, jobs, context):
    # Handle SIGINT to stop this script running
    def signal_handler(*arg, **kwarg):
        try:
            pool.terminate()
        finally:
            sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    # Start checking parallel
    pool = multiprocessing.Pool(jobs)
    # pool.map(check, actions, 1)
    try:
        # Workaround, equialent of map
        # The main script does not get signal
        # while map or map_async function is running
        # It is a python bug, this does not happen if a timeout is specified;
        # then receive the interrupt immediately

        # Different analyzer object belongs to for each build action, but the state of these object are same
        actions = [ (sa, a, context) for a in actions]

        pool.map_async(check, actions, 1, callback=worker_result_handler).get(float('inf'))
        pool.close()
    except Exception as e:
        pool.terminate()
        raise
    finally:
        pool.join()

#===-----------------------------------------------------------------------===#
def check_options_validity(args):
    # Args must has workspace and dbaddress
    if args.workspace and not util.is_localhost(args.dbaddress):
        LOG.info("Workspace is not required when postgreSql server run on remote host.")
        sys.exit(1)

    if not args.workspace and util.is_localhost(args.dbaddress):
        LOG.info("Workspace is required when postgreSql server run on localhost.")
        sys.exit(1)

#===-----------------------------------------------------------------------===#
def handle_list_checkers(args):
    context = generic_package_context.get_context()
    static_analyzer = analyzer.StaticAnalyzer(context)
    LOG.info(static_analyzer.get_checker_list())

#===-----------------------------------------------------------------------===#
def setup_connection_manager_db(args):
    client.ConnectionManager.database_host = args.dbaddress
    client.ConnectionManager.database_port = args.dbport

#===-----------------------------------------------------------------------===#
def handle_server(args):

    if not host_check.check_zlib():
        LOG.error("zlib error")
        sys.exit(1)


    check_options_validity(args)
    if args.suppress is None:
        LOG.warning('WARNING! No suppress file was given, suppressed results will be only stored in the database.')

    else:
        if not os.path.exists(args.suppress):
            LOG.error('Suppress file '+args.suppress+' not found!')
            sys.exit(1)


    context = generic_package_context.get_context()
    context.codechecker_workspace = args.workspace
    context.db_username = args.dbusername

    setup_connection_manager_db(args)

    check_env = analyzer_env.get_check_env(context.path_env_extra,
                                             context.ld_lib_path_extra)

    client.ConnectionManager.run_env = check_env

    if args.check_port:

        LOG.debug('Starting codechecker server and postgres.')
        client.ConnectionManager.host = args.check_address
        client.ConnectionManager.port = args.check_port
        client.ConnectionManager.run_env = check_env

        # starts posgres
        client.ConnectionManager.start_server(args.dbname, context)
    else:
        LOG.debug('Starting postgres.')
        client.ConnectionManager.start_postgres(context, init_db=False)

    client.ConnectionManager.block_until_db_start_proc_free(context)

    # start database viewer
    db_connection_string = 'postgresql://'+args.dbusername+ \
                                        '@'+args.dbaddress+ \
                                        ':'+str(args.dbport)+ \
                                        '/'+args.dbname

    suppress_handler = generic_package_suppress_handler.GenericSuppressHandler()
    suppress_handler.suppress_file = args.suppress
    LOG.debug('Using suppress file: ' + str(suppress_handler.suppress_file))

    package_data = {}
    package_data['www_root'] = context.www_root
    package_data['doc_root'] = context.doc_root


    checker_md_docs = os.path.join(context.doc_root, 'checker_md_docs')

    checker_md_docs_map = os.path.join(checker_md_docs,
                                       'checker_doc_map.json')

    package_data['checker_md_docs'] = checker_md_docs

    with open(checker_md_docs_map, 'r') as dFile:
        checker_md_docs_map = json.load(dFile)

    package_data['checker_md_docs_map'] = checker_md_docs_map

    client_db_access_server.start_server(package_data,
                                  args.view_port,
                                  db_connection_string,
                                  suppress_handler,
                                  args.not_host_only)


#===-----------------------------------------------------------------------===#
def handle_log(args):
    """ Log mode. """
    args.logfile = os.path.realpath(args.logfile)
    if os.path.exists(args.logfile):
        os.remove(args.logfile)

    context = generic_package_context.get_context()
    open(args.logfile, 'a').close() # same as linux's touch
    perform_build_command(args.logfile, args.command, context)

#===-----------------------------------------------------------------------===#
def handle_debug(args):
    setup_connection_manager_db(args)

    context = generic_package_context.get_context()
    context.codechecker_workspace = args.workspace
    context.db_username = args.dbusername

    check_env = analyzer_env.get_check_env(context.path_env_extra,
                                             context.ld_lib_path_extra)

    client.ConnectionManager.run_env = check_env

    client.ConnectionManager.start_postgres(context)

    client.ConnectionManager.block_until_db_start_proc_free(context)

    debug_reporter.debug(context, args.dbusername, args.dbaddress,
                         args.dbport, args.dbname, args.force)

#===-----------------------------------------------------------------------===#
def handle_check(args):
    """ Check mode. """

    if not host_check.check_zlib():
        LOG.error("zlib error")
        sys.exit(1)

    args.workspace = os.path.realpath(args.workspace)
    if not os.path.isdir(args.workspace):
        os.mkdir(args.workspace)

    log_file = ""
    if args.logfile:
        log_file = os.path.realpath(args.logfile)
        if not os.path.exists(args.logfile):
            LOG.info("Log file does not exists.")
            return

    context = generic_package_context.get_context()
    context.codechecker_workspace = args.workspace
    context.db_username = args.dbusername


    check_env = analyzer_env.get_check_env(context.path_env_extra,
                                             context.ld_lib_path_extra)

    if not host_check.check_clang(check_env):
        sys.exit(1)


    #load severity map from config file
    if os.path.exists(context.checkers_severity_map_file):
        with open(context.checkers_severity_map_file, 'r') as sev_conf_file:
            severity_config = sev_conf_file.read()

        context.severity_map = json.loads(severity_config)

    if args.command:
        # check if logger bin exists
        if not os.path.isfile(context.path_logger_bin):
            LOG.debug('Logger binary not found! Required for logging.')
            sys.exit(1)

        # check if logger lib exists
        if not os.path.exists(context.path_logger_lib):
            LOG.debug('Logger library directory not found! Libs are requires for logging.')
            sys.exit(1)


        log_file = os.path.join(context.codechecker_workspace, \
                                context.build_log_file_name)
        if os.path.exists(log_file):
            os.remove(log_file)
        open(log_file, 'a').close() # same as linux's touch
        perform_build_command(log_file, args.command, context)


    setup_connection_manager_db(args)
    client.ConnectionManager.port = util.get_free_port()

    if args.jobs <= 0:
        args.jobs = 1

    suppress_file = os.path.join(args.workspace, context.version) \
                            if not args.suppress \
                            else os.path.realpath(args.suppress)

    send_suppress = False
    if os.path.exists(suppress_file):
        send_suppress = True

    client.ConnectionManager.run_env = check_env

    client.ConnectionManager.start_server(args.dbname, context)

    LOG.debug("Checker server started.")

    with client.get_connection() as connection:
        try:
            context.run_id = connection.add_checker_run(' '.join(sys.argv), \
                                        args.name, context.version, args.update)
        except shared.ttypes.RequestFailed as thrift_ex:
            if 'violates unique constraint "runs_name_key"' not in thrift_ex.message:
                # not the unique name was the problem
                raise
            else:
                LOG.info("Name was already used in the database please choose another unique name for checking.")
                sys.exit(1)

        if send_suppress:
            client.send_suppress(connection, suppress_file)

        #static_analyzer.clean = args.clean
        if args.clean:
            #cleaning up previous results
            LOG.debug("Cleaning previous plist files in "+ \
                                context.codechecker_workspace)
            plist_files = glob.glob(os.path.join(context.codechecker_workspace,'*.plist'))
            for pf in plist_files:
                os.remove(pf)

        report_output = os.path.join(context.codechecker_workspace, context.report_output_dir_name)
        if not os.path.exists(report_output):
            os.mkdir(report_output)

        static_analyzer = analyzer.StaticAnalyzer(context)
        static_analyzer.workspace = report_output

        # first add checkers from config file
        static_analyzer.checkers = context.default_checkers

        # add user defined checkers
        try:
            static_analyzer.checkers = args.ordered_checker_args
        except AttributeError as aerr:
            LOG.debug('No checkers were defined in the command line')

        if args.configfile:
            static_analyzer.add_config(connection, args.configfile)
        # else:
            # add default config from package
            # static_analyzer.add_config(connection, context.checkers_config_file)

        if args.skipfile:
            static_analyzer.add_skip(connection, os.path.realpath(args.skipfile))

        actions = log_parser.parse_log(log_file)

    LOG.info("Static analysis is starting..")
    start_time = time.time()

    LOG.debug("Starting workers...")
    start_workers(static_analyzer, actions, args.jobs, context)

    end_time = time.time()

    with client.get_connection() as connection:
        connection.finish_checker_run()

    LOG.info("Analysis length: " + str(end_time - start_time) + " sec.")
    LOG.info("Analysis has finished.")

