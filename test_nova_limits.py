import StringIO
import sys
import time
import unittest

import argparse
from nova.api.openstack import wsgi
import stubout
from turnstile import limits
from turnstile import tools

import nova_limits


class FakeDatabase(object):
    def __init__(self, fake_db=None):
        self.fake_db = fake_db or {}
        self.actions = []

    def get(self, key):
        self.actions.append(('get', key))
        return self.fake_db.get(key)

    def set(self, key, value):
        self.actions.append(('set', key, value))
        self.fake_db[key] = value

    def delete(self, key):
        self.actions.append(('delete', key))
        if key in self.fake_db:
            del self.fake_db[key]


class FakeMiddleware(object):
    def __init__(self, db, limits):
        self.db = db
        self.limits = limits


class FakeObject(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestPreprocess(unittest.TestCase):
    def setUp(self):
        self.stubs = stubout.StubOutForTesting()

        self.stubs.Set(time, 'time', lambda: 1000000000)

    def tearDown(self):
        self.stubs.UnsetAll()

    def test_basic(self):
        db = FakeDatabase()
        midware = FakeMiddleware(db, [])
        environ = {}
        nova_limits.nova_preprocess(midware, environ)

        self.assertEqual(environ, {
                'turnstile.nova.tenant': '<NONE>',
                'turnstile.nova.limitclass': 'default',
                'nova.limits': [],
                })
        self.assertEqual(db.actions, [
                ('get', 'limit-class:<NONE>'),
                ])

    def test_tenant(self):
        db = FakeDatabase()
        midware = FakeMiddleware(db, [])
        environ = {
            'nova.context': FakeObject(project_id='spam'),
            }
        nova_limits.nova_preprocess(midware, environ)

        self.assertEqual(environ['turnstile.nova.tenant'], 'spam')
        self.assertEqual(environ['turnstile.nova.limitclass'], 'default')
        self.assertEqual(environ['nova.limits'], [])
        self.assertEqual(db.actions, [
                ('get', 'limit-class:spam'),
                ])

    def test_configured_class(self):
        db = FakeDatabase({'limit-class:spam': 'lim_class'})
        midware = FakeMiddleware(db, [])
        environ = {
            'nova.context': FakeObject(project_id='spam'),
            }
        nova_limits.nova_preprocess(midware, environ)

        self.assertEqual(environ['turnstile.nova.tenant'], 'spam')
        self.assertEqual(environ['turnstile.nova.limitclass'], 'lim_class')
        self.assertEqual(environ['nova.limits'], [])
        self.assertEqual(db.actions, [
                ('get', 'limit-class:spam'),
                ])

    def test_class_no_override(self):
        db = FakeDatabase({'limit-class:spam': 'lim_class'})
        midware = FakeMiddleware(db, [])
        environ = {
            'nova.context': FakeObject(project_id='spam'),
            'turnstile.nova.limitclass': 'override',
            }
        nova_limits.nova_preprocess(midware, environ)

        self.assertEqual(environ['turnstile.nova.tenant'], 'spam')
        self.assertEqual(environ['turnstile.nova.limitclass'], 'override')
        self.assertEqual(environ['nova.limits'], [])
        self.assertEqual(db.actions, [
                ('get', 'limit-class:spam'),
                ])

    def test_limits(self):
        db = FakeDatabase({'limit-class:spam': 'lim_class'})
        midware = FakeMiddleware(db, [
                FakeObject(
                    verbs=['GET', 'PUT'],
                    unit='minute',
                    uri='/spam/uri',
                    value=23),
                FakeObject(
                    verbs=[],
                    unit='second',
                    uri='/spam/uri2',
                    value=18),
                FakeObject(
                    rate_class='spam',
                    verbs=['GET'],
                    unit='hour',
                    uri='/spam/uri3',
                    value=17),
                FakeObject(
                    rate_class='lim_class',
                    verbs=['GET'],
                    unit='day',
                    uri='/spam/uri4',
                    value=1),
                FakeObject(
                    verbs=['GET'],
                    unit='1234',
                    uri='/spam/uri5',
                    value=183),
                ])
        environ = {
            'nova.context': FakeObject(project_id='spam'),
            }
        nova_limits.nova_preprocess(midware, environ)

        self.assertEqual(environ['turnstile.nova.tenant'], 'spam')
        self.assertEqual(environ['turnstile.nova.limitclass'], 'lim_class')
        self.assertEqual(environ['nova.limits'], [
                dict(
                    verb='GET',
                    URI='/spam/uri',
                    regex='/spam/uri',
                    value=23,
                    unit='MINUTE',
                    remaining=23,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='PUT',
                    URI='/spam/uri',
                    regex='/spam/uri',
                    value=23,
                    unit='MINUTE',
                    remaining=23,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='GET',
                    URI='/spam/uri2',
                    regex='/spam/uri2',
                    value=18,
                    unit='SECOND',
                    remaining=18,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='HEAD',
                    URI='/spam/uri2',
                    regex='/spam/uri2',
                    value=18,
                    unit='SECOND',
                    remaining=18,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='POST',
                    URI='/spam/uri2',
                    regex='/spam/uri2',
                    value=18,
                    unit='SECOND',
                    remaining=18,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='PUT',
                    URI='/spam/uri2',
                    regex='/spam/uri2',
                    value=18,
                    unit='SECOND',
                    remaining=18,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='DELETE',
                    URI='/spam/uri2',
                    regex='/spam/uri2',
                    value=18,
                    unit='SECOND',
                    remaining=18,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='GET',
                    URI='/spam/uri4',
                    regex='/spam/uri4',
                    value=1,
                    unit='DAY',
                    remaining=1,
                    resetTime=1000000000,
                    ),
                dict(
                    verb='GET',
                    URI='/spam/uri5',
                    regex='/spam/uri5',
                    value=183,
                    unit='UNKNOWN',
                    remaining=183,
                    resetTime=1000000000,
                    ),
                ])
        self.assertEqual(db.actions, [
                ('get', 'limit-class:spam'),
                ])


class TestNovaClassLimit(unittest.TestCase):
    def setUp(self):
        self.lim = nova_limits.NovaClassLimit('db', uri='/spam', value=18,
                                              unit='second',
                                              rate_class='lim_class')

    def test_route_base(self):
        route_args = {}
        result = self.lim.route('/spam', route_args)

        self.assertEqual(result, '/spam')

    def test_route_v1(self):
        route_args = {}
        result = self.lim.route('/v1.1/spam', route_args)

        self.assertEqual(result, '/spam')

    def test_route_v2(self):
        route_args = {}
        result = self.lim.route('/v2/spam', route_args)

        self.assertEqual(result, '/spam')

    def test_route_v1_base(self):
        route_args = {}
        result = self.lim.route('/v1.1', route_args)

        self.assertEqual(result, '/v1.1')

    def test_route_v2_base(self):
        route_args = {}
        result = self.lim.route('/v2', route_args)

        self.assertEqual(result, '/v2')

    def test_filter_noclass(self):
        environ = {
            'turnstile.nova.tenant': 'tenant',
            }
        params = {}
        unused = {}
        with self.assertRaises(limits.DeferLimit):
            self.lim.filter(environ, params, unused)

        self.assertEqual(environ, {
                'turnstile.nova.tenant': 'tenant',
                })
        self.assertEqual(params, {})
        self.assertEqual(unused, {})

    def test_filter_notenant(self):
        environ = {
            'turnstile.nova.limitclass': 'lim_class',
            }
        params = {}
        unused = {}
        with self.assertRaises(limits.DeferLimit):
            self.lim.filter(environ, params, unused)

        self.assertEqual(environ, {
                'turnstile.nova.limitclass': 'lim_class',
                })
        self.assertEqual(params, {})
        self.assertEqual(unused, {})

    def test_filter_wrong_class(self):
        environ = {
            'turnstile.nova.limitclass': 'spam',
            'turnstile.nova.tenant': 'tenant',
            }
        params = {}
        unused = {}
        with self.assertRaises(limits.DeferLimit):
            self.lim.filter(environ, params, unused)

        self.assertEqual(environ, {
                'turnstile.nova.limitclass': 'spam',
                'turnstile.nova.tenant': 'tenant',
                })
        self.assertEqual(params, {})
        self.assertEqual(unused, {})

    def test_filter(self):
        environ = {
            'turnstile.nova.limitclass': 'lim_class',
            'turnstile.nova.tenant': 'tenant',
            }
        params = {}
        unused = {}
        self.lim.filter(environ, params, unused)

        self.assertEqual(environ, {
                'turnstile.nova.limitclass': 'lim_class',
                'turnstile.nova.tenant': 'tenant',
                })
        self.assertEqual(params, dict(tenant='tenant'))
        self.assertEqual(unused, {})


class StubNovaTurnstileMiddleware(nova_limits.NovaTurnstileMiddleware):
    def __init__(self):
        pass


class TestNovaTurnstileMiddleware(unittest.TestCase):
    def setUp(self):
        self.midware = StubNovaTurnstileMiddleware()
        self.stubs = stubout.StubOutForTesting()

        def fake_over_limit_fault(msg, err, retry):
            def inner(environ, start_response):
                return (msg, err, retry, environ, start_response)
            return inner

        self.stubs.Set(wsgi, 'OverLimitFault', fake_over_limit_fault)
        self.stubs.Set(time, 'time', lambda: 1000000000)

    def tearDown(self):
        self.stubs.UnsetAll()

    def test_format_delay(self):
        lim = FakeObject(value=23, uri='/spam', unit='second')
        environ = dict(REQUEST_METHOD='SPAM')
        start_response = lambda: None
        result = self.midware.format_delay(18, lim, None,
                                           environ, start_response)

        self.assertEqual(result[0], 'This request was rate-limited.')
        self.assertEqual(result[1],
                         'Only 23 SPAM request(s) can be made to /spam '
                         'every SECOND.')
        self.assertEqual(result[2], 1000000018)
        self.assertEqual(id(result[3]), id(environ))
        self.assertEqual(id(result[4]), id(start_response))
