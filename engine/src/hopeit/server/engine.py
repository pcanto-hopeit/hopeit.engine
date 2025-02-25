"""
Engine module: handle apps load, setup and serving
"""

import asyncio
import random
import uuid
from asyncio import CancelledError
from datetime import datetime, timezone
from typing import Awaitable, Optional, Dict, List, Union, Tuple, Any

from hopeit.server.imports import find_datobject_type, find_event_handler
from hopeit.server.steps import (
    split_event_stages,
    event_and_step,
    extract_module_steps,
    effective_steps,
)
from hopeit.toolkit import auth
from hopeit.app.config import (
    AppConfig,
    EventSettings,
    EventType,
    ReadStreamDescriptor,
    EventDescriptor,
    StreamQueue,
    StreamQueueStrategy,
)
from hopeit.app.context import EventContext, PostprocessHook, PreprocessHook
from hopeit.app.client import register_app_connections, stop_app_connections
from hopeit.dataobjects import DataObject, EventPayload
from hopeit.server.config import ServerConfig
from hopeit.server.events import EventHandler, get_event_settings, get_runtime_settings
from hopeit.streams import (
    StreamCircuitBreaker,
    stream_auth_info,
    StreamEvent,
    StreamOSError,
    StreamManager,
)
from hopeit.server.logger import engine_logger, extra_logger, combined
from hopeit.server.metrics import metrics, stream_metrics, StreamStats

__all__ = ["AppEngine", "Server"]

logger = engine_logger()
extra = extra_logger()


class AppEngine:
    """
    Engine that handles a Hopeit Application
    """

    def __init__(
        self,
        *,
        app_config: AppConfig,
        plugins: List[AppConfig],
        enabled_groups: List[str],
        streams_enabled: bool = True,
    ):
        """
        Creates an instance of the AppEngine

        :param app_config: AppConfig, Hopeit application configuration as specified in config module
        :param plugins: List of AppConfig, Hopeit application configurations for enabled plugins
        :enabled_groups: List of str, list of enabled event groups
        :streams_enabled: bool, for testing, set to False to disable automatic starting streams
        """
        self.app_config = app_config
        self.app_key = app_config.app_key()
        self.effective_events = self._config_effective_events(app_config, enabled_groups)
        self.plugins = plugins
        self.settings = get_runtime_settings(app_config, plugins)
        self.event_handler: Optional[EventHandler] = None
        self.streams_enabled = streams_enabled
        self.stream_manager: Optional[StreamManager] = None
        self._running: Dict[str, asyncio.Lock] = {
            event_name: asyncio.Lock()
            for event_name, event_info in self.effective_events.items()
            if event_info.type in (EventType.STREAM, EventType.SERVICE)
        }
        logger.init_app(app_config, plugins)

    async def start(self):
        """
        Starts handlers, services and pools for this application
        """
        self.event_handler = EventHandler(
            app_config=self.app_config,
            plugins=self.plugins,
            effective_events=self.effective_events,
            settings=self.settings,
        )
        streams_present = any(
            True
            for _, event_info in self.effective_events.items()
            if (event_info.type == EventType.STREAM) or (event_info.write_stream is not None)
        )
        if streams_present and self.streams_enabled:
            stream_config = self.app_config.server.streams
            mgr = StreamManager.create(stream_config)
            self.stream_manager = StreamCircuitBreaker(
                stream_manager=await mgr.connect(stream_config),
                initial_backoff_seconds=stream_config.initial_backoff_seconds,
                num_failures_open_circuit_breaker=stream_config.num_failures_open_circuit_breaker,
                max_backoff_seconds=stream_config.max_backoff_seconds,
            )
        auth.init(self.app_key, self.app_config.server.auth)
        await register_app_connections(self.app_config)
        return self

    async def stop(self):
        """
        Stops and clean handlers
        """
        logger.info(__name__, f"Stopping app={self.app_key}...")
        for event_name, running in self._running.items():
            if running.locked():
                await self.stop_event(event_name)
        if self.stream_manager:
            await asyncio.sleep((self.app_config.engine.read_stream_timeout + 5000) / 1000)
            await self.stream_manager.close()
        await stop_app_connections(self.app_key)
        logger.info(__name__, f"Stopped app={self.app_key}")

    async def execute(
        self,
        *,
        context: EventContext,
        query_args: Optional[dict],
        payload: Optional[EventPayload],
    ) -> Optional[EventPayload]:
        """
        Executes a configured Event of type GET or POST using received payload as input,
        considering configured timeout.

        :param context: EventContext, info about app, event and tracking
        :param query_args: dict, containing query arguments to be passed to every step of event
        :param payload: EventPayload, payload to send to event handler
        :return: EventPayload, result from executing the event
        :raise: TimeoutException in case configured timeout is exceeded before getting the result
        """
        try:
            return await asyncio.wait_for(
                self._execute_event(context, query_args, payload),
                timeout=context.settings.response_timeout,
            )
        except asyncio.TimeoutError as e:
            raise asyncio.TimeoutError(
                f"Response timeout exceeded seconds={context.settings.response_timeout}"
            ) from e

    async def _execute_event(
        self,
        context: EventContext,
        query_args: Optional[dict],
        payload: Optional[EventPayload],
        queue: str = StreamQueue.AUTO,
    ) -> Optional[EventPayload]:
        """
        Process results of executing event specified in context for a given input payload.
        In case event yield multiple results (i.e. Spawn[...]) they are collected in batches
        of configured batch_size and written into output stream if configured.

        :return: result of executing the event. In case of multiple results yield from event,
        last item will be returned. If no items are yield, None will be returned.
        """
        assert self.event_handler is not None, "event_handler not created. Call `start()`."
        if self.streams_enabled and (context.event_info.write_stream is not None):
            assert self.stream_manager, "stream_manager not initialized. Call `start()`."
        event_info = self.effective_events[context.event_name]
        batch_size = context.settings.stream.batch_size
        batch = []
        result = None
        async for result in self.event_handler.handle_async_event(
            context=context, query_args=query_args, payload=payload
        ):
            if result is not None:
                batch.append(result)
            if (len(batch) >= batch_size) and (event_info.write_stream is not None):
                await self._write_stream_batch(
                    batch=batch, context=context, event_info=event_info, queue=queue
                )
                batch.clear()
        if (len(batch) > 0) and (event_info.write_stream is not None):
            await self._write_stream_batch(
                batch=batch, context=context, event_info=event_info, queue=queue
            )
        return result

    async def _write_stream_batch(
        self,
        *,
        batch: List[EventPayload],
        context: EventContext,
        event_info: EventDescriptor,
        queue: str,
    ):
        await asyncio.gather(
            *[
                self._write_stream(
                    payload=item,
                    context=context,
                    event_info=event_info,
                    upstream_queue=queue,
                )
                for item in batch
            ]
        )

    async def _write_stream(
        self,
        *,
        payload: EventPayload,
        context: EventContext,
        event_info: EventDescriptor,
        upstream_queue: str,
    ):
        """
        Publish payload in configured one or more queues for a given configured stream
        """
        assert self.stream_manager is not None, "stream_manager not created. Call `start()`."
        assert event_info.write_stream is not None, "write_stream name not configured"
        assert context.settings.stream.compression, "stream compression not configured"
        assert context.settings.stream.serialization, "stream serialization not configured"

        for configured_queue in event_info.write_stream.queues:
            stream_name = event_info.write_stream.name
            if (
                upstream_queue != StreamQueue.AUTO
                and configured_queue == StreamQueue.AUTO
                and event_info.write_stream.queue_strategy == StreamQueueStrategy.PROPAGATE
            ):
                stream_name += f".{upstream_queue}"
            elif configured_queue != StreamQueue.AUTO:
                stream_name += f".{configured_queue}"

            queue_name = (
                configured_queue
                if event_info.write_stream.queue_strategy == StreamQueueStrategy.DROP
                else upstream_queue
            )

            await self.stream_manager.write_stream(
                stream_name=stream_name,
                queue=queue_name,
                payload=StreamManager.as_data_event(payload),
                track_ids=context.track_ids,
                auth_info=context.auth_info,
                compression=context.settings.stream.compression,
                serialization=context.settings.stream.serialization,
                target_max_len=context.settings.stream.target_max_len,
            )

    async def preprocess(
        self,
        *,
        context: EventContext,
        query_args: Optional[Dict[str, Any]],
        payload: Optional[EventPayload],
        request: PreprocessHook,
    ) -> Optional[EventPayload]:
        assert self.event_handler, "event_handler not created. Call `start()`."
        return await self.event_handler.preprocess(
            context=context, query_args=query_args, payload=payload, request=request
        )

    async def postprocess(
        self,
        *,
        context: EventContext,
        payload: Optional[EventPayload],
        response: PostprocessHook,
    ) -> Optional[EventPayload]:
        assert self.event_handler, "event_handler not created. Call `start()`."
        return await self.event_handler.postprocess(
            context=context, payload=payload, response=response
        )

    async def _process_stream_event_with_timeout(
        self,
        stream_event: StreamEvent,
        stream_info: ReadStreamDescriptor,
        stream_name: str,
        queue: str,
        context: EventContext,
        stats: StreamStats,
        log_info: Dict[str, str],
    ) -> Union[EventPayload, Exception]:
        """
        Invokes _process_stream_event with a configured timeout
        :return: result of _process_stream_event
        :raise: TimeoutError in case the event is not processed on the configured timeout
        """
        try:
            return await asyncio.wait_for(
                self._process_stream_event(
                    stream_event=stream_event,
                    stream_info=stream_info,
                    stream_name=stream_name,
                    queue=queue,
                    context=context,
                    stats=stats,
                    log_info=log_info,
                ),
                timeout=context.settings.stream.timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            terr = asyncio.TimeoutError(
                f"Stream processing timeout exceeded seconds={context.settings.stream.timeout}"
            )
            logger.error(
                context,
                str(terr),
                extra=extra(
                    prefix="stream.",
                    **{**log_info, "name": stream_name, "queue": queue},
                ),
            )
            return terr

    async def _read_stream_cycle(
        self,
        event_name: str,
        event_settings: EventSettings,
        stream_info: ReadStreamDescriptor,
        datatypes: Dict[str, type],
        offset: str,
        stats: StreamStats,
        log_info: Dict[str, str],
        test_mode: bool,
        last_err: Optional[StreamOSError],
    ) -> Tuple[
        Optional[Union[EventPayload, Exception]],
        Optional[EventContext],
        Optional[StreamOSError],
    ]:
        """
        Single read_stream cycle used from read_stream while loop to allow wait and retry/recover on failures
        Will read from multiple queues if configured, always starting from the first queue and stopping
        when batch_size is reached
        """
        assert self.stream_manager is not None
        assert stream_info.consumer_group is not None

        last_res, last_context = None, None

        batch: List[Awaitable[Union[EventPayload, Exception]]] = []

        for queue in stream_info.queues:
            stream_name = stream_info.name
            if queue != StreamQueue.AUTO:
                stream_name += f".{queue}"

            for stream_event in await self.stream_manager.read_stream(
                stream_name=stream_name,
                consumer_group=stream_info.consumer_group,
                datatypes=datatypes,
                track_headers=self.app_config.engine.track_headers,
                offset=offset,
                batch_size=event_settings.stream.batch_size,
                timeout=self.app_config.engine.read_stream_timeout,
                batch_interval=self.app_config.engine.read_stream_interval,
            ):
                stats.ensure_start()

                if isinstance(stream_event, Exception):
                    logger.error(__name__, stream_event)
                    stats.inc(error=True)
                else:
                    context = EventContext(
                        app_config=self.app_config,
                        plugin_config=self.app_config,
                        event_name=event_name,
                        settings=event_settings,
                        track_ids=stream_event.track_ids,
                        auth_info=stream_auth_info(stream_event),
                    )
                    last_context = context
                    logger.start(
                        context,
                        extra=extra(
                            prefix="stream.",
                            **{**log_info, "name": stream_name, "queue": queue},
                        ),
                    )
                    batch.append(
                        self._process_stream_event_with_timeout(
                            stream_event=stream_event,
                            stream_info=stream_info,
                            stream_name=stream_name,
                            queue=stream_event.queue,
                            context=context,
                            stats=stats,
                            log_info=log_info,
                        )
                    )

        if len(batch) != 0:
            for result in await asyncio.gather(*batch):
                last_res = result
        if last_context:
            logger.stats(last_context, extra=extra(prefix="metrics.stream.", **stats.calc()))
        if test_mode:
            self._running[event_name].release()
        if last_err is not None:
            logger.warning(__name__, f"Recovered read stream for event={event_name}.")
            last_err = None

        return last_res, last_context, last_err

    async def read_stream(
        self, *, event_name: str, test_mode: bool = False
    ) -> Optional[Union[EventPayload, Exception]]:
        """
        Listens to a stream specified by event of type STREAM, and executes
        the event handler for each received event in the stream.

        When invoked, stream will be read continuously consuming events that are
        arriving, processing it according to configured steps.
        To interrupt listening for events, call `stop_event(event_name)`

        :param event_name: str, an event name contained in app_config
        :param test_mode: bool, set to True to immediately stop and return results for testing
        :return: last result or exception, only intended to be used in test_mode
        """
        assert self.app_config.server is not None
        stats = StreamStats()
        log_info = {"app_key": self.app_key, "event_name": event_name}
        wait = self.app_config.server.streams.delay_auto_start_seconds
        if wait > 0:
            wait = int(wait / 2) + random.randint(0, wait) - random.randint(0, int(wait / 2))
            logger.info(
                __name__,
                f"Start reading stream: waiting seconds={wait}...",
                extra=extra(prefix="stream.", **log_info),
            )
            await asyncio.sleep(wait)
        logger.info(
            __name__,
            "Starting reading stream...",
            extra=extra(prefix="stream.", **log_info),
        )
        try:
            assert self.event_handler, "event_handler not created. Call `start()`."
            assert self.stream_manager, "No active stream manager. Call `start()`"
            assert not self._running[event_name].locked(), (
                "Event already running. Call `stop_event(...)`"
            )

            event_config = self.effective_events[event_name]
            stream_info = event_config.read_stream
            assert stream_info, f"No read_stream section in config for event={event_name}"
            event_settings = get_event_settings(self.settings, event_name)
            assert not self._running[event_name].locked(), f"Event already running {event_name}"
            await self._running[event_name].acquire()

            for queue in stream_info.queues:
                while self._running[event_name].locked():
                    try:
                        await self.stream_manager.ensure_consumer_group(
                            stream_name=(
                                f"{stream_info.name}.{queue}"
                                if queue != StreamQueue.AUTO
                                else stream_info.name
                            ),
                            consumer_group=stream_info.consumer_group,
                        )
                        break  # success, do not try anymoere
                    except StreamOSError:
                        pass  # Will retry when circuit breaker opens

            datatypes = self._find_stream_datatype_handlers(event_name, event_config)
            log_info["name"] = stream_info.name
            log_info["consumer_group"] = stream_info.consumer_group
            logger.info(
                __name__,
                "Consuming stream...",
                extra=extra(prefix="stream.", **log_info),
            )
            offset = ">"
            last_res, last_context, last_err = None, None, None
            while self._running[event_name].locked():
                last_res, last_context, last_err = await self._read_stream_cycle(
                    event_name,
                    event_settings,
                    stream_info,
                    datatypes,
                    offset,
                    stats,
                    log_info,
                    test_mode,
                    last_err,
                )
            logger.info(
                __name__,
                "Stopped read_stream.",
                extra=extra(prefix="stream.", **log_info),
            )
            if last_context is None:
                logger.warning(__name__, f"No stream events consumed in {event_name}")
            return last_res
        except (AssertionError, NotImplementedError) as e:
            logger.error(__name__, e)
            logger.error(__name__, f"Unexpectedly stopped read stream for event={event_name}")
            return e

    async def _process_stream_event(
        self,
        *,
        stream_event: StreamEvent,
        stream_info: ReadStreamDescriptor,
        stream_name: str,
        queue: str,
        context: EventContext,
        stats: StreamStats,
        log_info: Dict[str, str],
    ) -> Optional[Union[EventPayload, Exception]]:
        """
        Process a single stream event, execute events, ack if not failed, log error if fail

        :return: results of executing the event, or Exception if errors during processing
        """
        try:
            assert self.event_handler
            assert self.stream_manager

            result = await self._execute_event(
                context=context,
                query_args=None,
                payload=stream_event.payload,
                queue=stream_event.queue,
            )
            await self.stream_manager.ack_read_stream(
                stream_name=stream_name,
                consumer_group=stream_info.consumer_group,
                stream_event=stream_event,
            )
            logger.done(
                context,
                extra=combined(
                    extra(
                        prefix="stream.",
                        **{**log_info, "name": stream_name, "queue": queue},
                    ),
                    metrics(context),
                    stream_metrics(context),
                ),
            )
            stats.inc()
            return result
        except CancelledError as e:
            extra_info = {**log_info, "name": stream_name, "queue": queue}
            logger.error(context, "Cancelled", extra=extra(prefix="stream.", **extra_info))
            logger.failed(context, extra=extra(prefix="stream.", **extra_info))
            stats.inc(error=True)
            return e
        except Exception as e:  # pylint: disable=broad-except
            extra_info = {**log_info, "name": stream_name, "queue": queue}
            logger.error(context, e, extra=extra(prefix="stream.", **extra_info))
            logger.failed(context, extra=extra(prefix="stream.", **extra_info))
            stats.inc(error=True)
            return e

    def _service_event_context(
        self,
        event_name: str,
        event_settings: EventSettings,
        previous_context: Optional[EventContext] = None,
    ) -> EventContext:
        """Creates context for a service event"""
        if previous_context is None:
            track_ids = {
                "track.request_id": str(uuid.uuid4()),
                "track.request_ts": datetime.now(tz=timezone.utc).isoformat(),
            }
        else:
            track_ids = previous_context.track_ids
        return EventContext(
            app_config=self.app_config,
            plugin_config=self.app_config,
            event_name=event_name,
            settings=event_settings,
            track_ids={
                **track_ids,
                "track.operation_id": str(uuid.uuid4()),
            },
            auth_info={},
        )

    async def service_loop(
        self, *, event_name: str, test_mode: bool = False
    ) -> Optional[Union[EventPayload, Exception]]:
        """
        Service loop, executes `__service__` handler in event and execute
        event steps for each yielded payload.

        :param event_name: str, an event name contained in app_config
        :param test_mode: bool, set to True to immediately stop and return results for testing
        :return: last result or exception, only intended to be used in test_mode
        """
        assert self.app_config.server is not None
        log_info = {"app_key": self.app_key, "event_name": event_name}
        assert not self._running[event_name].locked(), (
            f"Cannot start service, event already running {event_name}"
        )
        await self._running[event_name].acquire()
        wait = self.app_config.server.streams.delay_auto_start_seconds
        if wait > 0:
            wait = int(wait / 2) + random.randint(0, wait) - random.randint(0, int(wait / 2))
            logger.info(
                __name__,
                f"Start service: waiting seconds={wait}...",
                extra=extra(prefix="service.", **log_info),
            )
            await asyncio.sleep(wait)
        logger.info(__name__, "Starting service...", extra=extra(prefix="service.", **log_info))
        event_config = self.effective_events[event_name]
        impl = find_event_handler(
            app_config=self.app_config, event_name=event_name, event_info=event_config
        )
        service_handler = getattr(impl, "__service__")
        assert service_handler is not None, (
            f"{event_name} must implement method `__service__(context) -> Spawn[...]` to run as a service"
        )
        event_settings = get_event_settings(self.settings, event_name)
        context = self._service_event_context(event_name=event_name, event_settings=event_settings)
        last_result = None
        if self._running[event_name].locked():
            async for payload in service_handler(context):
                try:
                    context = self._service_event_context(
                        event_name=event_name,
                        event_settings=event_settings,
                        previous_context=context,
                    )
                    logger.start(context, extra=extra(prefix="service.", **log_info))
                    last_result = await self.execute(
                        context=context, query_args=None, payload=payload
                    )
                    logger.done(context, extra=extra(prefix="service.", **log_info))
                    if not self._running[event_name].locked():
                        logger.info(
                            __name__,
                            "Stopping service loop...",
                            extra=extra(prefix="service.", **log_info),
                        )
                        break
                except CancelledError as e:
                    logger.error(context, "Cancelled", extra=extra(prefix="service.", **log_info))
                    logger.failed(context, extra=extra(prefix="service.", **log_info))
                    last_result = e
                except Exception as e:  # pylint: disable=broad-except
                    logger.error(context, e, extra=extra(prefix="service.", **log_info))
                    logger.failed(context, extra=extra(prefix="service.", **log_info))
                    last_result = e
                if test_mode:
                    self._running[event_name].release()
                    return last_result
        else:
            logger.info(__name__, "Stopped service.", extra=extra(prefix="service.", **log_info))
        logger.info(__name__, "Finished service.", extra=extra(prefix="service.", **log_info))
        return last_result

    def is_running(self, event_name) -> bool:
        return self._running[event_name].locked()

    async def stop_event(self, event_name: str):
        """
        Sets running state to stopped for a continuous-running event.
        This acts as signling for stop for STREAM events.

        :param event_name: name of the event to signal stop
        """
        if self._running[event_name].locked():
            self._running[event_name].release()
        else:
            raise RuntimeError(f"Cannot stop non running event: {event_name}.")

    @staticmethod
    def _config_effective_events(
        app_config: AppConfig, enabled_groups: List[str]
    ) -> Dict[str, EventDescriptor]:
        """
        Return effective events computed from user app config.

        Effective events could be result of splitting a single event in stages,
        using the "SHUFFLE" keyword, that will internaally generate 2 events.
        Or for STREAMS that implements the `__service__` method, both a STREAM
        and a SERVICE event will be generated.
        Only events with groups defined in `enabled_groups` list will be returned.
        If `enabled_groups` is empty, all events wil be considered.
        """
        effective_events: Dict[str, EventDescriptor] = {}
        assert app_config.server
        for event_name, event_info in app_config.events.items():
            if (
                len(enabled_groups) == 0
                or event_info.group == EventDescriptor.DEFAULT_GROUP
                or event_info.group in enabled_groups
            ):
                impl = find_event_handler(
                    app_config=app_config, event_name=event_name, event_info=event_info
                )
                # Add events resultant of splitting steps on SHUFFLE (stages)
                splits = split_event_stages(app_config.app, event_name, event_info, impl)
                effective_events.update(**splits)
                # Add associated SERVICE events to streams
                if event_info.type == EventType.STREAM and hasattr(impl, "__service__"):
                    effective_events[f"{event_name}$__service__"] = EventDescriptor(
                        type=EventType.SERVICE,
                        connections=event_info.connections,
                        impl=event_info.impl,
                    )
        return effective_events

    def _find_stream_datatype_handlers(
        self, event_name: str, event_info: EventDescriptor
    ) -> Dict[str, type]:
        """
        Computes a dictionary of `{datatype name: datatype class}` that event steps
        can be handle when consuming from an stream.
        """
        base_event, _ = event_and_step(event_name)
        impl = find_event_handler(
            app_config=self.app_config, event_name=base_event, event_info=event_info
        )
        all_steps = extract_module_steps(impl)
        steps = effective_steps(event_name, all_steps)
        datatypes = {}
        for _, _, step in steps:
            _, datatype, _, _ = step
            if hasattr(datatype, "__stream_event__"):
                datatypes[f"{datatype.__module__}.{datatype.__qualname__}"] = datatype
            elif datatype is DataObject:
                for type_name in event_info.dataobjects:
                    datatype = find_datobject_type(type_name)
                    datatypes[f"{datatype.__module__}.{datatype.__qualname__}"] = datatype
        if len(datatypes) == 0:
            raise NotImplementedError(
                f"No data types found to read from stream in event={event_name}. "
                "Dataclasses must be decorated with `@dataobject` to be used in streams"
            )
        return datatypes


class Server:
    """
    Server engine.
    Call `start()` to create an instance of the engine
    Then start individual apps by calling `start_app(...)`
    End apps and stop engine with `stop()`
    """

    def __init__(self):
        self.app_engines = {}

    async def start(self, *, config: ServerConfig):
        """
        Starts a engine instance
        """
        global logger
        logger = engine_logger().init_server(config)
        logger.info(__name__, "Starting engine...")
        return self

    async def stop(self):
        """
        Stops every active app in the engine instance.
        """
        logger.info(__name__, "Stopping engine...")
        for _, app_engine in self.app_engines.items():
            await app_engine.stop()
        logger.info(__name__, "Engine stopped.")

    async def start_app(self, app_config: AppConfig, enabled_groups: List[str]):
        """
        Starts and register a Hopeit App into this engine instance

        :param app_config: AppConfig, app configuration as specified in config module
        """
        logger.info(__name__, f"Starting app={app_config.app_key()}...")
        plugins = [
            self.app_engine(app_key=plugin.app_key()).app_config for plugin in app_config.plugins
        ]
        app_engine = await AppEngine(
            app_config=app_config, plugins=plugins, enabled_groups=enabled_groups
        ).start()
        self.app_engines[app_config.app_key()] = app_engine
        return app_engine

    def app_engine(self, *, app_key: str) -> AppEngine:
        return self.app_engines[app_key]
