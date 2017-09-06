from contextlib import suppress
import signal
import multiprocessing
import asyncio
import os

from molotov.api import get_fixture
from molotov.listeners import EventSender
from molotov.stats import get_statsd_client
from molotov.sharedcounter import SharedCounters
from molotov.util import cancellable_sleep, stop, is_stopped, set_timer
from molotov.worker import Worker


class Runner(object):
    """Manages processes & workers and grabs results.
    """
    def __init__(self, args, loop=None):
        self.args = args
        self.console = self.args.shared_console
        if loop is None:
            loop = asyncio.get_event_loop()
        self.loop = loop
        if self.args.statsd:
            self.statsd = get_statsd_client(args.statsd_address,
                                            loop=self.loop)
        else:
            self.statsd = None
        self._tasks = []
        self._procs = []
        self._results = SharedCounters('WORKER', 'REACHED', 'RATIO', 'OK',
                                       'FAILED', 'MINUTE_OK', 'MINUTE_FAILED')
        self.eventer = EventSender(self.console)

    def gather(self, *futures):
        return asyncio.gather(*futures, loop=self.loop, return_exceptions=True)

    def ensure_future(self, coro):
        return asyncio.ensure_future(coro, loop=self.loop)

    def __call__(self):
        global_setup = get_fixture('global_setup')
        if global_setup is not None:
            try:
                global_setup(self.args)
            except Exception as e:
                self.console.print("The global_setup() fixture failed")
                self.console.print_error(e)
                raise

        try:
            return self._launch_processes()
        finally:
            global_teardown = get_fixture('global_teardown')
            if global_teardown is not None:
                try:
                    global_teardown()
                except Exception as e:
                    # we can't stop the teardown process
                    self.console.print_error(e)

    def _launch_processes(self):
        args = self.args
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        args.original_pid = os.getpid()

        if args.processes > 1:
            if not args.quiet:
                self.console.print('Forking %d processes' % args.processes)
            jobs = []
            for i in range(args.processes):
                p = multiprocessing.Process(target=self._process)
                jobs.append(p)
                p.start()

            for job in jobs:
                self._procs.append(job)

            async def run(quiet, console):
                while len(self._procs) > 0:
                    if not quiet:
                        console.print(self.display_results(), end='\r')
                    for job in jobs:
                        if job.exitcode is not None and job in self._procs:
                            self._procs.remove(job)
                    await cancellable_sleep(args.console_update)
                await self.console.stop()
                await self.eventer.stop()

            tasks = [self.ensure_future(self.console.display()),
                     self.ensure_future(self._send_workers_event(1)),
                     self.ensure_future(run(args.quiet, self.console))]
            self.loop.run_until_complete(self.gather(*tasks))
        else:
            self._process()

        return self._results

    def _shutdown(self, signal, frame):
        stop()
        self._kill_tasks()
        # send sigterms
        for proc in self._procs:
            proc.terminate()

    def _runner(self):
        args = self.args

        def _prepare():
            tasks = []
            delay = 0
            if args.ramp_up > 0.:
                step = args.ramp_up / args.workers
            else:
                step = 0.
            for i in range(self.args.workers):
                worker = Worker(i, self._results, self.console, self.args,
                                self.statsd, delay, self.loop)
                f = self.ensure_future(worker.run())
                tasks.append(f)
                delay += step
            return tasks

        if self.args.quiet:
            return _prepare()
        else:
            msg = 'Preparing {} worker{}'
            msg = msg.format(args.workers, 's' if args.workers > 1 else '')
            return self.console.print_block(msg, _prepare)

    def _process(self):
        set_timer()
        if self.args.processes > 1:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        if self.args.debug:
            self.console.print('**** RUNNING IN DEBUG MODE == SLOW ****')
            self.loop.set_debug(True)

        if self.args.original_pid == os.getpid():
            self._tasks.append(self.ensure_future(self._send_workers_event(1)))
            if not self.args.quiet:
                fut = self._display_results(self.args.console_update)
                update = self.ensure_future(fut)
                display = self.ensure_future(self.console.display())
                display = self.gather(update, display)
                self._tasks.append(display)

        workers = self.gather(*self._runner())
        workers.add_done_callback(lambda fut: stop())
        self._tasks.append(workers)

        try:
            self.loop.run_until_complete(self.gather(*self._tasks))
        finally:
            self._kill_tasks()
            if self.statsd is not None:
                self.statsd.close()
            self.loop.close()

    def _kill_tasks(self):
        cancellable_sleep.cancel_all()
        for task in reversed(self._tasks):
            with suppress(asyncio.CancelledError):
                task.cancel()
        for task in self._tasks:
            del task
        self._tasks[:] = []

    def display_results(self):
        ok, fail = self._results['OK'].value, self._results['FAILED'].value
        workers = self._results['WORKER'].value
        pat = 'SUCCESSES: %s | FAILURES: %s | WORKERS: %s'
        return pat % (ok, fail, workers)

    async def _display_results(self, update_interval):
        while not is_stopped():
            self.console.print(self.display_results(), end='\r')
            await cancellable_sleep(update_interval)
        await self.console.stop()

    async def _send_workers_event(self, update_interval):
        while not self.eventer.stopped() and not is_stopped():
            workers = self._results['WORKER'].value
            await self.eventer.send_event('current_workers', workers=workers)
            await cancellable_sleep(update_interval)
