#!/usr/bin/env python

import shlex
import sys
from os import devnull
import subprocess


def parse_env_file(env_file_path):
    '''Parse variable assignments from a .env file.'''
    with open(env_file_path) as envf:
        for token in shlex.split(envf.read(), comments=False):
            var, is_assignment, value = token.partition('=')
            if is_assignment:
                yield (var, value)


def to_unicode(s):
    if sys.version_info[0] >= 3:
        if isinstance(s, bytes):
            return s.decode("utf-8")  # to get newlines as such and not as escaped \n
        return str(s)
    elif isinstance(s, str):
        return unicode(s, "utf-8")  # utf-8 is a safe assumption
    elif not isinstance(s, unicode):
        return unicode(str(s))
    return s


def getout(cmd):
    with open(devnull) as dn:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=dn, shell=True)
        out = p.communicate()[0].decode('utf-8')
        code = p.returncode
    return (out, code)
