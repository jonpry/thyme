#!/usr/bin/env python
import os
import sys

import gevent.monkey
gevent.monkey.patch_all()

from psycogreen.gevent import patch_psycopg

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "multichat.settings")
    patch_psycopg()

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
