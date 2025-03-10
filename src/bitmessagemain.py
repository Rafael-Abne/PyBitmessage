#!/usr/bin/env python
"""
The PyBitmessage startup script
"""
# Copyright (c) 2012-2016 Jonathan Warren
# Copyright (c) 2012-2022 The Bitmessage developers
# Distributed under the MIT/X11 software license. See the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

# Right now, PyBitmessage only support connecting to stream 1. It doesn't
# yet contain logic to expand into further streams.
import os
import sys

try:
    import pathmagic
except ImportError:
    from pybitmessage import pathmagic
app_dir = pathmagic.setup()

import depends
depends.check_dependencies()

import getopt
import multiprocessing
# Used to capture a Ctrl-C keypress so that Bitmessage can shutdown gracefully.
import signal
import threading
import time
import traceback

import defaults
# Network subsystem
import network
import shared
import shutdown
import state

from testmode_init import populate_api_test_data
from bmconfigparser import config
from debug import logger  # this should go before any threads
from helper_startup import (
    adjustHalfOpenConnectionsLimit, fixSocket, start_proxyconfig)
from inventory import Inventory
from singleinstance import singleinstance
# Synchronous threads
from threads import (
    set_thread_name, printLock,
    addressGenerator, objectProcessor, singleCleaner, singleWorker, sqlThread)


def signal_handler(signum, frame):
    """Single handler for any signal sent to pybitmessage"""
    process = multiprocessing.current_process()
    thread = threading.current_thread()
    logger.error(
        'Got signal %i in %s/%s',
        signum, process.name, thread.name
    )
    if process.name == "RegExParser":
        # on Windows this isn't triggered, but it's fine,
        # it has its own process termination thing
        raise SystemExit
    if "PoolWorker" in process.name:
        raise SystemExit
    if thread.name not in ("PyBitmessage", "MainThread"):
        return
    logger.error("Got signal %i", signum)
    # there are possible non-UI variants to run bitmessage
    # which should shutdown especially test-mode
    if state.thisapp.daemon or not state.enableGUI:
        shutdown.doCleanShutdown()
    else:
        print('# Thread: %s(%d)' % (thread.name, thread.ident))
        for filename, lineno, name, line in traceback.extract_stack(frame):
            print('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                print('  %s' % line.strip())
        print('Unfortunately you cannot use Ctrl+C when running the UI'
              ' because the UI captures the signal.')


class Main(object):
    """Main PyBitmessage class"""
    def start(self):
        """Start main application"""
        # pylint: disable=too-many-statements,too-many-branches,too-many-locals
        fixSocket()
        adjustHalfOpenConnectionsLimit()

        daemon = config.safeGetBoolean('bitmessagesettings', 'daemon')

        try:
            opts, _ = getopt.getopt(
                sys.argv[1:], "hcdt",
                ["help", "curses", "daemon", "test"])

        except getopt.GetoptError:
            self.usage()
            sys.exit(2)

        for opt, _ in opts:
            if opt in ("-h", "--help"):
                self.usage()
                sys.exit()
            elif opt in ("-d", "--daemon"):
                daemon = True
            elif opt in ("-c", "--curses"):
                state.curses = True
            elif opt in ("-t", "--test"):
                state.testmode = True
                if os.path.isfile(os.path.join(
                        state.appdata, 'unittest.lock')):
                    daemon = True
                state.enableGUI = False  # run without a UI
                # Fallback: in case when no api command was issued
                state.last_api_response = time.time()
                # Apply special settings
                config.set(
                    'bitmessagesettings', 'apienabled', 'true')
                config.set(
                    'bitmessagesettings', 'apiusername', 'username')
                config.set(
                    'bitmessagesettings', 'apipassword', 'password')
                config.set(
                    'bitmessagesettings', 'apivariant', 'legacy')
                config.set(
                    'bitmessagesettings', 'apinotifypath',
                    os.path.join(app_dir, 'tests', 'apinotify_handler.py')
                )

        if daemon:
            state.enableGUI = False  # run without a UI

        if state.enableGUI and not state.curses and not depends.check_pyqt():
            sys.exit(
                'PyBitmessage requires PyQt unless you want'
                ' to run it as a daemon and interact with it'
                ' using the API. You can download PyQt from '
                'http://www.riverbankcomputing.com/software/pyqt/download'
                ' or by searching Google for \'PyQt Download\'.'
                ' If you want to run in daemon mode, see '
                'https://bitmessage.org/wiki/Daemon\n'
                'You can also run PyBitmessage with'
                ' the new curses interface by providing'
                ' \'-c\' as a commandline argument.'
            )
        # is the application already running?  If yes then exit.
        state.thisapp = singleinstance("", daemon)

        if daemon:
            with printLock:
                print('Running as a daemon. Send TERM signal to end.')
            self.daemonize()

        self.setSignalHandler()

        set_thread_name("PyBitmessage")

        state.dandelion = config.safeGetInt('network', 'dandelion')
        # dandelion requires outbound connections, without them,
        # stem objects will get stuck forever
        if state.dandelion and not config.safeGetBoolean(
                'bitmessagesettings', 'sendoutgoingconnections'):
            state.dandelion = 0

        if state.testmode or config.safeGetBoolean(
                'bitmessagesettings', 'extralowdifficulty'):
            defaults.networkDefaultProofOfWorkNonceTrialsPerByte = int(
                defaults.networkDefaultProofOfWorkNonceTrialsPerByte / 100)
            defaults.networkDefaultPayloadLengthExtraBytes = int(
                defaults.networkDefaultPayloadLengthExtraBytes / 100)

        # Start the SQL thread
        sqlLookup = sqlThread()
        # DON'T close the main program even if there are threads left.
        # The closeEvent should command this thread to exit gracefully.
        sqlLookup.daemon = False
        sqlLookup.start()

        Inventory()  # init

        if state.enableObjProc:  # Not needed if objproc is disabled
            shared.reloadMyAddressHashes()
            shared.reloadBroadcastSendersForWhichImWatching()

            # Start the address generation thread
            addressGeneratorThread = addressGenerator()
            # close the main program even if there are threads left
            addressGeneratorThread.daemon = True
            addressGeneratorThread.start()

            # Start the thread that calculates POWs
            singleWorkerThread = singleWorker()
            # close the main program even if there are threads left
            singleWorkerThread.daemon = True
            singleWorkerThread.start()

            # Start the object processing thread
            objectProcessorThread = objectProcessor()
            # DON'T close the main program even if the thread remains.
            # This thread checks the shutdown variable after processing
            # each object.
            objectProcessorThread.daemon = False
            objectProcessorThread.start()

            # SMTP delivery thread
            if daemon and config.safeGet(
                    'bitmessagesettings', 'smtpdeliver', '') != '':
                from class_smtpDeliver import smtpDeliver
                smtpDeliveryThread = smtpDeliver()
                smtpDeliveryThread.start()

            # SMTP daemon thread
            if daemon and config.safeGetBoolean(
                    'bitmessagesettings', 'smtpd'):
                from class_smtpServer import smtpServer
                smtpServerThread = smtpServer()
                smtpServerThread.start()

            # API is also objproc dependent
            if config.safeGetBoolean('bitmessagesettings', 'apienabled'):
                import api  # pylint: disable=relative-import
                singleAPIThread = api.singleAPI()
                # close the main program even if there are threads left
                singleAPIThread.daemon = True
                singleAPIThread.start()

        # Start the cleanerThread
        singleCleanerThread = singleCleaner()
        # close the main program even if there are threads left
        singleCleanerThread.daemon = True
        singleCleanerThread.start()

        # start network components if networking is enabled
        if state.enableNetwork:
            start_proxyconfig()
            network.start(config, state)

            if config.safeGetBoolean('bitmessagesettings', 'upnp'):
                import upnp
                upnpThread = upnp.uPnPThread()
                upnpThread.start()
        else:
            # Populate with hardcoded value (same as connectToStream above)
            state.streamsInWhichIAmParticipating.append(1)

        if not daemon and state.enableGUI:
            if state.curses:
                if not depends.check_curses():
                    sys.exit()
                print('Running with curses')
                import bitmessagecurses
                bitmessagecurses.runwrapper()
            else:
                import bitmessageqt
                bitmessageqt.run()
        else:
            config.remove_option('bitmessagesettings', 'dontconnect')

        if state.testmode:
            populate_api_test_data()

        if daemon:
            while state.shutdown == 0:
                time.sleep(1)
                if (
                    state.testmode
                    and time.time() - state.last_api_response >= 30
                ):
                    self.stop()
        elif not state.enableGUI:
            state.enableGUI = True
            try:
                # pylint: disable=relative-import
                from tests import core as test_core
            except ImportError:
                try:
                    from pybitmessage.tests import core as test_core
                except ImportError:
                    self.stop()
                    return

            test_core_result = test_core.run()
            self.stop()
            test_core.cleanup()
            sys.exit(not test_core_result.wasSuccessful())

    @staticmethod
    def daemonize():
        """Running as a daemon. Send signal in end."""
        grandfatherPid = os.getpid()
        parentPid = None
        try:
            if os.fork():
                # unlock
                state.thisapp.cleanup()
                # wait until grandchild ready
                while True:
                    time.sleep(1)
                os._exit(0)  # pylint: disable=protected-access
        except AttributeError:
            # fork not implemented
            pass
        else:
            parentPid = os.getpid()
            state.thisapp.lock()  # relock

        os.umask(0)
        try:
            os.setsid()
        except AttributeError:
            # setsid not implemented
            pass
        try:
            if os.fork():
                # unlock
                state.thisapp.cleanup()
                # wait until child ready
                while True:
                    time.sleep(1)
                os._exit(0)  # pylint: disable=protected-access
        except AttributeError:
            # fork not implemented
            pass
        else:
            state.thisapp.lock()  # relock
        state.thisapp.lockPid = None  # indicate we're the final child
        sys.stdout.flush()
        sys.stderr.flush()
        if not sys.platform.startswith('win'):
            si = open(os.devnull, 'r')
            so = open(os.devnull, 'a+')
            se = open(os.devnull, 'a+', 0)
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())
        if parentPid:
            # signal ready
            os.kill(parentPid, signal.SIGTERM)
            os.kill(grandfatherPid, signal.SIGTERM)

    @staticmethod
    def setSignalHandler():
        """Setting the Signal Handler"""
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        # signal.signal(signal.SIGINT, signal.SIG_DFL)

    @staticmethod
    def usage():
        """Displaying the usages"""
        print('Usage: ' + sys.argv[0] + ' [OPTIONS]')
        print('''
Options:
  -h, --help            show this help message and exit
  -c, --curses          use curses (text mode) interface
  -d, --daemon          run in daemon (background) mode
  -t, --test            dryrun, make testing

All parameters are optional.
''')

    @staticmethod
    def stop():
        """Stop main application"""
        with printLock:
            print('Stopping Bitmessage Deamon.')
        shutdown.doCleanShutdown()

    # .. todo:: nice function but no one is using this
    @staticmethod
    def getApiAddress():
        """This function returns API address and port"""
        if not config.safeGetBoolean(
                'bitmessagesettings', 'apienabled'):
            return None
        address = config.get('bitmessagesettings', 'apiinterface')
        port = config.getint('bitmessagesettings', 'apiport')
        return {'address': address, 'port': port}


def main():
    """Triggers main module"""
    mainprogram = Main()
    mainprogram.start()


if __name__ == "__main__":
    main()


# So far, the creation of and management of the Bitmessage protocol and this
# client is a one-man operation. Bitcoin tips are quite appreciated.
# 1H5XaDA6fYENLbknwZyjiYXYPQaFjjLX2u
