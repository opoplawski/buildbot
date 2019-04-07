# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members
# Portions Copyright Canonical Ltd. 2009

import enum
import random
import string

from twisted.internet import defer
from twisted.python import failure
from twisted.python import log
from zope.interface import implementer

from buildbot.interfaces import ILatentWorker
from buildbot.interfaces import LatentWorkerFailedToSubstantiate
from buildbot.interfaces import LatentWorkerSubstantiatiationCancelled
from buildbot.util import Notifier
from buildbot.worker.base import AbstractWorker


class States(enum.Enum):
    # Represents the states of AbstractLatentWorker

    NOT_SUBSTANTIATED = 0

    # When in this state, self._substantiation_notifier is waited on. The
    # notifier is notified immediately after the state transition out of
    # SUBSTANTIATING.
    SUBSTANTIATING = 1

    SUBSTANTIATED = 2

    # When in this state, self._insubstantiation_notifier may be potentially
    # waited on.
    INSUBSTANTIATING = 3

    # This state represents the case when insubstantiation is in progress and
    # we also request substantiation at the same time. Substantiation will be
    # started as soon as insubstantiation completes. Note, that the opposite
    # actions are not supported: insubstantiation during substantiation will
    # cancel the substantiation.
    #
    # When in this state, self._insubstantiation_notifier may be potentially
    # waited on.
    INSUBSTANTIATING_SUBSTANTIATING = 4


@implementer(ILatentWorker)
class AbstractLatentWorker(AbstractWorker):

    """A worker that will start up a worker instance when needed.

    To use, subclass and implement start_instance and stop_instance.

    Additionally, if the instances render any kind of data affecting instance
    type from the build properties, set the class variable
    builds_may_be_incompatible to True and override isCompatibleWithBuild
    method.

    See ec2.py for a concrete example.
    """

    substantiation_build = None
    build_wait_timer = None
    start_missing_on_startup = False

    # Caveats: The handling of latent workers is much more complex than it
    # might seem. The code must handle at least the following conditions:
    #
    #   - non-silent disconnection by the worker at any time which generated
    #   TCP resets and in the end resulted in detached() being called
    #
    #   - silent disconnection by worker at any time by silent TCP connection
    #   failure which did not generate TCP resets, but on the other hand no
    #   response may be received. self.conn is not None is that case.
    #
    #   - no disconnection by worker during substantiation when
    #   build_wait_timeout param is negative.
    #
    # The above means that the connection state of the worker (self.conn) must
    # be tracked separately from the intended state of the worker (self.state).

    state = States.NOT_SUBSTANTIATED

    # state transitions:
    #
    # substantiate(): either of
    # NOT_SUBSTANTIATED -> SUBSTANTIATING
    # INSUBSTANTIATING -> INSUBSTANTIATING_SUBSTANTIATING
    #
    # attached():
    # SUBSTANTIATING -> SUBSTANTIATED
    # self.conn -> not None
    #
    # detached():
    # self.conn -> None
    #
    # errors in any of above will call insubstantiate()
    #
    # insubstantiate():
    # SUBSTANTIATED -> INSUBSTANTIATING
    # INSUBSTANTIATING_SUBSTANTIATING -> INSUBSTANTIATING (cancels substantiation request)
    # < other state transitions may happen during this time >
    # INSUBSTANTIATING_SUBSTANTIATING -> SUBSTANTIATING
    # INSUBSTANTIATING -> NOT_SUBSTANTIATED

    def checkConfig(self, name, password,
                    build_wait_timeout=60 * 10,
                    **kwargs):
        super().checkConfig(name, password, **kwargs)

    def reconfigService(self, name, password,
                        build_wait_timeout=60 * 10,
                        **kwargs):
        self._substantiation_notifier = Notifier()
        self._insubstantiation_notifier = Notifier()
        self.build_wait_timeout = build_wait_timeout
        return super().reconfigService(name, password, **kwargs)

    def getRandomPass(self):
        """
        compute a random password
        There is no point to configure a password for a LatentWorker, as it is created by the master.
        For supporting backend, a password can be generated by this API
        """
        return ''.join(
            random.choice(string.ascii_letters + string.digits)
            for _ in range(20))

    @property
    def building(self):
        # A LatentWorkerForBuilder will only be busy if it is building.
        return {wfb for wfb in self.workerforbuilders.values()
                if wfb.isBusy()}

    def failed_to_start(self, instance_id, instance_state):
        log.msg('%s %s failed to start instance %s (%s)' %
                (self.__class__.__name__, self.workername,
                    instance_id, instance_state))
        raise LatentWorkerFailedToSubstantiate(instance_id, instance_state)

    def start_instance(self, build):
        # responsible for starting instance that will try to connect with this
        # master.  Should return deferred with either True (instance started)
        # or False (instance not started, so don't run a build here).  Problems
        # should use an errback.
        raise NotImplementedError

    def stop_instance(self, fast=False):
        # responsible for shutting down instance.
        raise NotImplementedError

    @property
    def substantiated(self):
        return self.state == States.SUBSTANTIATED and self.conn is not None

    def substantiate(self, wfb, build):
        log.msg("substantiating worker %s" % (wfb,))

        if self.state == States.SUBSTANTIATED and self.conn is not None:
            self._setBuildWaitTimer()
            return defer.succeed(True)

        if self.state in [States.SUBSTANTIATING,
                          States.INSUBSTANTIATING_SUBSTANTIATING]:
            return self._substantiation_notifier.wait()

        self.startMissingTimer()
        self.substantiation_build = build

        # if anything of the following fails synchronously we need to have a
        # deferred ready to be notified
        d = self._substantiation_notifier.wait()

        if self.state == States.SUBSTANTIATED and self.conn is None:
            # connection dropped while we were substantiated.
            # insubstantiate to clean up and then substantiate normally.
            d_ins = self.insubstantiate(force_substantiation=True)
            d_ins.addErrback(log.err, 'while insubstantiating')
            return d

        assert self.state in [States.NOT_SUBSTANTIATED,
                              States.INSUBSTANTIATING]

        if self.state == States.NOT_SUBSTANTIATED:
            self.state = States.SUBSTANTIATING
            self._substantiate(build)
        else:
            self.state = States.INSUBSTANTIATING_SUBSTANTIATING
        return d

    @defer.inlineCallbacks
    def _substantiate(self, build):
        # register event trigger
        try:
            # if build_wait_timeout is negative we don't ever disconnect the
            # worker ourselves, so we don't need to wait for it to attach
            # to declare it as substantiated.
            dont_wait_to_attach = \
                self.build_wait_timeout < 0 and self.conn is not None

            start_success = yield self.start_instance(build)

            if not start_success:
                # this behaviour is kept as compatibility, but it is better
                # to just errback with a workable reason
                msg = "Worker does not want to substantiate at this time"
                raise LatentWorkerFailedToSubstantiate(self.name, msg)

            if dont_wait_to_attach and \
                    self.state == States.SUBSTANTIATING and \
                    self.conn is not None:
                log.msg(r"Worker %s substantiated (already attached)" %
                    (self.name,))
                self.state = States.SUBSTANTIATED
                self._fireSubstantiationNotifier(True)

        except Exception as e:
            self.stopMissingTimer()
            self._substantiation_failed(failure.Failure(e))
            # swallow the failure as it is notified

    def _fireSubstantiationNotifier(self, result):
        if not self._substantiation_notifier:
            log.msg("No substantiation deferred for %s" % (self.name,))
            return

        result_msg = 'success' if result is True else 'failure'
        log.msg("Firing {} substantiation deferred with {}".format(
            self.name, result_msg))

        self.substantiation_build = None
        self._substantiation_notifier.notify(result)

    @defer.inlineCallbacks
    def attached(self, bot):
        if self.state != States.SUBSTANTIATING and \
                self.build_wait_timeout >= 0:
            msg = 'Worker %s received connection while not trying to ' \
                'substantiate.  Disconnecting.' % (self.name,)
            log.msg(msg)
            self._disconnect(bot)
            raise RuntimeError(msg)

        try:
            yield super().attached(bot)
        except Exception:
            self._substantiation_failed(failure.Failure())
            return
        log.msg(r"Worker %s substantiated \o/" % (self.name,))

        # only change state when we are actually substantiating. We could
        # end up at this point in different state than SUBSTANTIATING if
        # build_wait_timeout is negative. When build_wait_timeout is not
        # negative, we throw an error (see above)
        if self.state == States.SUBSTANTIATING:
            self.state = States.SUBSTANTIATED
        self._fireSubstantiationNotifier(True)

    def attachBuilder(self, builder):
        wfb = self.workerforbuilders.get(builder.name)
        return wfb.attached(self, self.worker_commands)

    def _missing_timer_fired(self):
        self.missing_timer = None
        return self._substantiation_failed(defer.TimeoutError())

    def _substantiation_failed(self, failure):
        if self.state == States.SUBSTANTIATING:
            self.substantiation_build = None
            self._fireSubstantiationNotifier(failure)
        d = self.insubstantiate()
        d.addErrback(log.err, 'while insubstantiating')
        # notify people, but only if we're still in the config
        if not self.parent or not self.notify_on_missing:
            return

        return self.master.data.updates.workerMissing(
            workerid=self.workerid,
            masterid=self.master.masterid,
            last_connection="Latent worker never connected",
            notify=self.notify_on_missing
        )

    def canStartBuild(self):
        # we were disconnected, but all the builds are not yet cleaned up.
        if self.conn is None and self.building:
            return False
        return super().canStartBuild()

    def buildStarted(self, wfb):
        assert wfb.isBusy()
        self._clearBuildWaitTimer()

    def buildFinished(self, wfb):
        assert not wfb.isBusy()
        if not self.building:
            if self.build_wait_timeout == 0:
                # we insubstantiate asynchronously to trigger more bugs with
                # the fake reactor
                self.master.reactor.callLater(0, self._soft_disconnect)
                # insubstantiate will automatically retry to create build for
                # this worker
            else:
                self._setBuildWaitTimer()

        # AbstractWorker.buildFinished() will try to start the next build for
        # that worker
        super().buildFinished(wfb)

    def _clearBuildWaitTimer(self):
        if self.build_wait_timer is not None:
            if self.build_wait_timer.active():
                self.build_wait_timer.cancel()
            self.build_wait_timer = None

    def _setBuildWaitTimer(self):
        self._clearBuildWaitTimer()
        if self.build_wait_timeout <= 0:
            return
        self.build_wait_timer = self.master.reactor.callLater(
            self.build_wait_timeout, self._soft_disconnect)

    @defer.inlineCallbacks
    def insubstantiate(self, fast=False, force_substantiation=False):
        # force_substantiation=True means we'll try to substantiate a build with stored
        # substantiation_build at the end of substantiation

        log.msg("insubstantiating worker {}".format(self))
        if self.state == States.NOT_SUBSTANTIATED:
            return

        if self.state == States.INSUBSTANTIATING:
            yield self._insubstantiation_notifier.wait()
            return

        if self.state == States.INSUBSTANTIATING_SUBSTANTIATING:
            self.state = States.INSUBSTANTIATING
            self._fireSubstantiationNotifier(
                failure.Failure(LatentWorkerSubstantiatiationCancelled()))
            yield self._insubstantiation_notifier.wait()
            return

        notify_cancel = self.state == States.SUBSTANTIATING

        if force_substantiation:
            self.state = States.INSUBSTANTIATING_SUBSTANTIATING
        else:
            self.state = States.INSUBSTANTIATING
        self._clearBuildWaitTimer()
        d = self.stop_instance(fast)
        try:
            yield d
        except Exception as e:
            # The case of failure for insubstantiation is bad as we have a
            # left-over costing resource There is not much thing to do here
            # generically, so we must put the problem of stop_instance
            # reliability to the backend driver
            log.err(e, "while insubstantiating")

        assert self.state in [States.INSUBSTANTIATING,
                              States.INSUBSTANTIATING_SUBSTANTIATING]

        if notify_cancel and self._substantiation_notifier:
            # if worker already tried to attach() then _substantiation_notifier is already notified
            self._fireSubstantiationNotifier(
                failure.Failure(LatentWorkerSubstantiatiationCancelled()))

        if self.state == States.INSUBSTANTIATING_SUBSTANTIATING:
            self.state = States.SUBSTANTIATING
            if self._insubstantiation_notifier:
                self._insubstantiation_notifier.notify(True)
            self._substantiate(self.substantiation_build)
        elif self.state == States.INSUBSTANTIATING:
            self.state = States.NOT_SUBSTANTIATED
            if self._insubstantiation_notifier:
                self._insubstantiation_notifier.notify(True)
        else:
            pass
        self.botmaster.maybeStartBuildsForWorker(self.name)

    @defer.inlineCallbacks
    def _soft_disconnect(self, fast=False, stopping_service=False):
        if self.building:
            # wait until build finished
            # TODO: remove this behavior as AbstractWorker disconnects forcibly
            return
        # a negative build_wait_timeout means the worker should never be shut
        # down, so just disconnect.
        if not stopping_service and self.build_wait_timeout < 0:
            yield super().disconnect()
            return

        self.stopMissingTimer()

        # if master is stopping, we will never achieve consistent state, as workermanager
        # won't accept new connection
        if self._substantiation_notifier and self.master.running:
            log.msg("Weird: Got request to stop before started. Allowing "
                    "worker to start cleanly to avoid inconsistent state")
            yield self._substantiation_notifier.wait()
            log.msg("Substantiation complete, immediately terminating.")

        yield defer.DeferredList([
            super().disconnect(),
            self.insubstantiate(fast)
        ], consumeErrors=True, fireOnOneErrback=True)

    def disconnect(self):
        # This returns a Deferred but we don't use it
        self._soft_disconnect()
        # this removes the worker from all builders.  It won't come back
        # without a restart (or maybe a sighup)
        self.botmaster.workerLost(self)

    @defer.inlineCallbacks
    def stopService(self):
        # the worker might be insubstantiating from buildWaitTimeout
        if self.state in [States.INSUBSTANTIATING,
                          States.INSUBSTANTIATING_SUBSTANTIATING]:
            yield self._insubstantiation_notifier.wait()

        if self.conn is not None or self.state in [States.SUBSTANTIATING,
                                                   States.SUBSTANTIATED]:
            yield self._soft_disconnect(stopping_service=True)
        self._clearBuildWaitTimer()
        res = yield super().stopService()
        return res

    def updateWorker(self):
        """Called to add or remove builders after the worker has connected.

        Also called after botmaster's builders are initially set.

        @return: a Deferred that indicates when an attached worker has
        accepted the new builders and/or released the old ones."""
        for b in self.botmaster.getBuildersForWorker(self.name):
            if b.name not in self.workerforbuilders:
                b.addLatentWorker(self)
        return super().updateWorker()
