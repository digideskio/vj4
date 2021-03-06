import accept
import asyncio
import calendar
import functools
import hmac
import logging
import markupsafe
import sockjs
import pytz
from aiohttp import web
from email import utils
from vj4 import app
from vj4 import error
from vj4 import template
from vj4.controller import setting
from vj4.model import builtin
from vj4.model import domain
from vj4.model import token
from vj4.model import user
from vj4.util import json
from vj4.util import locale
from vj4.util import options

_logger = logging.getLogger(__name__)

class ViewBase(setting.SettingMixin):
  NAME = None
  TITLE = None

  async def prepare(self):
    # TODO(iceboy): parallelize: (session -> user) | domain.
    self.session = await self.update_session()
    if self.session and 'uid' in self.session:
      self.user = await user.get_by_uid(self.session['uid']) or builtin.USER_GUEST
    else:
      self.user = builtin.USER_GUEST
    # TODO(iceboy): use user timezone.
    self.locale_context = _get_locale_context(self.get_setting('view_lang'), 'Asia/Shanghai')
    self.domain_id = self.request.match_info.pop('domain_id', builtin.DOMAIN_ID_SYSTEM)
    self.domain_context = self.get_domain_context(self.domain_id)
    self.reverse_url = self.domain_context['reverse_url']
    self.build_path = self.domain_context['build_path']
    self.domain = await domain.get(self.domain_id)
    if not self.domain:
      raise error.DomainNotFoundError(self.domain_id)

  @classmethod
  @functools.lru_cache()
  def get_domain_context(cls, domain_id):
    return {'domain_id': domain_id,
            'page_name': cls.NAME,
            'reverse_url': functools.partial(_reverse_url, domain_id=domain_id),
            'build_path': functools.partial(_build_path, domain_id=domain_id),
            'path_components': _build_path((cls.NAME, None), domain_id=domain_id)}

  def has_perm(self, perm):
    role = self.user['roles'].get(self.domain_id, builtin.ROLE_DEFAULT)
    mask = self.domain['roles'].get(role, builtin.PERM_NONE)
    return (perm & mask) == perm or self.domain['owner_uid'] == self.user['_id']

  def check_perm(self, perm):
    if not self.has_perm(perm):
      raise error.PermissionError(perm)

  def has_priv(self, priv):
    return (priv & self.user['priv']) == priv

  def check_priv(self, priv):
    if not self.has_priv(priv):
      raise error.PrivilegeError(priv)

  async def update_session(self, *, new_saved=False, **kwargs):
    """Update or create session if necessary.

    If 'sid' in cookie, the 'expire_at' field is updated.
    If 'sid' not in cookie, only create when there is extra data.

    Args:
      new_saved: use saved session on creation.
      kwargs: extra data.

    Returns:
      The session document.
    """
    (sid, save), session = map(self.request.cookies.get, ['sid', 'save']), None
    if not sid:
      save = new_saved
    if save:
      token_type = token.TYPE_SAVED_SESSION
      session_expire_seconds = options.options.saved_session_expire_seconds
    else:
      token_type = token.TYPE_UNSAVED_SESSION
      session_expire_seconds = options.options.unsaved_session_expire_seconds
    if sid:
      session = await token.update(sid, token_type, session_expire_seconds,
                                   **{**kwargs,
                                      'update_ip': self.remote_ip,
                                      'update_ua': self.request.headers.get('User-Agent')})
    if kwargs and not session:
      sid, session = await token.add(token_type, session_expire_seconds,
                                     **{**kwargs,
                                        'create_ip': self.remote_ip,
                                        'create_ua': self.request.headers.get('User-Agent')})
    if session:
      cookie_kwargs = {'domain': options.options.cookie_domain,
                       'secure': options.options.cookie_secure,
                       'httponly': True}
      if save:
        timestamp = calendar.timegm(session['expire_at'].utctimetuple())
        cookie_kwargs['expires'] = utils.formatdate(timestamp, usegmt=True)
        cookie_kwargs['max_age'] = session_expire_seconds
        self.response.set_cookie('save', '1', **cookie_kwargs)
      self.response.set_cookie('sid', sid, **cookie_kwargs)
    else:
      self.clear_cookies('sid', 'save')
    return session or {}

  async def delete_session(self):
    sid, save = map(self.request.cookies.get, ['sid', 'save'])
    if sid:
      if save:
        token_type = token.TYPE_SAVED_SESSION
      else:
        token_type = token.TYPE_UNSAVED_SESSION
      await token.delete(sid, token_type)
    self.clear_cookies('sid', 'save')

  def clear_cookies(self, *names):
    for name in names:
      if name in self.request.cookies:
        self.response.set_cookie(name, '',
                                 expires=utils.formatdate(0, usegmt=True),
                                 domain=options.options.cookie_domain,
                                 secure=options.options.cookie_secure,
                                 httponly=True)

  @property
  def remote_ip(self):
    if options.options.ip_header:
      return self.request.headers.get(options.options.ip_header)
    else:
      return self.request.transport.get_extra_info('peername')[0]

  @property
  def csrf_token(self):
    if self.session:
      return _get_csrf_token(self.session['_id'])
    else:
      return ''

class View(web.View, ViewBase):
  @asyncio.coroutine
  def __iter__(self):
    try:
      self.response = web.Response()
      yield from ViewBase.prepare(self)
      yield from super(View, self).__iter__()
    except error.UserFacingError as e:
      _logger.warning("User facing error: %s", repr(e))
      self.response.set_status(e.http_status, None)
      if self.prefer_json:
        self.response.content_type = 'application/json'
        self.response.text = json.encode({'error': e.to_dict()})
      else:
        self.render(e.template_name, error=e, path_components=self.build_path(('error', None)),
                    page_name='error', page_title=self.locale_context['_']('error'))
    return self.response

  def render(self, template_name, **kwargs):
    self.response.content_type = 'text/html'
    self.response.text = self.render_html(template_name, **kwargs)

  def render_html(self, template_name, **kwargs):
    return template.Environment().get_template(template_name).render(
        {'view': self, **self.domain_context, **self.locale_context, **kwargs})

  def json(self, obj):
    self.response.content_type = 'application/json'
    self.response.text = json.encode(obj)

  @property
  def prefer_json(self):
    for d in accept.parse(self.request.headers.get('Accept')):
      if d.media_type == 'application/json':
        return True
      elif d.media_type == 'text/html' or d.all_types:
        return False
    return False

  @property
  def referer_or_main(self):
    return self.request.headers.get('referer', self.reverse_url('main'))

  def redirect(self, redirect_url):
    self.response.set_status(web.HTTPFound.status_code, None)
    self.response.headers['Location'] = redirect_url

  def json_or_redirect(self, redirect_url, **kwargs):
    if self.prefer_json:
      self.response.content_type = 'application/json'
      self.response.text = json.encode(kwargs)
    else:
      self.redirect(redirect_url)

  @property
  def ui_context(self):
    return {'csrf_token': self.csrf_token,
            'cdn_prefix': options.options.cdn_prefix,
            'url_prefix': options.options.url_prefix}

class OperationView(View):
  async def post(self):
    arguments = (await self.request.post()).copy()
    operation = arguments.pop('operation')
    try:
      method = getattr(self, 'post_' + operation)
    except AttributeError:
      raise error.InvalidOperationError(operation) from None
    await method(**arguments)

class Connection(sockjs.Session, ViewBase):
  def __init__(self, request, *args, **kwargs):
    super(Connection, self).__init__(*args, **kwargs)
    self.request = request
    self.response = web.Response()  # dummy response

  async def on_open(self):
    pass

  async def on_message(self, **kwargs):
    pass

  async def on_close(self):
    pass

  def send(self, **kwargs):
    super(Connection, self).send(json.encode(kwargs))

  def render_html(self, template_name, **kwargs):
    return template.Environment().get_template(template_name).render(
        {**self.domain_context, **self.locale_context, **kwargs})

@functools.lru_cache()
def _get_csrf_token(session_id_binary):
  return hmac.new(b'csrf_token', session_id_binary, 'sha256').hexdigest()

@functools.lru_cache()
def _reverse_url(name, *, domain_id, **kwargs):
  if domain_id != builtin.DOMAIN_ID_SYSTEM:
    name += '_with_domain_id'
    kwargs['domain_id'] = domain_id
  if kwargs:
    return app.Application().router[name].url(parts=kwargs)
  else:
    return app.Application().router[name].url()

@functools.lru_cache()
def _build_path(*args, domain_id):
  return [(domain_id, _reverse_url('main', domain_id=domain_id)), *args]

@functools.lru_cache()
def _get_locale_context(locale_name, tzname):
  translate = locale.get_translate(locale_name)
  tz = pytz.timezone('Asia/Shanghai')
  datetime_full = translate('%Y-%m-%d %H:%M:%S')

  @functools.lru_cache()
  def _datetime_span(dt):
    if not dt.tzinfo:
      dt = dt.replace(tzinfo=pytz.utc)
    # TODO(iceboy): add a class for javascript selection.
    return markupsafe.Markup(
        '<span data-timestamp="{0}">{1}</span>'.format(
            int(dt.astimezone(pytz.utc).timestamp()),
            dt.astimezone(tz).strftime(datetime_full)))

  return {'_': translate, 'datetime_span': _datetime_span}

# Decorators
def require_perm(perm):
  def decorate(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
      self.check_perm(perm)
      return func(self, *args, **kwargs)
    return wrapper
  return decorate

def require_priv(priv):
  def decorate(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
      self.check_priv(priv)
      return func(self, *args, **kwargs)
    return wrapper
  return decorate

def require_csrf_token(func):
  @functools.wraps(func)
  def wrapper(self, *args, **kwargs):
    if self.csrf_token and self.csrf_token != kwargs.pop('csrf_token', ''):
      raise error.CsrfTokenError()
    return func(self, *args, **kwargs)
  return wrapper

def route_argument(func):
  @functools.wraps(func)
  def wrapped(self, **kwargs):
    return func(self, **kwargs, **self.request.match_info)
  return wrapped

def get_argument(func):
  @functools.wraps(func)
  def wrapped(self, **kwargs):
    return func(self, **kwargs, **self.request.GET)
  return wrapped

def post_argument(coro):
  @functools.wraps(coro)
  async def wrapped(self, **kwargs):
    return await coro(self, **kwargs, **await self.request.post())
  return wrapped

def sanitize(func):
  @functools.wraps(func)
  def wrapped(self, **kwargs):
    for key, value in kwargs.items():
      kwargs[key] = func.__annotations__[key](value)
    return func(self, **kwargs)
  return wrapped
