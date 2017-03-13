from __future__ import unicode_literals
import logging
import warnings
import sys
import time
import requests
import gevent
import json
from gevent import pywsgi
from gevent.event import Event
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket.websocket import WebSocket

from channels import DEFAULT_CHANNEL_LAYER, channel_layers


logger = logging.getLogger(__name__)
reply_channels = {}

def to_status(code):
   return str(str(code) + " " + requests.status_codes._codes[code][0].upper())

def ws_listen(handler):
   handler.listen()

class WSHandler(object):
   def __init__(self, environ, start_response):
        global reply_channels

        self.ws = environ['wsgi.websocket']

        # Make a name for our reply channel
        self.reply_channel = default_channel_layer.new_channel(u"websocket.send." + default_channel_layer.client_prefix + "!")
        self.last_keepalive = time.time()

        self.data = []
        self.ev = Event()
        self.rc_name = self.reply_channel.split("!")[1]
        reply_channels[self.rc_name] = self;

        clean_headers = []
        for k,v in environ.iteritems():
           if(k.startswith("HTTP_")):
              clean_headers.append([k[5:].lower(),v])
           elif k.lower().startswith("content"):
              clean_headers.append([k.lower().replace("_","-"),v])

        self.packets_received = 0

        self.request_info = {
             "path": environ['PATH_INFO'],
             "headers": clean_headers,
             "query_string": environ['QUERY_STRING'],
                        "client": environ['REMOTE_ADDR'],
                        "server": environ['SERVER_NAME'],
             "reply_channel": self.reply_channel,
             "order": self.packets_received,
        }

        default_channel_layer.send("websocket.connect", self.request_info)

        gevent.spawn(ws_listen, self)

        while(True):
           self.ev.wait()
           self.ev.clear()
           while len(self.data) > 0:
              d = self.data.pop(0)
              #print d
              if d == None:
                 break #TODO: notify about disconnect
              if 'conn_key' in d:
                 del d[u'conn_key']
                 if 'text' in d:
                    try:
                       self.ws.send_frame(d['text'],WebSocket.OPCODE_TEXT)
                    except:
                       break
              else:
                 self.packets_received += 1

                 self.request_info = {
                    "path": environ['PATH_INFO'],
                    "headers": clean_headers,
                    "query_string": environ['QUERY_STRING'],
                    "client": environ['REMOTE_ADDR'],
                    "server": environ['SERVER_NAME'],
                    "reply_channel": self.reply_channel,
                    "order": self.packets_received,
                    "text" : d
                 }

                 default_channel_layer.send("websocket.receive", self.request_info)

        self.packets_received += 1
        self.request_info = {
             "path": environ['PATH_INFO'],
             "headers": clean_headers,
             "query_string": environ['QUERY_STRING'],
                        "client": environ['REMOTE_ADDR'],
                        "server": environ['SERVER_NAME'],
             "reply_channel": self.reply_channel,
             "order": self.packets_received,
        }
        default_channel_layer.send("websocket.disconnect", self.request_info)

   def listen(self):
      while True:
        v = None
        try:
           v = self.ws.read_message()
           if v==None:
              continue
        except: 
           return
        self.data.append(v)  
        self.ev.set()

   def notify(self,content):
      self.data.append(content)  
      self.ev.set()

   def clean_up(self):
      del reply_channels[self.rc_name]


class HttpHandler(object):
   def __init__(self, environ, start_response):
        global reply_channels

        # Make a name for our reply channel
        self.reply_channel = default_channel_layer.new_channel(u"http.response." + default_channel_layer.client_prefix + "!")
        self.last_keepalive = time.time()
        #self.factory.reply_protocols[self.reply_channel] = self

        clean_headers = []
        for k,v in environ.iteritems():
           if(k.startswith("HTTP_")):
              clean_headers.append([k[5:].lower(),v])
           elif k.lower().startswith("content"):
              clean_headers.append([k.lower().replace("_","-"),v])

        self.data = []
        self.ev = Event()
        self.rc_name = self.reply_channel.split("!")[1]
        reply_channels[self.rc_name] = self;


        body = environ['wsgi.input'].read()
        #print clean_headers
        default_channel_layer.send("http.request", {
                        "reply_channel": self.reply_channel,
                        # TODO: Correctly say if it's 1.1 or 1.0
                        "http_version": "1.1",
                        "method": environ['REQUEST_METHOD'],
                        "path": environ['PATH_INFO'],
                        "root_path": environ['SCRIPT_NAME'],
                        "scheme": "http",
                        "query_string": environ['QUERY_STRING'],
                        "headers": clean_headers,
                        "body": body,
                        "client": environ['REMOTE_ADDR'],
                        "server": environ['SERVER_NAME'],
        })
 

        self.response = []
        while(True):
           self.ev.wait()
           self.ev.clear()            
           while len(self.data) > 0:
              d = self.data.pop(0)
              v = d['more_content']
              #print v
              if 'status' in d:
                 start_response(to_status(d['status']), d['headers'])

              if 'content' in d:
                 self.response.append(d['content'])

              if v == False:
                 #print d
                 return

   def notify(self,content):
      #print "inNotify"
      #print content
      self.data.append(content)  
      self.ev.set()

   def clean_up(self):
      del reply_channels[self.rc_name]

def app ( environ, start_response ):
    if 'wsgi.websocket' in environ:
       handler = WSHandler(environ, start_response)
    else:
       handler = HttpHandler(environ, start_response)
    handler.clean_up()

    #if len(handler.response) != 1:
    #    print handler.response

    return handler.response

class Server(object):

    def __init__(
        self,
        channel_layer,
        host=None,
        port=None,
        endpoints=None,
        unix_socket=None,
        file_descriptor=None,
        signal_handlers=True,
        action_logger=None,
        http_timeout=120,
        websocket_timeout=None,
        ping_interval=20,
        ping_timeout=30,
        ws_protocols=None,
        root_path="",
        proxy_forwarded_address_header=None,
        proxy_forwarded_port_header=None,
        verbosity=1
    ):
        global default_channel_layer
        self.channel_layer = channel_layer
        g_channel_layer = channel_layer

        self.endpoints = endpoints or []

        if any([host, port, unix_socket, file_descriptor]):
            warnings.warn('''
                The host/port/unix_socket/file_descriptor keyword arguments to %s are deprecated.
            ''' % self.__class__.__name__, DeprecationWarning)
            # build endpoint description strings from deprecated kwargs
            self.endpoints = sorted(self.endpoints + build_endpoint_description_strings(
                host=host,
                port=port,
                unix_socket=unix_socket,
                file_descriptor=file_descriptor
            ))

        if len(self.endpoints) == 0:
            raise UserWarning("No endpoints. This server will not listen on anything.")

        self.signal_handlers = signal_handlers
        self.action_logger = action_logger
        self.http_timeout = http_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.proxy_forwarded_address_header = proxy_forwarded_address_header
        self.proxy_forwarded_port_header = proxy_forwarded_port_header
        # If they did not provide a websocket timeout, default it to the
        # channel layer's group_expiry value if present, or one day if not.
        self.websocket_timeout = websocket_timeout or getattr(channel_layer, "group_expiry", 86400)
        self.ws_protocols = ws_protocols
        self.root_path = root_path
        self.verbosity = verbosity
        self.termed = False

        default_channel_layer = channel_layers[DEFAULT_CHANNEL_LAYER]

        self.channels = default_channel_layer.router.channels
        self.channels = []
        self.http_prefix = u"http.response." + default_channel_layer.client_prefix
        self.channels.append(self.http_prefix)
        self.websocket_prefix = u"websocket.send." + default_channel_layer.client_prefix
        self.channels.append(self.websocket_prefix)

        print self.channels

    def run(self):
        """
        self.factory = HTTPFactory(
            self.channel_layer,
            self.action_logger,
            timeout=self.http_timeout,
            websocket_timeout=self.websocket_timeout,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            ws_protocols=self.ws_protocols,
            root_path=self.root_path,
            proxy_forwarded_address_header=self.proxy_forwarded_address_header,
            proxy_forwarded_port_header=self.proxy_forwarded_port_header
        )
        """
        global reply_channels
     
        logger.info("Using gevent asynchronous mode on channel layer")

        for socket_description in self.endpoints:
            logger.info("Listening on endpoint %s" % socket_description)
            tokens = socket_description.split(":")
            for t in tokens:
              if(t.startswith("port")):
                 port = int(t.split("=")[1])
              if(t.startswith("interface")):
                 interface = t.split("=")[1]
            # Twisted requires str on python2 (not unicode) and str on python3 (not bytes)
            ep = pywsgi.WSGIServer((interface, port), app, handler_class=WebSocketHandler)
            ep.start()
            
        try:
            while not ep._stop_event.is_set():
               self.in_job = False
               #print self.channels
               channel, content = self.channel_layer.receive(self.channels, block=True)
               if channel == None:
                  continue
               if channel == self.http_prefix or channel == self.websocket_prefix:
                  conn_key = content['conn_key']
                  #print conn_key 
                  #print channel
                  #print content
                  if reply_channels[conn_key] != None:
                     reply_channels[conn_key].notify(content)
                  else:
                     print "key not in dict!!!!"

        except KeyboardInterrupt:
            sys.exit(0)

    def timeout_checker(self):
        """
        Called periodically to enforce timeout rules on all connections.
        Also checks pings at the same time.
        """
        self.factory.check_timeouts()
        reactor.callLater(2, self.timeout_checker)


def build_endpoint_description_strings(
    host=None,
    port=None,
    unix_socket=None,
    file_descriptor=None
    ):
    """
    Build a list of twisted endpoint description strings that the server will listen on.
    This is to streamline the generation of twisted endpoint description strings from easier
    to use command line args such as host, port, unix sockets etc.
    """
    socket_descriptions = []
    if host and port:
        host = host.strip('[]').replace(':', '\:')
        socket_descriptions.append('tcp:port=%d:interface=%s' % (int(port), host))
    elif any([host, port]):
        raise ValueError('TCP binding requires both port and host kwargs.')

    if unix_socket:
        socket_descriptions.append('unix:%s' % unix_socket)

    if file_descriptor:
        socket_descriptions.append('fd:fileno=%d' % int(file_descriptor))

    return socket_descriptions
