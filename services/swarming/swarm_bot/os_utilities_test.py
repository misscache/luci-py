#!/usr/bin/env python
# Copyright 2014 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import logging
import os
import re
import subprocess
import sys
import time
import unittest

# Import os_utilities first before manipulating sys.path to ensure it can load
# fine.
import os_utilities

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import test_env

test_env.setup_test_env()

from depot_tools import auto_stub

VERBOSE = '-v' in sys.argv


# Access to a protected member _XXX of a client class
# pylint: disable=W0212


class TestOsUtilitiesPrivate(auto_stub.TestCase):
  def setUp(self):
    super(TestOsUtilitiesPrivate, self).setUp()
    if not VERBOSE:
      self.mock(logging, 'error', lambda *_: None)

  def test_from_cygwin_path(self):
    data = [
      ('foo', None),
      ('x:\\foo$', None),
      ('X:\\foo$', None),
      ('/cygdrive/x/foo$', 'x:\\foo$'),
    ]
    for i, (inputs, expected) in enumerate(data):
      actual = os_utilities._from_cygwin_path(inputs)
      self.assertEqual(expected, actual, (inputs, expected, actual, i))

  def test_to_cygwin_path(self):
    data = [
      ('foo', None),
      ('x:\\foo$', '/cygdrive/x/foo$'),
      ('X:\\foo$', '/cygdrive/x/foo$'),
      ('/cygdrive/x/foo$', None),
    ]
    for i, (inputs, expected) in enumerate(data):
      actual = os_utilities._to_cygwin_path(inputs)
      self.assertEqual(expected, actual, (inputs, expected, actual, i))


class TestOsUtilities(auto_stub.TestCase):
  def test_get_os_version(self):
    version = os_utilities.get_os_version()
    self.assertTrue(version)
    self.assertTrue(re.match(r'^\d+\.\d+$', version), version)

  def test_get_os_name(self):
    expected = ('Linux', 'Mac', 'Windows')
    self.assertIn(os_utilities.get_os_name(), expected)

  def test_get_cpu_type(self):
    expected = ('arm', 'x86')
    self.assertIn(os_utilities.get_cpu_type(), expected)

  def test_get_cpu_bitness(self):
    expected = ('32', '64')
    self.assertIn(os_utilities.get_cpu_bitness(), expected)

  def test_get_ip(self):
    ip = os_utilities.get_ip()
    self.assertNotEqual('127.0.0.1', ip)
    ipv4 = r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$'
    ipv6 = r'^%s$' % ':'.join([r'[0-9a-f]{1,4}'] * 8)
    self.assertTrue(re.match(ipv4, ip) or re.match(ipv6, ip), ip)

  def test_get_num_processors(self):
    self.assertGreater(os_utilities.get_num_processors(), 0)

  def test_get_physical_ram(self):
    self.assertGreater(os_utilities.get_physical_ram(), 0)

  def test_get_free_disk(self):
    self.assertGreater(os_utilities.get_free_disk(), 0)

  def test_get_gpu(self):
    self.assertTrue(os_utilities.get_gpu())

  def test_get_integrity_level_win(self):
    if sys.platform == 'win32':
      self.assertIsInstance(os_utilities.get_integrity_level_win(), basestring)
    else:
      self.assertIs(os_utilities.get_integrity_level_win(), None)

  def test_get_attributes(self):
    actual = os_utilities.get_attributes('id')
    expected = set(['dimensions', 'id', 'ip'])
    self.assertEqual(expected, set(actual))

    expected_dimensions = set(
        ['cores', 'cpu', 'disk', 'gpu', 'hostname', 'id', 'os', 'ram'])
    if sys.platform in ('cygwin', 'win32'):
      expected_dimensions.add('cygwin')
    if sys.platform == 'win32':
      expected_dimensions.add('integrity')
    self.assertEqual(expected_dimensions, set(actual['dimensions']))

  def test_get_attributes_none(self):
    actual = os_utilities.get_attributes(None)
    expected = set(['dimensions', 'id', 'ip'])
    self.assertEqual(expected, set(actual))

  def test_get_adb_list_devices(self):
    stdout = (
      '* daemon not running. starting it now on port 5037 *\n'
      '* daemon started successfully *\n'
      'List of devices attached\n'
      'A12B304C5DE     device\n'
      'emulator-1234   device\n'
      '\n')
    self.mock(subprocess, 'check_output', lambda *_: stdout)
    actual = os_utilities.get_adb_list_devices()
    self.assertEqual(['A12B304C5DE', 'emulator-1234'], actual)

  def test_get_adb_device_properties_raw(self):
    stdout = (
      '# begin build properties\n'
      '# autogenerated by buildinfo.sh\n'
      'ro.build.id=KRT16S\n'
      'ro.build.display.id=KRT16S\n'
      'ro.build.version.incremental=920375\n'
      '\n')
    self.mock(subprocess, 'check_output', lambda *_: stdout)
    actual = os_utilities.get_adb_device_properties_raw('123')
    expected = {
      'ro.build.display.id': 'KRT16S',
      'ro.build.id': 'KRT16S',
      'ro.build.version.incremental': '920375',
    }
    self.assertEqual(expected, actual)

  def test_get_dimensions_android(self):
    props = dict((k, k) for k in os_utilities.ANDROID_DETAILS)
    props['bar'] = 'foo'
    self.mock(os_utilities, 'get_adb_device_properties_raw', lambda *_: props)
    actual = os_utilities.get_dimensions_android('123')
    expected = {
      'ro.board.platform': 'ro.board.platform',
      'ro.build.id': 'ro.build.id',
      'ro.build.tags': 'ro.build.tags',
      'ro.build.type': 'ro.build.type',
      'ro.build.version.sdk': 'ro.build.version.sdk',
      'ro.product.board': 'ro.product.board',
      'ro.product.cpu.abi': 'ro.product.cpu.abi',
      'ro.product.cpu.abi2': 'ro.product.cpu.abi2',
    }
    self.assertEqual(expected, actual)

  def test_get_attributes_android(self):
    expected_dimensions = dict((k, k) for k in os_utilities.ANDROID_DETAILS)
    props = expected_dimensions.copy()
    props['bar'] = 'foo'
    self.mock(os_utilities, 'get_adb_device_properties_raw', lambda *_: props)

    expected_dimensions['id'] = '123'
    actual = os_utilities.get_attributes_android('123')
    expected = {
      'dimensions': expected_dimensions,
      'id': '123',
      'ip': os_utilities.get_ip(),
    }
    self.assertEqual(expected, actual)

  def test_setup_auto_startup_win(self):
    # TODO(maruel): Figure out a way to test properly.
    pass

  def test_setup_auto_startup_osx(self):
    # TODO(maruel): Figure out a way to test properly.
    pass

  def test_restart(self):
    class Foo(Exception):
      pass

    def raise_exception(x):
      raise x

    self.mock(subprocess, 'check_call', lambda _: None)
    self.mock(time, 'sleep', lambda _: raise_exception(Foo()))
    self.mock(logging, 'error', lambda *_: None)
    with self.assertRaises(Foo):
      os_utilities.restart()

  def test_restart_and_return(self):
    self.mock(subprocess, 'check_call', lambda _: None)
    self.assertIs(True, os_utilities.restart_and_return())


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.ERROR)
  unittest.main()
