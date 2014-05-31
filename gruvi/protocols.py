#
# This file is part of Gruvi. Gruvi is free software available under the
# terms of the MIT license. See the file "LICENSE" that was provided
# together with this source file for the licensing terms.
#
# Copyright (c) 2012-2014 the Gruvi authors. See the file "AUTHORS" for a
# complete list.

from __future__ import absolute_import, print_function

from .sync import Event, Queue
from .errors import Error, Cancelled
from .hub import get_hub, switchpoint
from .fibers import Fiber
from . import logging, util

__all__ = ['ProtocolError', 'BaseProtocol', 'Protocol', 'DatagramProtocol']


class ProtocolError(Error):
    """Base class for Protocol errors."""


class BaseProtocol(object):
    """Base class for all protocols."""

    read_buffer_size = 65536

    def __init__(self):
        self._transport = None
        self._log = logging.get_logger()
        self._hub = get_hub()
        self._read_buffer_size = 0
        self._read_buffer_high = self.read_buffer_size
        self._read_buffer_low = self.read_buffer_size // 2
        self._error = None
        self._may_write = Event()
        self._may_write.set()
        self._closing = False
        self._closed = Event()
        self._reading = False

    def connection_made(self, transport):
        """Called when a connection is made."""
        self._transport = transport
        self._reading = True

    def connection_lost(self, exc):
        """Called when a connection is lost."""
        # Unblock everybody who might be waiting.
        if self._error is None:
            self._error = exc
        self._closed.set()
        self._closing = False
        self._may_write.set()
        self._transport = None

    def pause_writing(self):
        """Called when the write buffer in the transport has exceeded the high
        water mark. The protocol should stop writing new data."""
        self._may_write.clear()

    def resume_writing(self):
        """Called when the write buffer in the transport has fallen below the
        low water mark. The protocol can start writing data again."""
        self._may_write.set()

    def get_read_buffer_size(self):
        """Return the current size of the read buffer."""
        return self._read_buffer_size

    def set_read_buffer_limits(self, high=None, low=None):
        """Set the low and high watermark for the read buffer."""
        if high is None:
            high = self.read_buffer_size
        if low is None:
            low = high // 2
        if low > high:
            low = high
        self._read_buffer_high = high
        self._read_buffer_low = low

    def read_buffer_size_changed(self):
        """Notify the protocol that the buffer size has changed."""
        if self._transport is None:
            return
        bufsize = self.get_read_buffer_size()
        if bufsize >= self._read_buffer_high and self._reading:
            self._transport.pause_reading()
            self._reading = False
        elif bufsize <= self._read_buffer_low and not self._reading:
            self._transport.resume_reading()
            self._reading = True

    @switchpoint
    def close(self):
        """Close the protocol."""
        if self._closing or self._closed:
            return
        self._closing = True
        self._transport.close()
        self._closed.wait()


class Protocol(BaseProtocol):
    """Base class for stream oriented protocols."""

    def data_received(self, data):
        """A new chunk of data was received."""

    def eof_received(self):
        """An EOF was received."""

    @switchpoint
    def write(self, data):
        """Write *data* to the underlying transport.

        If the write bufer is currently above the high-water mark, then this
        method wait until it drops below the low-water mark.
        """
        self._may_write.wait()
        if self._error:
            raise self._error
        elif self._closing or self._closed:
            raise ProtocolError('protocol is closing/closed')
        self._transport.write(data)

    @switchpoint
    def writelines(self, seq):
        """Write all lines in *seq* to the underlying transport."""
        self._may_write.wait()
        if self._error:
            raise self._error
        elif self._closing or self._closed:
            raise ProtocolError('protocol is closing/closed')
        self._transport.writelines(seq)

    @switchpoint
    def write_eof(self):
        """Shut down the write direction."""
        self._may_write.wait()
        if self._error:
            raise self._error
        elif self._closing or self._closed:
            raise ProtocolError('protocol is closing/closed')
        self._transport.write_eof()


class MessageProtocol(Protocol):
    """Base class for message oriented protocols."""

    def __init__(self, message_handler=None):
        super(MessageProtocol, self).__init__()
        self._message_handler = message_handler
        self._queue = Queue()
        if callable(message_handler):
            name = util.split_cap_words(type(self).__name__)[0]
            key = 'next_{0}_dispatcher'.format(name.lower())
            seq = self._hub.data.setdefault(key, 1)
            self._hub.data[key] += 1
            name = '{0}-{1}'.format(name, seq)
            self._dispatcher = Fiber(self._dispatch_loop, name=name)
            self._dispatcher.start()
        else:
            self._dispatcher = None

    def connection_lost(self, exc):
        # Protocol callback.
        # The connection is lost, which means that no requests that is either
        # outstanding or in-progress will be able to send output to the remote
        # peer. Therefore we just to discard everything here.
        super(MessageProtocol, self).connection_lost(exc)
        if self._dispatcher:
            self._dispatcher.cancel()
            self._dispatcher = None

    def message_received(self, message):
        # Protocol callback
        self._message_handler(message, self._transport, self)

    def get_message(self, block=True, timeout=None):
        """Return a message from the queue."""
        return self._queue.get(block, timeout)

    def _dispatch_loop(self):
        # Dispatcher loop: runs in a separate fiber and is only started
        # when there is a message handler.
        self._log.debug('dispatcher starting')
        assert self._message_handler is not None
        try:
            while True:
                message = self._queue.get()
                self.read_buffer_size_changed()
                self.message_received(message)
        except Cancelled as e:
            self._log.debug('dispatcher was canceled')
        except ProtocolError as e:
            self._log.error('{!s}, closing connection', e)
            self._error = e
            self._transport.close()
        except Exception as e:
            self._log.exception('uncaught exception in dispatcher')
            self._error = ProtocolError('uncaught exception in dispatcher')
            self._transport.close()
        self._log.debug('dispatcher exiting')


class DatagramProtocol(BaseProtocol):
    """Base classs for datagram oriented protocols."""

    def datagram_received(self, data, addr):
        """A new datagram was received."""

    def error_received(self, exc):
        """An error was received."""
