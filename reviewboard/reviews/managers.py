import logging

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db import connections, router, transaction
from django.db.models import Manager, Q
from django.db.models.query import QuerySet
from djblets.db.managers import ConcurrencyManager

from reviewboard.diffviewer.models import DiffSetHistory
from reviewboard.scmtools.errors import ChangeNumberInUseError
from reviewboard.scmtools.models import Repository


logger = logging.getLogger(__name__)


class DefaultReviewerManager(Manager):
    """A manager for DefaultReviewer models."""

    def for_repository(self, repository, local_site):
        """Returns all DefaultReviewers that represent a repository.

        These include both DefaultReviewers that have no repositories
        (for backwards-compatibility) and DefaultReviewers that are
        associated with the given repository.
        """
        return self.filter(local_site=local_site).filter(
            Q(repository__isnull=True) | Q(repository=repository))

    def can_create(self, user, local_site=None):
        """Returns whether the user can create default reviewers."""
        return (user.is_superuser or
                (local_site and local_site.is_mutable_by(user)))


class ReviewGroupManager(Manager):
    """A manager for Group models."""

    def accessible(self, user, visible_only=True, local_site=None,
                   show_all_local_sites=False, distinct=True):
        """Return a queryset for review groups accessible by the given user.

        For superusers, all public and invite-only review groups will be
        returned.

        For regular users, only review groups that are public or that the
        user is on the access list for will be returned.

        For anonymous users, only public review groups will be returned.

        The returned list is further filtered down based on the
        ``visible_only``, ``local_site``, and ``show_all_local_sites``
        parameters.

        Version Changed:
            3.0.24:
            Added the ``distinct`` parameter.

        Args:
            user (django.contrib.auth.models.User):
                The user that must have access to any returned groups.

            visible_only (bool, optional):
                Whether only visible review groups should be returned.

            local_site (reviewboard.site.models.LocalSite, optional):
                A specific :term:`Local Site` that the groups must be
                associated with. By default, this will only return groups
                not part of a site.

            show_all_local_sites (bool, optional):
                Whether review groups for all :term:`Local Sites` should be
                returned. This cannot be ``True`` if a ``local_site`` argument
                was provided.

            distinct (bool, optional):
                Whether to return distinct results.

                Turning this off can increase performance. It's on by default
                for backwards-compatibility.

        Returns:
            django.db.models.query.QuerySet:
            The resulting queryset.
        """
        if user.is_superuser:
            qs = self.all()

            if visible_only:
                qs = qs.filter(visible=True)
        else:
            q = Q()

            if not user.has_perm('reviews.can_view_invite_only_groups',
                                 local_site):
                q = Q(invite_only=False)

            if visible_only:
                # We allow accessible() to return hidden groups if the user is
                # a member, so we must perform this check here.
                q &= Q(visible=True)

            if user.is_authenticated:
                q |= Q(users=user.pk)

            qs = self.filter(q)

        if show_all_local_sites:
            assert local_site is None
        else:
            qs = qs.filter(local_site=local_site)

        if distinct:
            qs = qs.distinct()

        return qs

    def accessible_ids(self, *args, **kwargs):
        """Return IDs of groups that are accessible by the given user.

        This wraps :py:meth:`accessible` and takes the same arguments
        (with the exception of ``distinct``, which is ignored).

        Version Changed:
            3.0.24:
            In prior versions, the order was not specified, but was
            generally numeric order. This should still be true, but
            officially, we no longer guarantee any order of results.

        Args:
            *args (tuple):
                Positional arguments to pass to :py:meth:`accessible`.

            **kwargs (dict):
                Keyword arguments to pass to :py:meth:`accessible`.

        Returns:
            list of int:
            The list of IDs.
        """
        kwargs['distinct'] = False

        return list(sorted(set(
            self.accessible(*args, **kwargs)
            .values_list('pk', flat=True)
        )))

    def can_create(self, user, local_site=None):
        """Returns whether the user can create groups."""
        return (user.is_superuser or
                (local_site and local_site.is_mutable_by(user)))


class ReviewRequestQuerySet(QuerySet):
    def with_counts(self, user):
        queryset = self

        if user and user.is_authenticated:
            select_dict = {}

            select_dict['new_review_count'] = """
                SELECT COUNT(*)
                  FROM reviews_review, accounts_reviewrequestvisit
                  WHERE reviews_review.public
                    AND reviews_review.review_request_id =
                        reviews_reviewrequest.id
                    AND accounts_reviewrequestvisit.review_request_id =
                        reviews_reviewrequest.id
                    AND accounts_reviewrequestvisit.user_id = %(user_id)s
                    AND reviews_review.timestamp >
                        accounts_reviewrequestvisit.timestamp
                    AND reviews_review.user_id != %(user_id)s
            """ % {
                'user_id': str(user.id)
            }

            queryset = self.extra(select=select_dict)

        return queryset


class ReviewRequestManager(ConcurrencyManager):
    """
    A manager for review requests. Provides specialized queries to retrieve
    review requests with specific targets or origins, and to create review
    requests based on certain data.
    """

    def get_queryset(self):
        """Return a QuerySet for ReviewRequest models.

        Returns:
            ReviewRequestQuerySet:
            The new QuerySet instance.
        """
        return ReviewRequestQuerySet(self.model)

    def create(self, user, repository, commit_id=None, local_site=None,
               create_from_commit_id=False, create_with_history=False):
        """Create a new review request.

        Args:
            user (django.contrib.auth.models.User):
                The user creating the review request. They will be tracked as
                the submitter.

            repository (reviewboard.scmtools.Models.Repository):
                The repository, if any, the review request is associated with.

                If ``None``, diffs cannot be added to the review request.

            commit_id (unicode, optional):
                An optional commit ID.

            local_site (reviewboard.site.models.LocalSite, optional):
                An optional LocalSite to associate the review request with.

            create_from_commit_id (bool, optional):
                Whether or not the given ``commit_id`` should be used to
                pre-populate the review request data. If ``True``, the given
                ``repository`` will be used to do so.

            create_with_history (bool, optional):
                Whether or not the created review request will support
                attaching multiple commits per diff revision.

                If ``False``, it will not be possible to use the
                :py:class:`~reviewboard.webapi.resources.diff.DiffResource` to
                upload diffs; the
                :py:class:`~reviewboard.webapi.resources.DiffCommitResource`
                must be used instead.

        Returns:
            reviewboard.reviews.models.review_request.ReviewRequest:
            The created review request.

        Raises:
            reviewboard.hostingsvcs.errors.HostingServiceError:
                The hosting service backing the repository encountered an
                error.

            reviewboard.scmtools.errors.ChangeNumberInUseError:
                The commit ID is already in use by another review request.

            reviewboard.scmtools.errors.SCMError:
                The repository tool encountered an error.

            ValueError:
                An invalid value was passed for an argument.
        """
        from reviewboard.reviews.models import ReviewRequestDraft

        if commit_id:
            # Try both the new commit_id and old changenum versions
            try:
                review_request = self.get(commit_id=commit_id,
                                          repository=repository)
                raise ChangeNumberInUseError(review_request)
            except ObjectDoesNotExist:
                pass

            try:
                draft = ReviewRequestDraft.objects.get(
                    commit_id=commit_id,
                    review_request__repository=repository)
                raise ChangeNumberInUseError(draft.review_request)
            except ObjectDoesNotExist:
                pass

            try:
                review_request = self.get(changenum=int(commit_id),
                                          repository=repository)
                raise ChangeNumberInUseError(review_request)
            except (ObjectDoesNotExist, TypeError, ValueError):
                pass

        if create_with_history:
            if repository is None:
                raise ValueError('create_with_history requires a repository.')
            elif create_from_commit_id:
                raise ValueError(
                    'create_from_commit_id and create_with_history cannot '
                    'both be set to True.')
            elif not repository.scmtool_class.supports_history:
                raise ValueError(
                    'This repository does not support review requests created '
                    'with history.')

        # Create the review request. We're not going to actually save this
        # until we're confident we have all the data we need.
        review_request = self.model(
            submitter=user,
            status='P',
            public=False,
            repository=repository,
            diffset_history=DiffSetHistory(),
            local_site=local_site)

        review_request.created_with_history = create_with_history

        if commit_id:
            review_request.commit = commit_id

        review_request.validate_unique()

        draft = None

        if commit_id and create_from_commit_id:
            try:
                draft = ReviewRequestDraft(review_request=review_request)
                draft.update_from_commit_id(commit_id)
            except Exception as e:
                logger.exception('Unable to update new review request from '
                                 'commit ID %s on repository ID=%s: %s',
                                 commit_id, repository.pk, e)
                raise

        # Now that we've guaranteed we have everything needed for this review
        # request, we can save all related objects and re-attach (since the
        # "None" IDs are cached).
        review_request.diffset_history.save()
        review_request.diffset_history = review_request.diffset_history
        review_request.save()

        if draft:
            draft.review_request = review_request
            draft.save()

            draft.add_default_reviewers()

        if local_site:
            # We want to atomically set the local_id to be a monotonically
            # increasing ID unique to the local_site. This isn't really
            # possible in django's DB layer, so we have to drop back to pure
            # SQL and then reload the model.
            from reviewboard.reviews.models import ReviewRequest

            with transaction.atomic():
                # TODO: Use the cursor as a context manager when we move over
                # to Django 1.7+.
                db = router.db_for_write(ReviewRequest)
                cursor = connections[db].cursor()
                cursor.execute(
                    'UPDATE %(table)s SET'
                    '  local_id = COALESCE('
                    '    (SELECT MAX(local_id) from'
                    '      (SELECT local_id FROM %(table)s'
                    '        WHERE local_site_id = %(local_site_id)s) as x'
                    '      ) + 1,'
                    '    1),'
                    '  local_site_id = %(local_site_id)s'
                    '    WHERE %(table)s.id = %(id)s' % {
                        'table': ReviewRequest._meta.db_table,
                        'local_site_id': local_site.pk,
                        'id': review_request.pk,
                    })
                cursor.close()

            review_request.local_id = (
                ReviewRequest.objects.filter(pk=review_request.pk)
                .values_list('local_id', flat=True)[0]
            )

        # Ensure that a draft exists, so that users will be prompted to publish
        # the new review request.
        ReviewRequestDraft.create(review_request)

        return review_request

    def get_to_group_query(self, group_name, local_site):
        """Returns the query targetting a group.

        This is meant to be passed as an extra_query to
        ReviewRequest.objects.public().
        """
        return Q(target_groups__name=group_name,
                 local_site=local_site)

    def get_to_user_groups_query(self, user_or_username):
        """Returns the query targetting groups joined by a user.

        This is meant to be passed as an extra_query to
        ReviewRequest.objects.public().
        """
        query_user = self._get_query_user(user_or_username)
        groups = list(query_user.review_groups.values_list('pk', flat=True))

        return Q(target_groups__in=groups)

    def get_to_user_directly_query(self, user_or_username):
        """Returns the query targetting a user directly.

        This will include review requests where the user has been listed
        as a reviewer, or the user has starred.

        This is meant to be passed as an extra_query to
        ReviewRequest.objects.public().
        """
        query_user = self._get_query_user(user_or_username)

        query = Q(target_people=query_user)

        try:
            profile = query_user.get_profile()
            query = query | Q(starred_by=profile)
        except ObjectDoesNotExist:
            pass

        return query

    def get_to_user_query(self, user_or_username):
        """Returns the query targetting a user indirectly.

        This will include review requests where the user has been listed
        as a reviewer, or a group that the user belongs to has been listed,
        or the user has starred.

        This is meant to be passed as an extra_query to
        ReviewRequest.objects.public().
        """
        query_user = self._get_query_user(user_or_username)
        groups = list(query_user.review_groups.values_list('pk', flat=True))

        query = Q(target_people=query_user) | Q(target_groups__in=groups)

        try:
            profile = query_user.get_profile()
            query = query | Q(starred_by=profile)
        except ObjectDoesNotExist:
            pass

        return query

    def get_from_user_query(self, user_or_username):
        """Returns the query for review requests created by a user.

        This is meant to be passed as an extra_query to
        ReviewRequest.objects.public().
        """

        if isinstance(user_or_username, User):
            return Q(submitter=user_or_username)
        else:
            return Q(submitter__username=user_or_username)

    def get_to_or_from_user_query(self, user_or_username):
        """Return the query for review requests a user is involved in.

        This is meant to be passed as an extra_query to
        :py:meth:`ReviewRequest.objects.public <reviewboard.reviews
        .managers.ReviewRequestManager.public>`.

        Args:
            user_or_username (django.contrib.auth.models.User or unicode):
                The user object or username to query for.

        Returns:
            django.db.models.Q:
            A query for all review requests the users is involved in as
            either a submitter or a reviewer (either directly assigned or
            indirectly as a member of a group).
        """
        return (self.get_to_user_query(user_or_username) |
                self.get_from_user_query(user_or_username))

    def public(self, filter_private=True, *args, **kwargs):
        return self._query(filter_private=filter_private, *args, **kwargs)

    def to_group(self, group_name, local_site, *args, **kwargs):
        return self._query(
            extra_query=self.get_to_group_query(group_name, local_site),
            local_site=local_site,
            *args, **kwargs)

    def to_or_from_user(self, user_or_username, *args, **kwargs):
        """Return the Queryset for review requests a user is involved in.

        Args:
            user_or_username (django.contrib.auth.models.User or unicode):
                The user object or username to query for.

            *args (tuple):
                Extra postional arguments passed into handler.

            **kwargs (dict):
                Extra keyword arguments passed into handler.

        Returns:
            django.db.models.query.QuerySet:
            A queryset of all review requests the users is involved in as
            either a submitter or a reviewer (either directly assigned or
            indirectly as a member of a group).
        """
        return self._query(
            extra_query=self.get_to_or_from_user_query(user_or_username),
            *args, **kwargs)

    def to_user_groups(self, username, *args, **kwargs):
        return self._query(
            extra_query=self.get_to_user_groups_query(username),
            *args, **kwargs)

    def to_user_directly(self, user_or_username, *args, **kwargs):
        return self._query(
            extra_query=self.get_to_user_directly_query(user_or_username),
            *args, **kwargs)

    def to_user(self, user_or_username, *args, **kwargs):
        return self._query(
            extra_query=self.get_to_user_query(user_or_username),
            *args, **kwargs)

    def from_user(self, user_or_username, *args, **kwargs):
        return self._query(
            extra_query=self.get_from_user_query(user_or_username),
            *args, **kwargs)

    def _query(self, user=None, status='P', with_counts=False,
               extra_query=None, local_site=None, filter_private=False,
               show_inactive=False, show_all_unpublished=False,
               show_all_local_sites=False):
        from reviewboard.reviews.models import Group

        is_authenticated = (user is not None and user.is_authenticated)

        if show_all_unpublished:
            query = Q()
        else:
            query = Q(public=True)

            if is_authenticated:
                query = query | Q(submitter=user)

        if not show_inactive:
            query = query & Q(submitter__is_active=True)

        if status:
            query = query & Q(status=status)

        if show_all_local_sites:
            assert local_site is None
        else:
            query = query & Q(local_site=local_site)

        if extra_query:
            query = query & extra_query

        if filter_private and (not user or not user.is_superuser):
            # This must always be kept in sync with RBSearchForm.search.
            repo_query = Q(repository=None)
            group_query = Q(target_groups=None)

            if is_authenticated:
                accessible_repo_ids = \
                    Repository.objects.accessible_ids(user, visible_only=False,
                                                      local_site=local_site)
                accessible_group_ids = \
                    Group.objects.accessible_ids(user, visible_only=False,
                                                 local_site=local_site)

                repo_query = repo_query | Q(repository__in=accessible_repo_ids)
                group_query = (group_query |
                               Q(target_groups__in=accessible_group_ids))

                query = query & (Q(submitter=user) |
                                 (repo_query &
                                  (Q(target_people=user) | group_query)))
            else:
                repo_query |= Q(repository__public=True)
                group_query |= Q(target_groups__invite_only=False)

                query = query & repo_query & group_query

        query = self.filter(query).distinct()

        if with_counts:
            query = query.with_counts(user)

        return query

    def _get_query_user(self, user_or_username):
        """Returns a User object, given a possible User or username."""
        if isinstance(user_or_username, User):
            return user_or_username
        else:
            return User.objects.get(username=user_or_username)

    def for_id(self, pk, local_site=None):
        """Returns the review request matching the given ID and LocalSite.

        If a LocalSite is provided, then the ID will be matched against the
        displayed ID for the LocalSite, rather than the in-database ID.
        """
        if local_site is None:
            return self.model.objects.get(pk=pk)
        else:
            return self.model.objects.get(Q(local_id=pk) &
                                          Q(local_site=local_site))


class ReviewManager(ConcurrencyManager):
    """A manager for Review models.

    This handles concurrency issues with Review models. In particular, it
    will try hard not to save two reviews at the same time, and if it does
    manage to do that (which may happen for pending reviews while a server
    is under heavy load), it will repair and consolidate the reviews on
    load. This prevents errors and lost data.
    """

    def get_pending_review(self, review_request, user):
        """Returns a user's pending review on a review request.

        This will handle fixing duplicate reviews if more than one pending
        review is found.
        """
        if not user.is_authenticated:
            return None

        query = self.filter(user=user,
                            review_request=review_request,
                            public=False,
                            base_reply_to__isnull=True)
        query = query.order_by("timestamp")

        reviews = list(query)

        if len(reviews) == 0:
            return None
        elif len(reviews) == 1:
            return reviews[0]
        else:
            # We have duplicate reviews, which will break things. We need
            # to condense them.
            logger.warning("Duplicate pending reviews found for review "
                           "request ID %s, user %s. Fixing." %
                           (review_request.id, user.username))

            return self.fix_duplicate_reviews(reviews)

    def fix_duplicate_reviews(self, reviews):
        """Fix duplicate reviews, condensing them into a single review.

        This will consolidate the data from all reviews into the first
        review in the list, and return the first review.
        """
        master_review = reviews[0]

        for review in reviews[1:]:
            for attname in ["body_top", "body_bottom", "body_top_reply_to",
                            "body_bottom_reply_to"]:
                review_value = getattr(review, attname)

                if (review_value and not getattr(master_review, attname)):
                    setattr(master_review, attname, review_value)

            for attname in ["comments", "screenshot_comments",
                            "file_attachment_comments",
                            "general_comments"]:
                master_m2m = getattr(master_review, attname)
                review_m2m = getattr(review, attname)

                for obj in review_m2m.all():
                    master_m2m.add(obj)
                    review_m2m.remove(obj)

            master_review.save()
            review.delete()

        return master_review

    def from_user(self, user_or_username, *args, **kwargs):
        """Return the query for reviews created by a user.

        Args:
            user_or_username (django.contrib.auth.models.User or unicode):
                The user object or username to query for.

        Returns:
            django.db.models.query.QuerySet:
            A queryset for all the reviews created by the given user.
        """
        if isinstance(user_or_username, User):
            extra_query = Q(user=user_or_username)
        else:
            extra_query = Q(user__username=user_or_username)

        return self._query(extra_query=extra_query, *args, **kwargs)

    def _query(self, user=None, public=True, status='P', extra_query=None,
               local_site=None, filter_private=False, base_reply_to=None):
        """Do a query for reviews.

        Args:
            user (django.contrib.auth.models.User, optional):
                A user to query for.

            public (bool, optional):
                Whether to filter for public (published) reviews.

            status (unicode, optional):
                The status of the review request that reviews are associated
                with.

            extra_query (django.db.models.Q, optional):
                Additional query parameters to add.

            local_site (reviewboard.site.models.LocalSite, optional):
                A local site to limit to, if appropriate.

            filter_private (bool, optional):
                Whether to limit the results based on the accessibility of
                related review requests.

            base_reply_to (reviewboard.reviews.models.review.Review, optional):
                If provided, limit results to reviews that are part of the
                thread of replies to this review.

        Returns:
            django.db.models.query.QuerySet:
            A queryset for the given conditions.
        """
        from reviewboard.reviews.models import Group

        query = Q(public=public) & Q(base_reply_to=base_reply_to)

        if status:
            query = query & Q(review_request__status=status)

        query = query & Q(review_request__local_site=local_site)

        if extra_query:
            query = query & extra_query

        if filter_private and (not user or not user.is_superuser):
            repo_query = Q(review_request__repository=None)
            group_query = Q(review_request__target_groups=None)

            # TODO: should be consolidated with queries in ReviewRequestManager
            if user and user.is_authenticated:
                accessible_repo_ids = Repository.objects.accessible_ids(
                    user=user,
                    visible_only=False,
                    local_site=local_site)
                accessible_group_ids = Group.objects.accessible_ids(
                    user=user,
                    visible_only=False,
                    local_site=local_site)

                repo_query |= \
                    Q(review_request__repository__in=accessible_repo_ids)
                group_query |= \
                    Q(review_request__target_groups__in=accessible_group_ids)

                query = query & (Q(user=user) |
                                 (repo_query &
                                  (Q(review_request__target_people=user) |
                                   group_query)))
            else:
                repo_query |= Q(review_request__repository__public=True)
                group_query |= \
                    Q(review_request__target_groups__invite_only=False)

                query &= repo_query & group_query

        query = self.filter(query).distinct()

        return query
