"""Calendar-only directory. Announcement opt-outs never enter public views."""

import asyncio
import calendar
import logging
import time

import discord

from .ui import OwnedView, button, edit, moderator, private

log = logging.getLogger(__name__)


class DirectoryView(OwnedView):
    def __init__(self, bot, owner, guild_id, *, public=True, summary=False):
        super().__init__(owner, admin=not public, queue=bot.queue)
        self.bot, self.guild_id, self.public = bot, guild_id, public
        self.summary, self.month, self.page = summary, None, 0
        self.message_id = None
        self.lock = asyncio.Lock()
        self.build({})

    def build(self, months):
        self.clear_items()
        choice = discord.ui.Select(
            placeholder="Choose a month",
            row=0,
            options=[
                discord.SelectOption(label="All months", value="all", default=self.month is None),
                *[
                    discord.SelectOption(
                        label=f"{calendar.month_name[m]} · {months.get(m, 0)}",
                        value=str(m),
                        default=m == self.month,
                    )
                    for m in range(1, 13)
                ],
            ],
        )

        async def choose(interaction):
            await self.refresh(
                interaction,
                month=None if choice.values[0] == "all" else int(choice.values[0]),
                page=0,
            )

        choice.callback = choose
        self.add_item(choice)
        self.add_item(button("Birthdays", self.birthdays, row=1))
        self.add_item(button("Monthly summary", self.monthly, row=1))
        self.add_item(
            button("Previous", self.previous, row=2, disabled=self.summary or self.page == 0)
        )
        self.add_item(
            button(
                "Next",
                self.next,
                row=2,
                disabled=self.summary or self.page >= getattr(self, "pages", 1) - 1,
            )
        )
        self.add_item(button("Refresh", self.refresh, row=2))
        if not self.public:
            self.add_item(button("Main menu", self.bot.menu_actions["home"], row=3))

    async def interaction_check(self, interaction):
        if not self.public:
            return await super().interaction_check(interaction)
        if interaction.guild_id != self.guild_id or not isinstance(
            interaction.user, discord.Member
        ):
            return False
        settings = await self.bot.db.guild(self.guild_id)
        current = await self.bot.db.directory_message(self.guild_id, interaction.channel_id)
        if (
            not settings["public_birthdays"]
            or not current
            or current["message_id"] != self.message_id
        ):
            self.stop()
            await edit(
                interaction,
                content="Directory expired. Open `/birthdaybot`.",
                embed=None,
                view=None,
            )
            return False
        if not self.queue.admit((interaction.guild_id, interaction.user.id)):
            await private(interaction, "Limit: 12 actions per 10 seconds.")
            return False
        return True

    async def render(self):
        settings = await self.bot.db.guild(self.guild_id)
        if self.public and not settings["public_birthdays"]:
            self.stop()
            return {
                "content": "Public birthday listings are disabled. Use `/birthdaybot`.",
                "embed": None,
                "view": None,
            }
        data = await self.bot.db.directory(
            self.guild_id, public_only=self.public, month=self.month, page=self.page
        )
        self.page, self.pages = data["page"], data["pages"]
        self.build(data["months"])
        counts = f"{data['configured']} total · " + (
            f"{data['visible']} public" if self.public else "Moderators only"
        )
        embed = discord.Embed(
            title="Birthday calendar" if not self.summary else "Birthdays by month",
            description=counts,
            color=0xF6B5D7,
        )
        if self.summary:
            for month in range(1, 13):
                embed.add_field(
                    name=calendar.month_name[month],
                    value=str(data["months"].get(month, 0)),
                    inline=True,
                )
        else:
            lines = [
                f"**{calendar.month_name[row['month']]} {row['day']}** — <@{row['user_id']}>"
                for row in data["rows"]
            ]
            embed.add_field(
                name=calendar.month_name[self.month] if self.month else "January → December",
                value="\n".join(lines) or "No birthdays.",
                inline=False,
            )
            embed.set_footer(
                text=f"Page {self.page + 1}/{self.pages} · {data['filtered']} birthdays"
            )
        return {"content": None, "embed": embed, "view": self}

    async def refresh(self, interaction, **changes):
        async with self.lock, self.bot.mutation_lock:
            for key, value in changes.items():
                setattr(self, key, value)
            await edit(
                interaction, **await self.render(), allowed_mentions=discord.AllowedMentions.none()
            )

    async def birthdays(self, interaction):
        await self.refresh(interaction, summary=False, page=0)

    async def monthly(self, interaction):
        await self.refresh(interaction, summary=True, page=0)

    async def previous(self, interaction):
        await self.refresh(interaction, page=self.page - 1)

    async def next(self, interaction):
        await self.refresh(interaction, page=self.page + 1)


async def open_directory(bot, interaction, *, summary=False):
    async with bot.mutation_lock:
        settings = await bot.db.guild(interaction.guild_id)
        public = bool(settings["public_birthdays"])
        if not public and not moderator(interaction):
            await private(
                interaction, "Birthday browsing is restricted to moderators in this server."
            )
            return
        view = DirectoryView(
            bot, interaction.user.id, interaction.guild_id, public=public, summary=summary
        )
        payload = await view.render()
        if not public:
            bot.track_view(view, interaction.guild_id, interaction.user.id)
            await private(interaction, **payload)
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await private(interaction, "Open the public directory in a server text channel.")
            return
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            await private(
                interaction,
                "I need View Channel, Send Messages and Embed Links here to post the directory.",
            )
            return
        existing = await bot.db.directory_message(interaction.guild_id, channel.id)
        # Avoid hammering the same edit route when many users request the menu.
        last = bot.directory_refreshes.get((interaction.guild_id, channel.id), 0)
        cached = bot.directory_views.get((interaction.guild_id, channel.id))
        if (
            existing
            and cached
            and cached.summary == summary
            and cached.month is None
            and cached.page == 0
            and time.monotonic() - last < 3
        ):
            view.stop()
            await private(
                interaction,
                f"[Open the birthday directory](https://discord.com/channels/{interaction.guild_id}/{channel.id}/{existing['message_id']}).",
            )
            return
        message = None
        if existing:
            try:
                message = await channel.get_partial_message(existing["message_id"]).edit(
                    **payload, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.NotFound:
                pass
        if message is None:
            message = await channel.send(**payload, allowed_mentions=discord.AllowedMentions.none())
            await bot.db.register_directory(
                interaction.guild_id, channel.id, message.id, message.created_at.timestamp()
            )
        view.message_id = message.id
        bot.track_directory(view, interaction.guild_id, channel.id)
        await private(interaction, f"[Open the birthday directory]({message.jump_url}).")
        log.info(
            "directory_opened guild=%s actor=%s channel=%s",
            interaction.guild_id,
            interaction.user.id,
            channel.id,
        )
