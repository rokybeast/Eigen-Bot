"""cogs/suggestions.py

Suggestion system for Eigen Bot.

Behavior:
- Users run `/suggest <message>` (hybrid command).
- Bot posts an embed to a configured suggestions channel.
- Bot adds reaction voting (approve / reject / neutral).
- Bot creates a discussion thread from that suggestion message.

Configuration:
- Admins set the suggestions channel with `/setsuggestchannel #channel`.
- Settings are persisted in `data/suggestions.json`.

This keeps the main suggestions channel clean while enabling organized discussion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


DATA_VERSION = 1

VOTE_APPROVE = "✅"
VOTE_REJECT = "❌"
VOTE_NEUTRAL = "😐"


class Suggestions(commands.Cog):
    """Suggestion submission + voting + threads."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        base_dir = Path(__file__).resolve().parents[1]
        self._data_path = base_dir / "data" / "suggestions.json"

        self._lock = asyncio.Lock()
        self._data: Dict[str, Any] = {}
        self._loaded = False

        self.bot.loop.create_task(self.load_data())

    # ----------------------------
    # Persistence
    # ----------------------------

    async def load_data(self) -> None:
        """Load config from disk (creates file if missing)."""
        needs_initial_save = False

        async with self._lock:
            if self._loaded and isinstance(self._data, dict) and "guilds" in self._data:
                return

            self._data_path.parent.mkdir(parents=True, exist_ok=True)

            if not self._data_path.exists():
                self._data = {"version": DATA_VERSION, "guilds": {}}
                self._loaded = True
                needs_initial_save = True

        if needs_initial_save:
            await self.save_data()
            return

        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._data_path.read_text, encoding="utf-8")
                loaded = json.loads(raw) if raw.strip() else {}
            except Exception as e:
                logger.exception("Failed reading suggestions data; recreating file: %s", e)
                loaded = {}

            if not isinstance(loaded, dict):
                loaded = {}

            loaded.setdefault("version", DATA_VERSION)
            loaded.setdefault("guilds", {})

            self._data = loaded
            self._loaded = True

    async def save_data(self) -> None:
        async with self._lock:
            self._data_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, indent=2, ensure_ascii=False, sort_keys=True)
            await asyncio.to_thread(self._data_path.write_text, payload, encoding="utf-8")

    def _ensure_guild_bucket(self, guild_id: int) -> Dict[str, Any]:
        guilds: Dict[str, Any] = self._data.setdefault("guilds", {})
        bucket: Dict[str, Any] = guilds.setdefault(str(guild_id), {"suggestions_channel_id": None})
        bucket.setdefault("suggestions_channel_id", None)
        return bucket

    async def _get_suggestions_channel_id(self, guild_id: int) -> Optional[int]:
        await self.load_data()
        async with self._lock:
            bucket = self._ensure_guild_bucket(guild_id)
            cid = bucket.get("suggestions_channel_id")
            return int(cid) if cid else None

    # ----------------------------
    # Permission helpers
    # ----------------------------

    async def _ensure_manage_guild(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("Server-only command.")
            return False
        if not isinstance(ctx.author, discord.Member):
            await ctx.send("Server member only.")
            return False
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.send("You need the **Manage Server** permission to use this command.")
            return False
        return True

    # ----------------------------
    # Commands
    # ----------------------------

    @commands.hybrid_command(name="setsuggestchannel", description="Set the channel where suggestions are posted")
    @app_commands.describe(channel="Channel where suggestions should be posted")
    async def setsuggestchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Admin: configure suggestions channel."""
        if not await self._ensure_manage_guild(ctx):
            return

        await self.load_data()
        async with self._lock:
            bucket = self._ensure_guild_bucket(ctx.guild.id)  # type: ignore[union-attr]
            bucket["suggestions_channel_id"] = channel.id
        await self.save_data()

        embed = discord.Embed(
            title="✅ Suggestions channel set",
            description=f"Suggestions will now be posted in {channel.mention}.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="suggest", description="Submit a suggestion to the server")
    @app_commands.describe(message="Your suggestion")
    async def suggest(self, ctx: commands.Context, *, message: str):
        """Submit a suggestion and create a discussion thread."""
        if ctx.guild is None:
            return await ctx.send("Server-only command.")

        # Basic validation
        content = message.strip()
        if not content:
            return await ctx.send("Please provide a suggestion message.")
        if len(content) > 1800:
            return await ctx.send("Suggestion is too long. Please keep it under 1800 characters.")

        channel_id = await self._get_suggestions_channel_id(ctx.guild.id)
        if not channel_id:
            return await ctx.send("Suggestions channel is not configured. An admin can set it with `/setsuggestchannel`. ")

        target_channel = ctx.guild.get_channel(channel_id)
        if target_channel is None:
            try:
                target_channel = await ctx.guild.fetch_channel(channel_id)
            except Exception:
                target_channel = None

        if not isinstance(target_channel, discord.TextChannel):
            return await ctx.send("Configured suggestions channel is invalid or not a text channel.")

        embed = discord.Embed(
            title="💡 New Suggestion",
            description=content,
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
        embed.add_field(name="Author", value=ctx.author.mention, inline=True)

        try:
            suggestion_msg = await target_channel.send(embed=embed)
        except discord.Forbidden:
            return await ctx.send("I don't have permission to post in the suggestions channel.")
        except Exception:
            logger.exception("Failed to post suggestion")
            return await ctx.send("An error occurred while posting your suggestion.")

        # Add voting reactions
        try:
            await suggestion_msg.add_reaction(VOTE_APPROVE)
            await suggestion_msg.add_reaction(VOTE_REJECT)
            await suggestion_msg.add_reaction(VOTE_NEUTRAL)
        except discord.Forbidden:
            # Not critical; continue
            pass
        except Exception:
            pass

        # Create discussion thread
        try:
            await suggestion_msg.create_thread(
                name="Suggestion Discussion",
                auto_archive_duration=1440,
                reason="Suggestion discussion thread",
            )
        except discord.Forbidden:
            # Inform the user but don't fail the suggestion itself.
            if ctx.interaction is not None:
                try:
                    await ctx.interaction.followup.send(
                        "Suggestion posted, but I couldn't create a thread (missing permissions).",
                        ephemeral=True,
                    )
                except Exception:
                    pass
            else:
                await ctx.send("Suggestion posted, but I couldn't create a thread (missing permissions).")
        except Exception:
            logger.exception("Failed to create suggestion thread")

        # Acknowledge to the user
        ack = discord.Embed(
            title="✅ Suggestion submitted",
            description=f"Posted in {target_channel.mention}.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=ack)

    # ----------------------------
    # Errors
    # ----------------------------

    @setsuggestchannel.error
    async def _setsuggestchannel_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send("You don't have permission to use this command.")
        if isinstance(error, commands.BadArgument):
            return await ctx.send("Invalid channel.")
        logger.exception("setsuggestchannel error: %s", error)
        await ctx.send("An error occurred.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
