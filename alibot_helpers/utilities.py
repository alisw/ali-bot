#!/usr/bin/env python

import shlex
import sys


def parse_env_file(env_file_path):
    '''Parse variable assignments from a .env file.'''
    with open(env_file_path) as envf:
        for token in shlex.split(envf.read(), comments=False):
            var, is_assignment, value = token.partition('=')
            if is_assignment:
                yield (var, value)


def to_unicode(s):
    if isinstance(s, bytes):
        return s.decode("utf-8")  # to get newlines as such and not as escaped \n
    return str(s)
