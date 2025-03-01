from djblets.features.testing import override_feature_check
from djblets.testing.decorators import add_fixtures

from reviewboard.diffviewer.features import dvcs_feature
from reviewboard.webapi.resources import resources
from reviewboard.webapi.tests.base import BaseWebAPITestCase
from reviewboard.webapi.tests.mimetypes import root_item_mimetype
from reviewboard.webapi.tests.mixins import BasicTestsMetaclass
from reviewboard.webapi.tests.urls import get_root_url


class ResourceTests(BaseWebAPITestCase, metaclass=BasicTestsMetaclass):
    """Testing the RootResource APIs."""
    fixtures = ['test_users']
    sample_api_url = '/'
    resource = resources.root
    test_http_methods = ('DELETE', 'PUT', 'POST')

    def setup_http_not_allowed_item_test(self, user):
        return get_root_url()

    def setup_http_not_allowed_list_test(self, user):
        return get_root_url()

    def test_get(self):
        """Testing the GET / API"""
        rsp = self.api_get(get_root_url(),
                           expected_mimetype=root_item_mimetype)
        self.assertEqual(rsp['stat'], 'ok')
        self.assertIn('uri_templates', rsp)
        self.assertIn('repository', rsp['uri_templates'])
        self.assertEqual(rsp['uri_templates']['repository'],
                         'http://testserver/api/repositories/{repository_id}/')

        self._check_common_root_fields(rsp)

    @add_fixtures(['test_users', 'test_site'])
    def test_get_with_site(self):
        """Testing the GET / API with local sites"""
        self._login_user(local_site=True)
        rsp = self.api_get(get_root_url('local-site-1'),
                           expected_mimetype=root_item_mimetype)
        self.assertEqual(rsp['stat'], 'ok')
        self.assertIn('uri_templates', rsp)
        self.assertIn('repository', rsp['uri_templates'])
        self.assertEqual(rsp['uri_templates']['repository'],
                         'http://testserver/s/local-site-1/api/'
                         'repositories/{repository_id}/')

        self._check_common_root_fields(rsp)

    @add_fixtures(['test_users', 'test_site'])
    def test_get_with_site_no_access(self):
        """Testing the GET / API without access to local site"""
        self.api_get(get_root_url('local-site-1'), expected_status=403)

    @add_fixtures(['test_users', 'test_site'])
    def test_get_with_site_and_cache(self):
        """Testing the GET / API with multiple local sites"""
        # djblets had a bug where the uri_templates were cached without any
        # consideration of the local site (or, more generally, the base uri).
        # In this case, fetching /s/<local_site>/api/ might return uri
        # templates for someone else's site. This was breaking rbt post.
        self.test_get_with_site()

        rsp = self.api_get(get_root_url('local-site-2'),
                           expected_mimetype=root_item_mimetype)
        self.assertEqual(rsp['stat'], 'ok')
        self.assertIn('uri_templates', rsp)
        self.assertIn('repository', rsp['uri_templates'])
        self.assertEqual(rsp['uri_templates']['repository'],
                         'http://testserver/s/local-site-2/api/'
                         'repositories/{repository_id}/')

    def test_get_capability_dvcs_enabled(self):
        """Testing the GET / API for capabilities with the DVCS feature enabled
        """
        with override_feature_check(dvcs_feature.feature_id, True):
            rsp = self.api_get(get_root_url(),
                               expected_mimetype=root_item_mimetype)

        self.assertEqual(rsp['stat'], 'ok')
        self.assertIn('capabilities', rsp)

        caps = rsp['capabilities']
        self.assertIn('review_requests', caps)

        review_request_caps = caps['review_requests']
        self.assertIn('supports_history', review_request_caps)
        self.assertTrue(review_request_caps['supports_history'])

    def test_get_capability_dvcs_disabled(self):
        """Testing the GET / API for capabilities with the DVCS feature
        disabled
        """
        with override_feature_check(dvcs_feature.feature_id, False):
            self.assertFalse(dvcs_feature.is_enabled())
            rsp = self.api_get(get_root_url(),
                               expected_mimetype=root_item_mimetype)

        self.assertEqual(rsp['stat'], 'ok')
        self.assertIn('capabilities', rsp)

        caps = rsp['capabilities']
        self.assertIn('review_requests', caps)
        self.assertNotIn('supports_history', caps['review_requests'])

    def _check_common_root_fields(self, item_rsp):
        self.assertIn('product', item_rsp)
        self.assertIn('site', item_rsp)
        self.assertIn('capabilities', item_rsp)

        caps = item_rsp['capabilities']
        self.assertIn('diffs', caps)

        diffs_caps = caps['diffs']
        self.assertTrue(diffs_caps['moved_files'])
        self.assertTrue(diffs_caps['base_commit_ids'])

        diff_validation_caps = diffs_caps['validation']
        self.assertTrue(diff_validation_caps['base_commit_ids'])

        review_request_caps = caps['review_requests']
        self.assertTrue(review_request_caps['commit_ids'])

        text_caps = caps['text']
        self.assertTrue(text_caps['markdown'])
