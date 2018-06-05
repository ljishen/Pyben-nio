#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from inspect import getfullargspec
from io import BufferedIOBase
from multiprocessing import Pool
from socket import socket

import logging
import typing
import os

import iofilter

from util import Util

# pylint: disable=invalid-name
expr_func = lambda v: v  # noqa: E731


def _check(byte_arr: bytearray, res: bytearray = None) -> bytearray:
    if res is None:
        res = bytearray()

    for byt in byte_arr:
        if expr_func(byt):
            res.append(byt)

    return res


# pylint: disable=global-statement
class Match(iofilter.IOFilter[iofilter.T]):
    """Read the bytes that match the function check."""

    logger = logging.getLogger(__name__)

    PARAM_FUNC = 'func'
    PARAM_MINPROCWORKSIZE = 'mpws'

    def __init__(
            self: 'Match',
            stream: iofilter.T,
            bufsize: int,
            **kwargs) -> None:
        """Initialize base attributes for all subclasses."""
        super().__init__(stream, bufsize, **kwargs)

        global expr_func  # pylint: disable=invalid-name
        # Make this variable global so all the subprocesses can inherit
        # this variable automatically
        expr_func = kwargs[self.PARAM_FUNC]

        num_procs = int(bufsize / kwargs[self.PARAM_MINPROCWORKSIZE] + 0.5)
        if num_procs < 1:
            num_procs = 1
        else:
            num_usable_cpus = len(os.sched_getaffinity(0))
            if num_procs > num_usable_cpus:
                kwargs[self.PARAM_MINPROCWORKSIZE] = \
                    int(bufsize / (num_usable_cpus - 0.5))
                self.logger.warning(
                    "Not enough CPU cores available. Change %r to %d",
                    self.PARAM_MINPROCWORKSIZE,
                    kwargs[self.PARAM_MINPROCWORKSIZE])
                num_procs = num_usable_cpus

        self._procs_pool = None
        if num_procs > 1:
            self._procs_pool = Pool(processes=num_procs)
        self.logger.info("Start %d process%s to handle data filtering",
                         num_procs,
                         'es' if num_procs > 1 else '')

        self._resbuf = bytearray()

    def close(self: 'Match') -> None:
        """Close associated resources."""
        super().close()
        if self._procs_pool:
            self._procs_pool.close()

    @classmethod
    def _get_method_params(cls: typing.Type['Match']) -> typing.List[
            iofilter.MethodParam]:
        return [
            iofilter.MethodParam(
                cls.PARAM_FUNC,
                cls.__convert_func,
                'It defines the function check that whether the read \
                operation should return the byte. This function should only \
                accpet a single argument as an int value of the byte and \
                return an object that subsequently will be used in the bytes \
                filtering based on its truth value. \
                Also see truth value testing in Python 3: \
        https://docs.python.org/3/library/stdtypes.html#truth-value-testing'),
            iofilter.MethodParam(
                cls.PARAM_MINPROCWORKSIZE,
                Util.human2bytes,
                'The minimum number of bytes that handle by each process \
                each time. The number of processes in use depends on the \
                bufsize and this value.',
                '50MB'
            )
        ]

    @classmethod
    def __convert_func(
            cls: typing.Type['Match'],
            expr: str) -> typing.Callable[[int], object]:
        try:
            func = eval(expr)  # pylint: disable=eval-used
        except Exception:
            cls.logger.exception(
                "Unable to parse function expression: %s", expr)
            raise

        try:
            num_args = len(getfullargspec(func).args)
        except TypeError:
            cls.logger.exception(
                "Fail to inspect parameters of function expresstion: %s", expr)
            raise

        if num_args != 1:
            raise ValueError("Function expresstion %s has more than 1 argument"
                             % expr)

        return func

    def _get_and_update_res(self: 'Match', size: int) -> bytearray:
        if size >= len(self._resbuf):
            ret_res = self._resbuf
            self._resbuf = bytearray()
        else:
            ret_res = self._resbuf[:size]
            self._resbuf = self._resbuf[size:]
        return ret_res

    def _allot_work_offsets(
            self: 'Match', total_size: int) -> typing.List[int]:
        min_proc_worksize = self.kwargs[self.PARAM_MINPROCWORKSIZE]

        least_num_procs = total_size // min_proc_worksize
        work_offsets = [min_proc_worksize] * least_num_procs
        left = total_size - least_num_procs * min_proc_worksize
        if left >= min_proc_worksize / 2:
            work_offsets.append(left)
        else:
            work_offsets[-1] += left

        for idx in range(1, least_num_procs):
            work_offsets[idx] += work_offsets[idx - 1]

        return work_offsets


class MatchIO(Match[BufferedIOBase]):
    """A subclass to handle reading data from file."""

    def __init__(
            self: 'MatchIO',
            stream: BufferedIOBase,
            bufsize: int,
            **kwargs) -> None:
        """Initialize addtional attribute for instance of this class.

        Attributes:
            __first_read (bool): Identify if it is the first time of
                reading through the whole file.

        """
        super().__init__(stream, bufsize, **kwargs)
        self.__first_read = True

    def read(self, size: int) -> bytes:
        """Read data from the file stream."""
        super().read(size)

        if len(self._resbuf) >= size:
            return self._get_and_update_res(size)

        view = self._get_or_create_bufview()

        while True:
            nbytes = self._stream.readinto(view[:size])
            if nbytes < size:
                self._stream.seek(0)

            if nbytes:
                self._incr_count(nbytes)

                if self._procs_pool is None:
                    _check(view[:nbytes], res=self._resbuf)
                else:
                    work_offsets = self._allot_work_offsets(nbytes)
                    prev_offset = 0
                    future_results = []
                    for offset in work_offsets:
                        future_results.append(
                            self._procs_pool.apply_async(
                                _check,
                                (bytearray(view[prev_offset:offset]),))
                        )
                        prev_offset = offset

                    for f_res in future_results:
                        self._resbuf.extend(f_res.get())

                if len(self._resbuf) >= size:
                    self.__first_read = False
                    return self._get_and_update_res(size)

            if (self.__first_read
                    and self._stream.tell() == 0
                    and not self._resbuf):
                raise ValueError(
                    "No matching byte in the buffered stream %r"
                    % self._stream)


class MatchSocket(Match[socket]):
    """A subclass to handle reading data from socket."""

    def read(self, size: int) -> bytes:
        """Read data from the socket stream."""
        super().read(size)

        if len(self._resbuf) >= size:
            return self._get_and_update_res(size)

        view = self._get_or_create_bufview()

        while True:
            nbytes = self._stream.recv_into(view, size)
            if not nbytes:
                return self._get_and_update_res(len(self._resbuf))

            self._incr_count(nbytes)

            if self._procs_pool is None:
                _check(view[:nbytes], self._resbuf)
            else:
                work_offsets = self._allot_work_offsets(nbytes)
                prev_offset = 0
                future_results = []
                for offset in work_offsets:
                    future_results.append(
                        self._procs_pool.apply_async(
                            _check,
                            (bytearray(view[prev_offset:offset]),))
                    )
                    prev_offset = offset

                for f_res in future_results:
                    self._resbuf.extend(f_res.get())

            if len(self._resbuf) >= size:
                return self._get_and_update_res(size)
