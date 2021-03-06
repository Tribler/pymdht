import logging

import core.message as message
from core.node import Node
import core.ptime as time
import pickle
import sys

logger = logging.getLogger('dht')

STATUS_PINGED = 'PINGED'
STATUS_OK = 'OK'
STATUS_FAIL = 'FAIL'

class ExperimentalManager:

    def __init__(self, my_id):
        self.my_id = my_id
        self._stop = False
        #TODO data structure to keep track of things
        self.pinged_ips = {}
        # this dict contains ip and status ................ #TODO
        self.num_ok = 0
        self.num_fail = 0
        pass


    def on_query_received(self, msg):
        if msg.query =='get_peers':
            logger.debug("%s", int(time.time()))

    def on_response_received(self, msg, related_query):
        pass

    def on_timeout(self, related_query):
        if related_query.experimental_obj:
            elapsed_time = time.time() - related_query.experimental_obj.query_ts
            logger.debug('prove FAILED Due to Time-Out %s', related_query.experimental_obj.value)
            logger.debug('RTT = %s', elapsed_time)
            self.pinged_ips[related_query.dst_node.ip] = STATUS_FAIL


    def on_stop(self):
       pass

class ExpObj:
    def __init__(self, value):
        self.value = value
        self.query_ts = time.time()
        logger.debug('Got query at Time: %s', self.query_ts)
        pass




