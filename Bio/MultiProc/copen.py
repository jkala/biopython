"""copen.py

This implements a set of classes that wraps a file object interface
around code that executes in another process.  This allows you fork
many different commands and let the run concurrently.

Functions:
copen_sys     Open a file-like pipe to a system command.
copen_fn      Open a file-like pipe to a python function.

"""

# The pickle stuff has to be redone.  We shouldn't have a whole new
# handle object to unpickle the results.  Instead, the pickling and
# unpickling should be done by function wrappers.
# e.g.
#    call_and_pickle(fn, args, keywds)
# _CommandHandle should take a function that processes the results,
# e.g. unpickle

import os
import sys
import traceback
import time
import signal
import select
try:
    import cPickle as pickle
except ImportError:
    import pickle

def copen_sys(syscmd, *args):
    """copen_sys(syscmd, *args) -> file-like object

    Open a file-like object that returns the output from a system
    command.

    """
    # python requires first element to be the path
    if not args or args[0] != syscmd:
        args = [syscmd] + list(args)

    r, w = os.pipe()
    er, ew = os.pipe()

    pid = os.fork()
    if pid == 0: # child process
        os.dup2(w, 1)
        os.dup2(ew, 2)
        try:
            os.execvp(syscmd, args)  # execute it!
        except:
            sys.stderr.write("%s could not be executed\n" % syscmd)
            os._exit(-1)
        os._exit(0)

    # parent
    os.close(w)
    os.close(ew)
    return _CommandHandle(pid, os.fdopen(r, 'r'), os.fdopen(er, 'r'))

def copen_fn(func, *args, **keywords):
    """copen_fn(func, *args, **keywords) -> file-like object

    Open a file-like object that returns the output from function
    call.  The object's 'read' method returns the return value from
    the function.  The function is executed as a separate process, so
    any variables modified by the function does not affect the ones in
    the parent process.  The return value of the function must be
    pickle-able.

    """
    r, w = os.pipe()
    er, ew = os.pipe()

    pid = os.fork()
    if pid == 0: # child process
        cwrite, errwrite = os.fdopen(w, 'w'), os.fdopen(ew, 'w')
        try:
            output = apply(func, args, keywords)
            s = pickle.dumps(output, 1)
        except:
            etype, value, tb = sys.exc_info()   # get the traceback
            tb = traceback.extract_tb(tb)
            s = pickle.dumps((etype, value, tb), 1)
            errwrite.write(s)
            errwrite.flush()
            os._exit(-1)
        cwrite.write(s)
        cwrite.flush()
        os._exit(0)

    # parent
    os.close(w)
    os.close(ew)
    return _PickleHandle(pid, os.fdopen(r, 'r'), os.fdopen(er, 'r'))


# Keep a list of all the active child processes.  If the process is
# forcibly killed, e.g. by a SIGTERM, make sure the child processes
# die too.
_active = []   # list of _CommandHandle objects

class _CommandHandle:
    """This file-like object is a wrapper around a command.

    Members:
    pid         what is the PID of the subprocess?
    killsig     what signal killed the child process?
    status      what was the status of the command?
    error       if an error occurred, this describes it.

    Methods:
    close       Close this process, killing it if necessary.
    fileno      Return the fileno used to read from the process.
    wait        Wait for the process to finish.
    poll        Is the process finished?
    elapsed     How much time has this process taken?
    read
    readline
    readlines
    
    """
    def __init__(self, pid, cread, errread=None):
        """_CommandHandle(pid, cread[, errread]) -> instance

        Create a wrapper around a command.  pid should be the process
        ID of the command that was created, probably by a fork/exec.
        cread should be a file object used to read from the child.  If
        errread is given, then I will look there for messages
        pertaining to error conditions.

        """
        _active.append(self)
        
        self.pid = pid
        self.status = None
        self.killsig = None
        
        self._start, self._end = time.time(), None
        self._cread, self._errread = cread, errread
        self._output = []
        self._done = 0
        self._closed = 0

    def __del__(self):
        self.close()  # kill the process

    def _kill(self):
        # return killsig
        try:
            pid, ind = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:   # died
                return 0
            # First, try to kill it with a SIGTERM.
            os.kill(self.pid, signal.SIGTERM)
            # Wait .5 seconds for the process to die.
            end = time.time() + 0.5
            while time.time() < end:
                pid, ind = os.waitpid(self.pid, os.WNOHANG)
                if pid == self.pid:
                    return ind & 0xff
                time.sleep(0.1)
            # It didn't die, so kill with a SIGKILL
            os.kill(self.pid, signal.SIGKILL)
            return signal.SIGKILL
        except OSError:
            pass
        
    def close(self):
        """S.close()

        Close the process, killing it if I must.

        """
        # If this gets called in the middle of object initialization,
        # the _closed attribute will not exist.
        if not hasattr(self, '_closed') or self._closed:
            return
        # on cleanup, _active may not be defined!
        if _active and self in _active:
            _active.remove(self)
        if not self._done:
            self.killsig = self._kill()
            self._end = time.time()
            self.status = None
            self.killsig = signal.SIGTERM
            self._done = 1
        self._output = []
        self._closed = 1

    def fileno(self):
        """S.fileno() -> file descriptor

        Return the file descriptor associated with the pipe.

        """
        return self._cread.fileno()
        
    def readline(self):
        """S.readline() -> string

        Return the next line read, or '' if finished.

        """
        self.wait()
        if not self._output:
            return ''
        line = self._output[0]
        del self._output[0]
        return line

    def readlines(self):
        """S.readlines() -> list of strings

        Return the output as a list of strings.

        """
        self.wait()
        output = self._output
        self._output = []
        return output

    def read(self):
        """S.read() -> string

        Return the output as a string.

        """
        self.wait()
        output = self._output
        self._output = []
        return "".join(output)

    def wait(self):
        """S.wait()

        Wait until the process is finished.

        """
        if self._done:
            return
        # wait until stuff's ready to be read
        select.select([self], [], [])
        self._cleanup_child()

    def poll(self):
        """S.poll() -> boolean

        Is the process finished running?

        """
        if self._done:
            return 1
        # If I'm done, then read the results.
        if select.select([self], [], [], 0)[0]:
            self._cleanup_child()
        return self._done

    def elapsed(self):
        """S.elapsed() -> num seconds

        How much time has elapsed since the process began?

        """
        if self._end:  # if I've finished, return the total time
            return self._end - self._start
        return time.time() - self._start

    def _cleanup_child(self):
        """S._cleanup_child()

        Do necessary cleanup functions after child is finished running.

        """
        if self._done:
            return

        # read the output
        self._output = self._cread.readlines()
        self._cread.close()
        if self._errread:
            error = self._errread.read()
            self._errread.close()
            if error:
                error = pickle.loads(error)
                etype, value, tb = error
                # tb gets lost, and the stack frame of the parent
                # process is printed out instead.  I should find a way
                # where the client can optionally get access to this.
                raise etype, value
        # Remove myself from the active list.
        if _active and self in _active:
            _active.remove(self)

        pid, ind = os.waitpid(self.pid, 0)
        self.status, self.killsig = ind >> 8, ind & 0xff
        self._end = time.time()
        self._done = 1

class _PickleHandle:
    """This is a decorator around a _CommandHandle.
    Instead of returning the results as a string, it returns a
    python object.  The child process must pickle its output to the pipe!

    Members:
    pid         what is the PID of the subprocess?
    killsig     what signal killed the child process?
    status      what was the status of the command?
    error       if an error occurred, this describes it.

    Methods:
    close       Close this process, killing it if necessary.
    fileno      Return the fileno used to read from the process.
    wait        Wait for the process to finish.
    poll        Is the process finished?
    elapsed     How much time has this process taken?
    read        Return a Python object.
    
    """
    def __init__(self, pid, cread, errread=None):
        """_PickleHandle(pid, cread[, errread])

        Create a wrapper around a command.  pid should be the process ID
        of the command that was created, probably by a fork/exec.
        cread should be a file object used to read from the child.
        If errread is given, then I will look there for messages pertaining to
        error conditions.

        """
        self._cmd_handle = _CommandHandle(pid, cread, errread)

    def __getattr__(self, attr):
        # This object does not support 'readline' or 'readlines'
        if attr.startswith('readline'):
            raise AttributeError, attr
        return getattr(self._cmd_handle, attr)

    def read(self):
        """S.read() -> python object

        Returns None on error.  Most likely, the function returned
        an object that could not be pickled.

        """
        r = self._cmd_handle.read()
        if not r:
            return r
        return pickle.loads(r)


# Handle SIGTERM below

def _handle_sigterm(signum, stackframe):
    """Handles a SIGTERM.  Cleans up."""
    _cleanup()
    # call the previous handler
    if _PREV_SIGTERM is not None:
        signal.signal(signal.SIGTERM, _PREV_SIGTERM)

def _cleanup():
    """_cleanup()

    Close all active commands.

    """
    for obj in _active[:]:
        obj.close()

_PREV_SIGTERM = signal.getsignal(signal.SIGTERM)
signal.signal(signal.SIGTERM, _handle_sigterm)