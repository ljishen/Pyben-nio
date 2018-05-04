#!/usr/bin/env python3

import argparse
import socket

from datetime import datetime as dt

from convert import human2bytes
from multiprocessing import Pool
from os import getpid

MEM_LIMIT = '500MB'


def get_args():
    parser = argparse.ArgumentParser(
        description='Simple network socket client.',
        epilog='[BKMG] indicates options that support a \
                B/K/M/G (b/kb/mb/gb) suffix for \
                byte, kilobyte, megabyte, or gigabyte')

    parser.add_argument(
        '-a', '--addresses', metavar='ADDRS', nargs='+',
        help='The list of host names or IP addresses the servers are running on \
              (separated by space)',
        required=True)
    parser.add_argument(
        '-s', '--size', type=str,
        help='The total size of raw data I/O ([BKMG])',
        required=True)
    parser.add_argument(
        '-p', '--port', type=int,
        help='The client connects to the port where the server is listening on \
             (default: 8881)',
        default=8881,
        required=False)
    parser.add_argument(
        '-b', '--bind', type=str,
        help='Specify the incoming interface for receiving data, \
              rather than allowing the kernel to set the local address to \
              INADDR_ANY during connect (see ip(7), connect(2))',
        required=False)
    parser.add_argument(
        '-l', '--bufsize', metavar='BS', type=str,
        help='The maximum amount of data in bytes to be received at once \
              (default: 4096) ([BKMG])',
        default='4K',
        required=False)

    args = parser.parse_args()

    host_addrs = args.addresses
    size = human2bytes(args.size)
    port = args.port
    bind_addr = args.bind
    bufsize = human2bytes(args.bufsize)

    return host_addrs, size, port, bind_addr, bufsize


def run(addr, size, port, bind_addr, bufsize, mem_limit_bs):
    # Create TCP socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except socket.error:
        print("\n[PID " + str(getpid()) + "]",
              "[ERROR] Could not create socket")
        raise

    print("\n[PID " + str(getpid()) + "]",
          "Connecting to servers", addr, "on port", port)

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
            s.bind((bind_addr, 0))
        except socket.error:
            print("\n[PID " + str(getpid()) + "]",
                  "[ERROR] Unable to bind on the local address", bind_addr)
            s.close()
            s = None
            raise

    # Connect to server
    try:
        s.connect((addr, port))
    except socket.error:
        print("\n[PID " + str(getpid()) + "]",
              "[ERROR] Could not connect to the server", addr)
        s.close()
        s = None
        raise

    print("\n[PID " + str(getpid()) + "]",
          "Connection established. Receiving data ...")

    left = size
    objs_size = 0
    obj_pool = []

    t_start = dt.now().timestamp()
    try:
        while left > 0:
            bys = min(bufsize, left)
            bytes_obj = s.recv(bys)
            if not bytes_obj:
                break

            obj_s = len(bytes_obj)
            # print("Received", obj_s, "bytes of data")
            obj_pool.append(bytes_obj)
            left -= obj_s
            objs_size += obj_s
            if objs_size > mem_limit_bs:
                del obj_pool[:(len(obj_pool) // 2)]
                objs_size //= 2
    finally:
        dur = dt.now().timestamp() - t_start
        recvd = size - left
        print("\n[PID " + str(getpid()) + "]",
              "Received", recvd,
              "bytes of data in", dur, "seconds",
              "(bitrate=" + str(recvd * 8 / dur) + "bit/s)")
        s.close()
        print("\n[PID " + str(getpid()) + "]", "Sockets closed")

    return recvd, dur


def main():
    host_addrs, size, port, bind_addr, bufsize = get_args()
    print("bufsize:", bufsize, "(bytes)")

    num_servs = len(host_addrs)
    if size % num_servs != 0:
        raise ValueError("Total size of raw data I/O " + str(size) +
                         " bytes is not divisible by the number of servers " +
                         str(num_servs))

    p_size = int(size / num_servs)
    mem_limit_bs = human2bytes(MEM_LIMIT) // num_servs

    with Pool(processes=num_servs) as pool:
        futures = [pool.apply_async(run,
                                    (addr, p_size, port, bind_addr,
                                     bufsize, mem_limit_bs))
                   for addr in host_addrs]
        multi_results = [f.get() for f in futures]

    (total_recvd, total_dur) = (sum(c) for c in zip(*multi_results))
    print("\n[SUMMARY] Received", total_recvd,
          "bytes of data in", total_dur, "seconds",
          "(bitrate=" + str(total_recvd * 8 / total_dur) + "bit/s)")


if __name__ == "__main__":
    main()
