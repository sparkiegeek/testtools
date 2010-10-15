# Copyright (c) 2010 Jonathan M. Lange. See LICENSE for details.

"""Tests for the DeferredRunTest single test execution logic."""

import os
import signal

from testtools import (
    TestCase,
    )
from testtools.deferredruntest import (
    AsynchronousDeferredRunTest,
    not_reentrant,
    ReentryError,
    SynchronousDeferredRunTest,
    )
from testtools.tests.helpers import ExtendedTestResult
from testtools.matchers import (
    Equals,
    )
from testtools.runtest import RunTest

from twisted.internet import defer


class TestNotReentrant(TestCase):

    def test_not_reentrant(self):
        # A function decorated as not being re-entrant will raise a
        # ReentryError if it is called while it is running.
        calls = []
        @not_reentrant
        def log_something():
            calls.append(None)
            if len(calls) < 5:
                log_something()
        self.assertRaises(ReentryError, log_something)
        self.assertEqual(1, len(calls))

    def test_deeper_stack(self):
        calls = []
        @not_reentrant
        def g():
            calls.append(None)
            if len(calls) < 5:
                f()
        @not_reentrant
        def f():
            calls.append(None)
            if len(calls) < 5:
                g()
        self.assertRaises(ReentryError, f)
        self.assertEqual(2, len(calls))


class X(object):
    """Tests that we run as part of our tests, nested to avoid discovery."""

    class Base(TestCase):
        def setUp(self):
            super(X.Base, self).setUp()
            self.calls = ['setUp']
            self.addCleanup(self.calls.append, 'clean-up')
        def test_something(self):
            self.calls.append('test')
        def tearDown(self):
            self.calls.append('tearDown')
            super(X.Base, self).tearDown()

    class Success(Base):
        expected_calls = ['setUp', 'test', 'tearDown', 'clean-up']
        expected_results = [['addSuccess']]

    class ErrorInSetup(Base):
        expected_calls = ['setUp', 'clean-up']
        expected_results = [('addError', RuntimeError)]
        def setUp(self):
            super(X.ErrorInSetup, self).setUp()
            raise RuntimeError("Error in setUp")

    class ErrorInTest(Base):
        expected_calls = ['setUp', 'tearDown', 'clean-up']
        expected_results = [('addError', RuntimeError)]
        def test_something(self):
            raise RuntimeError("Error in test")

    class FailureInTest(Base):
        expected_calls = ['setUp', 'tearDown', 'clean-up']
        expected_results = [('addFailure', AssertionError)]
        def test_something(self):
            self.fail("test failed")

    class ErrorInTearDown(Base):
        expected_calls = ['setUp', 'test', 'clean-up']
        expected_results = [('addError', RuntimeError)]
        def tearDown(self):
            raise RuntimeError("Error in tearDown")

    class ErrorInCleanup(Base):
        expected_calls = ['setUp', 'test', 'tearDown', 'clean-up']
        expected_results = [('addError', ZeroDivisionError)]
        def test_something(self):
            self.calls.append('test')
            self.addCleanup(lambda: 1/0)

    class TestIntegration(TestCase):

        def assertResultsMatch(self, test, result):
            events = list(result._events)
            self.assertEqual(('startTest', test), events.pop(0))
            for expected_result in test.expected_results:
                result = events.pop(0)
                if len(expected_result) == 1:
                    self.assertEqual((expected_result[0], test), result)
                else:
                    self.assertEqual((expected_result[0], test), result[:2])
                    error_type = expected_result[1]
                    self.assertIn(error_type.__name__, str(result[2]))
            self.assertEqual([('stopTest', test)], events)

        def test_runner(self):
            result = ExtendedTestResult()
            test = self.test_factory('test_something', runTest=self.runner)
            test.run(result)
            self.assertEqual(test.calls, self.test_factory.expected_calls)
            self.assertResultsMatch(test, result)


def make_integration_tests():
    from unittest import TestSuite
    from testtools import clone_test_with_new_id
    runners = [
        RunTest,
        SynchronousDeferredRunTest,
        AsynchronousDeferredRunTest,
        ]

    tests = [
        X.Success,
        X.ErrorInSetup,
        X.ErrorInTest,
        X.ErrorInTearDown,
        X.FailureInTest,
        X.ErrorInCleanup,
        ]
    base_test = X.TestIntegration('test_runner')
    integration_tests = []
    for runner in runners:
        for test in tests:
            new_test = clone_test_with_new_id(
                base_test, '%s(%s, %s)' % (
                    base_test.id(),
                    runner.__name__,
                    test.__name__))
            new_test.test_factory = test
            new_test.runner = runner
            integration_tests.append(new_test)
    return TestSuite(integration_tests)


class TestSynchronousDeferredRunTest(TestCase):

    def make_result(self):
        return ExtendedTestResult()

    def make_runner(self, test):
        return SynchronousDeferredRunTest(test, test.exception_handlers)

    def test_success(self):
        class SomeCase(TestCase):
            def test_success(self):
                return defer.succeed(None)
        test = SomeCase('test_success')
        runner = self.make_runner(test)
        result = self.make_result()
        runner.run(result)
        self.assertThat(
            result._events, Equals([
                ('startTest', test),
                ('addSuccess', test),
                ('stopTest', test)]))

    def test_failure(self):
        class SomeCase(TestCase):
            def test_failure(self):
                return defer.maybeDeferred(self.fail, "Egads!")
        test = SomeCase('test_failure')
        runner = self.make_runner(test)
        result = self.make_result()
        runner.run(result)
        self.assertThat(
            [event[:2] for event in result._events], Equals([
                ('startTest', test),
                ('addFailure', test),
                ('stopTest', test)]))

    def test_setUp_followed_by_test(self):
        class SomeCase(TestCase):
            def setUp(self):
                super(SomeCase, self).setUp()
                return defer.succeed(None)
            def test_failure(self):
                return defer.maybeDeferred(self.fail, "Egads!")
        test = SomeCase('test_failure')
        runner = self.make_runner(test)
        result = self.make_result()
        runner.run(result)
        self.assertThat(
            [event[:2] for event in result._events], Equals([
                ('startTest', test),
                ('addFailure', test),
                ('stopTest', test)]))


class TestAsynchronousDeferredRunTest(TestCase):

    def make_reactor(self):
        from twisted.internet import reactor
        return reactor

    def make_result(self):
        return ExtendedTestResult()

    def make_runner(self, test, timeout=None):
        if timeout is None:
            timeout = self.make_timeout()
        return AsynchronousDeferredRunTest(
            test, test.exception_handlers, timeout=timeout)

    def make_timeout(self):
        return 0.005

    def test_setUp_returns_deferred_that_fires_later(self):
        # setUp can return a Deferred that might fire at any time.
        # AsynchronousDeferredRunTest will not go on to running the test until
        # the Deferred returned by setUp actually fires.
        call_log = []
        marker = object()
        d = defer.Deferred().addCallback(call_log.append)
        class SomeCase(TestCase):
            def setUp(self):
                super(SomeCase, self).setUp()
                call_log.append('setUp')
                return d
            def test_something(self):
                call_log.append('test')
        def fire_deferred():
            self.assertThat(call_log, Equals(['setUp']))
            d.callback(marker)
        test = SomeCase('test_something')
        timeout = self.make_timeout()
        runner = self.make_runner(test, timeout=timeout)
        result = self.make_result()
        reactor = self.make_reactor()
        reactor.callLater(timeout, fire_deferred)
        runner.run(result)
        self.assertThat(call_log, Equals(['setUp', marker, 'test']))

    def test_calls_setUp_test_tearDown_in_sequence(self):
        # setUp, the test method and tearDown can all return
        # Deferreds. AsynchronousDeferredRunTest will make sure that each of
        # these are run in turn, only going on to the next stage once the
        # Deferred from the previous stage has fired.
        call_log = []
        a = defer.Deferred()
        a.addCallback(lambda x: call_log.append('a'))
        b = defer.Deferred()
        b.addCallback(lambda x: call_log.append('b'))
        c = defer.Deferred()
        c.addCallback(lambda x: call_log.append('c'))
        class SomeCase(TestCase):
            def setUp(self):
                super(SomeCase, self).setUp()
                call_log.append('setUp')
                return a
            def test_success(self):
                call_log.append('test')
                return b
            def tearDown(self):
                super(SomeCase, self).tearDown()
                call_log.append('tearDown')
                return c
        test = SomeCase('test_success')
        timeout = self.make_timeout()
        runner = self.make_runner(test, timeout)
        result = self.make_result()
        reactor = self.make_reactor()
        def fire_a():
            self.assertThat(call_log, Equals(['setUp']))
            a.callback(None)
        def fire_b():
            self.assertThat(call_log, Equals(['setUp', 'a', 'test']))
            b.callback(None)
        def fire_c():
            self.assertThat(
                call_log, Equals(['setUp', 'a', 'test', 'b', 'tearDown']))
            c.callback(None)
        reactor.callLater(timeout * 0.25, fire_a)
        reactor.callLater(timeout * 0.5, fire_b)
        reactor.callLater(timeout * 0.75, fire_c)
        runner.run(result)
        self.assertThat(
            call_log, Equals(['setUp', 'a', 'test', 'b', 'tearDown', 'c']))

    def test_async_cleanups(self):
        # Cleanups added with addCleanup can return
        # Deferreds. AsynchronousDeferredRunTest will run each of them in
        # turn.
        class SomeCase(TestCase):
            def test_whatever(self):
                pass
        test = SomeCase('test_whatever')
        log = []
        a = defer.Deferred().addCallback(lambda x: log.append('a'))
        b = defer.Deferred().addCallback(lambda x: log.append('b'))
        c = defer.Deferred().addCallback(lambda x: log.append('c'))
        test.addCleanup(lambda: a)
        test.addCleanup(lambda: b)
        test.addCleanup(lambda: c)
        def fire_a():
            self.assertThat(log, Equals([]))
            a.callback(None)
        def fire_b():
            self.assertThat(log, Equals(['a']))
            b.callback(None)
        def fire_c():
            self.assertThat(log, Equals(['a', 'b']))
            c.callback(None)
        timeout = self.make_timeout()
        reactor = self.make_reactor()
        reactor.callLater(timeout * 0.25, fire_a)
        reactor.callLater(timeout * 0.5, fire_b)
        reactor.callLater(timeout * 0.75, fire_c)
        runner = self.make_runner(test, timeout)
        result = self.make_result()
        runner.run(result)
        self.assertThat(log, Equals(['a', 'b', 'c']))

    def test_clean_reactor(self):
        # If there's cruft left over in the reactor, the test fails.
        reactor = self.make_reactor()
        timeout = self.make_timeout()
        class SomeCase(TestCase):
            def test_cruft(self):
                reactor.callLater(timeout * 2.0, lambda: None)
        test = SomeCase('test_cruft')
        runner = self.make_runner(test, timeout)
        result = self.make_result()
        runner.run(result)
        error = result._events[1][2]
        result._events[1] = ('addError', test, None)
        self.assertThat(result._events, Equals(
            [('startTest', test),
             ('addError', test, None),
             ('stopTest', test)]))
        self.assertThat(list(error.keys()), Equals(['traceback']))

    def test_unhandled_error_from_deferred(self):
        # If there's a Deferred with an unhandled error, the test fails.
        class SomeCase(TestCase):
            def test_cruft(self):
                # Note we aren't returning the Deferred so that the error will
                # be unhandled.
                defer.maybeDeferred(lambda: 1/0)
        test = SomeCase('test_cruft')
        runner = self.make_runner(test)
        result = self.make_result()
        runner.run(result)
        error = result._events[1][2]
        result._events[1] = ('addError', test, None)
        self.assertThat(result._events, Equals(
            [('startTest', test),
             ('addError', test, None),
             ('stopTest', test)]))
        self.assertThat(list(error.keys()), Equals(['traceback']))

    def test_keyboard_interrupt_stops_test_run(self):
        # If we get a SIGINT during a test run, the test stops and no more
        # tests run.
        class SomeCase(TestCase):
            def test_pause(self):
                return defer.Deferred()
        test = SomeCase('test_pause')
        reactor = self.make_reactor()
        timeout = self.make_timeout()
        runner = self.make_runner(test, timeout * 5)
        result = self.make_result()
        reactor.callLater(timeout, os.kill, os.getpid(), signal.SIGINT)
        self.assertRaises(KeyboardInterrupt, runner.run, result)

    def test_fast_keyboard_interrupt_stops_test_run(self):
        # If we get a SIGINT during a test run, the test stops and no more
        # tests run.
        class SomeCase(TestCase):
            def test_pause(self):
                return defer.Deferred()
        test = SomeCase('test_pause')
        reactor = self.make_reactor()
        timeout = self.make_timeout()
        runner = self.make_runner(test, timeout * 5)
        result = self.make_result()
        reactor.callWhenRunning(os.kill, os.getpid(), signal.SIGINT)
        self.assertRaises(KeyboardInterrupt, runner.run, result)

    def test_convenient_construction(self):
        # As a convenience method, AsynchronousDeferredRunTest has a
        # classmethod that returns an AsynchronousDeferredRunTest
        # factory. This factory has the same API as the RunTest constructor.
        reactor = object()
        timeout = object()
        handler = object()
        factory = AsynchronousDeferredRunTest.make_factory(reactor, timeout)
        runner = factory(self, [handler])
        self.assertIs(reactor, runner._reactor)
        self.assertIs(timeout, runner._timeout)
        self.assertIs(self, runner.case)
        self.assertEqual([handler], runner.handlers)

    def test_only_addError_once(self):
        # Even if the reactor is unclean and the test raises an error and the
        # cleanups raise errors, we only called addError once per test.
        reactor = self.make_reactor()
        class WhenItRains(TestCase):
            def it_pours(self):
                # Add a dirty cleanup.
                self.addCleanup(lambda: 3 / 0)
                # Dirty the reactor.
                from twisted.internet.protocol import ServerFactory
                reactor.listenTCP(0, ServerFactory())
                # Unhandled error.
                defer.maybeDeferred(lambda: 2 / 0)
                # Actual error.
                raise RuntimeError("Excess precipitation")
        test = WhenItRains('it_pours')
        runner = self.make_runner(test)
        result = self.make_result()
        runner.run(result)
        self.assertThat(
            [event[:2] for event in result._events],
            Equals([
                ('startTest', test),
                ('addError', test),
                ('stopTest', test)]))
        error = result._events[1][2]
        self.assertThat(
            sorted(error.keys()), Equals([
                'traceback',
                'traceback-1',
                'traceback-2',
                'traceback-3',
                ]))


def test_suite():
    from unittest import TestLoader, TestSuite
    return TestSuite(
        [TestLoader().loadTestsFromName(__name__),
         make_integration_tests()])
