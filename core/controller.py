# Copyright (C) 2009-2010 Raul Jimenez
# Released under GNU LGPL 2.1
# See LICENSE.txt for more information

"""
The controller module is designed to be the central point where most modules
are connected. This module delegates most of the implementation details to
other modules. This delegation model creates separated responsibility areas
where implementation can be changed in isolation.

The extreme cases are the plug-ins which allow us to develop/run different
implementations of routing and lookup managers in parallel.

"""

import sys
import ptime as time
import datetime
import os
import cPickle

import logging, logging_conf

import identifier
from identifier import Id
import message
from querier import Querier
from message import QUERY, RESPONSE, ERROR
from node import Node
import responder
import bootstrap
#import pkgutil

#from profilestats import profile

logger = logging.getLogger('dht')

CACHE_VALID_PERIOD = 5 * 60 # 5 minutes


class Controller:

    def __init__(self, version_label,
                 my_node, conf_path,
                 routing_m_mod, lookup_m_mod,
                 experimental_m_mod,
                 private_dht_name,
                 bootstrap_mode):
        self.bootstrapper = bootstrap.OverlayBootstrapper(conf_path)
        my_addr = my_node.addr
        self._my_id = my_node.id # id indicated by user 
        if not self._my_id:
            self._my_id = self._my_id = identifier.RandomId() # random id
        self._my_node = Node(my_addr, self._my_id, version=version_label)
        self.msg_f = message.MsgFactory(version_label, self._my_id,
                                        private_dht_name)
        self._querier = Querier()
        self._routing_m = routing_m_mod.RoutingManager(
            self._my_node, self.msg_f, self.bootstrapper)

        self._responder = responder.Responder(self._my_id, self._routing_m,
                                              self.msg_f, bootstrap_mode)
        self._tracker = self._responder._tracker
        
        self._lookup_m = lookup_m_mod.LookupManager(self._my_id, self.msg_f,
                                                    self.bootstrapper)
        self._experimental_m = experimental_m_mod.ExperimentalManager(
            self._my_node.id, self.msg_f) 
                  
        current_ts = time.time()
        self._next_maintenance_ts = current_ts
        self._next_timeout_ts = current_ts
        self._next_main_loop_call_ts = current_ts
        self._cached_lookups = []
           
    def on_stop(self):
        self._experimental_m.on_stop()
        self.bootstrapper.save_to_file()

    def get_peers(self, lookup_id, info_hash, callback_f, bt_port, use_cache):
        """
        Start a get\_peers lookup whose target is 'info\_hash'. The handler
        'callback\_f' will be called with three arguments ('lookup\_id',
        'peers', 'node') whenever peers are discovered. Once the lookup is
        completed, the handler will be called with arguments:
        ('lookup\_id', None, None).

        This method is called by minitwisted, using the minitwisted thread.

        """
        datagrams_to_send = []
        logger.debug('get_peers %d %r' % (bt_port, info_hash))
        if use_cache:
            peers = self._get_cached_peers(info_hash)
            if peers and callback_f and callable(callback_f):
                callback_f(lookup_id, peers, None)
                callback_f(lookup_id, None, None)
                return datagrams_to_send
        lookup_obj = self._lookup_m.get_peers(lookup_id,
                                              info_hash,
                                              callback_f,
                                              bt_port)
        queries_to_send = []
        distance = lookup_obj.info_hash.distance(self._my_id)
        bootstrap_rnodes = self._routing_m.get_closest_rnodes(
            distance.log, 0, True) #TODO: get the full bucket
        # look if I'm tracking this info_hash
        peers = self._tracker.get(lookup_obj.info_hash)
        callback_f = lookup_obj.callback_f
        if peers:
            self._add_cache_peers(lookup_obj.info_hash, peers)
            if callback_f and callable(callback_f):
                callback_f(lookup_obj.lookup_id, peers, None)
        # do the lookup
        # NOTE: if bootstrap_rnodes is empty, a OVERLAY BOOTSTRAP will be
        # done.
        queries_to_send = lookup_obj.start(bootstrap_rnodes, self.bootstrapper)

        datagrams_to_send = self._register_queries(queries_to_send)
        return datagrams_to_send
    
    def _get_cached_peers(self, info_hash):
        oldest_valid_ts = time.time() - CACHE_VALID_PERIOD
        for ts, cached_info_hash, peers in self._cached_lookups:
            if ts > oldest_valid_ts and info_hash == cached_info_hash:
                return peers

    def _add_cache_peers(self, info_hash, peers):
        oldest_valid_ts = time.time() - CACHE_VALID_PERIOD
        while self._cached_lookups and self._cached_lookups[0][0] < oldest_valid_ts:
            # clean up old entries
            del self._cached_lookups[0]
        if self._cached_lookups and self._cached_lookups[-1][1] == info_hash:
            self._cached_lookups[-1][2].extend(peers)
        else:
            self._cached_lookups.append((time.time(), info_hash, peers))

    def print_routing_table_stats(self):
        self._routing_m.print_stats()

    def print_routing_table(self):
        self._routing_m.print_table()

    def main_loop(self):
        """
        Perform maintenance operations. The main operation is routing table
        maintenance where staled nodes are added/probed/replaced/removed as
        needed. The routing management module specifies the implementation
        details.  This includes keeping track of queries that have not been
        responded for a long time (timeout) with the help of
        querier.Querier. The routing manager and the lookup manager will be
        informed of those timeouts.

        This method is designed to be used as minitwisted's heartbeat handler.

        """

        queries_to_send = []
        current_ts = time.time()
        #TODO: I think this if should be removed
        # At most, 1 second between calls to main_loop after the first call
        if current_ts >= self._next_main_loop_call_ts:
            self._next_main_loop_call_ts = current_ts + 1
        else:
            # It's too early
            return self._next_main_loop_call_ts, []
        
        # Take care of timeouts
        if current_ts >= self._next_timeout_ts:
            (self._next_timeout_ts,
             timeout_queries) = self._querier.get_timeout_queries()
            for query in timeout_queries:
                queries_to_send.extend(self._on_timeout(query))

        # Routing table maintenance
        if time.time() >= self._next_maintenance_ts:
            (maintenance_delay,
             queries,
             maintenance_lookup) = self._routing_m.do_maintenance()
            self._next_maintenance_ts = current_ts + maintenance_delay
            self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                               self._next_maintenance_ts)
            queries_to_send.extend(queries)
            if maintenance_lookup:
                target, rnodes = maintenance_lookup
                lookup_obj = self._lookup_m.maintenance_lookup(target)
                queries_to_send.extend(lookup_obj.start(rnodes))

        # Return control to reactor
        datagrams_to_send = self._register_queries(queries_to_send)
        return self._next_main_loop_call_ts, datagrams_to_send

    def _maintenance_lookup(self, target):
        self._lookup_m.maintenance_lookup(target)

    def on_datagram_received(self, datagram):
        """
        Perform the actions associated to the arrival of the given
        datagram. The datagram will be ignored in cases such as invalid
        format. Otherwise, the datagram will be decoded and different modules
        will be informed to take action on it. For instance, if the datagram
        contains a response to a lookup query, both routing and lookup manager
        will be informed. Additionally, if that response contains peers, the
        lookup's handler will be called (see get\_peers above).
        This method is designed to be used as minitwisted's networking handler.

        """
        exp_queries_to_send = []
        
        data = datagram.data
        addr = datagram.addr
        datagrams_to_send = []
        try:
            msg = self.msg_f.incoming_msg(datagram)
            
        except(message.MsgError):
            # ignore message
            return self._next_main_loop_call_ts, datagrams_to_send

        if msg.type == message.QUERY:
           
            if msg.src_node.id == self._my_id:
                logger.debug('Got a msg from myself:\n%r', msg)
                return self._next_main_loop_call_ts, datagrams_to_send
            #zinat: inform experimental_module
            exp_queries_to_send = self._experimental_m.on_query_received(msg)
            
            response_msg = self._responder.get_response(msg)
            if response_msg:
                bencoded_response = response_msg.stamp(msg.tid)
                datagrams_to_send.append(
                    message.Datagram(bencoded_response, addr))
            maintenance_queries_to_send = self._routing_m.on_query_received(
                msg.src_node)
            
        elif msg.type == message.RESPONSE:
            related_query = self._querier.get_related_query(msg)
            if not related_query:
                # Query timed out or unrequested response
                return self._next_main_loop_call_ts, datagrams_to_send
            ## zinat: if related_query.experimental_obj:
            exp_queries_to_send = self._experimental_m.on_response_received(
                                                        msg, related_query)
            #TODO: you need to get datagrams to be able to send messages (raul)
            # lookup related tasks
            if related_query.lookup_obj:
                (lookup_queries_to_send,
                 peers,
                 num_parallel_queries,
                 lookup_done
                 ) = related_query.lookup_obj.on_response_received(
                    msg, msg.src_node)
                datagrams = self._register_queries(lookup_queries_to_send)
                datagrams_to_send.extend(datagrams)

                lookup_obj = related_query.lookup_obj
                lookup_id = lookup_obj.lookup_id
                callback_f = lookup_obj.callback_f
                if peers:
                    self._add_cache_peers(lookup_obj.info_hash, peers)
                    if callback_f and callable(callback_f):
                        callback_f(lookup_id, peers, msg.src_node)
                if lookup_done:
                    if callback_f and callable(callback_f):
                        callback_f(lookup_id, None, msg.src_node)
                    queries_to_send = self._announce(
                        related_query.lookup_obj)
                    datagrams = self._register_queries(
                        queries_to_send)
                    datagrams_to_send.extend(datagrams)
                    
            # maintenance related tasks
            maintenance_queries_to_send = \
                self._routing_m.on_response_received(
                msg.src_node, related_query.rtt, msg.all_nodes)

        elif msg.type == message.ERROR:
            related_query = self._querier.get_related_query(msg)
            if not related_query:
                # Query timed out or unrequested response
                return self._next_main_loop_call_ts, datagrams_to_send
            #TODO: zinat: same as response
            exp_queries_to_send = self._experimental_m.on_error_received(
                msg, related_query)
            # lookup related tasks
            if related_query.lookup_obj:
                peers = None # an error msg doesn't have peers
                (lookup_queries_to_send,
                 num_parallel_queries,
                 lookup_done
                 ) = related_query.lookup_obj.on_error_received(msg, addr)
                datagrams = self._register_queries(lookup_queries_to_send)
                datagrams_to_send.extend(datagrams)

                callback_f = related_query.lookup_obj.callback_f
                if callback_f and callable(callback_f):
                    lookup_id = related_query.lookup_obj.lookup_id
                    if lookup_done:
                        callback_f(lookup_id, None, msg.src_node)
                if lookup_done:
                    datagrams = self._announce(related_query.lookup_obj)
                    datagrams_to_send.extend(datagrams)
            # maintenance related tasks
            maintenance_queries_to_send = \
                self._routing_m.on_error_received(addr)

        else: # unknown type
            return self._next_main_loop_call_ts, datagrams_to_send
        # we are done with the plugins
        # now we have maintenance_queries_to_send, let's send them!
        datagrams = self._register_queries(maintenance_queries_to_send)
        datagrams_to_send.extend(datagrams)
        if exp_queries_to_send:
            datagrams = self._register_queries(exp_queries_to_send)
            datagrams_to_send.extend(datagrams)
        return self._next_main_loop_call_ts, datagrams_to_send

    def _on_query_received(self):
        return
    def _on_response_received(self):
        return
    def _on_error_received(self):
        return

    def _on_timeout(self, related_query):
        queries_to_send = []
        #TODO: on_timeout should return queries (raul)
        exp_queries_to_send = self._experimental_m.on_timeout(related_query)
        if related_query.lookup_obj:
            (lookup_queries_to_send,
             num_parallel_queries,
             lookup_done
             ) = related_query.lookup_obj.on_timeout(related_query.dst_node)
            queries_to_send.extend(lookup_queries_to_send)
            callback_f = related_query.lookup_obj.callback_f
            if lookup_done:
                lookup_id = related_query.lookup_obj.lookup_id
                if callback_f and callable(callback_f):
                    related_query.lookup_obj.callback_f(lookup_id, None, None)
                queries_to_send.extend(self._announce(
                        related_query.lookup_obj))
        maintenance_queries_to_send = self._routing_m.on_timeout(
            related_query.dst_node)
        if maintenance_queries_to_send:
            queries_to_send.extend(maintenance_queries_to_send)
        if exp_queries_to_send:
            datagrams = self._register_queries(exp_queries_to_send)
            datagrams_to_send.extend(datagrams)
        return queries_to_send

    def _announce(self, lookup_obj):
        queries_to_send, announce_to_myself = lookup_obj.announce()
        return queries_to_send
    '''
    if announce_to_myself:
    self._tracker.put(lookup_obj._info_hash,
    (self._my_node.addr[0], lookup_obj._bt_port))
    '''
    
    def _register_queries(self, queries_to_send, lookup_obj=None):
        if not queries_to_send:
            return []
        timeout_call_ts, datagrams_to_send = self._querier.register_queries(
            queries_to_send)
        self._next_main_loop_call_ts = min(self._next_main_loop_call_ts,
                                           timeout_call_ts)
        return datagrams_to_send
    
