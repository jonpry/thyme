from __future__ import unicode_literals

#import gevent.monkey
#gevent.monkey.patch_all()

import base64
import binascii
import hashlib
import msgpack
import random
import redis
import six
import string
import time
import uuid
from gevent.lock import Semaphore
from asgiref.base_layer import BaseChannelLayer

block_tuple = 1
nonblock_tuple = 0

class UnsupportedRedis(Exception):
    pass


class GRedisChannelLayer(BaseChannelLayer):
    """
    Redis channel layer using gevent

    It routes all messages into remote Redis server.  Support for
    sharding among different Redis installations and message
    encryption are provided.  Both synchronous and asynchronous (via
    Twisted) approaches are implemented.
    """

    blpop_timeout = 5
    global_statistics_expiry = 86400
    channel_statistics_expiry = 3600
    global_stats_key = '#global#'  # needs to be invalid as a channel name

    def __init__(
        self,
        expiry=60,
        hosts=None,
        prefix="asgi:",
        group_expiry=86400,
        capacity=100,
        channel_capacity=None,
        symmetric_encryption_keys=None,
        stats_prefix="asgi-meta:",
        socket_connect_timeout=None,
        socket_timeout=None,
        socket_keepalive=None,
        socket_keepalive_options=None,
    ):
        super(GRedisChannelLayer, self).__init__(
            expiry=expiry,
            group_expiry=group_expiry,
            capacity=capacity,
            channel_capacity=channel_capacity,
        )
        # Make sure they provided some hosts, or provide a default
        if not hosts:
            hosts = [("localhost", 6379)]
        self.hosts = []

        if isinstance(hosts, six.string_types):
            # user accidentally used one host string instead of providing a list of hosts
            raise ValueError('ASGI Redis hosts must be specified as an iterable list of hosts.')

        for entry in hosts:
            if isinstance(entry, six.string_types):
                self.hosts.append(entry)
            else:
                self.hosts.append("redis://%s:%d/0" % (entry[0], entry[1]))
        self.prefix = prefix
        assert isinstance(self.prefix, six.text_type), "Prefix must be unicode"
        # Precalculate some values for ring selection
        self.ring_size = len(self.hosts)
        # Create connections ahead of time (they won't call out just yet, but
        # we want to connection-pool them later)
        if socket_timeout and socket_timeout < self.blpop_timeout:
            raise ValueError("The socket timeout must be at least %s seconds" % self.blpop_timeout)
        self._connection_list = self._generate_connections(
            redis_kwargs={
                "socket_connect_timeout": socket_connect_timeout,
                "socket_timeout": socket_timeout,
                "socket_keepalive": socket_keepalive,
                "socket_keepalive_options": socket_keepalive_options,
            },
        )
        # Decide on a unique client prefix to use in ! sections
        # TODO: ensure uniqueness better, e.g. Redis keys with SETNX
        self.client_prefix = "".join(random.choice(string.ascii_letters) for i in range(8))
        # Register scripts
        connection = self.connection(None)[block_tuple]
        self.chansend = connection.register_script(self.lua_chansend)
        #self.lpopmany = connection.register_script(self.lua_lpopmany)
        #self.delprefix = connection.register_script(self.lua_delprefix)
        #self.incrstatcounters = connection.register_script(self.lua_incrstatcounters)
        self.crypter = None
        self.stats_prefix = stats_prefix
        self.semaphore = Semaphore()

    def _generate_connections(self, redis_kwargs):
        return [
            (redis.Redis.from_url(host, **redis_kwargs),
                redis.Redis.from_url(host, **redis_kwargs))
            for host in self.hosts
        ]

    ### ASGI API ###

    extensions = ["groups", "flush", "statistics"]

    def send(self, channel, message):
        self.semaphore.acquire()
        #print "send"
        if "!" in channel:
           parts = channel.split("!")
           channel = parts[0]
           message["conn_key"] = parts[1] 
        #print channel
        #print message
        # Typecheck
        assert isinstance(message, dict), "message is not a dict"
        assert self.valid_channel_name(channel), "Channel name not valid"
        # Write out message into expiring key (avoids big items in list)
        # TODO: Use extended set, drop support for older redis?
        message_key = self.prefix + uuid.uuid4().hex
        channel_key = self.prefix + channel

        #print channel_key

        # Pick a connection to the right server - consistent for response
        # channels, random for normal channels
        if "!" in channel or "?" in channel:
            index = self.consistent_hash(channel)
            connection = self.connection(index)
        else:
            connection = self.connection(None)
        # Use the Lua function to do the set-and-push
        try:
            self.chansend(
                keys=[message_key, channel_key],
                args=[self.serialize(message), self.expiry, self.get_capacity(channel)],
                client=connection[block_tuple],
            )
        except redis.exceptions.ResponseError as e:
            print "some kind of exception"
            self.semaphore.release()
            # The Lua script handles capacity checking and sends the "full" error back
            if e.args[0] == "full":
                self._incr_statistics_counter(
                    stat_name=self.STAT_CHANNEL_FULL,
                    channel=channel,
                    connection=connection,
                )
                raise self.ChannelFull
            elif "unknown command" in e.args[0]:
                raise UnsupportedRedis(
                    "Redis returned an error (%s). Please ensure you're running a "
                    " version of redis that is supported by asgi_redis." % e.args[0])
            else:
                # Let any other exception bubble up
                raise
        self.semaphore.release()

    def receive(self, channels, block=False):
        # List name get
        indexes = self._receive_list_names(channels)
        #print indexes
        #print channels
        # Short circuit if no channels
        if indexes is None:
            return None, None
        # Get a message from one of our channels
        while True:
            # Select a random connection to use
            index = random.choice(list(indexes.keys()))
            list_names = indexes[index]
            # Shuffle list_names to avoid the first ones starving others of workers
            random.shuffle(list_names)
            # Open a connection
            connection = self.connection(index)
            # Pop off any waiting message
            if block:
                result = connection[nonblock_tuple].blpop(list_names, timeout=self.blpop_timeout)
            else:
                raise
                result = self.lpopmany(keys=list_names, client=connection)
            if result:
                content = connection[nonblock_tuple].get(result[1])
                #print content
                # If the content key expired, keep going.
                if content is None:
                    continue
                # Return the channel it's from and the message
                #print result[0][len(self.prefix):].decode("utf8")
                return result[0][len(self.prefix):].decode("utf8"), self.deserialize(content)
            else:
                return None, None

    def _receive_list_names(self, channels):
        """
        Inner logic of receive; takes channels, groups by shard, and
        returns {connection_index: list_names ...} if a query is needed or
        None for a vacuously empty response.
        """
        # Short circuit if no channels
        if not channels:
            return None
        # Check channel names are valid
        channels = list(channels)
        assert all(self.valid_channel_name(channel) for channel in channels), "One or more channel names invalid"
        # Work out what servers to listen on for the given channels
        indexes = {}
        random_index = self.random_index()
        for channel in channels:
            if "!" in channel or "?" in channel:
                indexes.setdefault(self.consistent_hash(channel), []).append(
                    self.prefix + channel,
                )
            else:
                indexes.setdefault(random_index, []).append(
                    self.prefix + channel,
                )
        return indexes

    def new_channel(self, pattern):
        self.semaphore.acquire()
        assert isinstance(pattern, six.text_type)
        # Keep making channel names till one isn't present.
        while True:
            random_string = "".join(random.choice(string.ascii_letters) for i in range(12))
            assert pattern.endswith("!") or pattern.endswith("?")
            new_name = pattern + random_string
            # Get right connection
            index = self.consistent_hash(new_name)
            connection = self.connection(index)
            # Check to see if it's in the connected Redis.
            # This fails to stop collisions for sharding where the channel is
            # non-single-listener, but that seems very unlikely.
            key = self.prefix + new_name
            if not connection[block_tuple].exists(key):
                self.semaphore.release()
                return new_name

    ### ASGI Group extension ###

    def group_add(self, group, channel):
        self.semaphore.acquire()
        """
        Adds the channel to the named group for at least 'expiry'
        seconds (expiry defaults to message expiry if not provided).
        """
        #print "add: " + str(group) + ", " + str(channel)
        assert self.valid_group_name(group), "Group name not valid"
        assert self.valid_channel_name(channel), "Channel name not valid"
        group_key = self._group_key(group)
        connection = self.connection(self.consistent_hash(group))
        # Add to group sorted set with creation time as timestamp
        connection[block_tuple].zadd(
            group_key,
            **{channel: time.time()}
        )
        # Set both expiration to be group_expiry, since everything in
        # it at this point is guaranteed to expire before that
        connection[block_tuple].expire(group_key, self.group_expiry)
        self.semaphore.release()
  
    def group_discard(self, group, channel):
        """
        Removes the channel from the named group if it is in the group;
        does nothing otherwise (does not error)
        """
        assert self.valid_group_name(group), "Group name not valid"
        assert self.valid_channel_name(channel), "Channel name not valid"
        key = self._group_key(group)
        self.connection(self.consistent_hash(group)).zrem(
            key,
            channel,
        )

    def group_channels(self, group):
        self.semaphore.acquire()
        """
        Returns all channels in the group as an iterable.
        """
        key = self._group_key(group)
        connection = self.connection(self.consistent_hash(group))[block_tuple]
        # Discard old channels based on group_expiry
        connection.zremrangebyscore(key, 0, int(time.time()) - self.group_expiry)
        # Return current lot
        ret = [x.decode("utf8") for x in connection.zrange(
            key,
            0,
            -1,
        )]
        self.semaphore.release()
        return ret

    def send_group(self, group, message):
        """
        Sends a message to the entire group.
        """
        assert self.valid_group_name(group), "Group name not valid"
        # TODO: More efficient implementation (lua script per shard?)
        for channel in self.group_channels(group):
            try:
                self.send(channel, message)
            except self.ChannelFull:
                pass

    def _group_key(self, group):
        return ("%s:group:%s" % (self.prefix, group)).encode("utf8")

    ### Flush extension ###

    def flush(self):
        """
        Deletes all messages and groups on all shards.
        """
        for connection in self._connection_list:
            self.delprefix(keys=[], args=[self.prefix + "*"], client=connection)
            self.delprefix(keys=[], args=[self.stats_prefix + "*"], client=connection)

    ### statistics extension ###

    STAT_MESSAGES_COUNT = 'messages_count'
    STAT_MESSAGES_PENDING = 'messages_pending'
    STAT_MESSAGES_MAX_AGE = 'messages_max_age'
    STAT_CHANNEL_FULL = 'channel_full_count'

    def global_statistics(self):
        """
        Returns dictionary of statistics across all channels on all shards.
        Return value is a dictionary with following fields:
            * messages_count, the number of messages processed since server start
            * channel_full_count, the number of times ChannelFull exception has been risen since server start

        This implementation does not provide calculated per second values.
        Due perfomance concerns, does not provide aggregated messages_pending and messages_max_age,
        these are only avaliable per channel.

        """
        statistics = {
            self.STAT_MESSAGES_COUNT: 0,
            self.STAT_CHANNEL_FULL: 0,
        }
        prefix = self.stats_prefix + self.global_stats_key
        for connection in self._connection_list:
            messages_count, channel_full_count = connection.mget(
                ':'.join((prefix, self.STAT_MESSAGES_COUNT)),
                ':'.join((prefix, self.STAT_CHANNEL_FULL)),
            )
            statistics[self.STAT_MESSAGES_COUNT] += int(messages_count or 0)
            statistics[self.STAT_CHANNEL_FULL] += int(channel_full_count or 0)

        return statistics

    def channel_statistics(self, channel):
        """
        Returns dictionary of statistics for specified channel.
        Return value is a dictionary with following fields:
            * messages_count, the number of messages processed since server start
            * messages_pending, the current number of messages waiting
            * messages_max_age, how long the oldest message has been waiting, in seconds
            * channel_full_count, the number of times ChannelFull exception has been risen since server start

        This implementation does not provide calculated per second values
        """
        statistics = {
            self.STAT_MESSAGES_COUNT: 0,
            self.STAT_MESSAGES_PENDING: 0,
            self.STAT_MESSAGES_MAX_AGE: 0,
            self.STAT_CHANNEL_FULL: 0,
        }
        prefix = self.stats_prefix + channel

        if "!" in channel or "?" in channel:
            connections = [self.connection(self.consistent_hash(channel))]
        else:
            # if we don't know where it is, we have to check in all shards
            connections = self._connection_list

        channel_key = self.prefix + channel

        for connection in connections:
            messages_count, channel_full_count = connection.mget(
                ':'.join((prefix, self.STAT_MESSAGES_COUNT)),
                ':'.join((prefix, self.STAT_CHANNEL_FULL)),
            )
            statistics[self.STAT_MESSAGES_COUNT] += int(messages_count or 0)
            statistics[self.STAT_CHANNEL_FULL] += int(channel_full_count or 0)
            statistics[self.STAT_MESSAGES_PENDING] += connection.llen(channel_key)
            oldest_message = connection.lindex(channel_key, 0)
            if oldest_message:
                messages_age = self.expiry - connection.ttl(oldest_message)
                statistics[self.STAT_MESSAGES_MAX_AGE] = max(statistics[self.STAT_MESSAGES_MAX_AGE], messages_age)
        return statistics

    ### Serialization ###

    def serialize(self, message):
        """
        Serializes message to a byte string.
        """
        value = msgpack.packb(message, use_bin_type=True)
        if self.crypter:
            value = self.crypter.encrypt(value)
        return value

    def deserialize(self, message):
        """
        Deserializes from a byte string.
        """
        if self.crypter:
            message = self.crypter.decrypt(message, self.expiry + 10)
        return msgpack.unpackb(message, encoding="utf8")

    ### Redis Lua scripts ###

    # Single-command channel send. Returns error if over capacity.
    # Keys: message, channel_list
    # Args: content, expiry, capacity
    lua_chansend = """
        if redis.call('llen', KEYS[2]) >= tonumber(ARGV[3]) then
            return redis.error_reply("full")
        end
        redis.call('set', KEYS[1], ARGV[1])
        redis.call('expire', KEYS[1], ARGV[2])
        redis.call('rpush', KEYS[2], KEYS[1])
        redis.call('expire', KEYS[2], ARGV[2] + 1)
    """

    lua_lpopmany = """
        for keyCount = 1, #KEYS do
            local result = redis.call('LPOP', KEYS[keyCount])
            if result then
                return {KEYS[keyCount], result}
            end
        end
        return {nil, nil}
    """

    lua_delprefix = """
        local keys = redis.call('keys', ARGV[1])
        for i=1,#keys,5000 do
            redis.call('del', unpack(keys, i, math.min(i+4999, #keys)))
        end
    """

    ### Internal functions ###

    def consistent_hash(self, value):
        """
        Maps the value to a node value between 0 and 4095
        using CRC, then down to one of the ring nodes.
        """
        if isinstance(value, six.text_type):
            value = value.encode("utf8")
        bigval = binascii.crc32(value) & 0xfff
        ring_divisor = 4096 / float(self.ring_size)
        v = int(bigval / ring_divisor)
        #print v
        return v

    def random_index(self):
        return random.randint(0, len(self.hosts) - 1)

    def connection(self, index):
        """
        Returns the correct connection for the current thread.

        Pass key to use a server based on consistent hashing of the key value;
        pass None to use a random server instead.
        """
        # If index is explicitly None, pick a random server
        if index is None:
            index = self.random_index()
        # Catch bad indexes
        if not 0 <= index < self.ring_size:
            raise ValueError("There are only %s hosts - you asked for %s!" % (self.ring_size, index))
        return self._connection_list[index]


    def __str__(self):
        return "%s(hosts=%s)" % (self.__class__.__name__, self.hosts)