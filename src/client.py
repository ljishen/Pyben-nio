#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from argparse import Namespace
from collections import deque
from datetime import datetime as dt
from multiprocessing import Pool

import logging
import socket

from paramparser import ParameterParser
from util import Util


def __populate_start_parser(start_parser):
    start_parser.add_argument(
        '-a', '--addresses', metavar='ADDRS', nargs='+',
        help='The list of host names or IP addresses the servers are running \
              on (separated by space)',
        required=True)
    start_parser.add_argument(
        '-s', '--size', type=str,
        help='The total size of data I/O ([BKMG])',
        required=True)
    start_parser.add_argument(
        '-p', '--port', type=int,
        help='The client connects to the port where the server is listening \
              on (default: 8881)',
        default=8881)
    start_parser.add_argument(
        '-b', '--bind', type=str,
        help='Specify the incoming interface for receiving data, \
              rather than allowing the kernel to set the local address to \
              INADDR_ANY during connect (see ip(7), connect(2))')
    start_parser.add_argument(
        '-l', '--bufsize', metavar='BS', type=str,
        help='The maximum amount of data to be received at once \
              (default: 4KB) ([BKMG])',
        default='4KB')
    start_parser.add_argument(
        '-c', '--cache', type=str,
        help='Size of cache for keeping the most recent received data \
              (default: 512MB) ([BKMG])',
        default='512MB')

    start_parser.set_multi_value_dest('method')
    start_parser.add_argument(
        '-m', '--method', type=str,
        help='The data filtering method to apply on reading from the socket \
              (default: raw). Use semicolon (;) to separate method parameters',
        choices=Util.list_methods(),
        default='raw')

    start_parser.set_defaults(func=__handle_start)


def __handle_start(arg_attrs_ns):
    args_ns = Namespace(
        host_addrs=arg_attrs_ns.addresses,
        size=Util.human2bytes(arg_attrs_ns.size),
        port=arg_attrs_ns.port,
        bind_addr=arg_attrs_ns.bind,
        bufsize=Util.human2bytes(arg_attrs_ns.bufsize),
        cache=Util.human2bytes(arg_attrs_ns.cache),
        method=ParameterParser.split_multi_value_param(arg_attrs_ns.method))

    __do_start(args_ns)


def __setup_socket(addr, port, bind_addr):
    # Create TCP socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except socket.error:
        logger.exception("Could not create socket")
        raise

    logger.info("Connecting to server %s on port %d", addr, port)

    if bind_addr:
        # Bind the interface for data receiving
        try:
            # See the "The port 0 trick"
            # (https://www.dnorth.net/2012/03/17/the-port-0-trick/)
            # and "Bind before connect"
            # (https://idea.popcount.org/2014-04-03-bind-before-connect/)
            #
            # We might not need to set the socket flag SO_REUSEADDR since
            # the server side also ready does so.
            sock.bind((bind_addr, 0))
        except socket.error:
            logger.exception(
                "Unable to bind on the local address %s", bind_addr)
            sock.close()
            sock = None
            raise

    # Connect to server
    try:
        sock.connect((addr, port))
        logger.info("Connection established. Receiving data ...")
    except socket.error:
        logger.exception("Could not connect to the server %s", addr)
        sock.close()
        sock = None
        raise

    return sock


def __run(idx, classobj, args_ns, size, mem_limit_bs):
    sock = __setup_socket(
        args_ns.host_addrs[idx], args_ns.port, args_ns.bind_addr)
    iofilter = classobj.create(
        sock, args_ns.bufsize, extra_args=args_ns.method[1:])

    if mem_limit_bs:
        byte_mem = deque(maxlen=mem_limit_bs)  # type: typing.Deque[int]

    left = size
    recvd = 0

    t_start = dt.now().timestamp()
    try:
        while left > 0:
            num_bys = min(args_ns.bufsize, left)
            bytes_obj, ctrl_num = iofilter.read(num_bys)
            if not ctrl_num:
                break

            if mem_limit_bs:
                byte_mem.extend(bytes_obj)

            byte_length = len(bytes_obj)
            recvd += byte_length

            left -= ctrl_num

            if logger.isEnabledFor(logging.DEBUG):
                bytes_summary = bytes(bytes_obj[:50])
                logger.debug("Received %d bytes of data (summary %r%s)",
                             byte_length,
                             bytes_summary,
                             '...' if byte_length > len(bytes_summary)
                             else '')
    except ValueError:
        logger.exception(
            "Fail to read data from buffered stream %r", sock)
        raise
    finally:
        t_end = dt.now().timestamp()
        t_dur = t_end - t_start
        logger.info("[Received: %d bytes (%d raw bytes)] \
[Duration: %s seconds] [Bitrate: %s bit/s]",
                    recvd,
                    iofilter.get_count(),
                    t_dur, recvd * 8 / t_dur)
        iofilter.close()
        logger.info("Socket closed")

    return t_start, t_end, recvd, iofilter.get_count()


def __allot_size(size, num):
    i_size = size // num
    left = size - i_size * num

    p_sizes = []
    for i in range(num):
        p_sizes.append(i_size)
        if i < left:
            p_sizes[i] += 1

    return p_sizes


def __do_start(args_ns):
    logger.info("[bufsize: %d bytes]", args_ns.bufsize)

    num_servs = len(args_ns.host_addrs)

    p_sizes = __allot_size(args_ns.size, num_servs)
    classobj = Util.get_classobj_of(args_ns.method[0], socket.socket)

    mem_limit_bs = args_ns.cache // num_servs

    with Pool(processes=num_servs) as pool:
        futures = [pool.apply_async(__run,
                                    (idx, classobj, args_ns,
                                     size, mem_limit_bs))
                   for idx, size in enumerate(p_sizes)]
        multi_results = [f.get() for f in futures]

    t_starts, t_ends, recvds, raw_bytes_reads = zip(*multi_results)
    total_dur = max(t_ends) - min(t_starts)
    total_recvd = sum(recvds)
    total_raw_bytes_read = sum(raw_bytes_reads)

    # We might not receive anything if the server failed.
    raw_bytes_read_info = ''
    if total_raw_bytes_read:
        raw_bytes_read_info = ' ({:.3f}% of {:d} raw bytes)'.format(
            total_recvd / total_raw_bytes_read * 100, total_raw_bytes_read)

    logger.info("[SUMMARY] [Received: %d bytes%s] [Duration: %s seconds] \
[Bitrate: %s bit/s]",
                total_recvd,
                raw_bytes_read_info,
                total_dur,
                total_recvd * 8 / total_dur)


def main():
    """Entrypoint function."""
    prog_desc = 'Simple network socket client with customized \
workload support.'

    parser = ParameterParser(description=prog_desc)
    start_parser = parser.prepare(socket.socket)
    __populate_start_parser(start_parser)

    parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        format='%(asctime)s | %(name)-16s | \
%(levelname)-8s | PID=%(process)d | %(message)s',
        level=logging.DEBUG)
    logger = logging.getLogger('client')  # pylint: disable=C0103

    main()
