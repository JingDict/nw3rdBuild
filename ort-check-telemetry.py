"""
check-telemetry.py

Whether an ONNX Runtime library for Windows registers ONNX Runtime's telemetry
provider, which Windows' diagnostic data listener subscribes to by its ID
(README.md). Loads the library into this process and makes an environment,
which is where Microsoft's library registers the provider, before an app could
turn its telemetry off. Then asks Windows whether the provider is registered in
this process, and which trace sessions enable it here.

    python check-telemetry.py --expect absent package/lib/onnxruntime.dll
    python check-telemetry.py --expect registered microsoft/lib/onnxruntime.dll

Exits 0 when the library does as --expect says, and 1 otherwise. Each check
runs in a process of its own, since a process loads one ONNX Runtime.
"""

import argparse
import ctypes
import os
import sys
import uuid
from ctypes import wintypes

# Microsoft.ML.ONNXRuntime, as onnxruntime/core/platform/windows/telemetry.cc
# defines it.
PROVIDER = uuid.UUID("3a26b1ff-7484-7484-7484-15261f42614d")

# The first version of ONNX Runtime's C API, which every library still serves,
# and CreateEnv's place in it.
ORT_API_VERSION = 1
CREATE_ENV = 3
ORT_LOGGING_LEVEL_WARNING = 2

# EnumerateTraceGuidsEx's query for one provider's registrations, and its
# answers for a provider registered nowhere and for a buffer too small.
TRACE_GUID_QUERY_INFO = 1
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_WMI_GUID_NOT_FOUND = 4200


class OrtApiBase(ctypes.Structure):
    _fields_ = [("GetApi", ctypes.c_void_p), ("GetVersionString", ctypes.c_void_p)]


class TraceGuidInfo(ctypes.Structure):
    _fields_ = [("InstanceCount", wintypes.ULONG), ("Reserved", wintypes.ULONG)]


class TraceProviderInstanceInfo(ctypes.Structure):
    _fields_ = [
        ("NextOffset", wintypes.ULONG),
        ("EnableCount", wintypes.ULONG),
        ("Pid", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
    ]


class TraceEnableInfo(ctypes.Structure):
    _fields_ = [
        ("IsEnabled", wintypes.ULONG),
        ("Level", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte),
        ("LoggerId", wintypes.USHORT),
        ("EnableProperty", wintypes.ULONG),
        ("Reserved2", wintypes.ULONG),
        ("MatchAnyKeyword", ctypes.c_ulonglong),
        ("MatchAllKeyword", ctypes.c_ulonglong),
    ]


def make_environment(library):
    """Loads the library and makes an environment, as an app does first, and
    returns the library's version."""
    ort = ctypes.WinDLL(os.path.abspath(library))
    ort.OrtGetApiBase.restype = ctypes.POINTER(OrtApiBase)
    base = ort.OrtGetApiBase().contents
    version = ctypes.WINFUNCTYPE(ctypes.c_char_p)(base.GetVersionString)().decode()
    api = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(base.GetApi)(ORT_API_VERSION)
    functions = ctypes.cast(api, ctypes.POINTER(ctypes.c_void_p))
    create_env = ctypes.WINFUNCTYPE(
        ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)
    )(functions[CREATE_ENV])
    environment = ctypes.c_void_p()
    if create_env(ORT_LOGGING_LEVEL_WARNING, b"check-telemetry", ctypes.byref(environment)):
        sys.exit(f"{library}: ONNX Runtime {version} could not make an environment.")
    return version


def sessions_enabling_it_here():
    """The trace sessions enabling the provider in this process, or None when
    the provider is not registered in this process."""
    query = ctypes.WinDLL("advapi32").EnumerateTraceGuidsEx
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(PROVIDER.bytes_le)
    size = wintypes.ULONG(0)
    buffer = None
    while True:
        status = query(TRACE_GUID_QUERY_INFO, guid, 16, buffer, size, ctypes.byref(size))
        if status == ERROR_WMI_GUID_NOT_FOUND:
            return None
        if status == 0 and buffer is not None:
            break
        if status not in (0, ERROR_INSUFFICIENT_BUFFER):
            raise ctypes.WinError(status)
        buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(TraceGuidInfo)))
        size = wintypes.ULONG(len(buffer))

    # One instance for each process that registered the provider, each followed
    # by the sessions enabling it there.
    offset = ctypes.sizeof(TraceGuidInfo)
    for _ in range(TraceGuidInfo.from_buffer(buffer).InstanceCount):
        instance = TraceProviderInstanceInfo.from_buffer(buffer, offset)
        if instance.Pid == os.getpid():
            first = offset + ctypes.sizeof(TraceProviderInstanceInfo)
            return [
                TraceEnableInfo.from_buffer(buffer, first + i * ctypes.sizeof(TraceEnableInfo))
                for i in range(instance.EnableCount)
            ]
        if instance.NextOffset == 0:
            break
        offset += instance.NextOffset
    return None


def main():
    parser = argparse.ArgumentParser(description="Whether an ONNX Runtime library registers its telemetry provider.")
    parser.add_argument("--expect", choices=("absent", "registered"), required=True)
    parser.add_argument("library")
    args = parser.parse_args()

    version = make_environment(args.library)
    sessions = sessions_enabling_it_here()
    if sessions is None:
        found, detail = "absent", "not registered"
    else:
        enabling = " and ".join(
            f"session {s.LoggerId} (level {s.Level}, keywords {s.MatchAnyKeyword:#x})" for s in sessions
        )
        found, detail = "registered", f"registered, enabled by {enabling or 'no session'}"
    print(f"{args.library}: ONNX Runtime {version}, telemetry provider {{{PROVIDER}}} {detail}")
    if found != args.expect:
        sys.exit(f"Expected it {args.expect}.")


if __name__ == "__main__":
    main()
