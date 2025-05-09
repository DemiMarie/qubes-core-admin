#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2010-2015  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2013-2015  Marek Marczykowski-Górecki
#                              <marmarek@invisiblethingslab.com>
# Copyright (C) 2014-2015  Wojtek Porczyk <woju@invisiblethingslab.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.
#

import asyncio
import hashlib
import logging
import random
import re
import string
import os
import os.path
import socket
import subprocess
import tempfile
from contextlib import contextmanager, suppress

import importlib.metadata

import docutils
import docutils.core
import docutils.io
import qubes.exc

LOGGER = logging.getLogger("qubes.utils")


def get_timezone():
    if os.path.islink("/etc/localtime"):
        tz_path = "/".join(os.readlink("/etc/localtime").split("/"))
        return tz_path.split("zoneinfo/")[1]
    # last resort way, some applications makes /etc/localtime
    # hardlink instead of symlink...
    tz_info = os.stat("/etc/localtime")
    if not tz_info:
        return None
    if tz_info.st_nlink > 1:
        with subprocess.Popen(
            [
                "find",
                "/usr/share/zoneinfo",
                "-inum",
                str(tz_info.st_ino),
                "-print",
                "-quit",
            ],
            stdout=subprocess.PIPE,
        ) as p:
            tz_path = p.communicate()[0].strip()
        return tz_path.replace(b"/usr/share/zoneinfo/", b"")
    return None


def format_doc(docstring):
    """Return parsed documentation string, stripping RST markup."""

    if not docstring:
        return ""

    # pylint: disable=unused-variable
    output, pub = docutils.core.publish_programmatically(
        source_class=docutils.io.StringInput,
        source=" ".join(docstring.strip().split()),
        source_path=None,
        destination_class=docutils.io.NullOutput,
        destination=None,
        destination_path=None,
        reader=None,
        reader_name="standalone",
        parser=None,
        parser_name="restructuredtext",
        writer=None,
        writer_name="null",
        settings=None,
        settings_spec=None,
        settings_overrides=None,
        config_section=None,
        enable_exit_status=None,
    )
    return pub.writer.document.astext()


def parse_size(size):
    units = [
        ("K", 1000),
        ("KB", 1000),
        ("M", 1000 * 1000),
        ("MB", 1000 * 1000),
        ("G", 1000 * 1000 * 1000),
        ("GB", 1000 * 1000 * 1000),
        ("Ki", 1024),
        ("KiB", 1024),
        ("Mi", 1024 * 1024),
        ("MiB", 1024 * 1024),
        ("Gi", 1024 * 1024 * 1024),
        ("GiB", 1024 * 1024 * 1024),
    ]

    size = size.strip().upper()
    if size.isdigit():
        return int(size)

    for unit, multiplier in units:
        if size.endswith(unit.upper()):
            size = size[: -len(unit)].strip()
            return int(size) * multiplier

    raise qubes.exc.QubesException("Invalid size: {0}.".format(size))


def mbytes_to_kmg(size):
    if size > 1024:
        return "%d GiB" % (size / 1024)

    return "%d MiB" % size


def kbytes_to_kmg(size):
    if size > 1024:
        return mbytes_to_kmg(size / 1024)

    return "%d KiB" % size


def bytes_to_kmg(size):
    if size > 1024:
        return kbytes_to_kmg(size / 1024)

    return "%d B" % size


def size_to_human(size):
    """Humane readable size, with 1/10 precision"""
    if size < 1024:
        return str(size)
    if size < 1024 * 1024:
        return str(round(size / 1024.0, 1)) + " KiB"
    if size < 1024 * 1024 * 1024:
        return str(round(size / (1024.0 * 1024), 1)) + " MiB"

    return str(round(size / (1024.0 * 1024 * 1024), 1)) + " GiB"


def urandom(size):
    rand = os.urandom(size)
    if rand is None:
        raise IOError("failed to read urandom")
    return hashlib.sha512(rand).digest()


def get_entry_point_one(group, name):
    epoints = tuple(importlib.metadata.entry_points(group=group, name=name))
    if not epoints:
        raise KeyError(name)
    if len(epoints) > 1:
        raise TypeError(
            "more than 1 implementation of {!r} found: {}".format(
                name,
                ", ".join(
                    "{}.{}".format(ep.module_name, ".".join(ep.attrs))
                    for ep in epoints
                ),
            )
        )
    return epoints[0].load()


def random_string(length=5):
    """Return random string consisting of ascii_leters and digits"""
    return "".join(
        random.choice(string.ascii_letters + string.digits)
        for _ in range(length)
    )


def systemd_notify():
    """Notify systemd"""
    nofity_socket = os.getenv("NOTIFY_SOCKET")
    if not nofity_socket:
        return
    if nofity_socket.startswith("@"):
        nofity_socket = "\0" + nofity_socket[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.connect(nofity_socket)
    sock.sendall(b"READY=1\nSTATUS=qubesd online and operational\n")
    sock.close()


def systemd_extend_timeout():
    """Extend systemd startup timeout by 60s"""
    notify_socket = os.getenv("NOTIFY_SOCKET")
    if not notify_socket:
        return
    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.connect(notify_socket)
    sock.sendall(
        b"EXTEND_TIMEOUT_USEC=60000000\n"
        b"STATUS=Cleaning up storage for stopped qubes\n"
    )
    sock.close()


def match_vm_name_with_special(vm, name):
    """Check if *vm* matches given name, which may be specified as @tag:...
    or @type:..."""
    if name.startswith("@tag:"):
        return name[len("@tag:") :] in vm.tags
    if name.startswith("@type:"):
        return name[len("@type:") :] == vm.__class__.__name__
    return name == vm.name


@contextmanager
def replace_file(
    dst,
    *,
    permissions,
    close_on_success=True,
    logger=LOGGER,
    log_level=logging.DEBUG,
):
    """Yield a tempfile whose name starts with dst. If the block does
    not raise an exception, apply permissions and persist the
    tempfile to dst (which is allowed to already exist). Otherwise
    ensure that the tempfile is cleaned up.
    """
    tmp_dir, prefix = os.path.split(dst + "~")
    tmp = tempfile.NamedTemporaryFile(dir=tmp_dir, prefix=prefix, delete=False)
    try:
        yield tmp
        tmp.flush()
        os.fchmod(tmp.fileno(), permissions)
        os.fsync(tmp.fileno())
        if close_on_success:
            tmp.close()
        rename_file(tmp.name, dst, logger=logger, log_level=log_level)
    except:
        try:
            tmp.close()
        finally:
            remove_file(tmp.name, logger=logger, log_level=log_level)
        raise


def rename_file(src, dst, *, logger=LOGGER, log_level=logging.DEBUG):
    """Durably rename src to dst."""
    os.rename(src, dst)
    dst_dir = os.path.dirname(dst)
    src_dir = os.path.dirname(src)
    fsync_path(dst_dir)
    if src_dir != dst_dir:
        fsync_path(src_dir)
    logger.log(log_level, "Renamed file: %r -> %r", src, dst)


def remove_file(path, *, logger=LOGGER, log_level=logging.DEBUG):
    """Durably remove the file at path, if it exists. Return whether
    we removed it."""
    with suppress(FileNotFoundError):
        os.remove(path)
        fsync_path(os.path.dirname(path))
        logger.log(log_level, "Removed file: %r", path)
        return True
    return False


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY)  # works for a file or a directory
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def coro_maybe(value):
    return (await value) if asyncio.iscoroutine(value) else value


_am_root = os.getuid() == 0


# pylint: disable=redefined-builtin
async def run_program(*args, check=True, input=None, sudo=False, **kwargs):
    """Async version of subprocess.run()"""
    if not _am_root and sudo:
        args = ["sudo"] + list(args)
    p = await asyncio.create_subprocess_exec(*args, **kwargs)
    stdouterr = await p.communicate(input=input)
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, args[0], *stdouterr)
    return p


async def void_coros_maybe(values):
    """Ignore elements of the iterable values that are not coroutine
    objects. Run all coroutine objects to completion, concurrent
    with each other. If there were exceptions, raise the leftmost
    one (not necessarily chronologically first). Return nothing.
    """
    coros = [
        asyncio.create_task(val) for val in values if asyncio.iscoroutine(val)
    ]
    if coros:
        done, _ = await asyncio.wait(coros)
        for task in done:
            task.result()  # re-raises exception if task failed


def cryptsetup(*args):
    """
    Run cryptsetup with the given arguments.  This method returns a future.
    """
    prog = ("/usr/sbin/cryptsetup", *args)
    return run_program(
        *prog,
        # otherwise cryptsetup tries to mlock() the entire locale archive :(
        env={"LC_ALL": "C", **os.environ},
        cwd="/",
        stdin=subprocess.DEVNULL,
        check=True,
        sudo=True,
    )


def sanitize_stderr_for_log(untrusted_stderr: bytes) -> str:
    """Helper function to sanitize qrexec service stderr for logging"""
    # limit size
    untrusted_stderr = untrusted_stderr[:4096]
    # limit to subset of printable ASCII, especially do not allow newlines,
    # control characters etc
    allowed = string.ascii_letters + string.digits + string.punctuation + " "
    allowed_bytes = allowed.encode()
    stderr = bytes(
        b if b in allowed_bytes else b"_"[0] for b in untrusted_stderr
    )
    return stderr.decode("ascii")


SYSFS_BASE = "/sys"


def sbdf_to_path(device_id: str):
    """
    Lookup full path for a given device

    :param device_id: sbdf, for example 0000:02:03.0; accepts also libvirt
    format like 0000_02_03_0
    :return: converted identifier of None if device is not found
    """
    regex = re.compile(
        r"\A(?:pci_)?((?P<segment>[0-9a-f]{4})[_:])?(?P<bus>[0-9a-f]{2})[_:]"
        r"(?P<device>[0-9a-f]{2})[._](?P<function>[0-9a-f])\Z"
    )
    sysfs_pci_devs_base = f"{SYSFS_BASE}/bus/pci/devices"

    root_buses = [
        dev[3:]
        for dev in os.listdir(f"{SYSFS_BASE}/devices")
        if dev.startswith("pci")
    ]

    dev_match = regex.match(device_id)
    if not dev_match:
        raise ValueError("Invalid device identifier: {!r}".format(device_id))
    if dev_match["segment"] is not None:
        segment = dev_match["segment"]
    else:
        segment = "0000"
    if f"{segment}:{dev_match['bus']}" in root_buses:
        return (f"{segment}_" if segment != "0000" else "") + (
            f"{dev_match['bus']}_"
            f"{dev_match['device']}.{dev_match['function']}"
        )
    sbdf = (
        f"{segment}:{dev_match['bus']}:"
        f"{dev_match['device']}.{dev_match['function']}"
    )
    try:
        sysfs_path = os.readlink(f"{sysfs_pci_devs_base}/{sbdf}")
    except FileNotFoundError:
        return None
    # example: ../../../devices/pci0000:00/0000:00:1a.0/0000:02:00.0
    rel_links, _, path = sysfs_path.partition(f"/pci{segment}:")
    assert os.path.normpath(
        os.path.join(sysfs_pci_devs_base, rel_links)
    ) == os.path.normpath(f"{SYSFS_BASE}/devices")
    # drop also 00/ part, which may be also 40/, 80/ etc
    path = path[3:]
    bus_offset = 0
    result_list = []
    for path_part in path.split("/"):
        assert bus_offset != -1
        bridge_match = regex.match(path_part)
        if not bridge_match:
            raise ValueError("Invalid bridge found: {!r}".format(path_part))
        assert int(bridge_match["bus"], 16) >= bus_offset
        bus_num = int(bridge_match["bus"], 16) - bus_offset
        bridge_str = (
            f"{bus_num:02x}_{bridge_match['device']}."
            f"{bridge_match['function']}"
        )
        result_list.append(bridge_str)
        try:
            with open(
                f"{sysfs_pci_devs_base}/{path_part}/secondary_bus_number",
                encoding="ascii",
            ) as f_bus_num:
                # this one is in decimal
                # this can raise ValueError, propagate it
                bus_offset = int(f_bus_num.read())
        except FileNotFoundError:
            # last device in chain
            bus_offset = -1

    if segment == "0000":
        return "-".join(result_list)
    return segment + "_" + "-".join(result_list)


def path_to_sbdf(path: str):
    """
    Convert device path as done by *sbdf_to_path* back to SBDF
    :param path:
    :return:
    """

    regex = re.compile(
        r"\A(?P<bus>[0-9a-f]+)[_:]"
        r"(?P<device>[0-9a-f]+)[._](?P<function>[0-9a-f]+)\Z"
    )
    segment_re = re.compile(r"\A(?P<segment>[0-9a-f]{4})[_:](?P<rest>.*)\Z")

    # default segment
    segment = "0000"
    bus_offset = 0
    current_dev = ""
    for path_part in path.split("-"):
        assert bus_offset != -1
        # first part may include segment
        if bus_offset == 0:
            segment_match = segment_re.match(path_part)
            if segment_match:
                segment = segment_match["segment"]
                path_part = segment_match["rest"]
        part_match = regex.match(path_part)
        if not part_match:
            raise ValueError(
                "Invalid PCI device path at {!r}".format(path_part)
            )
        bus_num = int(part_match["bus"], 16) + bus_offset
        current_dev = (
            f"{segment}:{bus_num:02x}:{part_match['device']}."
            f"{part_match['function']}"
        )
        try:
            with open(
                f"{SYSFS_BASE}/bus/pci/devices/"
                f"{current_dev}/secondary_bus_number",
                encoding="ascii",
            ) as f_bus_num:
                # this one is in decimal
                # this can raise ValueError, propagate it
                bus_offset = int(f_bus_num.read())
        except FileNotFoundError:
            # last device in chain
            bus_offset = -1

    return current_dev


def is_pci_path(device_id: str):
    """Check if given device id is already a device path.

    :param device_id: device id to check
    :return:
    """

    root_buses = [
        dev[3:].replace(":", "_")
        for dev in os.listdir(f"{SYSFS_BASE}/devices")
        if dev.startswith("pci")
    ]
    # add segment prefix for easier matching
    if len(device_id) > 2 and device_id[2] == "_":
        device_id = "0000_" + device_id
    path_re = re.compile(
        r"\A(" + "|".join(root_buses) + r")_[0-9a-f]{2}\.[0-9a-f]"
        r"(-[0-9a-f]{2}_[0-9a-f]{2}\.[0-9a-f])*\Z"
    )
    return bool(path_re.match(device_id))
