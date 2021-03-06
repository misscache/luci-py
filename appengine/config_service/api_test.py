#!/usr/bin/env python
# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import base64
import copy
import httplib

from test_env import future
import test_env
test_env.setup_test_env()

import endpoints

from components import auth
from components import utils
from test_support import test_case
import mock

from proto import project_config_pb2
from proto import service_config_pb2
import acl
import api
import projects
import storage


class ApiTest(test_case.EndpointsTestCase):
  api_service_cls = api.ConfigApi

  def setUp(self):
    super(ApiTest, self).setUp()
    self.mock(acl, 'has_project_access', mock.Mock())
    acl.has_project_access.side_effect = (
        lambda pid: pid != 'secret'
    )
    self.mock(acl, 'has_service_access', mock.Mock(return_value=True))
    self.mock(projects, 'get_projects', mock.Mock())
    projects.get_projects.return_value = [
      service_config_pb2.Project(id='chromium'),
      service_config_pb2.Project(id='v8'),
    ]
    self.mock(projects, 'get_metadata', mock.Mock())
    projects.get_metadata.return_value = project_config_pb2.ProjectCfg()
    self.mock(projects, 'get_repo', mock.Mock())
    projects.get_repo.return_value = (
        projects.RepositoryType.GITILES, 'https://localhost/project')

  def mock_config(self):
    self.mock(storage, 'get_config_hash_async', mock.Mock())
    self.mock(storage, 'get_config_by_hash_async', mock.Mock())
    storage.get_config_hash_async.return_value = future(('deadbeef', 'abc0123'))
    storage.get_config_by_hash_async.return_value = future('config text')

  def mock_refs(self):
    self.mock(projects, 'get_refs', mock.Mock())
    projects.get_refs.return_value = [
      project_config_pb2.RefsCfg.Ref(name='refs/heads/master'),
      project_config_pb2.RefsCfg.Ref(name='refs/heads/release42'),
    ]

  ##############################################################################
  # get_mapping

  def test_get_config_one(self):
    self.mock(storage, 'get_mapping_async', mock.Mock())
    storage.get_mapping_async.return_value = future({
      'services/x': 'http://x',
    })

    req = {
      'config_set': 'services/x',
    }
    resp = self.call_api('get_mapping', req).json_body

    storage.get_mapping_async.assert_called_once_with(config_set='services/x')

    self.assertEqual(resp, {
      'mappings': [
        {
          'config_set': 'services/x',
          'location': 'http://x',
        },
      ],
    })

  def test_get_config_one_forbidden(self):
    self.mock(acl, 'can_read_config_set', mock.Mock(return_value=False))
    with self.call_should_fail(httplib.FORBIDDEN):
      req = {
        'config_set': 'services/x',
      }
      self.call_api('get_mapping', req)

  def test_get_config_all(self):
    self.mock(storage, 'get_mapping_async', mock.Mock())
    storage.get_mapping_async.return_value = future({
      'services/x': 'http://x',
      'services/y': 'http://y',
    })

    resp = self.call_api('get_mapping', {}).json_body

    self.assertEqual(resp, {
      'mappings': [
        {
          'config_set': 'services/x',
          'location': 'http://x',
        },
        {
          'config_set': 'services/y',
          'location': 'http://y',
        },
      ],
    })

  def test_get_config_all_partially_forbidden(self):
    self.mock(storage, 'get_mapping_async', mock.Mock())
    storage.get_mapping_async.return_value = future({
      'services/x': 'http://x',
      'services/y': 'http://y',
    })
    self.mock(acl, 'can_read_config_set', mock.Mock(side_effect=[True, False]))

    resp = self.call_api('get_mapping', {}).json_body

    self.assertEqual(resp, {
      'mappings': [
        {
          'config_set': 'services/x',
          'location': 'http://x',
        },
      ],
    })

  ##############################################################################
  # get_config

  def test_get_config(self):
    self.mock_config()

    req = {
      'config_set': 'services/luci-config',
      'path': 'my.cfg',
      'revision': 'deadbeef',
    }
    resp = self.call_api('get_config', req).json_body

    self.assertEqual(resp, {
      'content': base64.b64encode('config text'),
      'content_hash': 'abc0123',
      'revision': 'deadbeef',
    })
    storage.get_config_hash_async.assert_called_once_with(
      'services/luci-config', 'my.cfg', revision='deadbeef')
    storage.get_config_by_hash_async.assert_called_once_with('abc0123')

  def test_get_config_hash_only(self):
    self.mock_config()

    req = {
      'config_set': 'services/luci-config',
      'hash_only': True,
      'path': 'my.cfg',
      'revision': 'deadbeef',
    }
    resp = self.call_api('get_config', req).json_body

    self.assertEqual(resp, {
      'content_hash': 'abc0123',
      'revision': 'deadbeef',
    })
    self.assertFalse(storage.get_config_by_hash_async.called)

  def test_get_config_blob_not_found(self):
    self.mock_config()
    storage.get_config_by_hash_async.return_value = future(None)

    req = {
      'config_set': 'services/luci-config',
      'path': 'my.cfg',
    }
    with self.call_should_fail(httplib.NOT_FOUND):
      self.call_api('get_config', req)

  def test_get_config_not_found(self):
    self.mock(
        storage, 'get_config_hash_async', lambda *_, **__: future((None, None)))

    req = {
      'config_set': 'services/x',
      'path': 'a.cfg',
    }
    with self.call_should_fail(httplib.NOT_FOUND):
      self.call_api('get_config', req)

  def test_get_wrong_config_set(self):
    self.mock(acl, 'can_read_config_set', mock.Mock(side_effect=ValueError))

    req = {
      'config_set': 'xxx',
      'path': 'my.cfg',
      'revision': 'deadbeef',
    }
    with self.call_should_fail(httplib.BAD_REQUEST):
      self.call_api('get_config', req).json_body

  def test_get_config_without_permissions(self):
    self.mock(acl, 'can_read_config_set', mock.Mock(return_value=False))
    self.mock(storage, 'get_config_hash_async', mock.Mock())

    req = {
      'config_set': 'services/luci-config',
      'path': 'projects.cfg',
    }
    with self.call_should_fail(404):
      self.call_api('get_config', req)
    self.assertFalse(storage.get_config_hash_async.called)

  ##############################################################################
  # get_config_by_hash

  def test_get_config_by_hash(self):
    self.mock(storage, 'get_config_by_hash_async', mock.Mock())
    storage.get_config_by_hash_async.return_value = future('some content')

    req = {'content_hash': 'deadbeef'}
    resp = self.call_api('get_config_by_hash', req).json_body

    self.assertEqual(resp, {
      'content': base64.b64encode('some content'),
    })

    storage.get_config_by_hash_async.return_value = future(None)
    with self.call_should_fail(httplib.NOT_FOUND):
      self.call_api('get_config_by_hash', req)

  ##############################################################################
  # get_projects

  def test_get_projects(self):
    projects.get_projects.return_value = [
      service_config_pb2.Project(id='chromium'),
      service_config_pb2.Project(id='v8'),
      service_config_pb2.Project(id='inconsistent'),
      service_config_pb2.Project(id='secret'),
    ]
    projects.get_metadata.side_effect = [
      project_config_pb2.ProjectCfg(
          name='Chromium, the best browser', access='all'),
      project_config_pb2.ProjectCfg(access='all'),
      project_config_pb2.ProjectCfg(access='all'),
      project_config_pb2.ProjectCfg(access='administrators'),
    ]
    projects.get_repo.side_effect = [
      (projects.RepositoryType.GITILES, 'http://localhost/chromium'),
      (projects.RepositoryType.GITILES, 'http://localhost/v8'),
      (None, None),
      (projects.RepositoryType.GITILES, 'http://localhost/secret'),
    ]

    resp = self.call_api('get_projects', {}).json_body

    self.assertEqual(resp, {
      'projects': [
        {
          'id': 'chromium',
          'name': 'Chromium, the best browser',
          'repo_type': 'GITILES',
          'repo_url': 'http://localhost/chromium',
        },
        {
          'id': 'v8',
          'repo_type': 'GITILES',
          'repo_url': 'http://localhost/v8',
        },
      ],
    })

  def test_get_projects_without_permissions(self):
    acl.has_project_access.return_value = False
    self.call_api('get_projects', {})

  ##############################################################################
  # get_refs

  def test_get_refs(self):
    self.mock_refs()

    req = {'project_id': 'chromium'}
    resp = self.call_api('get_refs', req).json_body

    self.assertEqual(resp, {
      'refs': [
        {'name': 'refs/heads/master'},
        {'name': 'refs/heads/release42'},
      ],
    })

  def test_get_refs_without_permissions(self):
    self.mock_refs()
    acl.has_project_access.side_effect = None
    acl.has_project_access.return_value = False

    req = {'project_id': 'chromium'}
    with self.call_should_fail(httplib.NOT_FOUND):
      self.call_api('get_refs', req)
    self.assertFalse(projects.get_refs.called)

  def test_get_refs_of_non_existent_project(self):
    self.mock(projects, 'get_refs', mock.Mock())
    projects.get_refs.return_value = None
    req = {'project_id': 'non-existent'}
    with self.call_should_fail(httplib.NOT_FOUND):
      self.call_api('get_refs', req)

  ##############################################################################
  # get_project_configs

  def test_get_config_multi(self):
    self.mock_refs()
    projects.get_projects.return_value.extend([
      service_config_pb2.Project(id='inconsistent'),
      service_config_pb2.Project(id='secret'),
    ])

    self.mock(storage, 'get_latest_multi_async', mock.Mock())
    storage.get_latest_multi_async.return_value = future([
      {
        'config_set': 'projects/chromium',
        'revision': 'deadbeef',
        'content_hash': 'abc0123',
        'content': 'config text',
      },
      {
        'config_set': 'projects/v8',
        'revision': 'beefdead',
        'content_hash': 'ccc123',
        # No content
      }
    ])

    req = {'path': 'cq.cfg'}
    resp = self.call_api('get_project_configs', req).json_body

    self.assertEqual(resp, {
      'configs': [{
        'config_set': 'projects/chromium',
        'revision': 'deadbeef',
        'content_hash': 'abc0123',
        'content': base64.b64encode('config text'),
      }],
    })
    config_sets_arg = storage.get_latest_multi_async.call_args[0][0]
    self.assertEqual(
        list(config_sets_arg),
        ['projects/chromium', 'projects/v8', 'projects/inconsistent'])

  ##############################################################################
  # get_ref_configs

  def test_get_ref_configs(self):
    self.mock_refs()

    self.mock(api, 'get_config_multi', mock.Mock())
    res = api.GetConfigMultiResponseMessage()
    api.get_config_multi.return_value = res

    req = {'path': 'cq.cfg'}
    resp = self.call_api('get_ref_configs', req).json_body

    config_sets = api.get_config_multi.call_args[0][0]
    self.assertEqual(
        list(config_sets),
        [
          'projects/chromium/refs/heads/master',
          'projects/chromium/refs/heads/release42',
          'projects/v8/refs/heads/master',
          'projects/v8/refs/heads/release42',
        ])

  def test_get_ref_configs_without_permission(self):
    acl.has_project_access.return_value = False

    req = {'path': 'cq.cfg'}
    resp = self.call_api('get_ref_configs', req).json_body
    self.assertEqual(resp, {})


if __name__ == '__main__':
  test_env.main()
