# Copyright 2015 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

"""Storage of config files."""

import hashlib
import logging

from google.appengine.api import app_identity
from google.appengine.ext import ndb
from google.protobuf import text_format

from components import utils
from components.datastore_utils import txn


class Blob(ndb.Model):
  """Content-addressed blob. Immutable.

  Entity key:
    Id is content hash that has format "v1:<sha>"
    where sha is hex-encoded Git-compliant SHA-1 of
    'blob {content len}\0{content}'. Computed by compute_hash function.
    Blob has no parent.
  """
  created_ts = ndb.DateTimeProperty(auto_now_add=True)
  content = ndb.BlobProperty(required=True)


class ConfigSet(ndb.Model):
  """Versioned collection of config files.

  Entity key:
    Id is a config set name. Examples: services/luci-config, projects/chromium.

  gitiles_import.py relies on the fact that this class has only one attribute.
  """
  # last imported revision of the config set. See also Revision and File.
  latest_revision = ndb.StringProperty(required=True)
  location = ndb.StringProperty(required=True)


class Revision(ndb.Model):
  """A single revision of a config set. Immutable.

  Parent of File entities. Revision entity does not have to exist.

  Entity key:
    Id is a revision name. If imported from Git, it is a commit hash.
    Parent is ConfigSet.
  """


class File(ndb.Model):
  """A single file in a revision. Immutable.

  Entity key:
    Id is a filename without a leading slash. Parent is Revision.
  """
  created_ts = ndb.DateTimeProperty(auto_now_add=True)
  # hash of the file content, computed by compute_hash().
  # A Blob entity with this key must exist.
  content_hash = ndb.StringProperty(indexed=False, required=True)

  def _pre_put_hook(self):
    assert isinstance(self.key.id(), str)
    assert not self.key.id().startswith('/')


@ndb.tasklet
def get_mapping_async(config_set=None):
  if config_set:
    existing = yield ConfigSet.get_by_id_async(config_set)
    config_sets = [existing or ConfigSet(id=config_set)]
  else:
    config_sets = yield ConfigSet.query().fetch_async()
  raise ndb.Return({
      cs.key.id(): cs.location
      for cs in config_sets
      if cs
    })


@ndb.tasklet
def get_latest_revision_async(config_set):
  """Returns latest known revision of the |config_set|. May return None."""
  config_set_entity = yield ConfigSet.get_by_id_async(config_set)
  raise ndb.Return(
      config_set_entity.latest_revision if config_set_entity else None)


@ndb.tasklet
def get_config_hash_async(config_set, path, revision=None):
  """Returns tuple (revision, content_hash).

  |revision| detaults to the latest revision.
  """
  assert isinstance(config_set, basestring)
  assert config_set
  assert isinstance(path, basestring)
  assert path
  assert not path.startswith('/')

  if not revision:
    revision = yield get_latest_revision_async(config_set)
    if revision is None:
      logging.warning('Config set not found: %s' % config_set)
      raise ndb.Return(None, None)

  assert revision
  file_key = ndb.Key(
      ConfigSet, config_set,
      Revision, revision,
      File, path)
  file_entity = yield file_key.get_async()
  content_hash = file_entity.content_hash if file_entity else None
  if not content_hash:
    revision = None
  raise ndb.Return(revision, content_hash)


@ndb.tasklet
def get_config_by_hash_async(content_hash):
  """Returns config content by its hash."""
  blob = yield Blob.get_by_id_async(content_hash)
  raise ndb.Return(blob.content if blob else None)


@ndb.tasklet
def get_latest_async(config_set, path):
  """Returns latest content of a config file."""
  _, content_hash = yield get_config_hash_async(config_set, path)
  if not content_hash:  # pragma: no cover
    raise ndb.Return(None)
  content = yield get_config_by_hash_async(content_hash)
  raise ndb.Return(content)


@ndb.tasklet
def get_latest_multi_async(config_sets, path, hashes_only=False):
  """Returns latest contents of all <config_set>:<path> config files.

  Returns:
    A a list of dicts with keys 'config_set', 'revision', 'content_hash' and
    'content'. Content is not available if |hashes_only| is True.
  """
  assert path
  assert not path.startswith('/')

  config_set_keys = [ndb.Key(ConfigSet, cs) for cs in config_sets]
  config_set_entities = yield ndb.get_multi_async(config_set_keys)
  config_set_entities = filter(None, config_set_entities)

  file_keys = [
    ndb.Key(ConfigSet, cs.key.id(), Revision, cs.latest_revision, File, path)
    for cs in config_set_entities
  ]
  file_entities = yield ndb.get_multi_async(file_keys)
  file_entities = filter(None, file_entities)

  results = [
    {
      'config_set': f.key.parent().parent().id(),
      'revision': f.key.parent().id(),
      'content_hash': f.content_hash,
      'content': (
          None if hashes_only else ndb.Key(Blob, f.content_hash).get_async()),
    }
    for f in file_entities
  ]

  if not hashes_only:
    for r in results:
      blob = yield r['content']
      r['content'] = blob.content if blob else None

  raise ndb.Return(results)


@utils.memcache_async('latest_message', ['config_set', 'path'], time=60)
@ndb.tasklet
def get_latest_as_message_async(config_set, path, message_factory):
  """Reads latest config file as a text-formatted protobuf message.

  |message_factory| is a function that creates a message. Typically the message
  type itself. Values found in the retrieved config file are merged into the
  return value of the factory.

  Memcaches results.
  """
  msg = message_factory()
  text = yield get_latest_async(config_set, path)
  if text:
    text_format.Merge(text, msg)
  raise ndb.Return(msg)


@utils.cache
def get_self_config_set():
  return 'services/%s' % app_identity.get_application_id()


def get_self_config_async(path, message_factory):
  """Parses a config file in the app's config set into a protobuf message."""
  return get_latest_as_message_async(
      get_self_config_set(), path, message_factory)


def compute_hash(content):
  """Computes Blob id by its content.

  See Blob docstring for Blob id format.
  """
  sha = hashlib.sha1()
  sha.update('blob %d\0' % len(content))
  sha.update(content)
  return 'v1:%s' % sha.hexdigest()


@ndb.tasklet
def import_blob_async(content, content_hash=None):
  """Saves |content| to a Blob entity.

  Returns:
    Content hash.
  """
  content_hash = content_hash or compute_hash(content)

  # pylint: disable=E1120
  if not Blob.get_by_id(content_hash):
    yield Blob(id=content_hash, content=content).put_async()
  raise ndb.Return(content_hash)


def import_blob(content, content_hash=None):
  return import_blob_async(content, content_hash=content_hash).get_result()
