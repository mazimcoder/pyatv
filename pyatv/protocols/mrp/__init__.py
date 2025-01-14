"""Implementation of the MediaRemoteTV Protocol used by ATV4 and later."""

import asyncio
import crypt
import datetime
import encodings.utf_8
import logging
import math
import re
import threading
import time
from typing import Any, Dict, Generator, List, Mapping, Optional, Set, Tuple
import uuid
from pyatv import exceptions
from pyatv.CompanionApi import atvMeta, atvConfigAir
from pyatv.auth.hap_srp import SRPAuthHandler
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    InputAction,
    MediaType,
    OperatingSystem,
    PairingRequirement,
    PowerState,
    Protocol,
    RepeatState,
    ShuffleState,
)
from pyatv.core import MutableService, SetupData, TakeoverMethod, mdns
from pyatv.core.scan import ScanHandler, ScanHandlerReturn
from pyatv.helpers import get_unique_id
from pyatv.interface import (
    App,
    ArtworkInfo,
    Audio,
    BaseConfig,
    BaseService,
    DeviceInfo,
    FeatureInfo,
    Features,
    Metadata,
    PairingHandler,
    Playing,
    Power,
    PushUpdater,
    RemoteControl,
)
from pyatv.protocols.mrp import messages, protobuf
from pyatv.protocols.mrp.connection import AbstractMrpConnection, MrpConnection
from pyatv.protocols.mrp.pairing import MrpPairingHandler
from pyatv.protocols.mrp.player_state import PlayerState, PlayerStateManager
from pyatv.protocols.mrp.protobuf import CommandInfo_pb2, ProtocolMessage_pb2, GetRemoteTextInputSessionMessage
from pyatv.protocols.mrp.protobuf import ContentItemMetadata as cim
from pyatv.protocols.mrp.protobuf import PlaybackState
from pyatv.protocols.mrp.protobuf.ProtocolMessage_pb2 import ProtocolMessage
from pyatv.protocols.mrp.protocol import MrpProtocol
from pyatv.support.cache import Cache
from pyatv.support.device_info import lookup_model, lookup_version
from pyatv.support.http import ClientSessionManager
from pyatv.support.state_producer import StateProducer

_LOGGER = logging.getLogger(__name__)

# Source: https://github.com/Daij-Djan/DDHidLib/blob/master/usb_hid_usages.txt
_KEY_LOOKUP = {
    # name: [usage_page, usage]
    "up": (1, 0x8C),
    "down": (1, 0x8D),
    "left": (1, 0x8B),
    "right": (1, 0x8A),
    "stop": (12, 0xB7),
    "next": (12, 0xB5),
    "previous": (12, 0xB6),
    "select": (1, 0x89),
    "menu": (1, 0x86),
    "topmenu": (12, 0x60),
    "home": (12, 0x40),
    "suspend": (1, 0x82),
    "wakeup": (1, 0x83),
    "volume_up": (12, 0xE9),
    "volume_down": (12, 0xEA),
    # 'mic': (12, 0x04),  # Siri
}  # Dict[str, Tuple[int, int]]

_FEATURES_SUPPORTED: List[FeatureName] = [
    FeatureName.Down,
    FeatureName.Home,
    FeatureName.HomeHold,
    FeatureName.Left,
    FeatureName.Menu,
    FeatureName.Right,
    FeatureName.Select,
    FeatureName.TopMenu,
    FeatureName.Up,
    FeatureName.TurnOn,
    FeatureName.TurnOff,
    FeatureName.PowerState,
]

_FEATURE_COMMAND_MAP = {
    FeatureName.Next: CommandInfo_pb2.NextTrack,
    FeatureName.Pause: CommandInfo_pb2.Pause,
    FeatureName.Play: CommandInfo_pb2.Play,
    FeatureName.PlayPause: CommandInfo_pb2.TogglePlayPause,
    FeatureName.Previous: CommandInfo_pb2.PreviousTrack,
    FeatureName.Stop: CommandInfo_pb2.Stop,
    FeatureName.SetPosition: CommandInfo_pb2.SeekToPlaybackPosition,
    FeatureName.SetRepeat: CommandInfo_pb2.ChangeRepeatMode,
    FeatureName.SetShuffle: CommandInfo_pb2.ChangeShuffleMode,
    FeatureName.Shuffle: CommandInfo_pb2.ChangeShuffleMode,
    FeatureName.Repeat: CommandInfo_pb2.ChangeRepeatMode,
    FeatureName.SkipForward: CommandInfo_pb2.SkipForward,
    FeatureName.SkipBackward: CommandInfo_pb2.SkipBackward,
}

# Features that are considered available if corresponding
_FIELD_FEATURES: Dict[FeatureName, str] = {
    FeatureName.Title: "title",
    FeatureName.Artist: "trackArtistName",
    FeatureName.Album: "albumName",
    FeatureName.Genre: "genre",
    FeatureName.TotalTime: "duration",
    FeatureName.SeriesName: "seriesName",
    FeatureName.Position: "elapsedTimeTimestamp",
    FeatureName.SeasonNumber: "seasonNumber",
    FeatureName.EpisodeNumber: "episodeNumber",
    FeatureName.ContentIdentifier: "contentIdentifier",
}

DELAY_BETWEEN_COMMANDS = 0.1


def _cocoa_to_timestamp(time):
    delta = datetime.datetime(2001, 1, 1) - datetime.datetime(1970, 1, 1)
    time_seconds = (datetime.timedelta(seconds=time) + delta).total_seconds()
    return datetime.datetime.fromtimestamp(time_seconds)


def build_playing_instance(  # pylint: disable=too-many-locals
        state: PlayerState,
) -> Playing:
    """Build a Playing instance from play state."""

    def media_type() -> MediaType:
        """Type of media is currently playing, e.g. video, music."""
        if state.metadata:
            media_type = state.metadata.mediaType
            if media_type == cim.Audio:
                return MediaType.Music
            if media_type == cim.Video:
                return MediaType.Video

        return MediaType.Unknown

    def device_state() -> DeviceState:
        """Device state, e.g. playing or paused."""
        return {
            None: DeviceState.Idle,
            PlaybackState.Playing: DeviceState.Playing,
            PlaybackState.Paused: DeviceState.Paused,
            PlaybackState.Stopped: DeviceState.Stopped,
            PlaybackState.Interrupted: DeviceState.Loading,
            PlaybackState.Seeking: DeviceState.Seeking,
        }.get(state.playback_state, DeviceState.Paused)

    def title() -> Optional[str]:
        """Title of the current media, e.g. movie or song name."""
        return state.metadata_field("title")

    def artist() -> Optional[str]:
        """Artist of the currently playing song."""
        return state.metadata_field("trackArtistName")

    def album() -> Optional[str]:
        """Album of the currently playing song."""
        return state.metadata_field("albumName")

    def genre() -> Optional[str]:
        """Genre of the currently playing song."""
        return state.metadata_field("genre")

    def total_time() -> Optional[int]:
        """Total play time in seconds."""
        duration = state.metadata_field("duration")
        if duration is None or math.isnan(duration):
            return None
        return int(duration)

    def position() -> Optional[int]:
        """Position in the playing media (seconds)."""
        elapsed_timestamp = state.metadata_field("elapsedTimeTimestamp")

        # If we don't have reference time, we can't do anything
        if not elapsed_timestamp:
            return None

        elapsed_time: int = state.metadata_field("elapsedTime") or 0
        diff = (
                datetime.datetime.now() - _cocoa_to_timestamp(elapsed_timestamp)
        ).total_seconds()

        if device_state() == DeviceState.Playing:
            return int(elapsed_time + diff)
        return int(elapsed_time)

    def shuffle() -> ShuffleState:
        """If shuffle is enabled or not."""
        info = state.command_info(CommandInfo_pb2.ChangeShuffleMode)
        if info is None:
            return ShuffleState.Off
        if info.shuffleMode == protobuf.ShuffleMode.Off:
            return ShuffleState.Off
        if info.shuffleMode == protobuf.ShuffleMode.Albums:
            return ShuffleState.Albums

        return ShuffleState.Songs

    def repeat() -> RepeatState:
        """Repeat mode."""
        info = state.command_info(CommandInfo_pb2.ChangeRepeatMode)
        if info is None:
            return RepeatState.Off
        if info.repeatMode == protobuf.RepeatMode.One:
            return RepeatState.Track
        if info.repeatMode == protobuf.RepeatMode.All:
            return RepeatState.All

        return RepeatState.Off

    def item_hash() -> str:
        """Create a unique hash for what is currently playing."""
        return state.item_identifier

    def series_name() -> str:
        """Series name."""
        return state.metadata_field("seriesName")

    def season_number() -> int:
        """Season number."""
        return state.metadata_field("seasonNumber")

    def episode_number() -> int:
        """Episode number."""
        return state.metadata_field("episodeNumber")

    def content_identifier() -> str:
        """Content identifier."""
        return state.metadata_field("contentIdentifier")

    return Playing(
        media_type=media_type(),
        device_state=device_state(),
        title=title(),
        artist=artist(),
        album=album(),
        genre=genre(),
        total_time=total_time(),
        position=position(),
        shuffle=shuffle(),
        repeat=repeat(),
        hash=item_hash(),
        series_name=series_name(),
        season_number=season_number(),
        episode_number=episode_number(),
        content_identifier=content_identifier(),
    )


async def _send_hid_key(protocol: MrpProtocol, key: str, action: InputAction) -> None:
    async def _do_press(keycode: Tuple[int, int], hold: bool):
        await protocol.send(messages.send_hid_event(keycode[0], keycode[1], True))

        if hold:
            # Hardcoded hold time for one second
            await asyncio.sleep(1)

        await protocol.send(messages.send_hid_event(keycode[0], keycode[1], False))

    keycode = _KEY_LOOKUP.get(key)
    if not keycode:
        raise exceptions.NotSupportedError(f"unsupported key: {key}")

    if action == InputAction.SingleTap:
        await _do_press(keycode, False)
    elif action == InputAction.DoubleTap:
        await _do_press(keycode, False)
        await _do_press(keycode, False)
    elif action == InputAction.Hold:
        await _do_press(keycode, True)
    else:
        raise exceptions.NotSupportedError(f"unsupported input action: {action}")


def _send_text(protocol: protocol, text: str, devid: str) -> []:
    try:
        k = []
        cnt = 0
        l = len(text)
        ev.set()
        for t in text:
            k.append(_send_keyboard(protocol=protocol, key=t, devid=devid, action=InputAction.SingleTap, fn=0x00))
            cnt += 1
        return k
    except Exception as ex:
        print(f'Error _send_text {ex}')
        raise ex


ev = threading.Event()


def _send_keyboard(protocol: MrpProtocol, key: str, action: InputAction, fn: int, devid: str) -> []:
    try:

        async def task_do_press(keycode: [int], action: InputAction):
            try:
                if keycode[0] == 1:
                    await protocol.send(messages.send_hid_event(keycode[0], keycode[1], True))

                elif action == InputAction.Hold:
                    await protocol.send(messages.send_hid_event(keycode[0], keycode[1], True))
                elif action == InputAction.Release:
                    await protocol.send(messages.send_hid_event(keycode[0], keycode[1], False))
                elif action == InputAction.SingleTap or action == InputAction.DoubleTap:
                    await protocol.send(messages.send_hid_event(keycode[0], keycode[1], True))
                    await protocol.send(messages.send_hid_event(keycode[0], keycode[1], False))
                    print(f'send >>>>{keycode}')
                time.sleep(1)
                ev.set()
            except Exception as ex:
                print(f'Error task_do_press {ex}')
                ev.set()

        def thread_do_press(keycode: [int], action: InputAction):
            loop = asyncio.new_event_loop()
            ev.wait(5)
            print(f'tasking >>>>{keycode}')
            future = loop.create_task(coro=task_do_press(keycode=keycode, action=action))
            loop.run_until_complete(future)

            ev.clear()
            loop.close()

        keycode = [7, 0x00]
        if fn != 0:
            keycode = [int(key, base=16), fn]
        else:
            ch = ord(key)
            if key.isalpha():  # 0x04    >>>     0x1D
                if key.isupper() is False:
                    keycode = [7, ch - 93]
                else:
                    k = ord(key.lower())
                    keycode = [7, k - 93]
            elif key.isnumeric():  # 0x59    >>>     0x62
                if key.__eq__('0'):
                    keycode = [7, ch + 50]
                else:
                    keycode = [7, ch + 40]
            elif key.__eq__(' '):
                keycode = [7, 0x2C]
            else:
                keycode = [7, ch - 44]

        #   0x2D    -
        #   0x2E    =
        #   0x2F    [
        #   0x30    ]
        #   0x31    \
        #   0x32    \
        #   0x33    ;
        #   0x34    '
        #   0x35    `
        #   0x36    ,
        #   0x37    .
        #   0x38    /
        # spacebar  0x2C
        # backspace 0x2A
        #   clear   0x9C

        _hold = False
        xth = 1
        if action == InputAction.DoubleTap:
            _hold = False
            xth = 2
        while xth > 0:
            press_th = threading.Thread(target=thread_do_press, args=(keycode, action))
            press_th.start()
            xth -= 1
        return keycode
    except Exception as ex:
        print(f'Error _send_keyboard {ex}')
        raise ex


# pylint: disable=too-many-public-methods
class MrpRemoteControl(RemoteControl):
    """Implementation of API for controlling an Apple TV."""

    def __init__(
            self,
            loop: asyncio.AbstractEventLoop,
            psm: PlayerStateManager,
            protocol: MrpProtocol,
    ) -> None:
        """Initialize a new MrpRemoteControl."""
        self.loop = loop
        self.psm = psm
        self.protocol = protocol
        self._add_listeners()
        ##### Listner here Mustafa

    def _add_listeners(self):
        try:
            msgs = [
                # protobuf.ProtocolMessage.AUDIO_FADE_MESSAGE,
                # protobuf.ProtocolMessage.AUDIO_FADE_RESPONSE_MESSAGE,
                # protobuf.ProtocolMessage.CLIENT_UPDATES_CONFIG_MESSAGE,
                # protobuf.ProtocolMessage.CRYPTO_PAIRING_MESSAGE,
                # protobuf.ProtocolMessage.DEVICE_INFO_MESSAGE,
                # protobuf.ProtocolMessage.DEVICE_INFO_UPDATE_MESSAGE,
                # protobuf.ProtocolMessage.GENERIC_MESSAGE,
                # protobuf.ProtocolMessage.GET_KEYBOARD_SESSION_MESSAGE,
                protobuf.ProtocolMessage.KEYBOARD_MESSAGE,
                # protobuf.ProtocolMessage.GET_REMOTE_TEXT_INPUT_SESSION_MESSAGE,
                # protobuf.ProtocolMessage.GET_VOLUME_MESSAGE,
                # protobuf.ProtocolMessage.GET_VOLUME_RESULT_MESSAGE,
                # protobuf.ProtocolMessage.KEYBOARD_MESSAGE,
                # protobuf.ProtocolMessage.NOTIFICATION_MESSAGE,
                # protobuf.ProtocolMessage.ORIGIN_CLIENT_PROPERTIES_MESSAGE,
                # protobuf.ProtocolMessage.PLAYBACK_QUEUE_REQUEST_MESSAGE,
                # protobuf.ProtocolMessage.PLAYER_CLIENT_PROPERTIES_MESSAGE,
                # protobuf.ProtocolMessage.REGISTER_FOR_GAME_CONTROLLER_EVENTS_MESSAGE,
                protobuf.ProtocolMessage.REGISTER_HID_DEVICE_MESSAGE,
                protobuf.ProtocolMessage.REGISTER_HID_DEVICE_RESULT_MESSAGE,
                # protobuf.ProtocolMessage.REGISTER_VOICE_INPUT_DEVICE_MESSAGE,
                # protobuf.ProtocolMessage.REGISTER_VOICE_INPUT_DEVICE_RESPONSE_MESSAGE,
                # protobuf.ProtocolMessage.REMOTE_TEXT_INPUT_MESSAGE,
                # protobuf.ProtocolMessage.REMOVE_CLIENT_MESSAGE,
                # protobuf.ProtocolMessage.REMOVE_ENDPOINTS_MESSAGE,
                # protobuf.ProtocolMessage.REMOVE_OUTPUT_DEVICES_MESSAGE,
                # protobuf.ProtocolMessage.REMOVE_PLAYER_MESSAGE,
                # protobuf.ProtocolMessage.SEND_COMMAND_MESSAGE,
                # protobuf.ProtocolMessage.SEND_COMMAND_RESULT_MESSAGE,
                protobuf.ProtocolMessage.SEND_HID_EVENT_MESSAGE,
                protobuf.ProtocolMessage.SEND_HID_REPORT_MESSAGE,
                protobuf.ProtocolMessage.timestamp,
                # protobuf.ProtocolMessage.SEND_PACKED_VIRTUAL_TOUCH_EVENT_MESSAGE,
                # protobuf.ProtocolMessage.SEND_VOICE_INPUT_MESSAGE,
                # protobuf.ProtocolMessage.SET_ARTWORK_MESSAGE,
                # protobuf.ProtocolMessage.SET_CONNECTION_STATE_MESSAGE,
                # protobuf.ProtocolMessage.SET_DEFAULT_SUPPORTED_COMMANDS_MESSAGE,
                # protobuf.ProtocolMessage.SET_DISCOVERY_MODE_MESSAGE,
                # protobuf.ProtocolMessage.SET_HILITE_MODE_MESSAGE,
                # protobuf.ProtocolMessage.SET_NOW_PLAYING_CLIENT_MESSAGE,
                # protobuf.ProtocolMessage.SET_NOW_PLAYING_PLAYER_MESSAGE,
                # protobuf.ProtocolMessage.SET_RECORDING_STATE_MESSAGE,
                # protobuf.ProtocolMessage.SET_STATE_MESSAGE,
                # protobuf.ProtocolMessage.SET_VOLUME_MESSAGE,
                # protobuf.ProtocolMessage.TEXT_INPUT_MESSAGE,
                # protobuf.ProtocolMessage.TRANSACTION_MESSAGE,
                # protobuf.ProtocolMessage.UPDATE_CLIENT_MESSAGE,
                # protobuf.ProtocolMessage.UPDATE_CONTENT_ITEM_ARTWORK_MESSAGE,
                # protobuf.ProtocolMessage.UPDATE_CONTENT_ITEM_MESSAGE,
                # protobuf.ProtocolMessage.UPDATE_END_POINTS_MESSAGE,
                # protobuf.ProtocolMessage.UPDATE_OUTPUT_DEVICE_MESSAGE,
                # protobuf.ProtocolMessage.VOLUME_CONTROL_AVAILABILITY_MESSAGE,
                # protobuf.ProtocolMessage.VOLUME_CONTROL_CAPABILITIES_DID_CHANGE_MESSAGE,
                # protobuf.ProtocolMessage.VOLUME_DID_CHANGE_MESSAGE,
                # protobuf.ProtocolMessage.WAKE_DEVICE_MESSAGE,

            ]
            # for m in msgs:
            #     self.protocol.listen_to()   #old.add_listener(
            #         self.Keyboard_changed,
            #         m,
            #     )
            # protobuf.DEVICE_INFO_UPDATE_MESSAGE

            for m in msgs:
                self.protocol.listen_to(m, self._update_keyboard_state)
        except Exception as ex:
            print(f'Error _add_listner {ex}')

    async def _update_keyboard_state(self, message):
        try:
            msg = message
            # _msg = msg.inner()
            print(f'++++++++++ \n{msg}++++++++++\n')
        except Exception as ex:
            print(f'Error _update_keyboard_state {ex}')

    def keyboad_pull(self, keyboard: str, action: InputAction, fn: int, devid: str):
        try:

            async def _pull(_=None):
                try:
                    while True:
                        # res0 = await self.protocol.send_and_receive(
                        #     message=messages.create(protobuf.ProtocolMessage.GET_KEYBOARD_SESSION_MESSAGE),
                        #     generate_identifier=False,
                        #     timeout=5)  # Mustafa
                        # await asyncio.sleep(delay=.3, loop=asyncio.get_event_loop())
                        #
                        # for m in msgs:
                        #     # __m = m.inner()
                        #     try:
                        #         _m = messages.create(m)
                        #         __m = _m.inner()
                        #         print(f'{__m}')
                        #         await self.protocol.send(message=_m)
                        #         msgs[m] = True
                        #         print(f'{m} :   {msgs[m]}')
                        #     except Exception as ex:
                        #         if ex.__eq__('TimeoutError'):
                        #             msgs[m] = False
                        #             print(f'{m} :   {msgs[m]}')
                        #             # self.protocol.message_received(_m, _)
                        #
                        # print(f'=======\n{msgs.items()}\n========')
                        # return
                        # for m in msgs:

                        # await self.protocol.send_and_receive(messages.get_keyboard_session())

                        # identifier = str(uuid.uuid4()).upper()

                        # message = messages.create(protobuf.ProtocolMessage.TEXT_INPUT_MESSAGE, error_code=0,
                        #                           identifier=identifier)
                        # m = protobuf.ProtocolMessage.TEXT_INPUT_MESSAGE
                        # m = message.inner()
                        # m.encryptedTextCyphertext = bytes('123', encoding="ascii")
                        # m.text = "123D"
                        # t = datetime.datetime.utcnow()
                        # ts = t.timestamp()
                        # m.timestamp =ts
                        # m.actionType = 2

                        # res1 = await self.protocol.send(
                        #     messages.GetRemoteInput_text())
                        #
                        # print(res1)

                        # await self.protocol.send_and_receive(messages.get_keyboard_session())
                        # await asyncio.sleep(delay=1, loop=asyncio.get_event_loop())
                        # res = await self.protocol.send_and_receive(
                        #     messages.RemoteInput_text(action=protobuf.ActionType.Set, text='123'))
                        #
                        # print(res)
                        # return

                        # res = await self.protocol.send(messages.Input_text(action=protobuf.ActionType.Set,text='123'))
                        #
                        # print(res)
                        # return
                        #
                        # msg = messages.keyboard_text(action=protobuf.ActionType.Set, text='123')
                        # await self.protocol.send(msg)
                        # self.protocol.message_received(msg,_)
                        #
                        # # await self.protocol.send_and_receive(message=message, generate_identifier=True, timeout=3)
                        # # self.protocol.message_received(message, _)
                        # return
                        #
                        # message = messages.create(protobuf.ProtocolMessage.GET_REMOTE_TEXT_INPUT_SESSION_MESSAGE)
                        # m = message.inner()
                        # # m.HILITEMODE_FIELD_NUMBER = 1
                        # # m.hiliteMode = 0
                        #
                        # await self.protocol.send(message=m)
                        #
                        # message = messages.create(protobuf.ProtocolMessage.REMOTE_TEXT_INPUT_MESSAGE)
                        # m = message.inner()
                        # # m.data = bytes("123Da", encoding="UTF-8")
                        # m.attributes.prompt = "123D"
                        # await self.protocol.send(message=m)
                        #
                        # message = messages.create(protobuf.ProtocolMessage.TEXT_INPUT_MESSAGE)
                        # m = message.inner()
                        # m.text = "ads123"
                        #
                        # await self.protocol.send(message=m)

                        message = messages.create(protobuf.ProtocolMessage.REGISTER_HID_DEVICE_MESSAGE)
                        m = message.inner()

                        await self.protocol.send(message=m)

                        message = messages.create(protobuf.ProtocolMessage.REGISTER_HID_DEVICE_RESULT_MESSAGE)
                        m = message.inner()

                        await self.protocol.send(message=m)
                        res = self.protocol.message_received(message, _)
                        print(res)
                        # get_key_in = messages.create(protobuf.REGISTER_FOR_GAME_CONTROLLER_EVENTS_MESSAGE)
                        # get_key_in.data = bytes("123DDD", encoding="UTF-8")
                        # await self.protocol.send(get_key_in)
                        # self.protocol.message_received(get_key_in,_)

                        # _m = messages.create(protobuf.GET_KEYBOARD_SESSION_MESSAGE)
                        # __m = _m.inner()

                        # msg = messages.create(protobuf.ProtocolMessage.GET_KEYBOARD_SESSION_MESSAGE)
                        #
                        # res0 = await self.protocol.send_and_receive(msg)
                        # print(f'======\n{res0}\n=====')

                        # self.protocol.message_received(msg, _)

                        # k = keyboard.replace('get_session', '')
                        # res = _send_keyboard(protocol=self.protocol, key=k, action=action, fn=fn, devid=devid)

                        # for m in res0:
                        #     self.protocol.message_received(m, _)

                        # await self.protocol.send(get_key)
                        # await self.protocol.send(get_key_in)
                        # msg = messages.create(protobuf.TEXT_INPUT_MESSAGE)
                        # _msg = msg.inner()
                        # _msg.text = "Hi123"
                        # await self.protocol.send(_msg)
                        # res = await self.protocol.send_and_receive_custom(message=_msg
                        #                                            , generate_identifier=False,
                        #                                            timeout=5
                        #                                            )
                        # await self.protocol.send(msg)

                        # await self._send_command(CommandInfo_pb2.Play)

                        # res0 = self.protocol.message_received(msg, _)

                        # res = await self._send_command(ProtocolMessage.GET_REMOTE_TEXT_INPUT_SESSION_MESSAGE) #CommandInfo_pb2

                        # print(
                        #     f'==========={res}========================================================')

                        # await self.protocol.send(messages.get_keyboardtext_session())  # Mustafa
                        # await self.protocol.send(messages.get_keyboardtext1_session())  # Mustafa
                        # await self.protocol.send(messages.create(protobuf.ProtocolMessage.KEYBOARD_MESSAGE))  # Mustafa
                        # res = await self.protocol.send_and_receive(
                        #     message=messages.create(protobuf.REGISTER_HID_DEVICE_MESSAGE),
                        #     generate_identifier=True,
                        #     timeout=5)  # mustafa
                        # print(
                        #     f'===========get_hid_state====================\n{res}\n======================================')
                except Exception as ex:
                    print(f'Error keyboad_pull {ex}')

            loop = asyncio.new_event_loop()
            future = loop.create_task(coro=_pull())
            loop.run_until_complete(future)
        except Exception as ex:
            print(f'Error keyboad_pull {ex}')

    async def set_custom(self, keyboard: str, action: InputAction, fn: int, devid: str) -> bool:
        """" Mustafa custom cmd"""
        try:
            res = []
            if keyboard.__contains__('get_keyboard_session_pull'):
                th = threading.Thread(target=self.keyboad_pull, args=(keyboard, action, fn, devid))
                th.start()
            elif len(keyboard) > 1:
                res = _send_text(protocol=self.protocol, text=keyboard, devid=devid)
            else:
                res = _send_keyboard(protocol=self.protocol, key=keyboard, action=action, fn=fn, devid=devid)
                # object.close()

            print(f'Keyboard data={keyboard} , action={action}, {res}')
            if len(res) > 0:
                return True
            else:
                return False
        except Exception as ex:
            print(f'Error set_custom {ex}')
            raise ex

    async def _send_command(self, command, **kwargs):
        resp = await self.protocol.send_and_receive(messages.command(command, **kwargs))
        inner = resp.inner()

        if inner.sendError == protobuf.SendError.NoError:
            return

        raise exceptions.CommandError(
            f"{CommandInfo_pb2.Command.Name(command)} failed: "
            f"SendError={protobuf.SendError.Enum.Name(inner.sendError)}, "
            "HandlerReturnStatus="
            f"{protobuf.HandlerReturnStatus.Enum.Name(inner.handlerReturnStatus)}"
        )

    async def up(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key up."""
        await _send_hid_key(self.protocol, "up", action)

    async def down(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key down."""
        await _send_hid_key(self.protocol, "down", action)

    async def left(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key left."""
        await _send_hid_key(self.protocol, "left", action)

    async def right(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key right."""
        await _send_hid_key(self.protocol, "right", action)

    async def play(self) -> None:
        """Press key play."""
        await self._send_command(CommandInfo_pb2.Play)

    async def play_pause(self) -> None:
        """Toggle between play and pause."""
        # Cannot use the feature interface here since it emulates the feature state
        cmd = self.psm.playing.command_info(CommandInfo_pb2.TogglePlayPause)
        if cmd and cmd.enabled:
            await self._send_command(CommandInfo_pb2.TogglePlayPause)
        else:
            state = self.psm.playing.playback_state
            if state == PlaybackState.Playing:
                await self.pause()
            elif state == PlaybackState.Paused:
                await self.play()

    async def pause(self) -> None:
        """Press key play."""
        await self._send_command(CommandInfo_pb2.Pause)

    async def stop(self) -> None:
        """Press key stop."""
        await self._send_command(CommandInfo_pb2.Stop)

    async def next(self) -> None:
        """Press key next."""
        await self._send_command(CommandInfo_pb2.NextTrack)

    async def previous(self) -> None:
        """Press key previous."""
        await self._send_command(CommandInfo_pb2.PreviousTrack)

    async def select(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key select."""
        await _send_hid_key(self.protocol, "select", action)

    async def menu(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key menu."""
        await _send_hid_key(self.protocol, "menu", action)

    async def volume_up(self) -> None:
        """Press key volume up."""
        await _send_hid_key(self.protocol, "volume_up", InputAction.SingleTap)

    async def volume_down(self) -> None:
        """Press key volume down."""
        await _send_hid_key(self.protocol, "volume_down", InputAction.SingleTap)

    async def home(self, action: InputAction = InputAction.SingleTap) -> None:
        """Press key home."""
        await _send_hid_key(self.protocol, "home", action)

    async def home_hold(self) -> None:
        """Hold key home."""
        await _send_hid_key(self.protocol, "home", InputAction.Hold)

    async def top_menu(self) -> None:
        """Go to main menu (long press menu)."""
        await _send_hid_key(self.protocol, "topmenu", InputAction.SingleTap)

    async def suspend(self) -> None:
        """Suspend the device."""
        await _send_hid_key(self.protocol, "suspend", InputAction.SingleTap)

    async def wakeup(self) -> None:
        """Wake up the device."""
        await _send_hid_key(self.protocol, "wakeup", InputAction.SingleTap)

    async def skip_forward(self) -> None:
        """Skip forward a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        await self._skip_command(CommandInfo_pb2.SkipForward)

    async def skip_backward(self) -> None:
        """Skip backwards a time interval.

        Skip interval is typically 15-30s, but is decided by the app.
        """
        await self._skip_command(CommandInfo_pb2.SkipBackward)

    async def _skip_command(self, command) -> None:
        info = self.psm.playing.command_info(command)

        # Pick the first preferred interval for simplicity
        if info and info.preferredIntervals:
            skip_interval = info.preferredIntervals[0]
        else:
            skip_interval = 15  # Default value

        await self._send_command(command, skipInterval=skip_interval)

    async def set_position(self, pos: int) -> None:
        """Seek in the current playing media."""
        await self.protocol.send_and_receive(messages.seek_to_position(pos))

    async def set_shuffle(self, shuffle_state: ShuffleState) -> None:
        """Change shuffle mode to on or off."""
        await self.protocol.send_and_receive(messages.shuffle(shuffle_state))

    async def set_repeat(self, repeat_state: RepeatState) -> None:
        """Change repeat state."""
        await self.protocol.send_and_receive(messages.repeat(repeat_state))


class MrpMetadata(Metadata):
    """Implementation of API for retrieving metadata."""

    def __init__(self, protocol, psm, identifier):
        """Initialize a new MrpPlaying."""
        self.protocol = protocol
        self.psm = psm
        self.identifier = identifier
        self.artwork_cache = Cache(limit=4)

    @property
    def device_id(self) -> Optional[str]:
        """Return a unique identifier for current device."""
        return self.identifier

    async def artwork(
            self, width: Optional[int] = 512, height: Optional[int] = None
    ) -> Optional[ArtworkInfo]:
        """Return artwork for what is currently playing (or None).

        The parameters "width" and "height" makes it possible to request artwork of a
        specific size. This is just a request, the device might impose restrictions and
        return artwork of a different size. Set both parameters to None to request
        default size. Set one of them and let the other one be None to keep original
        aspect ratio.
        """
        identifier = self.artwork_id
        if not identifier:
            _LOGGER.debug("No artwork available")
            return None

        if identifier in self.artwork_cache:
            _LOGGER.debug("Retrieved artwork %s from cache", identifier)
            return self.artwork_cache.get(identifier)

        artwork: Optional[ArtworkInfo] = None
        try:
            artwork = await self._fetch_artwork(width or 0, height or -1)
        except Exception:
            _LOGGER.warning("Artwork not present in response")
        else:
            self.artwork_cache.put(identifier, artwork)

        return artwork

    async def _fetch_artwork(self, width, height) -> Optional[ArtworkInfo]:
        playing = self.psm.playing
        resp = await self.psm.protocol.send_and_receive(
            messages.playback_queue_request(playing.location, width, height)
        )
        if not resp.HasField("type"):
            return None

        item = resp.inner().playbackQueue.contentItems[playing.location]
        return ArtworkInfo(
            bytes=item.artworkData,
            mimetype=playing.metadata.artworkMIMEType,
            width=item.artworkDataWidth,
            height=item.artworkDataHeight,
        )

    @property
    def artwork_id(self):
        """Return a unique identifier for current artwork."""
        metadata = self.psm.playing.metadata
        if metadata and metadata.artworkAvailable:
            if metadata.HasField("artworkIdentifier"):
                return metadata.artworkIdentifier
            if metadata.HasField("contentIdentifier"):
                return metadata.contentIdentifier
            return self.psm.playing.item_identifier
        return None

    async def playing(self) -> Playing:
        """Return what is currently playing."""
        return build_playing_instance(self.psm.playing)

    @property
    def app(self) -> Optional[App]:
        """Return information about running app."""
        client = self.psm.client
        if client:
            return App(client.display_name, client.bundle_identifier)
        return None


class MrpPower(Power):
    """Implementation of API for retrieving a power state from an Apple TV."""

    def __init__(
            self,
            loop: asyncio.AbstractEventLoop,
            protocol: MrpProtocol,
            remote: MrpRemoteControl,
    ) -> None:
        """Initialize a new MrpPower instance."""
        super().__init__()
        self.loop = loop
        self.protocol = protocol
        self.remote = remote
        self.device_info = None
        self._waiters: Dict[PowerState, asyncio.Event] = {}

        self.protocol.listen_to(
            protobuf.DEVICE_INFO_UPDATE_MESSAGE, self._update_power_state
        )

    def _get_current_power_state(self) -> PowerState:
        latest_device_info = self.device_info or self.protocol.device_info
        return self._get_power_state(latest_device_info)

    @property
    def power_state(self) -> PowerState:
        """Return device power state."""
        currect_power_state = self._get_current_power_state()
        return currect_power_state

    async def turn_on(self, await_new_state: bool = False) -> None:
        """Turn device on."""
        await self.protocol.send(messages.wake_device())

        if await_new_state and self.power_state != PowerState.On:
            await self._waiters.setdefault(PowerState.On, asyncio.Event()).wait()

    async def turn_off(self, await_new_state: bool = False) -> None:
        """Turn device off."""
        await self.remote.home(InputAction.Hold)
        await asyncio.sleep(DELAY_BETWEEN_COMMANDS)
        await self.remote.select()

        if await_new_state and self.power_state != PowerState.Off:
            await self._waiters.setdefault(PowerState.Off, asyncio.Event()).wait()

    async def _update_power_state(self, message):
        old_state = self.power_state
        new_state = self._get_power_state(message)
        self.device_info = message

        if new_state != old_state:
            _LOGGER.debug("Power state changed from %s to %s", old_state, new_state)
            self.loop.call_soon(self.listener.powerstate_update, old_state, new_state)

        if new_state in self._waiters:
            self._waiters[new_state].set()
            del self._waiters[new_state]

    @staticmethod
    def _get_power_state(device_info_message) -> PowerState:
        logical_device_count = device_info_message.inner().logicalDeviceCount
        if logical_device_count >= 1:
            return PowerState.On
        if logical_device_count == 0:
            return PowerState.Off
        return PowerState.Unknown


class MrpPushUpdater(PushUpdater):
    """Implementation of API for handling push update from an Apple TV."""

    def __init__(self, loop, metadata, psm):
        """Initialize a new MrpPushUpdater instance."""
        super().__init__(loop)
        self.metadata = metadata
        self.psm = psm

    @property
    def active(self):
        """Return if push updater has been started."""
        return self.psm.listener == self

    def start(self, initial_delay=0):
        """Wait for push updates from device.

        Will throw NoAsyncListenerError if no listener has been set.
        """
        if self.listener is None:
            raise exceptions.NoAsyncListenerError()
        if self.active:
            return

        self.psm.listener = self
        asyncio.ensure_future(self.state_updated())

    def stop(self):
        """No longer forward updates to listener."""
        self.psm.listener = None

    async def state_updated(self):
        """State was updated for active player."""
        try:
            playstatus = await self.metadata.playing()
            self.post_update(playstatus)
        except asyncio.CancelledError:
            pass
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Playstatus error occurred: %s", ex)
            self.loop.call_soon(self.listener.playstatus_error, self, ex)


class MrpAudio(Audio):
    """Implementation of audio functionality."""

    def __init__(self, protocol: MrpProtocol):
        """Initialize a new MrpAudio instance."""
        self.protocol: MrpProtocol = protocol
        self._volume_controls_available: bool = False
        self._output_device_uid: Optional[str] = None
        self._volume: float = 0.0
        self._volume_event: asyncio.Event = asyncio.Event()
        self._add_listeners()

    @property
    def is_available(self):
        """Return if audio controls are available."""
        return self._volume_controls_available and self._output_device_uid is not None

    def _add_listeners(self):
        self.protocol.listen_to(
            protobuf.VOLUME_CONTROL_AVAILABILITY_MESSAGE,
            self._volume_control_availability,
        )
        self.protocol.listen_to(
            protobuf.VOLUME_CONTROL_CAPABILITIES_DID_CHANGE_MESSAGE,
            self._volume_control_changed,
        )
        self.protocol.listen_to(
            protobuf.VOLUME_DID_CHANGE_MESSAGE, self._volume_did_change
        )

    async def _volume_control_availability(self, message) -> None:
        self._update_volume_controls(message.inner())

    async def _volume_control_changed(self, message) -> None:
        inner = message.inner()

        self._output_device_uid = inner.outputDeviceUID
        self._update_volume_controls(inner.capabilities)

    def _update_volume_controls(
            self, availabilty_message: protobuf.VolumeControlAvailabilityMessage
    ) -> None:
        self._volume_controls_available = availabilty_message.volumeControlAvailable
        _LOGGER.debug(
            "Volume control availability changed to %s", self._volume_controls_available
        )

    async def _volume_did_change(self, message) -> None:
        inner = message.inner()

        # Make sure update is for our device (in case it changed for someone else)
        if inner.outputDeviceUID == self._output_device_uid:
            self._volume = round(inner.volume * 100.0, 1)
            _LOGGER.debug("Volume changed to %0.1f", self.volume)

            # There are no responses to the volume_up/down commands sent to the device.
            # So when calling volume_up/down here, they will wait for the volume to
            # change to know when done. Here, a single asyncio.Event is used which
            # works as long as no more than one task is calling either function at the
            # same time. If two or more call those functions, all of them will return
            # at once (when first volume update occurs) instead of one by one. This is
            # generally fine, but can be improved if there's a need for it.
            self._volume_event.set()
            self._volume_event.clear()

    @property
    def volume(self) -> float:
        """Return current volume level."""
        return self._volume

    async def set_volume(self, level: float) -> None:
        """Change current volume level."""
        if self._output_device_uid is None:
            raise exceptions.ProtocolError("no output device")

        await self.protocol.send(
            messages.set_volume(self._output_device_uid, level / 100.0)
        )

        if self._volume != level:
            await asyncio.wait_for(self._volume_event.wait(), timeout=5.0)

    async def volume_up(self) -> None:
        """Increase volume by one step."""
        if self._volume < 100.0:
            await _send_hid_key(self.protocol, "volume_up", InputAction.SingleTap)
            await asyncio.wait_for(self._volume_event.wait(), timeout=5.0)

    async def volume_down(self) -> None:
        """Decrease volume by one step."""
        if self._volume > 0.0:
            await _send_hid_key(self.protocol, "volume_down", InputAction.SingleTap)
            await asyncio.wait_for(self._volume_event.wait(), timeout=5.0)


class MrpFeatures(Features):
    """Implementation of API for supported feature functionality."""

    def __init__(self, config: BaseConfig, psm: PlayerStateManager, audio: MrpAudio):
        """Initialize a new MrpFeatures instance."""
        self.config = config
        self.psm = psm
        self.audio = audio

    def get_feature(  # pylint: disable=too-many-return-statements,too-many-branches
            self, feature_name: FeatureName
    ) -> FeatureInfo:
        """Return current state of a feature."""
        if feature_name in _FEATURES_SUPPORTED:
            return FeatureInfo(state=FeatureState.Available)
        if feature_name == FeatureName.Artwork:
            metadata = self.psm.playing.metadata
            if metadata and metadata.artworkAvailable:
                return FeatureInfo(state=FeatureState.Available)
            return FeatureInfo(state=FeatureState.Unavailable)

        field_name = _FIELD_FEATURES.get(feature_name)
        if field_name:
            available = self.psm.playing.metadata_field(field_name) is not None
            return FeatureInfo(
                state=FeatureState.Available if available else FeatureState.Unavailable
            )

        # Special case for PlayPause emulation. Based on the behavior in the Youtube
        # app, only the "opposite" feature to current state is available. E.g. if
        # something is playing, then pause will be available but not play. So take that
        # into consideration here.
        if feature_name == FeatureName.PlayPause:
            playback_state = self.psm.playing.playback_state
            if playback_state == PlaybackState.Playing and self.in_state(
                    FeatureState.Available, FeatureName.Pause
            ):
                return FeatureInfo(state=FeatureState.Available)
            if playback_state == PlaybackState.Paused and self.in_state(
                    FeatureState.Available, FeatureName.Play
            ):
                return FeatureInfo(state=FeatureState.Available)

        cmd_id = _FEATURE_COMMAND_MAP.get(feature_name)
        if cmd_id:
            cmd = self.psm.playing.command_info(cmd_id)
            if cmd and cmd.enabled:
                return FeatureInfo(state=FeatureState.Available)
            return FeatureInfo(state=FeatureState.Unavailable)

        if feature_name == FeatureName.App:
            if self.psm.client:
                return FeatureInfo(state=FeatureState.Available)
            return FeatureInfo(state=FeatureState.Unavailable)

        if feature_name in [
            FeatureName.VolumeDown,
            FeatureName.VolumeUp,
            FeatureName.Volume,
            FeatureName.SetVolume,
        ]:
            if self.audio.is_available:
                return FeatureInfo(state=FeatureState.Available)
            return FeatureInfo(state=FeatureState.Unavailable)

        return FeatureInfo(state=FeatureState.Unsupported)


def mrp_service_handler(
        mdns_service: mdns.Service, response: mdns.Response
) -> Optional[ScanHandlerReturn]:
    """Parse and return a new MRP service."""
    # Ignore this service if tvOS version is >= 15 as it doesn't work anymore
    build = mdns_service.properties.get("SystemBuildVersion", "")
    match = re.match(r"^(\d+)[A-Z]", build)
    if match:
        base = int(match.groups()[0])
        if base >= 19:
            _LOGGER.debug("Ignoring MRP service since tvOS >= 15")
            return None

    name = mdns_service.properties.get("Name", "Unknown")
    service = MutableService(
        get_unique_id(mdns_service.type, mdns_service.name, mdns_service.properties),
        Protocol.MRP,
        mdns_service.port,
        properties=mdns_service.properties,
    )
    return name, service


def scan() -> Mapping[str, ScanHandler]:
    """Return handlers used for scanning."""
    return {
        "_mediaremotetv._tcp.local": mrp_service_handler,
    }


def device_info(service_type: str, properties: Mapping[str, Any]) -> Dict[str, Any]:
    """Return device information from zeroconf properties."""
    devinfo: Dict[str, Any] = {}
    if "systembuildversion" in properties:
        devinfo[DeviceInfo.BUILD_NUMBER] = properties["systembuildversion"]

        version = lookup_version(properties["systembuildversion"])
        if version:
            devinfo[DeviceInfo.VERSION] = version
    if "macaddress" in properties:
        devinfo[DeviceInfo.MAC] = properties["macaddress"]

    # MRP has only been seen on Apple TV and HomePod, which both run tvOS,
    # so an educated guess is made here. It is border line OK, but will
    # do for now.
    devinfo[DeviceInfo.OPERATING_SYSTEM] = OperatingSystem.TvOS

    return devinfo


async def service_info(
        service: MutableService,
        devinfo: DeviceInfo,
        services: Mapping[Protocol, BaseService],
) -> None:
    """Update service with additional information.

    Pairing has never been enforced by MRP (maybe by design), but it is
    possible to pair if AllowPairing is YES.
    """
    service.pairing = (
        PairingRequirement.Optional
        if service.properties.get("allowpairing", "no").lower() == "yes"
        else PairingRequirement.Disabled
    )


def create_with_connection(  # pylint: disable=too-many-locals
        loop: asyncio.AbstractEventLoop,
        config: BaseConfig,
        service: BaseService,
        device_listener: StateProducer,
        session_manager: ClientSessionManager,
        takeover: TakeoverMethod,
        connection: AbstractMrpConnection,
        requires_heatbeat: bool = True,
) -> SetupData:
    """Set up a new MRP service from a connection."""
    protocol = MrpProtocol(connection, SRPAuthHandler(), service)
    psm = PlayerStateManager(protocol)

    remote_control = MrpRemoteControl(loop, psm, protocol)
    metadata = MrpMetadata(protocol, psm, config.identifier)
    power = MrpPower(loop, protocol, remote_control)
    push_updater = MrpPushUpdater(loop, metadata, psm)
    audio = MrpAudio(protocol)

    interfaces = {
        RemoteControl: remote_control,
        Metadata: metadata,
        Power: power,
        PushUpdater: push_updater,
        Features: MrpFeatures(config, psm, audio),
        Audio: audio,
    }

    async def _connect() -> bool:
        await protocol.start()
        if requires_heatbeat:
            protocol.enable_heartbeat()
        return True

    def _close() -> Set[asyncio.Task]:
        push_updater.stop()
        protocol.stop()
        return set()

    def _device_info() -> Dict[str, Any]:
        devinfo = device_info(list(scan().keys())[0], service.properties)

        # Extract build number from DEVICE_INFO_MESSAGE from device
        if protocol.device_info:
            info = protocol.device_info.inner()
            devinfo[DeviceInfo.BUILD_NUMBER] = info.systemBuildVersion
            if info.modelID:
                devinfo[DeviceInfo.RAW_MODEL] = info.modelID
                devinfo[DeviceInfo.MODEL] = lookup_model(info.modelID)

        return devinfo

    # Features managed by this protocol
    features = set(
        [
            FeatureName.Artwork,
            FeatureName.VolumeDown,
            FeatureName.VolumeUp,
            FeatureName.SetVolume,
            FeatureName.Volume,
            FeatureName.App,
        ]
    )
    features.update(_FEATURES_SUPPORTED)
    features.update(_FEATURE_COMMAND_MAP.keys())
    features.update(_FIELD_FEATURES.keys())

    return SetupData(Protocol.MRP, _connect, _close, _device_info, interfaces, features)


def setup(
        loop: asyncio.AbstractEventLoop,
        config: BaseConfig,
        service: BaseService,
        device_listener: StateProducer,
        session_manager: ClientSessionManager,
        takeover: TakeoverMethod,
) -> Generator[SetupData, None, None]:
    """Set up a new MRP service."""
    yield create_with_connection(
        loop,
        config,
        service,
        device_listener,
        session_manager,
        takeover,
        MrpConnection(config.address, service.port, loop, atv=device_listener),
    )


def pair(
        config: BaseConfig,
        service: BaseService,
        session_manager: ClientSessionManager,
        loop: asyncio.AbstractEventLoop,
        **kwargs
) -> PairingHandler:
    """Return pairing handler for protocol."""
    return MrpPairingHandler(config, service, session_manager, loop, **kwargs)
