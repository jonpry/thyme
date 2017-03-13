Gredis
=========
The application part of this project is a nearly unmodified copy of Andrew Godwin's 
MultiChat example from the channel-examples project. The "Thyme" server is a heavily
modified version of django's Daphne and some code has been imported from the 
Django Channels project itself. 

The point of this project is to demonstrate the feasibility of implementing channels 
under gevent. This allows for some unique use cases. Psycogreen has been integrated
into the project to demonstrate use of async greenlets within the context of channels. 

This example project uses something similar to the Redis channel layer but with some
performance enhancements. It is also worth noting that all of the hot idle issues
with daphne etc have been resolved. No timeouts or sleeps are needed with this design. 



MultiChat
=========

Basic example of a multi-room chatroom, with messages from all rooms a user
is in multiplexed over a single WebSocket connection.

There is no chat persistence; you only see messages sent to a room while you
are in that room.

Uses the Django auth system to provide user accounts; users are only able to
use the chat once logged in, and this provides their username details for the
chatroom.

Some channels can be limited to only "staff" users; the example includes
code that checks user credentials on incoming WebSockets to allow or deny them
access to chatroom streams based on their staff status.


Installation
------------

Manual installation
~~~~~~~~~~~~~~~~~~~~~~

Make a new virtualenv for the project, and run::

    pip install -r requirements.txt

Then, you'll need Redis running locally; the settings are configured to
point to ``localhost``, port ``6379``, but you can change this in the
``CHANNEL_LAYERS`` setting in ``settings.py``.

You will need postgresql installed. then:

    sudo postgres
    creatuser -P multichat
        answer the questions and set the password to 'multichat'
    createdb multichat -O multichat

Finally, run::

    python manage.py migrate
    python manage.py runworker2 &
    python thyme.py multichat.asgi:channel_layer 


Usage
-----

Make yourself a superuser account::

    python manage.py createsuperuser

Then, log into http://localhost:8000/admin/ and make a couple of Room objects.
Be sure to make one that is set to "staff-only",

Finally, make a second user account in the admin that doesn't have staff
privileges. You'll use this to log into the chat in a second window, and to test
the authentication on that staff-only room.

Now, open a second window in another browser or in "incognito" mode - you'll be
logging in to the same site with two user accounts. Navigate to
http://localhost:8000 in both browsers and open the same chatroom.

Now, you can type messages and see them appear on both screens at once. You can
join other rooms and try there, and see how you receive messages from all rooms
you've currently joined.

If you try and make the non-staff user join your staff-only chatroom, you should
see an error as the server-side authentication code kicks in.



Further Notes
---------------

The Gredis layer and the stock Redis layer have very similar physical transport. 
Imho Redis layer does some pathological stuff where you need to either open a 
a new connection to Redis for every websocket, or you end up needing to upload
very large LPOP queries for every event received. Instead I used a more complex
naming system so it is possible to route through the transport to the right 
destination without the overhead. 

Assuming that the stock channels code was updated to use this same protocol. 
Then it should be possible to use any combination of GEvent/Non-GEvent server 
and GEvent/Non-GEvent application that one desires. This however would be 
difficult with the current channels implementation as it assumes that
a channel layer driver and a transport are essentially the same thing. 

When you get right to it. This entire system is nothing more than a crossbar. 
It is unclear to me why Redis is such a great thing compared to like TCP sockets
for example. 


