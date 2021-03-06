#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Routines to enumerate mapped memory, and attempt to associate
address ranges with various ELF files and permissions.

The reason that we need robustness is that not every operating
system has /proc/$$/maps, which backs 'info proc mapping'.
"""
import sys

import gdb
import pwndbg.compat
import pwndbg.events
import pwndbg.file
import pwndbg.memoize
import pwndbg.memory
import pwndbg.proc
import pwndbg.regs
import pwndbg.remote
import pwndbg.stack
import pwndbg.typeinfo

# List of manually-explored pages which were discovered
# by analyzing the stack or register context.
explored_pages = []

def get():
    pages = []
    pages.extend(proc_pid_maps())

    if not pages:
        pages.extend(info_auxv())

        if pages: pages.extend(info_sharedlibrary())
        else:     pages.extend(info_files())

        pages.extend(pwndbg.stack.stacks.values())

    pages.extend(explored_pages)
    pages.sort()
    return pages

@pwndbg.memoize.reset_on_stop
def find(address):
    if address is None or address < pwndbg.memory.MMAP_MIN_ADDR:
        return None

    for page in get():
        if address in page:
            return page

    return explore(address)

def explore(address_maybe):
    """
    Given a potential address, check to see what permissions it has.

    Returns:
        Page object

    Note:
        Adds the Page object to a persistent list of pages which are
        only reset when the process dies.  This means pages which are
        added this way will not be removed when unmapped.

        Also assumes the entire contiguous section has the same permission.
    """
    address_maybe = pwndbg.memory.page_align(address_maybe)

    flags = 4 if pwndbg.memory.peek(address_maybe) else 0

    if not flags:
        return None

    flags |= 2 if pwndbg.memory.poke(address_maybe) else 0
    flags |= 1 if not pwndbg.stack.nx               else 0

    page = find_boundaries(address_maybe)
    page.flags = flags

    explored_pages.append(page)

    return page

# Automatically ensure that all registers are explored on each stop
@pwndbg.events.stop
def explore_registers():
    for regname in pwndbg.regs.common:
        find(pwndbg.regs[regname])


@pwndbg.events.exit
def clear_explored_pages():
    while explored_pages:
        explored_pages.pop()

@pwndbg.memoize.reset_on_stop
def proc_pid_maps():
    """
    Parse the contents of /proc/$PID/maps on the server.

    Returns:
        A list of pwndbg.memory.Page objects.
    """

    example_proc_pid_maps = """
    7f95266fa000-7f95268b5000 r-xp 00000000 08:01 418404                     /lib/x86_64-linux-gnu/libc-2.19.so
    7f95268b5000-7f9526ab5000 ---p 001bb000 08:01 418404                     /lib/x86_64-linux-gnu/libc-2.19.so
    7f9526ab5000-7f9526ab9000 r--p 001bb000 08:01 418404                     /lib/x86_64-linux-gnu/libc-2.19.so
    7f9526ab9000-7f9526abb000 rw-p 001bf000 08:01 418404                     /lib/x86_64-linux-gnu/libc-2.19.so
    7f9526abb000-7f9526ac0000 rw-p 00000000 00:00 0
    7f9526ac0000-7f9526ae3000 r-xp 00000000 08:01 418153                     /lib/x86_64-linux-gnu/ld-2.19.so
    7f9526cbe000-7f9526cc1000 rw-p 00000000 00:00 0
    7f9526ce0000-7f9526ce2000 rw-p 00000000 00:00 0
    7f9526ce2000-7f9526ce3000 r--p 00022000 08:01 418153                     /lib/x86_64-linux-gnu/ld-2.19.so
    7f9526ce3000-7f9526ce4000 rw-p 00023000 08:01 418153                     /lib/x86_64-linux-gnu/ld-2.19.so
    7f9526ce4000-7f9526ce5000 rw-p 00000000 00:00 0
    7f9526ce5000-7f9526d01000 r-xp 00000000 08:01 786466                     /bin/dash
    7f9526f00000-7f9526f02000 r--p 0001b000 08:01 786466                     /bin/dash
    7f9526f02000-7f9526f03000 rw-p 0001d000 08:01 786466                     /bin/dash
    7f9526f03000-7f9526f05000 rw-p 00000000 00:00 0
    7f95279fe000-7f9527a1f000 rw-p 00000000 00:00 0                          [heap]
    7fff3c177000-7fff3c199000 rw-p 00000000 00:00 0                          [stack]
    7fff3c1e8000-7fff3c1ea000 r-xp 00000000 00:00 0                          [vdso]
    ffffffffff600000-ffffffffff601000 r-xp 00000000 00:00 0                  [vsyscall]
    """

    locations = [
        '/proc/%s/maps' % pwndbg.proc.pid,
        '/proc/%s/map'  % pwndbg.proc.pid,
        '/usr/compat/linux/proc/%s/maps'  % pwndbg.proc.pid,
    ]

    for location in locations:
        try:
            data = pwndbg.file.get(location)
            break
        except (OSError, gdb.error):
            continue
    else:
        return tuple()

    if pwndbg.compat.python3:
        data = data.decode()

    pages = []
    for line in data.splitlines():
        maps, perm, offset, dev, inode_objfile = line.split(None, 4)

        try:    inode, objfile = inode_objfile.split()
        except: objfile = ''

        start, stop = maps.split('-')

        start  = int(start, 16)
        stop   = int(stop, 16)
        offset = int(offset, 16)
        size   = stop-start

        flags = 0
        if 'r' in perm: flags |= 4
        if 'w' in perm: flags |= 2
        if 'x' in perm: flags |= 1

        page = pwndbg.memory.Page(start, size, flags, offset, objfile)
        pages.append(page)

    return tuple(pages)


@pwndbg.memoize.reset_on_objfile
def info_sharedlibrary():
    """
    Parses the output of `info sharedlibrary`.

    Specifically, all we really want is any valid pointer into each library,
    and the path to the library on disk.

    With this information, we can use the ELF parser to get all of the
    page permissions for every mapped page in the ELF.

    Returns:
        A list of pwndbg.memory.Page objects.
    """

    exmaple_info_sharedlibrary_freebsd = """
    From        To          Syms Read   Shared Object Library
    0x280fbea0  0x2810e570  Yes (*)     /libexec/ld-elf.so.1
    0x281260a0  0x281495c0  Yes (*)     /lib/libncurses.so.8
    0x28158390  0x2815dcf0  Yes (*)     /usr/local/lib/libintl.so.9
    0x28188b00  0x2828e060  Yes (*)     /lib/libc.so.7
    (*): Shared library is missing debugging information.
    """

    exmaple_info_sharedlibrary_linux = """
    From                To                  Syms Read   Shared Object Library
    0x00007ffff7ddaae0  0x00007ffff7df54e0  Yes         /lib64/ld-linux-x86-64.so.2
    0x00007ffff7bbd3d0  0x00007ffff7bc9028  Yes (*)     /lib/x86_64-linux-gnu/libtinfo.so.5
    0x00007ffff79aded0  0x00007ffff79ae9ce  Yes         /lib/x86_64-linux-gnu/libdl.so.2
    0x00007ffff76064a0  0x00007ffff774c113  Yes         /lib/x86_64-linux-gnu/libc.so.6
    (*): Shared library is missing debugging information.
    """
    pages = []

    for line in gdb.execute('info sharedlibrary', to_string=True).splitlines():
        if not line.startswith('0x'):
            continue

        tokens = line.split()
        text   = int(tokens[0], 16)
        obj    = tokens[-1]

        pages.extend(pwndbg.elf.map(text, obj))

    return tuple(sorted(pages))

@pwndbg.memoize.reset_on_objfile
def info_files():

    example_info_files_linues = """
    Symbols from "/bin/bash".
    Unix child process:
    Using the running image of child process 5903.
    While running this, GDB does not access memory from...
    Local exec file:
    `/bin/bash', file type elf64-x86-64.
    Entry point: 0x42020b
    0x0000000000400238 - 0x0000000000400254 is .interp
    0x0000000000400254 - 0x0000000000400274 is .note.ABI-tag
    ...
    0x00000000006f06c0 - 0x00000000006f8ca8 is .data
    0x00000000006f8cc0 - 0x00000000006fe898 is .bss
    0x00007ffff7dda1c8 - 0x00007ffff7dda1ec is .note.gnu.build-id in /lib64/ld-linux-x86-64.so.2
    0x00007ffff7dda1f0 - 0x00007ffff7dda2ac is .hash in /lib64/ld-linux-x86-64.so.2
    0x00007ffff7dda2b0 - 0x00007ffff7dda38c is .gnu.hash in /lib64/ld-linux-x86-64.so.2
    """

    seen_files = set()
    pages      = list()
    main_exe   = ''

    for line in gdb.execute('info files', to_string=True).splitlines():
        line = line.strip()

        # The name of the main executable
        if line.startswith('`'):
            exename, filetype = line.split(None, 1)
            main_exe = exename.strip("`,'")
            continue

        # Everything else should be addresses
        if not line.startswith('0x'):
            continue

        # start, stop, _, segment, _, filename = line.split(None,6)
        fields = line.split(None,6)
        vaddr  = int(fields[0], 16)

        if len(fields) == 5:    objfile = main_exe
        elif len(fields) == 7:  objfile = fields[6]
        else:
            print("Bad data: %r" % line)
            continue

        if objfile in seen_files:
            continue
        else:
            seen_files.add(objfile)

        pages.extend(pwndbg.elf.map(vaddr, objfile))

    return tuple(pages)



@pwndbg.memoize.reset_on_exit
def info_auxv(skip_exe=False):
    """
    Extracts the name of the executable from the output of the command
    "info auxv".

    Arguments:
        skip_exe(bool): Do not return any mappings that belong to the exe.

    Returns:
        A list of pwndbg.memory.Page objects.
    """
    auxv = pwndbg.auxv.get()

    if not auxv:
        return tuple()

    pages    = []
    exe_name = auxv.AT_EXECFN or 'main.exe'
    entry    = auxv.AT_ENTRY
    vdso     = auxv.AT_SYSINFO_EHDR
    phdr     = auxv.AT_PHDR

    if not skip_exe and (entry or phdr):
        pages.extend(pwndbg.elf.map(entry or phdr, exe_name))

    if vdso:
        pages.append(find_boundaries(vdso, '[vdso]'))

    return tuple(sorted(pages))


def find_boundaries(addr, name=''):
    """
    Given a single address, find all contiguous pages
    which are mapped.
    """
    start = pwndbg.memory.find_lower_boundary(addr)
    end   = pwndbg.memory.find_upper_boundary(addr)
    return pwndbg.memory.Page(start, end-start, 4, 0, name)

aslr = False

@pwndbg.events.new_objfile
@pwndbg.memoize.while_running
def check_aslr():
    vmmap = sys.modules[__name__]
    vmmap.aslr = False

    # Check to see if ASLR is disabled on the system.
    # if not pwndbg.remote.is_remote():
    system_aslr = True
    data        = b''

    try:
        data = pwndbg.file.get('/proc/sys/kernel/randomize_va_space')
    except Exception as e:
        print(e)
        pass

    # Systemwide ASLR is disabled
    if b'0' in data:
        return

    output = gdb.execute('show disable-randomization', to_string=True)
    if "is off." in output:
        vmmap.aslr = True

    return vmmap.aslr

