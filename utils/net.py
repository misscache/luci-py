# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Classes and functions for generic network communication over HTTP."""

import cookielib
import cStringIO as StringIO
import httplib
import itertools
import logging
import math
import os
import random
import socket
import ssl
import threading
import time
import urllib
import urllib2
import urlparse

from third_party.rietveld import upload

# Hack out upload logging.info()
upload.logging = logging.getLogger('upload')
# Mac pylint choke on this line.
upload.logging.setLevel(logging.WARNING)  # pylint: disable=E1103


# The name of the key to store the count of url attempts.
COUNT_KEY = 'UrlOpenAttempt'

# Default maximum number of attempts to trying opening a url before aborting.
URL_OPEN_MAX_ATTEMPTS = 30

# Default timeout when retrying.
URL_OPEN_TIMEOUT = 6*60.

# Content type for url encoded POST body.
URL_ENCODED_FORM_CONTENT_TYPE = 'application/x-www-form-urlencoded'

# Default content type for POST body.
DEFAULT_CONTENT_TYPE = URL_ENCODED_FORM_CONTENT_TYPE

# Content type -> function that encodes a request body.
CONTENT_ENCODERS = {
  URL_ENCODED_FORM_CONTENT_TYPE: urllib.urlencode,
}

# File to use to store all auth cookies.
COOKIE_FILE = os.path.join(os.path.expanduser('~'), '.isolated_cookies')

# Global (for now) map: server URL (http://example.com) -> HttpService instance.
# Used by get_http_service to cache HttpService instances.
_http_services = {}
_http_services_lock = threading.Lock()

# CookieJar reused by all services + lock that protects its instantiation.
_cookie_jar = None
_cookie_jar_lock = threading.Lock()


class NetError(IOError):
  """Generic network related error."""

  def __init__(self, inner_exc=None):
    super(NetError, self).__init__(str(inner_exc or self.__doc__))
    self.inner_exc = inner_exc

  def format(self, verbose=False):
    """Human readable description with detailed information about the error."""
    out = ['Exception: %s' % (self.inner_exc,)]
    if verbose:
      if isinstance(self.inner_exc, urllib2.HTTPError):
        out.append('----------')
        if self.inner_exc.hdrs:
          for header, value in self.inner_exc.hdrs.iteritems():
            if not header.startswith('x-'):
              out.append('%s: %s' % (header.capitalize(), value))
          out.append('')
        out.append(self.inner_exc.read() or '<empty body>')
        out.append('----------')
    return '\n'.join(out)


class TimeoutError(NetError):
  """Timeout while reading HTTP response."""


class ConnectionError(NetError):
  """Failed to connect to the server."""


class HttpError(NetError):
  """Server returned HTTP error code."""

  def __init__(self, code, inner_exc=None):
    super(HttpError, self).__init__(inner_exc)
    self.code = code


def url_open(url, **kwargs):
  """Attempts to open the given url multiple times.

  |data| can be either:
    - None for a GET request
    - str for pre-encoded data
    - list for data to be encoded
    - dict for data to be encoded

  See HttpService.request for a full list of arguments.

  Returns HttpResponse object, where the response may be read from, or None
  if it was unable to connect.
  """
  urlhost, urlpath = split_server_request_url(url)
  service = get_http_service(urlhost)
  return service.request(urlpath, **kwargs)


def url_read(url, **kwargs):
  """Attempts to open the given url multiple times and read all data from it.

  Accepts same arguments as url_open function.

  Returns all data read or None if it was unable to connect or read the data.
  """
  response = url_open(url, **kwargs)
  if not response:
    return None
  try:
    return response.read()
  except TimeoutError:
    return None


def split_server_request_url(url):
  """Splits the url into scheme+netloc and path+params+query+fragment."""
  url_parts = list(urlparse.urlparse(url))
  urlhost = '%s://%s' % (url_parts[0], url_parts[1])
  urlpath = urlparse.urlunparse(['', ''] + url_parts[2:])
  return urlhost, urlpath


def get_http_service(urlhost):
  """Returns existing or creates new instance of HttpService that can send
  requests to given base urlhost.
  """
  # Ensure consistency.
  urlhost = str(urlhost).lower().rstrip('/')
  with _http_services_lock:
    service = _http_services.get(urlhost)
    if not service:
      cookie_jar = get_cookie_jar()
      service = HttpService(
          urlhost,
          engine=Urllib2Engine(cookie_jar),
          authenticator=AppEngineAuthenticator(urlhost, cookie_jar))
      _http_services[urlhost] = service
    return service


def get_cookie_jar():
  """Returns global CoookieJar object that stores cookies in the file."""
  global _cookie_jar
  with _cookie_jar_lock:
    if _cookie_jar is not None:
      return _cookie_jar
    jar = ThreadSafeCookieJar(COOKIE_FILE)
    jar.load()
    _cookie_jar = jar
    return jar


class HttpService(object):
  """Base class for a class that provides an API to HTTP based service:
    - Provides 'request' method.
    - Supports automatic request retries.
    - Supports persistent cookies.
    - Thread safe.
  """
  def __init__(self, urlhost, engine, authenticator=None):
    self.urlhost = urlhost
    self.engine = engine
    self.authenticator = authenticator

  @staticmethod
  def encode_request_body(body, content_type):
    """Returns request body encoded according to its content type."""
    # No body or it is already encoded.
    if body is None or isinstance(body, str):
      return body
    # Any body should have content type set.
    assert content_type, 'Request has body, but no content type'
    encoder = CONTENT_ENCODERS.get(content_type)
    assert encoder, ('Unknown content type %s' % content_type)
    return encoder(body)

  def request(
      self,
      urlpath,
      data=None,
      content_type=None,
      max_attempts=URL_OPEN_MAX_ATTEMPTS,
      retry_404=False,
      retry_50x=True,
      timeout=URL_OPEN_TIMEOUT,
      read_timeout=None):
    """Attempts to open the given url multiple times.

    |urlpath| is relative to the server root, i.e. '/some/request?param=1'.

    |data| can be either:
      - None for a GET request
      - str for pre-encoded data
      - list for data to be form-encoded
      - dict for data to be form-encoded

    - Optionally retries HTTP 404 and 50x.
    - Retries up to |max_attempts| times. If None or 0, there's no limit in the
      number of retries.
    - Retries up to |timeout| duration in seconds. If None or 0, there's no
      limit in the time taken to do retries.
    - If both |max_attempts| and |timeout| are None or 0, this functions retries
      indefinitely.

    If |read_timeout| is not None will configure underlying socket to
    raise TimeoutError exception whenever there's no response from the server
    for more than |read_timeout| seconds. It can happen during any read
    operation so once you pass non-None |read_timeout| be prepared to handle
    these exceptions in subsequent reads from the stream.

    Returns a file-like object, where the response may be read from, or None
    if it was unable to connect.
    """
    assert urlpath and urlpath[0] == '/'

    if data is not None:
      method = 'POST'
      content_type = content_type or DEFAULT_CONTENT_TYPE
      body = self.encode_request_body(data, content_type)
    else:
      method = 'GET'
      body = None
      assert not content_type, 'Can\'t use content_type on GET'

    # Prepare request info.
    parsed = urlparse.urlparse('/' + urlpath.lstrip('/'))
    resource_url = urlparse.urljoin(self.urlhost, parsed.path)
    query_params = urlparse.parse_qsl(parsed.query)

    last_error = None
    auth_attempted = False

    for attempt in retry_loop(max_attempts, timeout):
      # Log non-first attempt.
      if attempt.attempt:
        logging.warning(
            'Retrying request %s, attempt %d/%d...',
            resource_url, attempt.attempt, max_attempts)

      try:
        # Prepare and send a new request.
        request = HttpRequest(method, resource_url, query_params, body,
            content_type, read_timeout)
        self.prepare_request(request, attempt.attempt)
        response = self.engine.perform_request(request)
        logging.debug('Request %s succeeded', request.get_full_url())
        return response

      except (ConnectionError, TimeoutError) as e:
        last_error = e
        logging.warning(
            'Unable to open url %s on attempt %d.\n%s',
            request.get_full_url(), attempt.attempt, e.format())
        continue

      except HttpError as e:
        last_error = e

        # Access denied -> authenticate.
        if e.code in (401, 403):
          logging.error(
              'Authentication is required for %s on attempt %d.\n%s',
              request.get_full_url(), attempt.attempt, e.format())
          # Try to authenticate only once. If it doesn't help, then server does
          # not support app engine authentication.
          if not auth_attempted:
            auth_attempted = True
            if self.authenticator and self.authenticator.authenticate():
              # Success! Run request again immediately.
              attempt.skip_sleep = True
              continue
          # Authentication attempt was unsuccessful.
          logging.error(
              'Unable to authenticate to %s.\n%s',
              request.get_full_url(), e.format(verbose=True))
          return None

        # Hit a error that can not be retried -> stop retry loop.
        if ((e.code < 500 and not (retry_404 and e.code == 404)) or
            (e.code >= 500 and not retry_50x)):
          # This HttpError means we reached the server and there was a problem
          # with the request, so don't retry.
          logging.error(
              'Able to connect to %s but an exception was thrown.\n%s',
              request.get_full_url(), e.format(verbose=True))
          return None

        # Retry all other errors.
        logging.warning(
            'Server responded with error on %s on attempt %d.\n%s',
            request.get_full_url(), attempt.attempt, e.format())
        continue

    logging.error(
        'Unable to open given url, %s, after %d attempts.\n%s',
        request.get_full_url(), max_attempts, last_error.format(verbose=True))
    return None

  def prepare_request(self, request, attempt):  # pylint: disable=R0201
    """Modify HttpRequest before sending it by adding COUNT_KEY parameter."""
    # Add COUNT_KEY only on retries.
    if attempt:
      request.params += [(COUNT_KEY, attempt)]


class HttpRequest(object):
  """Request to HttpService."""

  def __init__(self, method, url, params, body, content_type, timeout):
    """Arguments:
      |method| - HTTP method to use
      |url| - relative URL to the resource, without query parameters
      |params| - list of (key, value) pairs to put into GET parameters
      |body| - encoded body of the request (None or str)
      |content_type| - body content type or None if no body
      |timeout| - socket read timeout (None to disable)
    """
    self.method = method
    self.url = url
    self.params = params[:]
    self.body = body
    self.content_type = content_type
    self.timeout = timeout

  def get_full_url(self):
    """Resource URL with url-encoded GET parameters."""
    if not self.params:
      return self.url
    else:
      return '%s?%s' % (self.url, urllib.urlencode(self.params))

  def make_fake_response(self, content=''):
    """Makes new fake HttpResponse to this request, useful in tests."""
    return HttpResponse.get_fake_response(content, self.get_full_url())


class HttpResponse(object):
  """Response from HttpService."""

  def __init__(self, stream, url, headers):
    self._stream = stream
    self._url = url
    self._headers = headers
    self._read = 0

  @property
  def content_length(self):
    """Total length to the response or None if not known in advance."""
    length = self._headers.get('Content-Length')
    return int(length) if length is not None else None

  def read(self, size=None):
    """Reads up to |size| bytes from the stream and returns them.

    If |size| is None reads all available bytes.

    Raises TimeoutError on read timeout.
    """
    try:
      # cStringIO has a bug: stream.read(None) is not the same as stream.read().
      data = self._stream.read() if size is None else self._stream.read(size)
      self._read += len(data)
      return data
    except (socket.timeout, ssl.SSLError) as e:
      logging.error('Timeout while reading from %s, read %d of %s: %s',
          self._url, self._read, self.content_length, e)
      raise TimeoutError(e)

  @classmethod
  def get_fake_response(cls, content, url):
    """Returns HttpResponse with predefined content, useful in tests."""
    return cls(StringIO.StringIO(content),
        url, {'content-length': len(content)})


class RequestEngine(object):
  """Base class for objects that know how to execute HttpRequests."""

  def perform_request(self, request):
    """Sends a HttpRequest to the server and reads back the response.

    Returns HttpResponse.

    Raises:
      ConnectionError - failed to establish connection to the server.
      TimeoutError - timeout while connecting or reading response.
      HttpError - server responded with >= 400 error code.
    """
    raise NotImplementedError()


class Authenticator(object):
  """Base class for objects that know how to authenticate into http services."""

  def authenticate(self):
    """Authenticates in the app engine service."""
    raise NotImplementedError()


class Urllib2Engine(RequestEngine):
  """Class that knows how to execute HttpRequests via urllib2."""

  def __init__(self, cookie_jar):
    super(Urllib2Engine, self).__init__()
    self.opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cookie_jar))

  def perform_request(self, request):
    try:
      req = self.make_urllib2_request(request)
      if request.timeout:
        resp = self.opener.open(req, timeout=request.timeout)
      else:
        resp = self.opener.open(req)
      return HttpResponse(resp, req.get_full_url(), resp.headers)
    except urllib2.HTTPError as e:
      raise HttpError(e.code, e)
    except (urllib2.URLError, httplib.HTTPException,
            socket.timeout, ssl.SSLError) as e:
      raise ConnectionError(e)

  @staticmethod
  def make_urllib2_request(request):
    """Converts HttpRequest to urllib2.Request."""
    result = urllib2.Request(request.get_full_url(), data=request.body)
    if request.body is not None:
      result.add_header('Content-Length', len(request.body))
      if request.content_type:
        result.add_header('Content-Type', request.content_type)
    return result


class AppEngineAuthenticator(Authenticator):
  """Helper class to perform AppEngine authentication dance via upload.py."""

  # This lock ensures that user won't be confused with multiple concurrent
  # login prompts.
  _auth_lock = threading.Lock()

  def __init__(self, urlhost, cookie_jar, email=None, password=None):
    super(AppEngineAuthenticator, self).__init__()
    self.urlhost = urlhost
    self.cookie_jar = cookie_jar
    self.email = email
    self.password = password
    self._keyring = None

  def authenticate(self):
    """Authenticates in the app engine application.

    Mutates |self.cookie_jar| in place by adding all required cookies.

    Returns True on success.
    """
    # To be used from inside AuthServer.
    cookie_jar = self.cookie_jar
    # RPC server that uses AuthenticationSupport's cookie jar.
    class AuthServer(upload.AbstractRpcServer):
      def _GetOpener(self):
        # Authentication code needs to know about 302 response.
        # So make OpenerDirector without HTTPRedirectHandler.
        opener = urllib2.OpenerDirector()
        opener.add_handler(urllib2.ProxyHandler())
        opener.add_handler(urllib2.UnknownHandler())
        opener.add_handler(urllib2.HTTPHandler())
        opener.add_handler(urllib2.HTTPDefaultErrorHandler())
        opener.add_handler(urllib2.HTTPSHandler())
        opener.add_handler(urllib2.HTTPErrorProcessor())
        opener.add_handler(urllib2.HTTPCookieProcessor(cookie_jar))
        return opener
      def PerformAuthentication(self):
        self._Authenticate()
        return self.authenticated
    with cookie_jar:
      with self._auth_lock:
        rpc_server = AuthServer(self.urlhost, self.get_credentials)
        return rpc_server.PerformAuthentication()

  def get_credentials(self):
    """Called during authentication process to get the credentials.
    May be called multiple times if authentication fails.
    Returns tuple (email, password).
    """
    if self.email and self.password:
      return (self.email, self.password)
    self._keyring = self._keyring or upload.KeyringCreds(self.urlhost,
        self.urlhost, self.email)
    return self._keyring.GetUserCredentials()


class ThreadSafeCookieJar(cookielib.MozillaCookieJar):
  """MozillaCookieJar with thread safe load and save."""

  def __enter__(self):
    """Context manager interface."""
    return self

  def __exit__(self, *_args):
    """Saves cookie jar when exiting the block."""
    self.save()
    return False

  def load(self, filename=None, ignore_discard=False, ignore_expires=False):
    """Loads cookies from the file if it exists."""
    filename = os.path.expanduser(filename or self.filename)
    with self._cookies_lock:
      if os.path.exists(filename):
        try:
          cookielib.MozillaCookieJar.load(self, filename,
                                          ignore_discard,
                                          ignore_expires)
          logging.debug('Loaded cookies from %s', filename)
        except (cookielib.LoadError, IOError):
          pass
      else:
        try:
          fd = os.open(filename, os.O_CREAT, 0600)
          os.close(fd)
        except OSError:
          logging.error('Failed to create %s', filename)
      try:
        os.chmod(filename, 0600)
      except OSError:
        logging.error('Failed to fix mode for %s', filename)

  def save(self, filename=None, ignore_discard=False, ignore_expires=False):
    """Saves cookies to the file, completely overwriting it."""
    logging.debug('Saving cookies to %s', filename or self.filename)
    with self._cookies_lock:
      try:
        cookielib.MozillaCookieJar.save(self, filename,
                                        ignore_discard,
                                        ignore_expires)
      except OSError:
        logging.error('Failed to save %s', filename)


class RetryAttempt(object):
  """Contains information about current retry attempt.

  Yielded from retry_loop.
  """

  def __init__(self, attempt, remaining):
    """Information about current attempt in retry loop:
      |attempt| - zero based index of attempt.
      |remaining| - how much time is left before retry loop finishes retries.
    """
    self.attempt = attempt
    self.remaining = remaining
    self.skip_sleep = False


def calculate_sleep_before_retry(attempt, max_duration):
  """How long to sleep before retrying an attempt in retry_loop."""
  # Maximum sleeping time. We're hammering a cloud-distributed service, it'll
  # survive.
  MAX_SLEEP = 10.
  # random.random() returns [0.0, 1.0). Starts with relatively short waiting
  # time by starting with 1.5/2+1.5^-1 median offset.
  duration = (random.random() * 1.5) + math.pow(1.5, (attempt - 1))
  assert duration > 0.1
  duration = min(MAX_SLEEP, duration)
  if max_duration:
    duration = min(max_duration, duration)
  return duration


def sleep_before_retry(attempt, max_duration):
  """Sleeps for some amount of time when retrying the attempt in retry_loop.

  To be mocked in tests.
  """
  time.sleep(calculate_sleep_before_retry(attempt, max_duration))


def current_time():
  """Used by retry loop to get current time.

  To be mocked in tests.
  """
  return time.time()


def retry_loop(max_attempts=None, timeout=None):
  """Yields whenever new attempt to perform some action is needed.

  Yields instances of RetryAttempt class that contains information about current
  attempt. Setting |skip_sleep| attribute of RetryAttempt to True will cause
  retry loop to run next attempt immediately.
  """
  start = current_time()
  for attempt in itertools.count():
    # Too many attempts?
    if max_attempts and attempt == max_attempts:
      break
    # Retried for too long?
    remaining = (timeout - (current_time() - start)) if timeout else None
    if remaining is not None and remaining < 0:
      break
    # Kick next iteration.
    attemp_obj = RetryAttempt(attempt, remaining)
    yield attemp_obj
    if attemp_obj.skip_sleep:
      continue
    # Only sleep if we are going to try again.
    if max_attempts and attempt != max_attempts - 1:
      remaining = (timeout - (current_time() - start)) if timeout else None
      if remaining is not None and remaining < 0:
        break
      sleep_before_retry(attempt, remaining)
