#!/usr/bin/env python
# Copyright 2014 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import datetime
import logging
import sys
import unittest

import test_env
test_env.setup_test_env()

from support import test_case

from components.ereporter2 import logscraper
from components.ereporter2 import testing


# Access to a protected member XXX of a client class - pylint: disable=W0212


class ErrorRecordStub(object):
  """Intentionally thin stub to test should_ignore_error_record()."""
  def __init__(self, message, exception_type):
    self.signature = None
    self.message = message
    self.exception_type = exception_type
    self.module = None
    self.resource = None
    self.version = None


class Ereporter2LogscraperTest(test_case.TestCase):
  def setUp(self):
    super(Ereporter2LogscraperTest, self).setUp()
    self._now = datetime.datetime(2014, 6, 24, 20, 19, 42, 653775)
    testing.mock_now(self, self._now, 0)

  def test_signatures(self):
    messages = [
      (
        ('\nTraceback (most recent call last):\n'
        '  File \"appengine/runtime/wsgi.py\", line 239, in Handle\n'
        '    handler = _config_handle.add_wsgi_middleware(self._LoadHandler())'
            '\n'
        '  File \"appengine/ext/ndb/utils.py\", line 28, in wrapping\n'
        '    def wrapping_wrapper(wrapper):\n'
        'DeadlineExceededError'),
        'DeadlineExceededError@utils.py:28',
        'DeadlineExceededError',
      ),
      (
        ('\'error\' is undefined\n'
        'Traceback (most recent call last):\n'
        '  File \"tp/webapp2-2.5/webapp2.py\", line 1535, in __call__\n'
        '    rv = self.handle_exception(request, response, e)\n'
        '  File \"tp/jinja2-2.6/jinja2/environment.py\", line 894, in render\n'
        '    return self.environment.handle_exception(exc_info, True)\n'
        '  File \"<template>\", line 6, in top-level template code\n'
        '  File \"tp/jinja2-2.6/jinja2/environment.py\", line 372, in getattr\n'
        '    return getattr(obj, attribute)\n'
        'UndefinedError: \'error\' is undefined'),
        'UndefinedError@environment.py:372',
        'UndefinedError',
      ),
      (
        ('\nTraceback (most recent call last):\n'
        '  File \"api.py\", line 74\n'
        '    class ErrorReportingInfo(ndb.Model):\n'
        '        ^\n'
        'SyntaxError: invalid syntax'),
        'SyntaxError@api.py:74',
        'SyntaxError',
      ),
    ]

    for (message, expected_signature, excepted_exception) in messages:
      signature, exception_type = logscraper._signature_from_message(message)
      self.assertEqual(expected_signature, signature)
      self.assertEqual(excepted_exception, exception_type)

  def test_capped_list(self):
    l = logscraper._CappedList(5, 10)

    # Grow a bit, should go to head.
    for i in xrange(5):
      l.append(i)
    self.assertFalse(l.has_gap)
    self.assertEqual(5, l.total_count)
    self.assertEqual(range(5), l.head)
    self.assertEqual(0, len(l.tail))

    # Start growing a tail, still not long enough to start evicting items.
    for i in xrange(5, 15):
      l.append(i)
    self.assertFalse(l.has_gap)
    self.assertEqual(15, l.total_count)
    self.assertEqual(range(5), l.head)
    self.assertEqual(range(5, 15), list(l.tail))

    # Adding one more item should evict oldest one ('5') from tail.
    l.append(15)
    self.assertTrue(l.has_gap)
    self.assertEqual(16, l.total_count)
    self.assertEqual(range(5), l.head)
    self.assertEqual(range(6, 16), list(l.tail))


if __name__ == '__main__':
  if '-v' in sys.argv:
    unittest.TestCase.maxDiff = None
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.ERROR)
  unittest.main()