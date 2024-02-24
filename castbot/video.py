import html
import logging
import re
import typing

from pyrogram.errors import MessageNotModified
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

from .device import Device
from .utils import LocalToken, NoDeviceException, ActionNotSupportedException
from .http import Http


class PlayingVideos:
    def __init__(self, http: Http):
        self._http = http
        self._playing_videos: typing.Dict[LocalToken, PlayingVideo] = {}

    def remove(self, playing_video: "PlayingVideo"):
        local_token = playing_video.local_token
        if local_token in self._playing_videos:
            del self._playing_videos[local_token]

    def new_video(self, local_token: LocalToken, user_id, device, video_message, control_message, link_message):
        self._playing_videos[local_token] = PlayingVideo(
            local_token=local_token,
            user_id=user_id,
            playing_videos=self,
            playing_device=device,
            video_message=video_message,
            control_message=control_message,
            link_message=link_message,
        )
        return self._playing_videos[local_token]

    def add_to_http(self, playing_video):
        return self._http.add_remote_token(playing_video)

    async def reconstruct_playing_video(self, local_token: LocalToken, user_id, control_message, bot):
        if local_token in self._playing_videos:
            return self._playing_videos[local_token]
        # re-construct PlayVideo when the bot is restarted
        video_message: Message = await bot.get_message(local_token.message_id)
        if control_message.reply_to_message_id and control_message.reply_to_message_id != local_token.message_id:
            link_message = await bot.get_message(control_message.reply_to_message_id)
        else:
            link_message = None

        device_name = PlayingVideo.parse_device_str(control_message.text)
        device = await bot.find_device(device_name, user_id)

        return self.new_video(local_token, user_id, device, video_message, control_message, link_message)

    async def handle_closed(self, remains: float, local_token: LocalToken):
        if local_token in self._playing_videos:
            playing_video = self._playing_videos[local_token]
            device = playing_video.playing_device
            await playing_video.send_stopped_control_message(remaining=remains)
            del self._playing_videos[local_token]
            await device.on_close(local_token)


class PlayingVideo:
    def __init__(
        self,
        local_token: LocalToken,
        user_id: int,
        playing_videos: PlayingVideos,
        playing_device: typing.Optional[Device] = None,
        video_message: typing.Optional[Message] = None,
        control_message: typing.Optional[Message] = None,
        link_message: typing.Optional[Message] = None,
    ):
        self.local_token = local_token
        self.user_id = user_id
        self.video_message = video_message
        self.playing_device: typing.Optional[Device] = playing_device
        self.control_message = control_message
        self.link_message = link_message
        self.playing_videos = playing_videos

    def _gen_device_str(self):
        return (
            f"on device <code>"
            f"{html.escape(self.playing_device.get_device_name()) if self.playing_device else 'NONE'}</code>"
        )

    @classmethod
    def parse_device_str(cls, text):
        groups = re.search("on device ([^,]*)", text)
        if not groups:
            return None
        return groups.group(1)

    def _gen_message_str(self):
        return f"for file <code>{self.video_message.id}</code>"

    def _gen_device_button(self, device):
        return InlineKeyboardButton(repr(device), f"s:{self.local_token}:{repr(device)}")

    async def send_stopped_control_message(self, remaining=None):
        buttons = [
            [DEVICE_BUTTON.get_button(self.local_token)],
            [PLAY_BUTTON.get_button(self.local_token)],
        ]
        if not remaining:
            text = f"Controller {self._gen_message_str()} {self._gen_device_str()}"
        else:
            text = f"Streaming closed {self._gen_message_str()} {self._gen_device_str()}, {remaining:0.2f}% remains"
        await self.create_or_update_control_message(text, buttons)

    async def send_playing_control_message(self):
        buttons = [
            [STOP_BUTTON.get_button(self.local_token)],
            [PAUSE_BUTTON.get_button(self.local_token)],
        ]
        text = f"Playing {self._gen_message_str()} {self._gen_device_str()}"
        await self.create_or_update_control_message(text, buttons)

    async def send_paused_control_message(self):
        buttons = [
            [STOP_BUTTON.get_button(self.local_token)],
            [RESUME_BUTTON.get_button(self.local_token)],
        ]
        text = f"Paused {self._gen_message_str()} {self._gen_device_str()}"
        await self.create_or_update_control_message(text, buttons)

    async def send_select_device_message(self, devices):
        device_buttons = [[Button("s", repr(d), repr(d)).get_button(self.local_token)] for d in devices]
        refresh_button = [[REFRESH_BUTTON.get_button(self.local_token)]]
        await self.create_or_update_control_message("Select a device", device_buttons + refresh_button)

    async def create_or_update_control_message(self, text, buttons):
        if self.control_message:
            try:
                await self.control_message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except MessageNotModified:
                pass
        else:
            self.control_message = await self.video_message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

    async def play(self):
        if not self.playing_device:
            raise NoDeviceException

        uri = self.playing_videos.add_to_http(self)

        try:
            filename = pyrogram_filename(self.video_message)
        except TypeError:
            filename = "None"

        await self.playing_device.stop()
        await self.playing_device.play(uri, str(filename), self.local_token)
        await self.send_playing_control_message()

    async def stop(self):
        if self.playing_device:
            # noinspection PyBroadException
            try:
                await self.playing_device.stop()
            except Exception:
                # make sure stop always succeeds even if the device is gone
                logging.exception("Failed to stop device %r", self.playing_device)
        await self.send_stopped_control_message()
        if not self.playing_device:
            raise NoDeviceException

    async def pause(self):
        if not self.playing_device:
            raise NoDeviceException
        if hasattr(self.playing_device, "pause"):
            await self.playing_device.pause()
            return await self.send_paused_control_message()
        raise ActionNotSupportedException

    async def resume(self):
        if not self.playing_device:
            raise NoDeviceException
        if hasattr(self.playing_device, "resume"):
            await self.playing_device.resume()
            return await self.send_playing_control_message()
        raise ActionNotSupportedException

    async def close(self, remains):
        device = self.playing_device
        await self.send_stopped_control_message(remaining=remains)
        await device.on_close(self.local_token)
        self.playing_videos.remove(self)

    async def select_device(self, device: Device):
        self.playing_device = device
        await self.send_stopped_control_message()


def pyrogram_filename(message: Message) -> str:
    named_media_types = ("document", "video", "audio", "video_note", "animation")
    try:
        return next(
            getattr(message, t, None).file_name for t in named_media_types if getattr(message, t, None) is not None
        )
    except StopIteration as error:
        raise TypeError() from error


class Button:
    def __init__(self, prefix, text, status):
        self.prefix = prefix
        self.text = text
        self.status = status

    def get_button(self, local_token: LocalToken):
        return InlineKeyboardButton(self.text, f"{self.prefix}:{local_token}:{self.text}")


PLAY_BUTTON = Button("c", "PLAY", "Playing")
STOP_BUTTON = Button("c", "STOP", "Stopped")
PAUSE_BUTTON = Button("c", "PAUSE", "Paused")
RESUME_BUTTON = Button("c", "RESUME", "Resumed")
REFRESH_BUTTON = Button("c", "REFRESH", "REFRESH")
DEVICE_BUTTON = Button("c", "DEVICE", "DEVICE")
