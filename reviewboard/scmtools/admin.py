from django.conf.urls import include, url
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import get_object_or_404
from django.utils.html import format_html
from django.utils.translation import ugettext_lazy as _
from djblets.util.compat.django.shortcuts import render

from reviewboard.accounts.admin import fix_review_counts
from reviewboard.admin import ModelAdmin, admin_site
from reviewboard.admin.server import get_server_url
from reviewboard.scmtools.forms import RepositoryForm
from reviewboard.scmtools.models import Repository, Tool


class RepositoryAdmin(ModelAdmin):
    list_display = ('name', 'path', 'hosting', '_visible', 'inline_actions')
    list_select_related = ('hosting_account',)
    search_fields = ('name', 'path', 'mirror_path', 'tool__name')
    raw_id_fields = ('local_site',)
    ordering = ('name',)
    fieldsets = (
        (_('General Information'), {
            'fields': ('name', 'visible',),
            'classes': ('wide',),
        }),
        (RepositoryForm.REPOSITORY_HOSTING_FIELDSET, {
            'fields': (
                'hosting_type',
                'hosting_account',
                'force_authorize',
            ),
            'classes': ('wide',),
        }),
        (RepositoryForm.REPOSITORY_INFO_FIELDSET, {
            'fields': (
                'tool',
                'repository_plan',
            ),
            'classes': ('wide',),
        }),
        (RepositoryForm.SSH_KEY_FIELDSET, {
            'fields': (
                'associate_ssh_key',
            ),
            'classes': ('wide',),
        }),
        (RepositoryForm.BUG_TRACKER_FIELDSET, {
            'fields': (
                'bug_tracker_use_hosting',
                'bug_tracker_type',
                'bug_tracker_hosting_url',
                'bug_tracker_plan',
                'bug_tracker_hosting_account_username',
                'bug_tracker',
            ),
            'classes': ('wide',),
        }),
        (_('Access Control'), {
            'fields': ('public', 'users', 'review_groups'),
            'classes': ('wide',),
        }),
        (_('Advanced Settings'), {
            'fields': ('encoding',),
            'classes': ('wide', 'collapse'),
        }),
        (_('Internal State'), {
            'description': _('<p>This is advanced state that should not be '
                             'modified unless something is wrong.</p>'),
            'fields': ('local_site', 'hooks_uuid', 'extra_data'),
            'classes': ['collapse'],
        }),
    )
    form = RepositoryForm

    fieldset_template_name = 'admin/scmtools/repository/_fieldset.html'

    def hosting(self, repository):
        if repository.hosting_account_id:
            account = repository.hosting_account

            if account.service:
                return '%s@%s' % (account.username, account.service.name)

        return ''
    hosting.short_description = _('Hosting Service Account')

    def inline_actions(self, repository):
        """Return HTML containing actions to show for each repository.

        Args:
            repository (reviewboard.scmtools.models.Repository):
                The repository to return actions for.

        Returns:
            unicode:
            The resulting HTML.
        """
        s = ['<div class="rb-c-admin-change-list__item-actions">']

        def _build_item(url, css_class, name):
            return format_html(
                '<a class="rb-c-admin-change-list__item-action {0}"'
                ' href="{1}">{2}</a>',
                css_class, url, name)

        if repository.hosting_account:
            service = repository.hosting_account.service

            if service and service.has_repository_hook_instructions:
                s.append(_build_item(
                    url='%s/hooks-setup/' % repository.pk,
                    css_class='action-hooks-setup',
                    name=_('Hooks')))

        s.append(_build_item(
            url='%s/rbtools-setup/' % repository.pk,
            css_class='action-rbtools-setup',
            name=_('RBTools Setup')))

        s.append('</div>')

        return ''.join(s)
    inline_actions.allow_tags = True
    inline_actions.short_description = ''

    def _visible(self, repository):
        return repository.visible
    _visible.boolean = True
    _visible.short_description = _('Show')

    def get_urls(self):
        return [
            url(r'^(?P<repository_id>\d+)/', include([
                url(r'^hooks-setup/$',
                    self.admin_site.admin_view(self.hooks_setup)),
                url(r'^rbtools-setup/$',
                    self.admin_site.admin_view(self.rbtools_setup)),
            ])),
        ] + super(RepositoryAdmin, self).get_urls()

    def hooks_setup(self, request, repository_id):
        repository = get_object_or_404(Repository, pk=repository_id)

        if repository.hosting_account:
            service = repository.hosting_account.service

            if service and service.has_repository_hook_instructions:
                return HttpResponse(service.get_repository_hook_instructions(
                    request, repository))

        return HttpResponseNotFound()

    def rbtools_setup(self, request, repository_id):
        repository = get_object_or_404(Repository, pk=repository_id)

        return render(
            request=request,
            template_name='admin/scmtools/repository/rbtools_setup.html',
            context={
                'repository': repository,
                'reviewboard_url': get_server_url(
                    local_site=repository.local_site),
            })


@receiver(pre_delete, sender=Repository,
          dispatch_uid='repository_delete_reset_review_counts')
def repository_delete_reset_review_counts(sender, instance, using, **kwargs):
    """Reset review counts in the dashboard when deleting repository objects.

    There doesn't seem to be a good way to get notified on cascaded delete
    operations, which means that when deleting a repository, there's no
    good way to update the review counts that are shown to users. This
    method clears them out entirely to be regenerated. Deleting
    repositories should be a very rare occurrance, so it's not too
    upsetting to do this.
    """
    fix_review_counts()


class ToolAdmin(ModelAdmin):
    list_display = ('__str__', 'class_name')


admin_site.register(Repository, RepositoryAdmin)
admin_site.register(Tool, ToolAdmin)
