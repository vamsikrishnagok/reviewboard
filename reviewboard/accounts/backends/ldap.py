"""LDAP authentication backend."""

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.utils.encoding import force_text
from django.utils.translation import ugettext_lazy as _

try:
    import ldap
    from ldap.filter import filter_format
except ImportError:
    ldap = None
    filter_format = None

from reviewboard.accounts.backends.base import BaseAuthBackend
from reviewboard.accounts.forms.auth import LDAPSettingsForm


logger = logging.getLogger(__name__)


class LDAPBackend(BaseAuthBackend):
    """Authentication backend for LDAP servers.

    This allows the use of LDAP servers for authenticating users in Review
    Board, and for importing individual users on-demand. It allows for a lot of
    customization in terms of how the LDAP server is queried, providing
    compatibility with most open source and commercial LDAP servers.

    The following Django settings are supported:

    ``LDAP_ANON_BIND_UID``:
        The full DN (distinguished name) of a user account with
        sufficient access to perform lookups of users and groups in the LDAP
        server. This is treated as a general or "anonymous" user for servers
        requiring authentication, and will not be otherwise imported into the
        Review Board server (unless attempting to log in with the same name).

        This can be unset if the LDAP server supports actual anonymous binds
        without a DN.

    ``LDAP_ANON_BIND_PASSWD``:
        The password used for the account specified in ``LDAP_ANON_BIND_UID``.

    ``LDAP_ANON_BIND_UID``:
        The full distinguished name of a user account with sufficient access
        to perform lookups of users and groups in the LDAP server. This can
        be unset if the LDAP server supports anonymous binds.

    ``LDAP_BASE_DN``:
        The base DN (distinguished name) used to perform LDAP searches.

    ``LDAP_EMAIL_ATTRIBUTE``:
        The attribute designating the e-mail address of a user in the
        directory. E-mail attributes are only used if this is set and if
        ``LDAP_EMAIL_DOMAIN`` is not set.

    ``LDAP_EMAIL_DOMAIN``:
        The domain name to use for e-mail addresses. If set, users imported
        from LDAP will have an e-mail address in the form of
        :samp:`{username}@{LDAP_EMAIL_DOMAIN}`. This takes priority over
        ``LDAP_EMAIL_ATTRIBUTE``.

    ``LDAP_GIVEN_NAME_ATTRIBUTE``:
        The attribute designating the given name (or first name) of a user
        in the directory. This defaults to ``givenName`` if not provided.

    ``LDAP_SURNAME_ATTRIBUTE``:
        The attribute designating the surname (or last name) of a user in the
        directory. This defaults to ``sn`` if not provided.

    ``LDAP_TLS``:
        Whether to use TLS to communicate with the LDAP server.

    ``LDAP_UID``:
        The attribute indicating a user's unique ID in the directory. This
        is used to compute a user lookup filter in the format of
        :samp:`({LDAP_UID}={username})`.

    ``LDAP_UID_MASK``:
        A mask defining a filter for looking up users. This must contain
        ``%s`` somewhere in the string, representing the username.
        For example: ``(something_special=%s)``.

    ``LDAP_URI``:
        The URI to the LDAP server to connect to for all communication.
    """

    backend_id = 'ldap'
    name = _('LDAP')
    settings_form = LDAPSettingsForm
    login_instructions = \
        _('Use your standard LDAP username and password.')

    def authenticate(self, request, username, password, **kwargs):
        """Authenticate a user.

        This will attempt to authenticate the user against the LDAP server.
        If the username and password are valid, a user will be returned, and
        added to the database if it doesn't already exist.

        Version Changed:
            4.0:
            The ``request`` argument is now mandatory as the first positional
            argument, as per requirements in Django.

        Args:
            request (django.http.HttpRequest):
                The HTTP request from the caller. This may be ``None``.

            username (unicode):
                The username used to authenticate.

            password (unicode):
                The password used to authenticate.

            **kwargs (dict, unused):
                Additional keyword arguments supplied by the caller.

        Returns:
            django.contrib.auth.models.User:
            The authenticated user, or ``None`` if the user could not be
            authenticated for any reason.
        """
        username = username.strip()

        if not password:
            # Don't try to bind using an empty password; the server will
            # return success, which doesn't mean we have authenticated.
            # http://tools.ietf.org/html/rfc4513#section-5.1.2
            # http://tools.ietf.org/html/rfc4513#section-6.3.1
            logger.warning('Attempted to authenticate "%s" with an empty '
                           'password against LDAP.',
                           username,
                           request=request)
            return None

        ldapo = self._connect()

        if ldapo is None:
            return None

        userdn = self._get_user_dn(ldapo, username)

        try:
            # Now that we have the user, attempt to bind to verify
            # authentication.
            logger.debug('Attempting to authenticate user DN "%s" '
                         '(username %s) in LDAP',
                         userdn, username,
                         request=request)
            ldapo.bind_s(userdn, password)

            return self.get_or_create_user(username=username,
                                           ldapo=ldapo,
                                           userdn=userdn,
                                           request=request)
        except ldap.INVALID_CREDENTIALS:
            logger.warning('Error authenticating user "%s": The credentials '
                           'provided were invalid',
                           username,
                           request=request)
        except ldap.LDAPError as e:
            logger.warning('Error authenticating user "%s": %s',
                           username, e,
                           request=request)
        except Exception as e:
            logger.exception('Unexpected error authenticating user "%s": %s',
                             username, e,
                             request=request)

        return None

    def get_or_create_user(self, username, request=None, ldapo=None,
                           userdn=None):
        """Return a user account, importing from LDAP if necessary.

        If the user already exists in the database, it will be returned
        directly. Otherwise, this will attempt to look up the user in LDAP
        and create a local user account representing that user.

        Args:
            username (unicode):
                The username to look up.

            request (django.http.HttpRequest, optional):
                The optional HTTP request for this operation.

            ldapo (ldap.LDAPObject, optional):
                The existing LDAP connection, if the caller has one. If not
                provided, a new connection will be created.

            userdn (unicode, optional):
                The DN for the user being looked up, if the caller knows it.
                If not provided, the DN will be looked up.

        Returns:
            django.contrib.auth.models.User:
            The resulting user, if it could be found either locally or in
            LDAP. If the user does not exist, ``None`` is returned.
        """
        username = self.INVALID_USERNAME_CHAR_REGEX.sub('', username).lower()

        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            # The user wasn't in the database, so we'll look it up in
            # LDAP below.
            pass

        if ldap is None:
            logger.error('Attempted to look up user "%s" in LDAP, but the '
                         'python-ldap package is not installed! Please '
                         '`pip install ReviewBoard[ldap]`.',
                         username,
                         request=request)
            return None

        try:
            if ldapo is None:
                ldapo = self._connect(request=request)

                if ldapo is None:
                    return None

            if userdn is None:
                userdn = self._get_user_dn(ldapo=ldapo,
                                           username=username,
                                           request=request)

                if userdn is None:
                    return None

            # Perform a BASE search since we already know the DN of
            # the user
            search_result = ldapo.search_s(userdn, ldap.SCOPE_BASE)
            user_info = search_result[0][1]

            given_name_attr = getattr(settings, 'LDAP_GIVEN_NAME_ATTRIBUTE',
                                      'givenName')
            first_name = force_text(
                user_info.get(given_name_attr, [username])[0])

            surname_attr = getattr(settings, 'LDAP_SURNAME_ATTRIBUTE', 'sn')
            last_name = force_text(user_info.get(surname_attr, [''])[0])

            # If a single ldap attribute is used to hold the full name of
            # a user, split it into two parts.  Where to split was a coin
            # toss and I went with a left split for the first name and
            # dumped the remainder into the last name field.  The system
            # admin can handle the corner cases.
            #
            # If the full name has no white space to split on, then the
            # entire full name is dumped into the first name and the
            # last name becomes an empty string.
            try:
                if settings.LDAP_FULL_NAME_ATTRIBUTE:
                    full_name = force_text(
                        user_info[settings.LDAP_FULL_NAME_ATTRIBUTE][0])

                    try:
                        first_name, last_name = full_name.split(' ', 1)
                    except ValueError:
                        first_name = full_name
                        last_name = ''
            except AttributeError:
                pass

            if settings.LDAP_EMAIL_DOMAIN:
                email = '%s@%s' % (username, settings.LDAP_EMAIL_DOMAIN)
            elif settings.LDAP_EMAIL_ATTRIBUTE:
                try:
                    email = force_text(
                        user_info[settings.LDAP_EMAIL_ATTRIBUTE][0])
                except KeyError:
                    logger.error('Could not find the e-mail address for '
                                 'user "%s" using attribute "%s"',
                                 username, settings.LDAP_EMAIL_ATTRIBUTE,
                                 request=request)
                    email = ''
            else:
                logger.warning('E-mail address for user "%s" is not specified',
                               username)
                email = ''

            user = User(username=username,
                        password='',
                        first_name=first_name,
                        last_name=last_name,
                        email=email)
            user.set_unusable_password()
            user.save()

            return user
        except ldap.NO_SUCH_OBJECT as e:
            logger.warning('Unable to locate the user "%s" using user DN '
                           '"%s" on base ' 'DN "%s": %s',
                           username,
                           userdn,
                           settings.LDAP_BASE_DN,
                           e,
                           exc_info=1,
                           request=request)
        except ldap.LDAPError as e:
            logger.warning('Unexpected LDAP error when locating user "%s": %s',
                           username,
                           e,
                           exc_info=1,
                           request=request)

        return None

    def _connect(self, request=None):
        """Connect to LDAP.

        This will attempt to connect and authenticate (if needed) to the
        configured LDAP server.

        Args:
            request (django.http.HttpRequest, optional):
                The optional HTTP request used for logging context.

        Returns:
            ldap.LDAPObject:
            The resulting LDAP connection, if it could connect. If LDAP
            support isn't available, or there was an error, this will return
            ``None``.
        """
        if ldap is None:
            return None

        try:
            ldapo = ldap.initialize(settings.LDAP_URI,
                                    bytes_mode=False)
            ldapo.set_option(ldap.OPT_REFERRALS, 0)
            ldapo.set_option(ldap.OPT_PROTOCOL_VERSION, 3)

            if settings.LDAP_TLS:
                ldapo.start_tls_s()

            if settings.LDAP_ANON_BIND_UID:
                # Log in as the service account before searching.
                ldapo.simple_bind_s(settings.LDAP_ANON_BIND_UID,
                                    settings.LDAP_ANON_BIND_PASSWD)
            else:
                # Bind anonymously to the server.
                ldapo.simple_bind_s()

            return ldapo
        except ldap.INVALID_CREDENTIALS:
            if settings.LDAP_ANON_BIND_UID:
                logger.warning('Error authenticating with LDAP: The '
                               'credentials provided for "%s" were invalid.',
                               settings.LDAP_ANON_BIND_UID,
                               request=request)
            else:
                logger.warning('Error authenticating with LDAP: Anonymous '
                               'access to this server is not permitted.',
                               request=request)
        except ldap.LDAPError as e:
            logger.warning('Error authenticating with LDAP: %s',
                           e,
                           request=request)
        except Exception as e:
            logger.exception('Unexpected error occurred while authenticating '
                             'with LDAP: %s',
                             e,
                             request=request)

        return None

    def _get_user_dn(self, ldapo, username, request=None):
        """Return the DN for a given username.

        This will perform a lookup in LDAP to try to find a DN for a given
        username, which can be used in subsequent lookups and for
        authentication.

        Args:
            ldapo (ldap.LDAPObject):
                The LDAP connection.

            username (unicode):
                The username to look up in the directory.

            request (django.http.HttpRequest, optional):
                The optional HTTP request used for logging context.

        Returns:
            unicode:
            The DN for the username, if found. If not found, this will return
            ``None``.
        """
        assert ldapo is not None

        try:
            # If the UID mask has been explicitly set, use it instead of
            # computing a search filter.
            if settings.LDAP_UID_MASK:
                uid_filter = filter_format(settings.LDAP_UID_MASK, [username])
            else:
                uid_filter = filter_format('(%s=%s)',
                                           [settings.LDAP_UID, username])

            # Search for the user with the given base DN and uid. If the user
            # is found, a fully qualified DN is returned.
            search = ldapo.search_s(settings.LDAP_BASE_DN,
                                    ldap.SCOPE_SUBTREE,
                                    uid_filter)

            if search:
                return search[0][0]

            logger.warning('LDAP error: The specified object does '
                           'not exist in the Directory: %s',
                           username,
                           request=request)
        except ldap.LDAPError as e:
            logger.warning('Error authenticating user "%s" in LDAP: %s',
                           username, e,
                           request=request)
        except Exception as e:
            logger.exception('Unexpected error authenticating user "%s" '
                             'in LDAP: %s',
                             username, e,
                             request=request)

        return None
