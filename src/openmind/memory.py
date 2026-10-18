"""Current and peak process RSS without optional dependencies.

These measure the server process, not GPU allocations. Linux's inexpensive
statm RSS is a kernel estimate. Releasing application buffers need not return
allocator or driver memory to the OS immediately; the lifetime peak cannot fall.
"""

import ctypes
from functools import lru_cache
import os
from pathlib import Path
import sys

try:
    import resource
except ImportError:  # The module is unavailable on Windows.
    resource = None


class _ProcTaskInfo(ctypes.Structure):
    # ABI: Apple SDK sys/proc_info.h, struct proc_taskinfo. The full 96-byte
    # buffer is required by proc_pidinfo, even though we read only resident size.
    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


@lru_cache(maxsize=1)
def _proc_pidinfo():
    """Bind libproc once, including a cached unavailable result."""
    try:
        function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def current_rss_bytes() -> int | None:
    """Return this process's current resident bytes, or None when unavailable.

    Darwin reads libproc's byte count. Linux converts statm's second field
    from pages using the running OS page size. No peak value is substituted
    when current RSS cannot be measured.
    """
    try:
        if sys.platform == "darwin":
            function = _proc_pidinfo()
            if function is None:
                return None
            info = _ProcTaskInfo()
            size = ctypes.sizeof(info)
            # PROC_PIDTASKINFO = 4 in sys/proc_info.h.
            received = function(os.getpid(), 4, 0, ctypes.byref(info), size)
            if received != size:
                return None
            resident = int(info.pti_resident_size)
        elif sys.platform.startswith("linux"):
            fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
            if len(fields) < 2:
                return None
            pages = int(fields[1])
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            if pages < 0 or page_size <= 0:
                return None
            resident = pages * page_size
        else:
            return None
        return resident if resident > 0 else None
    except (OSError, ValueError, OverflowError, AttributeError):
        return None


def peak_rss_bytes() -> int | None:
    """Return lifetime maximum RSS in bytes on Darwin/Linux, otherwise None."""
    if resource is None or not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return None
    try:
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if peak <= 0:
            return None
        # Darwin getrusage(2) reports bytes; Linux getrusage(2) reports KiB.
        return peak if sys.platform == "darwin" else peak * 1024
    except (OSError, ValueError, OverflowError, AttributeError):
        return None
