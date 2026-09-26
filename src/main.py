import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

from settings import Settings

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


class UserModel:
    id: int
    name: str | None

    def __init__(self, id: int, name: str | None = None) -> None:
        self.id = id
        self.name = name

    @property
    def tag(self) -> str:
        return f"@{self.name}" if self.name is not None else f'<a href="tg://user?id={self.id}">id{self.id}</a>'


class ChatModel(UserModel):
    members: dict[int, UserModel]

    def __init__(self, id: int, name: str | None = None, members: dict[int, UserModel] | None = None) -> None:
        super().__init__(id, name)
        self.members = members if members is not None else {}


blank_chat = ChatModel(-1)

chats: dict[int, ChatModel] = {}


def is_admin(user_id: int) -> bool:
    return user_id == Settings.ADMIN_ID


async def is_authorized(message: Message) -> bool:
    if message.from_user is not None and is_admin(message.from_user.id):
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, Settings.ADMIN_ID)
    except TelegramBadRequest:
        return False
    return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)


@dp.message(Command("all"))
async def all_handler(message: Message) -> None:
    if not await is_authorized(message):
        return
    sender = message.from_user
    answer = " ".join(user.tag for user in chats.get(
        message.chat.id, blank_chat
    ).members.values() if sender is None or user.id != sender.id)
    if answer == "":
        await message.answer("No one user have registered yet...")
    else:
        await message.answer(answer, parse_mode=ParseMode.HTML)


@dp.message(Command("subscribe"))
async def subscribe_handler(message: Message) -> None:
    if not await is_authorized(message):
        return
    sender = message.from_user
    if sender is None:
        await message.answer(f"Something went wrong...")
    else:
        user = UserModel(sender.id, sender.username)
        if not message.chat.id in chats:
            chats[message.chat.id] = ChatModel(message.chat.id, message.chat.username)
        chats[message.chat.id].members[sender.id] = user
        await message.answer(f"You have registered as: {user.tag}", parse_mode=ParseMode.HTML)


@dp.message(Command("whoami"))
async def whoami_handler(message: Message) -> None:
    if not await is_authorized(message):
        return
    await message.answer(f"Your id: {message.from_user.id}")


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    if not await is_authorized(message):
        return
    answer = "Users ping bot:\n  " + "\n  ".join([
        f"<code>/{cmd}</code>" for cmd in ["help", "whoami", "subscribe", "all"]
    ])
    await message.answer(answer, parse_mode=ParseMode.HTML)


async def main() -> None:
    bot = Bot(token=Settings.BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())