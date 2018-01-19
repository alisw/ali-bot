#!/usr/bin/env python
import sys

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
