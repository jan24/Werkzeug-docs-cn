# -*- coding: utf-8 -*-
"""
    werkzeug.test
    ~~~~~~~~~~~~~

    This module implements a client to WSGI applications for testing.

    :copyright: (c) 2014 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import sys
import mimetypes
from time import time
from random import random
from itertools import chain
from tempfile import TemporaryFile
from io import BytesIO

try:
    from urllib2 import Request as U2Request
except ImportError:
    from urllib.request import Request as U2Request
try:
    from http.cookiejar import CookieJar
except ImportError: # Py2
    from cookielib import CookieJar

from werkzeug._compat import iterlists, iteritems, itervalues, to_bytes, \
     string_types, text_type, reraise, wsgi_encoding_dance, \
     make_literal_wrapper
from werkzeug._internal import _empty_stream, _get_environ
from werkzeug.wrappers import BaseRequest
from werkzeug.urls import url_encode, url_fix, iri_to_uri, url_unquote, \
     url_unparse, url_parse
from werkzeug.wsgi import get_host, get_current_url, ClosingIterator
from werkzeug.utils import dump_cookie
from werkzeug.datastructures import FileMultiDict, MultiDict, \
     CombinedMultiDict, Headers, FileStorage


def stream_encode_multipart(values, use_tempfile=True, threshold=1024 * 500,
                            boundary=None, charset='utf-8'):
    """Encode a dict of values (either strings or file descriptors or
    :class:`FileStorage` objects.) into a multipart encoded string stored
    in a file descriptor.
    """
    if boundary is None:
        boundary = '---------------WerkzeugFormPart_%s%s' % (time(), random())
    _closure = [BytesIO(), 0, False]

    if use_tempfile:
        def write_binary(string):
            stream, total_length, on_disk = _closure
            if on_disk:
                stream.write(string)
            else:
                length = len(string)
                if length + _closure[1] <= threshold:
                    stream.write(string)
                else:
                    new_stream = TemporaryFile('wb+')
                    new_stream.write(stream.getvalue())
                    new_stream.write(string)
                    _closure[0] = new_stream
                    _closure[2] = True
                _closure[1] = total_length + length
    else:
        write_binary = _closure[0].write

    def write(string):
        write_binary(string.encode(charset))

    if not isinstance(values, MultiDict):
        values = MultiDict(values)

    for key, values in iterlists(values):
        for value in values:
            write('--%s\r\nContent-Disposition: form-data; name="%s"' %
                  (boundary, key))
            reader = getattr(value, 'read', None)
            if reader is not None:
                filename = getattr(value, 'filename',
                                   getattr(value, 'name', None))
                content_type = getattr(value, 'content_type', None)
                if content_type is None:
                    content_type = filename and \
                        mimetypes.guess_type(filename)[0] or \
                        'application/octet-stream'
                if filename is not None:
                    write('; filename="%s"\r\n' % filename)
                else:
                    write('\r\n')
                write('Content-Type: %s\r\n\r\n' % content_type)
                while 1:
                    chunk = reader(16384)
                    if not chunk:
                        break
                    write_binary(chunk)
            else:
                if not isinstance(value, string_types):
                    value = str(value)
                else:
                    value = to_bytes(value, charset)
                write('\r\n\r\n')
                write_binary(value)
            write('\r\n')
    write('--%s--\r\n' % boundary)

    length = int(_closure[0].tell())
    _closure[0].seek(0)
    return _closure[0], length, boundary

'''
def encode_multipart(values, boundary=None, charset='utf-8'):
    """Like `stream_encode_multipart` but returns a tuple in the form
    (``boundary``, ``data``) where data is a bytestring.
    """
    stream, length, boundary = stream_encode_multipart(
        values, use_temp file=False, boundary=boundary, charset=charset)
    return boundary, stream.read()
'''

def File(fd, filename=None, mimetype=None):
    """Backwards compat."""
    from warnings import warn
    warn(DeprecationWarning('werkzeug.test.File is deprecated, use the '
                            'EnvironBuilder or FileStorage instead'))
    return FileStorage(fd, filename=filename, content_type=mimetype)


class _TestCookieHeaders(object):
    """A headers adapter for cookielib
    """

    def __init__(self, headers):
        self.headers = headers

    def getheaders(self, name):
        headers = []
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                headers.append(v)
        return headers

    def get_all(self, name, default=None):
        rv = []
        for k, v in self.headers:
            if k.lower() == name.lower():
                rv.append(v)
        return rv or default or []


class _TestCookieResponse(object):
    """Something that looks like a httplib.HTTPResponse, but is actually just an
    adapter for our test responses to make them available for cookielib.
    """

    def __init__(self, headers):
        self.headers = _TestCookieHeaders(headers)

    def info(self):
        return self.headers


class _TestCookieJar(CookieJar):
    """A cookielib.CookieJar modified to inject and read cookie headers from
    and to wsgi environments, and wsgi application responses.
    """

    def inject_wsgi(self, environ):
        """Inject the cookies as client headers into the server's wsgi
        environment.
        """
        cvals = []
        for cookie in self:
            cvals.append('%s=%s' % (cookie.name, cookie.value))
        if cvals:
            environ['HTTP_COOKIE'] = '; '.join(cvals)

    def extract_wsgi(self, environ, headers):
        """Extract the server's set-cookie headers as cookies into the
        cookie jar.
        """
        self.extract_cookies(
            _TestCookieResponse(headers),
            U2Request(get_current_url(environ)),
        )


def _iter_data(data):
    """Iterates over a dict or multidict yielding all keys and values.
    This is used to iterate over the data passed to the
    :class:`EnvironBuilder`.
    """
    if isinstance(data, MultiDict):
        for key, values in iterlists(data):
            for value in values:
                yield key, value
    else:
        for key, values in iteritems(data):
            if isinstance(values, list):
                for value in values:
                    yield key, value
            else:
                yield key, values


class EnvironBuilder(object):
    """这个类为了测试可以方便的创建一个 WSGI 环境。他可以从任意数据快速创建 WSGI
    环境或请求对象。

    这个类的签名也可用于 Werkzeug 的其他地方(:func:`create_environ`, 
    :meth:`BaseResponse.from_values`, :meth:`Client.open`)。因为大多数功能只可通
    过构造函数实现。

    文件和表格数据可以被各自的 :attr:`form` 和 :attr:`files` 属性独立处理。但是以
    相同的参数传入构造函数:`data`。

    `data` 可以是这些值:

    -   a `str`: 如果一个字符串被转化为一个 :attr:`input_stream`，将会设置
        :attr:`content_length` ，你还要提供一个 :attr:`content_type`。
    -   a `dict`: 如果是一个字典，键将是一个字符串，值是以下对象:

        -   一个 :class:`file`-like 对象。他们会被自动转化成 :class:`FileStorage` 对象。
        -   一个元组。 :meth:`~FileMultiDict.add_file` 方法调用元组项目作为参数。

    .. versionadded:: 0.6
       `path` 和 `base_url` 现在是 unicode 字符串，它可以使用 :func:`iri_to_uri`
       函数编码。

    :param path: 请求的路径。在 WSGI 环境它等效于 `PATH_INFO`。如果 `query_string`
                没有被定义，这里有一个问题要注意，`path` 后面的将被当作 `query string`。
    :param base_url: base URL 是一个用于提取 WSGI URL ，主机 (服务器名 + 服务端口)
                    和根脚本的 (`SCRIPT_NAME`) 的 URL。
    :param query_string: URL 参数可选的字符串和字典。
    :param method: HTTP 方法，默认为 `GET`。
    :param input_stream: 一个可选输入流。不要指定它，一旦输入流被设定，你将不能
                        更改 :attr:`args` 属性和 :attr:`files` 属性除非你将
                        :attr:`input_stream` 重新设为 `None` 。
    :param content_type: 请求的内容类型。在0.5 版本当你指定文件和表格数据的时候
                        不必必须指定他。
    :param content_length: 请求的内容长度。当通过 `data` 提供数据不必必须指定他。
    :param errors_stream: 用于 `wsgi.errors` 可选的错误流。默认为 :data:`stderr`。
    :param multithread: 控制 `wsgi.multithread`。默认为 `False`。
    :param multiprocess: 控制 `wsgi.multiprocess`。默认为 `False`。
    :param run_once: 控制 `wsgi.run_once`。默认为 `False`。
    :param headers: headers 一个可选的列表或者 :class:`Headers` 对象。
    :param data: 一个字符串或者表单数据字典。看上边的 explanation。
    :param environ_base: 一个可选的默认环境。
    :param environ_overrides: 一个可选的覆盖环境。
    :param charset: 编码 unicode 数据的字符集。
    """

    #: 服务器使用协议。默认为 HTTP/1.1
    server_protocol = 'HTTP/1.1'

    #: 使用的 WSGI 版本。默认为(1, 0)。
    wsgi_version = (1, 0)

    #: 默认的请求类 :meth:`get_request`。
    request_class = BaseRequest

    def __init__(self, path='/', base_url=None, query_string=None,
                 method='GET', input_stream=None, content_type=None,
                 content_length=None, errors_stream=None, multithread=False,
                 multiprocess=False, run_once=False, headers=None, data=None,
                 environ_base=None, environ_overrides=None, charset='utf-8'):
        path_s = make_literal_wrapper(path)
        if query_string is None and path_s('?') in path:
            path, query_string = path.split(path_s('?'), 1)
        self.charset = charset
        self.path = iri_to_uri(path)
        if base_url is not None:
            base_url = url_fix(iri_to_uri(base_url, charset), charset)
        self.base_url = base_url
        if isinstance(query_string, (bytes, text_type)):
            self.query_string = query_string
        else:
            if query_string is None:
                query_string = MultiDict()
            elif not isinstance(query_string, MultiDict):
                query_string = MultiDict(query_string)
            self.args = query_string
        self.method = method
        if headers is None:
            headers = Headers()
        elif not isinstance(headers, Headers):
            headers = Headers(headers)
        self.headers = headers
        if content_type is not None:
            self.content_type = content_type
        if errors_stream is None:
            errors_stream = sys.stderr
        self.errors_stream = errors_stream
        self.multithread = multithread
        self.multiprocess = multiprocess
        self.run_once = run_once
        self.environ_base = environ_base
        self.environ_overrides = environ_overrides
        self.input_stream = input_stream
        self.content_length = content_length
        self.closed = False

        if data:
            if input_stream is not None:
                raise TypeError('can\'t provide input stream and data')
            if isinstance(data, text_type):
                data = data.encode(self.charset)
            if isinstance(data, bytes):
                self.input_stream = BytesIO(data)
                if self.content_length is None:
                    self.content_length = len(data)
            else:
                for key, value in _iter_data(data):
                    if isinstance(value, (tuple, dict)) or \
                       hasattr(value, 'read'):
                        self._add_file_from_data(key, value)
                    else:
                        self.form.setlistdefault(key).append(value)

    def _add_file_from_data(self,  key, value):
        """Called in the EnvironBuilder to add files from the data dict."""
        if isinstance(value, tuple):
            self.files.add_file(key, *value)
        elif isinstance(value, dict):
            from warnings import warn
            warn(DeprecationWarning('it\'s no longer possible to pass dicts '
                                    'as `data`.  Use tuples or FileStorage '
                                    'objects instead'), stacklevel=2)
            value = dict(value)
            mimetype = value.pop('mimetype', None)
            if mimetype is not None:
                value['content_type'] = mimetype
            self.files.add_file(key, **value)
        else:
            self.files.add_file(key, value)

    def _get_base_url(self): 
        return url_unparse((self.url_scheme, self.host,
                            self.script_root, '', '')).rstrip('/') + '/'

    def _set_base_url(self, value):
        if value is None:
            scheme = 'http'
            netloc = 'localhost'
            script_root = ''
        else:
            scheme, netloc, script_root, qs, anchor = url_parse(value)
            if qs or anchor:
                raise ValueError('base url must not contain a query string '
                                 'or fragment')
        self.script_root = script_root.rstrip('/')
        self.host = netloc
        self.url_scheme = scheme

    base_url = property(_get_base_url, _set_base_url, doc='''
        base URL 是一个用于提取 WSGI URL ，主机(服务器名 + 服务器端口) 和根脚本 
        (`SCRIPT_NAME`) 的 URL ''')
        
    del _get_base_url, _set_base_url

    def _get_content_type(self):
        ct = self.headers.get('Content-Type')
        if ct is None and not self._input_stream:
            if self.method in ('POST', 'PUT', 'PATCH'):
                if self._files:
                    return 'multipart/form-data'
                return 'application/x-www-form-urlencoded'
            return None
        return ct

    def _set_content_type(self, value):
        if value is None:
            self.headers.pop('Content-Type', None)
        else:
            self.headers['Content-Type'] = value

    content_type = property(_get_content_type, _set_content_type, doc='''
        请求的内容类型。反射给 :attr:`headers`。如果你设置了 :attr:`files` 和
        :attr:`form` 属性就不能设置内容类型。''')
    del _get_content_type, _set_content_type

    def  _get_content_length(self):
        return self.headers.get('Content-Length', type=int)

    def _set_content_length(self, value):
        if value is None:
            self.headers.pop('Content-Length', None)
        else:
            self.headers['Content-Length'] = str(value)

    content_length = property(_get_content_length, _set_content_length, doc='''
        整数的长度，反射给 :attr:`headers`。如果你设置了 :attr:`files` 或 :attr:`form`
        属性不要设置这个参数。''')
    del _get_content_length, _set_content_length

    def form_property(name, storage, doc):
        key = '_' + name
        def getter(self):
            if self._input_stream is not None:
                raise AttributeError('an input stream is defined')
            rv = getattr(self, key)
            if rv is None:
                rv = storage()
                setattr(self, key, rv)
            return rv
        def setter(self, value):
            self._input_stream = None
            setattr(self, key, value)
        return property(getter, setter, doc)

    form = form_property('form', MultiDict, doc='''
        A :class:`MultiDict` of form values.''')
    files = form_property('files', FileMultiDict, doc='''
        A :class:`FileMultiDict` of uploaded files.  You can use the
        :meth:`~FileMultiDict.add_file` method to add new files to the
        dict.''')
    del form_property

    def _get_input_stream(self):
        return self._input_stream

    def _set_input_stream(self, value):
        self._input_stream = value
        self._form = self._files = None

    input_stream = property(_get_input_stream, _set_input_stream, doc='''
        一个可选的输入流。如果你设置它，将会清空 :attr:`form` 和 :attr:`files`。''')
    del _get_input_stream, _set_input_stream

    def _get_query_string(self):
        if self._query_string is None:
            if self._args is not None:
                return url_encode(self._args, charset=self.charset)
            return ''
        return self._query_string

    def _set_query_string(self, value):
        self._query_string = value
        self._args = None

    query_string = property(_get_query_string, _set_query_string, doc='''
        查询字符串。如果你设置它， :attr:`args` 属性将不再可用。''')
    del _get_query_string, _set_query_string

    def _get_args(self):
        if self._query_string is not None:
            raise AttributeError('a query string is defined')
        if self._args is None:
            self._args = MultiDict()
        return self._args

    def _set_args(self, value):
        self._query_string = None
        self._args = value

    args = property(_get_args, _set_args, doc='''
        URL 参数是 :class:`MultiDict`。''') 
    del _get_args, _set_args

    @property
    def server_name(self):
        """服务器名 (只读， 使用 :attr:`host` 设置)"""
        return self.host.split(':', 1)[0] 

    @property
    def server_port(self):
        """整型服务器接口(只读，使用 :attr:`host` 设置)"""
        pieces = self.host.split(':', 1)
        if len(pieces) == 2 and pieces[1].isdigit():
            return int(pieces[1])
        elif self.url_scheme == 'https':
            return 443
        return 80

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self):
        """关闭所有文件。如果把 :class:`file` 对象放入 :attr:`files` 字典，你可
        以通过调用这个方法自动关闭他们。
        """
        if self.closed:
            return
        try:
            files = itervalues(self.files)
        except AttributeError:
            files = ()
        for f in files:
            try:
                f.close()
            except Exception:
                pass
        self.closed = True

    def get_environ(self):
        """返回内置环境。"""
        input_stream = self.input_stream
        content_length = self.content_length
        content_type = self.content_type

        if input_stream is not None:
            start_pos = input_stream.tell()
            input_stream.seek(0, 2)
            end_pos = input_stream.tell()
            input_stream.seek(start_pos)
            content_length = end_pos - start_pos
        elif content_type == 'multipart/form-data':
            values = CombinedMultiDict([self.form, self.files])
            input_stream, content_length, boundary = \
                stream_encode_multipart(values, charset=self.charset)
            content_type += '; boundary="%s"' % boundary
        elif content_type == 'application/x-www-form-urlencoded':
            #py2v3 review
            values = url_encode(self.form, charset=self.charset)
            values = values.encode('ascii')
            content_length = len(values)
            input_stream = BytesIO(values)
        else:
            input_stream = _empty_stream

        result = {}
        if self.environ_base:
            result.update(self.environ_base)

        def _path_encode(x):
            return wsgi_encoding_dance(url_unquote(x, self.charset), self.charset)

        qs = wsgi_encoding_dance(self.query_string)

        result.update({
            'REQUEST_METHOD':       self.method,
            'SCRIPT_NAME':          _path_encode(self.script_root),
            'PATH_INFO':            _path_encode(self.path),
            'QUERY_STRING':         qs,
            'SERVER_NAME':          self.server_name,
            'SERVER_PORT':          str(self.server_port),
            'HTTP_HOST':            self.host,
            'SERVER_PROTOCOL':      self.server_protocol,
            'CONTENT_TYPE':         content_type or '',
            'CONTENT_LENGTH':       str(content_length or '0'),
            'wsgi.version':         self.wsgi_version,
            'wsgi.url_scheme':      self.url_scheme,
            'wsgi.input':           input_stream,
            'wsgi.errors':          self.errors_stream,
            'wsgi.multithread':     self.multithread,
            'wsgi.multiprocess':    self.multiprocess,
            'wsgi.run_once':        self.run_once
        })
        for key, value in self.headers.to_wsgi_list():
            result['HTTP_%s' % key.upper().replace('-', '_')] = value
        if self.environ_overrides:
            result.update(self.environ_overrides)
        return result

    def get_request(self, cls=None):
        """返回一个带数据的请求。如果没有指定请求类，将会是用 :attr:`request_class`。

        :param cls: 使用 request 包装。
        """
        if cls is None:
            cls = self.request_class
        return cls(self.get_environ())


class ClientRedirectError(Exception):
    """
    If a redirect loop is detected when using follow_redirects=True with
    the :cls:`Client`, then this exception is raised.
    """


class Client(object):
    """这个类允许你发送请求给一个包裹的应用。

    响应可以是一个类或者一个有三个参数工厂函数: app_iter, status and headers。默
    认的响应仅仅是一个元组。

    例如::

        class ClientResponse(BaseResponse):
             ...

         client = Client(MyApplication(), response_wrapper=ClientResponse)

    use_cookies 参数默认是开启的，无论 cookies 是否被存储，他都会和请求一起传输。
    但是你也可以关闭 cookie。

    如果你想要请求应用的子域名，你可以设置 `allow_subdomain_redirects` 为 `True` ，
    如果为 `False` ,将不允许外部重定向。

    .. versionadded:: 0.5
       `use_cookies` 是在这个版本添加的。老版本不提供内置 cookie 支持。
    """

    def __init__(self, application, response_wrapper=None, use_cookies=True,
                 allow_subdomain_redirects=False):
        self.application = application
        self.response_wrapper = response_wrapper
        if use_cookies:
            self.cookie_jar = _TestCookieJar()
        else:
            self.cookie_jar = None
        self.allow_subdomain_redirects = allow_subdomain_redirects

    def set_cookie(self, server_name, key, value='', max_age=None,
                   expires=None, path='/', domain=None, secure=None,
                   httponly=False, charset='utf-8'):
        """Sets a cookie in the client's cookie jar.  The server name
        is required and has to match the one that is also passed to
        the open call.
        """
        assert self.cookie_jar is not None, 'cookies disabled'
        header = dump_cookie(key, value, max_age, expires, path, domain,
                             secure, httponly, charset)
        environ = create_environ(path, base_url='http://' + server_name)
        headers = [('Set-Cookie', header)]
        self.cookie_jar.extract_wsgi(environ, headers)

    def delete_cookie(self, server_name, key, path='/', domain=None):
        """Deletes a cookie in the test client."""
        self.set_cookie(server_name, key, expires=0, max_age=0,
                        path=path, domain=domain)

    def run_wsgi_app(self, environ, buffered=False):
        """Runs the wrapped WSGI app with the given environment."""
        if self.cookie_jar is not None:
            self.cookie_jar.inject_wsgi(environ)
        rv = run_wsgi_app(self.application, environ, buffered=buffered)
        if self.cookie_jar is not None:
            self.cookie_jar.extract_wsgi(environ, rv[2])
        return rv

    def resolve_redirect(self, response, new_location, environ, buffered=False):
        """Resolves a single redirect and triggers the request again
        directly on this redirect client.
        """
        scheme, netloc, script_root, qs, anchor = url_parse(new_location)
        base_url = url_unparse((scheme, netloc, '', '', '')).rstrip('/') + '/'

        cur_server_name = netloc.split(':', 1)[0].split('.')
        real_server_name = get_host(environ).rsplit(':', 1)[0].split('.')

        if self.allow_subdomain_redirects:
            allowed = cur_server_name[-len(real_server_name):] == real_server_name
        else:
            allowed = cur_server_name == real_server_name

        if not allowed:
            raise RuntimeError('%r does not support redirect to '
                               'external targets' % self.__class__)

        # For redirect handling we temporarily disable the response
        # wrapper.  This is not threadsafe but not a real concern
        # since the test client must not be shared anyways.
        old_response_wrapper = self.response_wrapper
        self.response_wrapper = None
        try:
            return self.open(path=script_root, base_url=base_url,
                             query_string=qs, as_tuple=True,
                             buffered=buffered)
        finally:
            self.response_wrapper = old_response_wrapper

    def open(self, *args, **kwargs):
        """和 :class:`EnvironBuilder` 一样的参数还有一些补充: 你可以提供一个
        :class:`EnvironBuilder` 类或一个 WSGI 环境代替 :class:`EnvironBuilder`
        类作为参数。同时有两个可选参数 (`as_tuple`, `buffered`)，可以改变返回值
        的类型或应用执行方法。

        .. versionchanged:: 0.5
           如果为 `data` 参数提供一个带文件的字典，那么内容类型必须为 `content_type`
           而不是 `mimetype`。这个改变是为了和 :class:`werkzeug.FileWrapper` 保
           持一致。

            `follow_redirects` 参数被添加到 :func:`open`.

        Additional parameters:

        :param as_tuple: 在表格中返回一个元组 ``(environ, result)``。
        :param buffered: 把这个设为 True 来缓冲区运行应用。这个将会为你自动关闭所有应用。
        :param follow_redirects: 如果接下来 `Client` HTTP 重定向，这个将会设为 True。
        """
        as_tuple = kwargs.pop('as_tuple', False)
        buffered = kwargs.pop('buffered', False)
        follow_redirects = kwargs.pop('follow_redirects', False)
        environ = None
        if not kwargs and len(args) == 1:
            if isinstance(args[0], EnvironBuilder):
                environ = args[0].get_environ()
            elif isinstance(args[0], dict):
                environ = args[0]
        if environ is None:
            builder = EnvironBuilder(*args, **kwargs)
            try:
                environ = builder.get_environ()
            finally:
                builder.close()

        response = self.run_wsgi_app(environ, buffered=buffered)

        # handle redirects
        redirect_chain = []
        while 1:
            status_code = int(response[1].split(None, 1)[0])
            if status_code not in (301, 302, 303, 305, 307) \
               or not follow_redirects:
                break
            new_location = response[2]['location']
            new_redirect_entry = (new_location, status_code)
            if new_redirect_entry in redirect_chain:
                raise ClientRedirectError('loop detected')
            redirect_chain.append(new_redirect_entry)
            environ, response = self.resolve_redirect(response, new_location,
                                                      environ, buffered=buffered)

        if self.response_wrapper is not None:
            response = self.response_wrapper(*response)
        if as_tuple:
            return environ, response
        return response


    def get(self, *args, **kw):
        """和 open 相似，但是方法强制执行 GET。"""
        kw['method'] = 'GET'
        return self.open(*args, **kw) 

    def patch(self, *args, **kw):
        """和 open 相似，但是方法强制执行 PATCH。"""
        kw['method'] = 'PATCH'
        return self.open(*args, **kw)
   
    def post(self, *args, **kw):
        """和 open 相似，但是方法强制执行 POST。"""
        kw['method'] = 'POST'
        return self.open(*args, **kw)

    def head(self, *args, **kw): 
        """和 open 相似，但是方法强制执行 HEAD。"""
        kw['method'] = 'HEAD'
        return self.open(*args, **kw)

    def put(self, *args, **kw):
        """和 open 相似，但是方法强制执行 PUT。"""
        kw['method'] = 'PUT'
        return self.open(*args, **kw)

    def delete(self, *args, **kw):
        """和 open 相似，但是方法强制执行 DELETE。"""
        kw['method'] = 'DELETE'
        return self.open(*args, **kw)

    def options(self, *args, **kw):
        """Like open but method is enforced to OPTIONS."""
        kw['method'] = 'OPTIONS'
        return self.open(*args, **kw)

    def trace(self, *args, **kw):
        """Like open but method is enforced to TRACE."""
        kw['method'] = 'TRACE'
        return self.open(*args, **kw)

    def __repr__(self):
        return '<%s %r>' % (
            self.__class__.__name__,
            self.application
        )


def create_environ(*args, **kwargs):
    """根据传入的值创建一个 WSGI 环境。第一个参数应该是请求的路径，默认为 '/'。
    另一个参数或者是一个绝对路径(在这个例子中主机是 localhost:80)或请求的完整
    路径，端口和脚本路径。

    它和 :class:`EnvironBuilder` 构造函数接受相同的参数。

    .. versionchanged:: 0.5
       这个函数现在是一个 :class:`EnvironBuilder` 包裹，在 0.5 版本被添加。需要 
       `headers`, `environ_base`, `environ_overrides` 和 `charset` 参数。
    """
    builder = EnvironBuilder(*args, **kwargs)
    try:
        return builder.get_environ()
    finally:
        builder.close()


def run_wsgi_app(app, environ, buffered=False):
    """返回一个应用输出的元组形式 (app_iter, status, headers)。如果你通过应用
    返回一个迭代器他将会工作的更好。

    有时应用可以使用 `start_ewsponse` 返回的 `write()` 回调函数。这将会自动解
    决边界情况。如果没有得到预期输出，你应该将 `buffered` 设为 `True` 执行
    buffering

    如果传入一个错误的应用，这个函数将会是未定义的。不要给这个函数传入一个不标准
    的 WSGI 应用。

    :param app: 要执行的应用。
    :param buffered: 设为 `True` 来执行 buffering.
    :return: 元组形式 ``(app_iter, status, headers)``
    """
    environ = _get_environ(environ)
    response = []
    buffer = []

    def start_response(status, headers, exc_info=None):
        if exc_info is not None:
            reraise(*exc_info)
        response[:] = [status, headers]
        return buffer.append

    app_iter = app(environ, start_response)

    # when buffering we emit the close call early and convert the
    # application iterator into a regular list
    if buffered:
        close_func = getattr(app_iter, 'close', None)
        try:
            app_iter = list(app_iter)
        finally:
            if close_func is not None:
                close_func()

    # otherwise we iterate the application iter until we have
    # a response, chain the already received data with the already
    # collected data and wrap it in a new `ClosingIterator` if
    # we have a close callable.
    else:
        while not response:
            buffer.append(next(app_iter))
        if buffer:
            close_func = getattr(app_iter, 'close', None)
            app_iter = chain(buffer, app_iter)
            if close_func is not None:
                app_iter = ClosingIterator(app_iter, close_func)

    return app_iter, response[0], Headers(response[1])

if __name__ == '__main__':
    main()
