# Copyright (c) Microsoft Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from greenlet import greenlet
from pyee import AsyncIOEventEmitter

from playwright._impl._helper import ParsedMessagePayload, parse_error
from playwright._impl._transport import Transport


class Channel(AsyncIOEventEmitter):
    def __init__(self, connection: "Connection", guid: str) -> None:
        super().__init__()
        self._connection: Connection = connection
        self._guid = guid
        self._object: Optional[ChannelOwner] = None

    async def send(self, method: str, params: Dict = None) -> Any:
        return await self.inner_send(method, params, False)

    async def send_return_as_dict(self, method: str, params: Dict = None) -> Any:
        return await self.inner_send(method, params, True)

    async def inner_send(
        self, method: str, params: Optional[Dict], return_as_dict: bool
    ) -> Any:
        if params is None:
            params = {}
        callback = self._connection._send_message_to_server(self._guid, method, params)
        result = await callback.future
        # Protocol now has named return values, assume result is one level deeper unless
        # there is explicit ambiguity.
        if not result:
            return None
        assert isinstance(result, dict)
        if return_as_dict:
            return result
        if len(result) == 0:
            return None
        assert len(result) == 1
        key = next(iter(result))
        return result[key]

    def send_no_reply(self, method: str, params: Dict = None) -> None:
        if params is None:
            params = {}
        self._connection._send_message_to_server(self._guid, method, params)


class ChannelOwner(AsyncIOEventEmitter):
    def __init__(
        self,
        parent: Union["ChannelOwner", "Connection"],
        type: str,
        guid: str,
        initializer: Dict,
    ) -> None:
        super().__init__(loop=parent._loop)
        self._loop: asyncio.AbstractEventLoop = parent._loop
        self._dispatcher_fiber: Any = parent._dispatcher_fiber
        self._type = type
        self._guid = guid
        self._connection: Connection = (
            parent._connection if isinstance(parent, ChannelOwner) else parent
        )
        self._parent: Optional[ChannelOwner] = (
            parent if isinstance(parent, ChannelOwner) else None
        )
        self._objects: Dict[str, "ChannelOwner"] = {}
        self._channel = Channel(self._connection, guid)
        self._channel._object = self
        self._initializer = initializer

        self._connection._objects[guid] = self
        if self._parent:
            self._parent._objects[guid] = self

    def _wait_for_event_info_before(self, wait_id: str, api_name: str) -> None:
        self._connection._send_message_to_server(
            self._guid,
            "waitForEventInfo",
            {
                "info": {
                    "apiName": api_name,
                    "waitId": wait_id,
                    "phase": "before",
                    "stack": capture_call_stack(),
                }
            },
        )

    def _wait_for_event_info_after(
        self, wait_id: str, exception: Exception = None
    ) -> None:
        info = {"waitId": wait_id, "phase": "after"}
        if exception:
            info["error"] = str(exception)
        self._connection._send_message_to_server(
            self._guid,
            "waitForEventInfo",
            {"info": info},
        )

    def _dispose(self) -> None:
        # Clean up from parent and connection.
        if self._parent:
            del self._parent._objects[self._guid]
        del self._connection._objects[self._guid]

        # Dispose all children.
        for object in list(self._objects.values()):
            object._dispose()
        self._objects.clear()


class ProtocolCallback:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.stack_trace: traceback.StackSummary = traceback.StackSummary()
        self.future = loop.create_future()


class RootChannelOwner(ChannelOwner):
    def __init__(self, connection: "Connection") -> None:
        super().__init__(connection, "", "", {})


class Connection:
    def __init__(
        self, dispatcher_fiber: Any, object_factory: Any, driver_executable: Path
    ) -> None:
        self._dispatcher_fiber: Any = dispatcher_fiber
        self._transport = Transport(driver_executable)
        self._transport.on_message = lambda msg: self._dispatch(msg)
        self._waiting_for_object: Dict[str, Any] = {}
        self._last_id = 0
        self._objects: Dict[str, ChannelOwner] = {}
        self._callbacks: Dict[int, ProtocolCallback] = {}
        self._object_factory = object_factory
        self._is_sync = False
        self._api_name = ""

    async def run_as_sync(self) -> None:
        self._is_sync = True
        await self.run()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._root_object = RootChannelOwner(self)
        await self._transport.run()

    def stop_sync(self) -> None:
        self._transport.stop()
        self._dispatcher_fiber.switch()

    async def stop_async(self) -> None:
        self._transport.stop()
        await self._transport.wait_until_stopped()

    async def wait_for_object_with_known_name(self, guid: str) -> Any:
        if guid in self._objects:
            return self._objects[guid]
        callback = self._loop.create_future()

        def callback_wrapper(result: Any) -> None:
            callback.set_result(result)

        self._waiting_for_object[guid] = callback_wrapper
        return await callback

    def call_on_object_with_known_name(
        self, guid: str, callback: Callable[[Any], None]
    ) -> None:
        self._waiting_for_object[guid] = callback

    def _send_message_to_server(
        self, guid: str, method: str, params: Dict
    ) -> ProtocolCallback:
        self._last_id += 1
        id = self._last_id
        callback = ProtocolCallback(self._loop)
        task = asyncio.current_task(self._loop)
        callback.stack_trace = getattr(task, "__pw_stack_trace__", None)
        if not callback.stack_trace:
            callback.stack_trace = traceback.extract_stack()

        metadata = {"stack": serialize_call_stack(callback.stack_trace)}
        api_name = getattr(task, "__pw_api_name__", None)
        if api_name:
            metadata["apiName"] = api_name

        message = dict(
            id=id,
            guid=guid,
            method=method,
            params=self._replace_channels_with_guids(params, "params"),
            metadata=metadata,
        )
        self._transport.send(message)
        self._callbacks[id] = callback
        return callback

    def _dispatch(self, msg: ParsedMessagePayload) -> None:
        id = msg.get("id")
        if id:
            callback = self._callbacks.pop(id)
            if callback.future.cancelled():
                return
            error = msg.get("error")
            if error:
                parsed_error = parse_error(error["error"])  # type: ignore
                parsed_error.stack = "".join(
                    traceback.format_list(callback.stack_trace)[-10:]
                )
                callback.future.set_exception(parsed_error)
            else:
                result = self._replace_guids_with_channels(msg.get("result"))
                callback.future.set_result(result)
            return

        guid = msg["guid"]
        method = msg.get("method")
        params = msg["params"]
        if method == "__create__":
            parent = self._objects[guid]
            self._create_remote_object(
                parent, params["type"], params["guid"], params["initializer"]
            )
            return
        if method == "__dispose__":
            self._objects[guid]._dispose()
            return

        object = self._objects[guid]
        try:
            if self._is_sync:
                for listener in object._channel.listeners(method):
                    g = greenlet(listener)
                    g.switch(self._replace_guids_with_channels(params))
            else:
                object._channel.emit(method, self._replace_guids_with_channels(params))
        except Exception:
            print(
                "Error dispatching the event",
                "".join(traceback.format_exception(*sys.exc_info())),
            )

    def _create_remote_object(
        self, parent: ChannelOwner, type: str, guid: str, initializer: Dict
    ) -> Any:
        result: ChannelOwner
        initializer = self._replace_guids_with_channels(initializer)
        result = self._object_factory(parent, type, guid, initializer)
        if guid in self._waiting_for_object:
            self._waiting_for_object.pop(guid)(result)
        return result

    def _replace_channels_with_guids(self, payload: Any, param_name: str) -> Any:
        if payload is None:
            return payload
        if isinstance(payload, Path):
            return str(payload)
        if isinstance(payload, list):
            return list(
                map(lambda p: self._replace_channels_with_guids(p, "index"), payload)
            )
        if isinstance(payload, Channel):
            return dict(guid=payload._guid)
        if isinstance(payload, dict):
            result = {}
            for key in payload:
                result[key] = self._replace_channels_with_guids(payload[key], key)
            return result
        return payload

    def _replace_guids_with_channels(self, payload: Any) -> Any:
        if payload is None:
            return payload
        if isinstance(payload, list):
            return list(map(lambda p: self._replace_guids_with_channels(p), payload))
        if isinstance(payload, dict):
            if payload.get("guid") in self._objects:
                return self._objects[payload["guid"]]._channel
            result = {}
            for key in payload:
                result[key] = self._replace_guids_with_channels(payload[key])
            return result
        return payload


def from_channel(channel: Channel) -> Any:
    return channel._object


def from_nullable_channel(channel: Optional[Channel]) -> Optional[Any]:
    return channel._object if channel else None


def serialize_call_stack(stack_trace: traceback.StackSummary) -> List[Dict]:
    stack: List[Dict] = []
    for frame in stack_trace:
        if "_generated.py" in frame.filename:
            break
        stack.append(
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
        )
    stack.reverse()
    return stack


def capture_call_stack() -> List[Dict]:
    return serialize_call_stack(traceback.extract_stack())
