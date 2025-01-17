import os

import paramiko
from django.utils import six
from django.utils.encoding import force_str

from reviewboard.ssh.client import SSHClient
from reviewboard.ssh.errors import (BadHostKeyError, SSHAuthenticationError,
                                    SSHError, SSHInvalidPortError)
from reviewboard.ssh.policy import RaiseUnknownHostKeyPolicy


SSH_PORT = 22


try:
    import urlparse
    uses_netloc = urlparse.uses_netloc
    urllib_parse = urlparse.urlparse
except ImportError:
    import urllib.parse
    uses_netloc = urllib.parse.uses_netloc
    urllib_parse = urllib.parse.urlparse


# A list of known SSH URL schemes.
ssh_uri_schemes = ["ssh", "sftp"]
uses_netloc.extend(ssh_uri_schemes)


def humanize_key(key):
    """Return a human-readable key as a series of hex characters.

    Each byte of the key will be converted into a 2-byte hex representation
    in the form of ``XX:XX:XX:XX...``.

    Args:
        key (paramiko.PKey):
            The key to humanize.

    Returns:
        unicode:
        The human-readable key.
    """
    fingerprint = key.get_fingerprint()

    if six.PY3:
        # On Python 3, iterating through the byte string will give us integers.
        # No need for ord().
        values = fingerprint
    else:
        # On Python 2, iterating through the byte string will give us
        # characters, so we'll need to convert to integers.
        values = (
            ord(c)
            for c in fingerprint
        )

    return ':'.join(
        '%02x' % i
        for i in values
    )


def is_ssh_uri(url):
    """Returns whether or not a URL represents an SSH connection."""
    return urllib_parse(url)[0] in ssh_uri_schemes


def check_host(netloc, username=None, password=None, namespace=None):
    """
    Checks if we can connect to a host with a known key.

    This will raise an exception if we cannot connect to the host. The
    exception will be one of BadHostKeyError, UnknownHostKeyError, or
    SCMError.
    """
    from django.conf import settings

    client = SSHClient(namespace=namespace)
    client.set_missing_host_key_policy(RaiseUnknownHostKeyPolicy())

    kwargs = {}

    if ':' in netloc:
        hostname, port = netloc.split(':')

        try:
            port = int(port)
        except ValueError:
            raise SSHInvalidPortError(port)
    else:
        hostname = netloc
        port = SSH_PORT

    # We normally want to notify on unknown host keys, but not when running
    # unit tests.
    if getattr(settings, 'RUNNING_TEST', False):
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
        kwargs['allow_agent'] = False

    try:
        client.connect(hostname, port, username=username, password=password,
                       pkey=client.get_user_key(), **kwargs)
    except paramiko.BadHostKeyException as e:
        raise BadHostKeyError(e.hostname, e.key, e.expected_key)
    except paramiko.AuthenticationException as e:
        # Some AuthenticationException instances have allowed_types set,
        # and some don't.
        allowed_types = getattr(e, 'allowed_types', [])

        if 'publickey' in allowed_types:
            key = client.get_user_key()
        else:
            key = None

        raise SSHAuthenticationError(allowed_types=allowed_types, user_key=key)
    except paramiko.SSHException as e:
        msg = str(e)
        if msg == 'No authentication methods available':
            raise SSHAuthenticationError
        else:
            raise SSHError(msg)


def register_rbssh(envvar):
    """Register rbssh in an environment variable.

    This is a convenience method for making sure that rbssh is set properly
    in the environment for different tools. In some cases, we need to
    specifically place it in the system environment using ``os.putenv``,
    while in others (Mercurial, Bazaar), we need to place it in ``os.environ``.

    Args:
        envvar (unicode or bytes):
            The name of the environment variable.
    """
    envvar = force_str(envvar)

    os.putenv(envvar, str('rbssh'))
    os.environ[envvar] = str('rbssh')
