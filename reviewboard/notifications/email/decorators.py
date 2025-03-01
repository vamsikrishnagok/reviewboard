"""E-mail message-related decorators."""

from functools import wraps

from django.conf import settings
from django.http.response import Http404, HttpResponse


def preview_email(email_builder):
    """Create a view to preview an e-mail message.

    The decorated function should accept the arguments a view would expect,
    i.e. an :py:class:`~django.http.HttpRequest` and the keyword arguments
    generated from the URL. It should return a :py:class:`dict` of keyword
    arguments to be passed to the ``email_builder`` function.

    However, if the decorated function returns a
    :py:class:`~django.http.HttpRequest` that response will be returned
    instead.

    The returned view will **always** return a :http:`404` when
    :django:setting:`DEBUG` is ``False`` (i.e., the view only works when Review
    Board is running as a development server.

    Args:
        email_builder (callable):
            A function that generates an
            :py:class:`~reviewboard.notifications.email.message.EmailMessage`
            from the keyword arguments passed into it.

            This function may also return ``None``, in which case a 404 Not
            Found will be returned.

    Returns:
        callable:
        A view that will generate a preview of the e-mail generated by the
        specified function.
    """
    def decorator(f):
        @wraps(f)
        def decorated(request, message_format, **kwargs):
            if not settings.DEBUG:
                raise Http404

            if message_format not in ('text', 'html'):
                raise Http404

            result = f(request, **kwargs)

            if isinstance(result, HttpResponse):
                return result

            message = email_builder(**result)

            if not message:
                raise Http404

            if message_format == 'text':
                content = message.body
                content_type = 'text/plain'
            else:
                for content, content_type in message.alternatives:
                    if content_type == 'text/html':
                        break
                else:
                    raise Http404

            content_type = '%s; charset=utf-8' % content_type
            return HttpResponse(content, content_type=content_type)

        return decorated

    return decorator
