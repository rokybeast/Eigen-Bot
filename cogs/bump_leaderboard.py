"""cogs/bump_leaderboard.py

Bump leaderboard cog.

What this does
-------------
Counts bumps done via the **Disboard bot** in a configured bump channel.

Important technical note
------------------------
Discord does not expose *another bot's* slash-command interactions to your bot,
so we cannot directly "listen to /bump" when Disboard handles it.
Instead, we listen for Disboard's **confirmation message** (e.g. "Bump done")
in the bump channel and attribute the bump to the mentioned user.

Data is stored in `data/bump_leaderboard.json` and is created automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


DATA_VERSION = 1
DEFAULT_COOLDOWN_SECONDS = 60  # basic anti-spam; adjust as needed

# Default Disboard bot user id. If your server uses a different bump bot,
# you can change this constant.
DISBOARD_BOT_ID = 302050872383242240

_MENTION_RE = re.compile(r"<@!?(\d{15,25})>")


def _utcnow() -> datetime:
    return discord.utils.utcnow()


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


@dataclass(frozen=True)
class BumpEntry:
    user_id: int
    username: str
    total_bumps: int
    last_bump_time: Optional[datetime]


class BumpLeaderboard(commands.Cog):
    """Track bumps in a configured channel and provide leaderboards."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        base_dir = Path(__file__).resolve().parents[1]
        self._data_path = base_dir / "data" / "bump_leaderboard.json"

        self._lock = asyncio.Lock()
        self._data: Dict[str, Any] = {}
        self._loaded = False

        # In-memory cache to avoid repeatedly parsing ISO strings for cooldown checks.
        self._last_bump_cache: Dict[int, Dict[int, datetime]] = {}

        # Prevent double counting when Disboard edits the same message.
        self._processed_message_ids: Dict[int, float] = {}

        # Remember who invoked /bump most recently per (guild, channel).
        # Disboard's confirmation embed often does not mention the user.
        self._recent_bump_invoker: Dict[Tuple[int, int], Tuple[int, float]] = {}

        # Load data lazily on first use (and also attempt in background early).
        self.bot.loop.create_task(self.load_data())

    # ----------------------------
    # Disboard message parsing
    # ----------------------------

    def _is_disboard_message(self, message: discord.Message) -> bool:
        return message.author is not None and message.author.id == DISBOARD_BOT_ID

    def _message_text_blob(self, message: discord.Message) -> str:
        parts: List[str] = []
        if message.content:
            parts.append(message.content)

        for emb in message.embeds or []:
            if emb.title:
                parts.append(str(emb.title))
            if emb.description:
                parts.append(str(emb.description))
            for f in emb.fields or []:
                if f.name:
                    parts.append(str(f.name))
                if f.value:
                    parts.append(str(f.value))

        return "\n".join(parts)

    def _looks_like_bump_success(self, message: discord.Message) -> bool:
        blob = self._message_text_blob(message).lower()
        # Common Disboard phrases.
        return (
            "bump done" in blob
            or "bumped" in blob and "done" in blob
            or "successful" in blob and "bump" in blob
        )

    async def _extract_bumper_user(self, message: discord.Message) -> Optional[discord.abc.User]:
        # Prefer real resolved mentions.
        for m in message.mentions or []:
            if not m.bot:
                return m

        # Fall back to parsing mention tags in text.
        blob = self._message_text_blob(message)
        m = _MENTION_RE.search(blob)
        if not m:
            return None

        user_id = int(m.group(1))
        if message.guild:
            member = message.guild.get_member(user_id)
            if member:
                return member

        try:
            return await self.bot.fetch_user(user_id)
        except Exception:
            return None

    def _cleanup_processed_cache(self) -> None:
        # Keep ~10 minutes of ids; enough to cover edits/reposts.
        cutoff = time.monotonic() - 600
        stale = [mid for mid, ts in self._processed_message_ids.items() if ts < cutoff]
        for mid in stale:
            self._processed_message_ids.pop(mid, None)

        # Keep ~2 minutes of recent invokers.
        inv_cutoff = time.monotonic() - 120
        stale_keys = [k for k, (_, ts) in self._recent_bump_invoker.items() if ts < inv_cutoff]
        for k in stale_keys:
            self._recent_bump_invoker.pop(k, None)

    def _record_bump_invocation(self, message: discord.Message) -> None:
        """Record a visible '/bump' invocation message so we can attribute Disboard's confirmation."""
        if message.guild is None:
            return

        # This is the "<user> used /bump" system message shown in Discord.
        # In discord.py it comes through as MessageType.chat_input_command with a MessageInteraction.
        if message.type != discord.MessageType.chat_input_command:
            return

        # discord.py 2.4+: message.interaction_metadata (preferred)
        # Older versions: message.interaction (deprecated)
        meta = getattr(message, "interaction_metadata", None)
        mi = meta if meta is not None else getattr(message, "interaction", None)
        if mi is None:
            return

        name = getattr(mi, "name", None)
        user = getattr(mi, "user", None)
        if name != "bump" or user is None:
            return

        # Only store humans.
        if getattr(user, "bot", False):
            return

        key = (message.guild.id, message.channel.id)
        self._recent_bump_invoker[key] = (int(user.id), time.monotonic())

    async def _get_recent_invoker(self, guild: discord.Guild, channel_id: int) -> Optional[discord.abc.User]:
        key = (guild.id, channel_id)
        rec = self._recent_bump_invoker.get(key)
        if not rec:
            return None

        user_id, ts = rec
        # Disboard replies quickly; allow a generous window.
        if time.monotonic() - ts > 45:
            return None

        m = guild.get_member(user_id)
        if m:
            return m

        try:
            return await self.bot.fetch_user(user_id)
        except Exception:
            return None

    async def _handle_possible_disboard_bump(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        await self.load_data()

        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(message.guild.id)
            bump_channel_id = guild_bucket.get("bump_channel_id")

        if not bump_channel_id:
            return

        if message.channel.id != int(bump_channel_id):
            return

        if not self._is_disboard_message(message):
            return

        if not self._looks_like_bump_success(message):
            return

        # Deduplicate message id (Disboard often edits the same message).
        self._cleanup_processed_cache()
        if message.id in self._processed_message_ids:
            return
        self._processed_message_ids[message.id] = time.monotonic()

        bumper = await self._extract_bumper_user(message)
        if bumper is None:
            # Disboard embed often doesn't mention the user; fall back to the
            # last '/bump' invoker message in the channel.
            bumper = await self._get_recent_invoker(message.guild, message.channel.id)
            if bumper is None:
                return

        # Count the bump. Disboard enforces ~2h cooldown, so we bypass our own.
        await self.update_bump_count(message.guild, bumper, now=message.created_at or _utcnow(), amount=1, bypass_cooldown=True)

    # ----------------------------
    # Event listeners
    # ----------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Do not react to our own bot.
        if message.author and message.author.id == getattr(self.bot.user, "id", None):
            return
        try:
            # Track who invoked /bump (system message).
            self._record_bump_invocation(message)
            await self._handle_possible_disboard_bump(message)
        except Exception:
            logger.exception("Failed handling possible Disboard bump message")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Disboard frequently edits the confirmation message; handle edits too.
        if after.author and after.author.id == getattr(self.bot.user, "id", None):
            return
        try:
            self._record_bump_invocation(after)
            await self._handle_possible_disboard_bump(after)
        except Exception:
            logger.exception("Failed handling edited Disboard bump message")

    # ----------------------------
    # Persistence helpers
    # ----------------------------

    async def load_data(self) -> None:
        """Load bump data from disk (creates file if missing)."""
        needs_initial_save = False

        async with self._lock:
            if self._loaded and isinstance(self._data, dict) and "guilds" in self._data:
                return

            self._data_path.parent.mkdir(parents=True, exist_ok=True)

            if not self._data_path.exists():
                self._data = {
                    "version": DATA_VERSION,
                    "guilds": {},
                    "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
                }
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
                logger.exception("Failed reading bump leaderboard data; recreating file: %s", e)
                loaded = {}

            if not isinstance(loaded, dict):
                loaded = {}

            loaded.setdefault("version", DATA_VERSION)
            loaded.setdefault("guilds", {})
            loaded.setdefault("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)

            self._data = loaded
            self._loaded = True

    async def save_data(self) -> None:
        """Save bump data to disk."""
        async with self._lock:
            self._data_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, indent=2, ensure_ascii=False, sort_keys=True)
            await asyncio.to_thread(self._data_path.write_text, payload, encoding="utf-8")

    # ----------------------------
    # Data model helpers
    # ----------------------------

    def _ensure_guild_bucket(self, guild_id: int) -> Dict[str, Any]:
        guilds: Dict[str, Any] = self._data.setdefault("guilds", {})
        bucket: Dict[str, Any] = guilds.setdefault(
            str(guild_id),
            {
                "bump_channel_id": None,
                "users": {},
                "total_bumps": 0,
                "last_bumper_id": None,
                "last_bump_time": None,
            },
        )

        bucket.setdefault("bump_channel_id", None)
        bucket.setdefault("users", {})
        bucket.setdefault("total_bumps", 0)
        bucket.setdefault("last_bumper_id", None)
        bucket.setdefault("last_bump_time", None)
        return bucket

    def _cooldown_seconds(self) -> int:
        return _safe_int(self._data.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS)

    def _get_user_bucket(self, guild_bucket: Dict[str, Any], user: discord.abc.User) -> Dict[str, Any]:
        users: Dict[str, Any] = guild_bucket.setdefault("users", {})
        u: Dict[str, Any] = users.setdefault(
            str(user.id),
            {
                "user_id": user.id,
                "username": getattr(user, "display_name", user.name),
                "total_bumps": 0,
                "last_bump_time": None,
            },
        )

        # Keep username current.
        u["username"] = getattr(user, "display_name", user.name)
        u.setdefault("user_id", user.id)
        u.setdefault("total_bumps", 0)
        u.setdefault("last_bump_time", None)
        return u

    def _format_relative_time(self, dt: Optional[datetime]) -> str:
        if not dt:
            return "Never"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return f"<t:{ts}:R>"

    def _format_full_time(self, dt: Optional[datetime]) -> str:
        if not dt:
            return "Never"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return f"<t:{ts}:f>"

    # ----------------------------
    # Core bump logic
    # ----------------------------

    async def update_bump_count(
        self,
        guild: discord.Guild,
        user: discord.abc.User,
        *,
        now: Optional[datetime] = None,
        amount: int = 1,
        bypass_cooldown: bool = False,
    ) -> Tuple[bool, Optional[float]]:
        """Update a user's bump count.

        Returns:
            (ok, retry_after_seconds)
        """
        now = now or _utcnow()

        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(guild.id)
            user_bucket = self._get_user_bucket(guild_bucket, user)

            # Cooldown check (only for positive increments).
            if amount > 0 and not bypass_cooldown:
                cooldown = self._cooldown_seconds()
                last_dt: Optional[datetime] = None

                # Prefer in-memory timestamp cache.
                last_dt = self._last_bump_cache.get(guild.id, {}).get(user.id)
                if last_dt is None:
                    last_dt = _iso_to_dt(user_bucket.get("last_bump_time"))

                if last_dt is not None:
                    elapsed = (now - last_dt).total_seconds()
                    if elapsed < cooldown:
                        return False, float(cooldown - elapsed)

            # Apply update.
            current_total = _safe_int(user_bucket.get("total_bumps"), 0)
            new_total = max(0, current_total + int(amount))
            user_bucket["total_bumps"] = new_total

            if amount > 0:
                user_bucket["last_bump_time"] = _dt_to_iso(now)
                guild_bucket["last_bumper_id"] = user.id
                guild_bucket["last_bump_time"] = _dt_to_iso(now)

                # Cache for cooldown checks.
                self._last_bump_cache.setdefault(guild.id, {})[user.id] = now

            # Update guild total bumps (keep it consistent with user totals).
            # For small guilds this is fine; avoids drift from manual edits.
            total = 0
            for v in guild_bucket.get("users", {}).values():
                if isinstance(v, dict):
                    total += _safe_int(v.get("total_bumps"), 0)
            guild_bucket["total_bumps"] = total

        # Save outside of the lock-holder section to reduce contention.
        await self.save_data()
        return True, None

    async def get_leaderboard(self, guild: discord.Guild, limit: int = 10) -> List[BumpEntry]:
        """Return sorted bump leaderboard for the guild."""
        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(guild.id)
            rows: List[BumpEntry] = []

            for user_id_str, u in guild_bucket.get("users", {}).items():
                if not isinstance(u, dict):
                    continue
                user_id = _safe_int(u.get("user_id") or user_id_str, 0)
                username = str(u.get("username") or f"{user_id}")
                total_bumps = _safe_int(u.get("total_bumps"), 0)
                last_bump_time = _iso_to_dt(u.get("last_bump_time"))
                rows.append(BumpEntry(user_id=user_id, username=username, total_bumps=total_bumps, last_bump_time=last_bump_time))

        rows.sort(key=lambda r: (r.total_bumps, (r.last_bump_time or datetime.min.replace(tzinfo=timezone.utc))), reverse=True)
        return rows[: max(1, int(limit))]

    async def get_my_stats(self, guild: discord.Guild, user: discord.abc.User) -> BumpEntry:
        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(guild.id)
            user_bucket = self._get_user_bucket(guild_bucket, user)
            return BumpEntry(
                user_id=user.id,
                username=str(user_bucket.get("username") or getattr(user, "display_name", user.name)),
                total_bumps=_safe_int(user_bucket.get("total_bumps"), 0),
                last_bump_time=_iso_to_dt(user_bucket.get("last_bump_time")),
            )

    async def get_bump_stats(self, guild: discord.Guild) -> Tuple[int, Optional[int], Optional[datetime]]:
        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(guild.id)
            total = _safe_int(guild_bucket.get("total_bumps"), 0)
            last_bumper_id = _safe_int(guild_bucket.get("last_bumper_id"), 0) or None
            last_bump_time = _iso_to_dt(guild_bucket.get("last_bump_time"))
            return total, last_bumper_id, last_bump_time

    # ----------------------------
    # Permission helpers
    # ----------------------------

    def _is_manage_guild_member(self, member: discord.Member) -> bool:
        return member.guild_permissions.manage_guild

    async def _ensure_manage_guild(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("Server-only command.")
            return False
        if not isinstance(ctx.author, discord.Member):
            await ctx.send("Server member only.")
            return False
        if not self._is_manage_guild_member(ctx.author):
            await ctx.send("You need the **Manage Server** permission to use this command.")
            return False
        return True

    async def _ensure_manage_guild_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return False
        if isinstance(interaction.user, discord.Member):
            if interaction.user.guild_permissions.manage_guild:
                return True
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this command.",
                ephemeral=True,
            )
            return False

        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            await interaction.response.send_message("Could not verify permissions.", ephemeral=True)
            return False

        if member.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "You need the **Manage Server** permission to use this command.",
            ephemeral=True,
        )
        return False

    # ----------------------------
    # Hybrid commands (slash + prefix)
    # ----------------------------

    @commands.hybrid_command(name="bumplb", description="Show the bump leaderboard (top 10)")
    async def bumplb(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("Server-only command.")

        await self.load_data()
        rows = await self.get_leaderboard(ctx.guild, limit=10)

        embed = discord.Embed(
            title="🏆 Bump Leaderboard",
            description="Top 10 bumpers in this server",
            color=discord.Color.blurple(),
        )

        if not rows or all(r.total_bumps == 0 for r in rows):
            embed.description = "No bumps recorded yet. Use Disboard's `/bump` in the bump channel to get started."
            return await ctx.send(embed=embed)

        lines: List[str] = []
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows, start=1):
            rank = medals[i - 1] if i <= 3 else f"`#{i}`"
            lines.append(
                f"{rank} <@{r.user_id}> — **{r.total_bumps}** bumps · last: {self._format_relative_time(r.last_bump_time)}"
            )

        embed.add_field(name="Rankings", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="bumpstats", description="Show total bumps and the most recent bumper")
    async def bumpstats(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("Server-only command.")

        await self.load_data()
        total, last_bumper_id, last_bump_time = await self.get_bump_stats(ctx.guild)

        embed = discord.Embed(title="📈 Bump Stats", color=discord.Color.gold())
        embed.add_field(name="Total bumps", value=str(total), inline=True)

        if last_bumper_id:
            embed.add_field(name="Most recent bumper", value=f"<@{last_bumper_id}>", inline=True)
            embed.add_field(name="Last bump", value=self._format_full_time(last_bump_time), inline=False)
        else:
            embed.add_field(name="Most recent bumper", value="None yet", inline=True)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setbumpchannel", description="Set the channel where bumps count")
    @app_commands.describe(channel="Channel where Disboard bumps should be counted")
    async def setbumpchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._ensure_manage_guild(ctx):
            return

        await self.load_data()
        async with self._lock:
            guild_bucket = self._ensure_guild_bucket(ctx.guild.id)  # type: ignore[union-attr]
            guild_bucket["bump_channel_id"] = channel.id
        await self.save_data()

        embed = discord.Embed(
            title="✅ Bump channel set",
            description=f"Bumps will now count in {channel.mention}.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="addbumps", description="Admin: add bumps to a user")
    @app_commands.describe(user="User to modify", amount="Number of bumps to add")
    async def addbumps(self, ctx: commands.Context, user: discord.Member, amount: int):
        if not await self._ensure_manage_guild(ctx):
            return

        if amount < 1 or amount > 100000:
            return await ctx.send("Amount must be between 1 and 100000.")

        await self.load_data()
        ok, _ = await self.update_bump_count(ctx.guild, user, amount=int(amount), bypass_cooldown=True)  # type: ignore[arg-type]
        if not ok:
            # bypass_cooldown=True should always be ok
            pass

        entry = await self.get_my_stats(ctx.guild, user)  # type: ignore[arg-type]
        embed = discord.Embed(
            title="✅ Bumps added",
            description=f"Added **{amount}** bumps to {user.mention}.\nTotal: **{entry.total_bumps}**",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="removebumps", description="Admin: remove bumps from a user")
    @app_commands.describe(user="User to modify", amount="Number of bumps to remove")
    async def removebumps(self, ctx: commands.Context, user: discord.Member, amount: int):
        if not await self._ensure_manage_guild(ctx):
            return

        if amount < 1 or amount > 100000:
            return await ctx.send("Amount must be between 1 and 100000.")

        await self.load_data()
        ok, _ = await self.update_bump_count(ctx.guild, user, amount=-int(amount), bypass_cooldown=True)  # type: ignore[arg-type]
        if not ok:
            pass

        entry = await self.get_my_stats(ctx.guild, user)  # type: ignore[arg-type]
        embed = discord.Embed(
            title="✅ Bumps removed",
            description=f"Removed **{amount}** bumps from {user.mention}.\nTotal: **{entry.total_bumps}**",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    # ----------------------------
    # Prefix-only commands (requested aliases)
    # ----------------------------

    @commands.command(name="mybumps")
    async def mybumps(self, ctx: commands.Context):
        """Prefix-only: show your bump stats."""
        if ctx.guild is None:
            return await ctx.send("Server-only command.")

        await self.load_data()
        entry = await self.get_my_stats(ctx.guild, ctx.author)

        embed = discord.Embed(title="🙋 Your bumps", color=discord.Color.blurple())
        embed.add_field(name="Total bumps", value=str(entry.total_bumps), inline=True)
        embed.add_field(name="Last bump", value=self._format_full_time(entry.last_bump_time), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="blb")
    async def blb(self, ctx: commands.Context):
        """Prefix-only alias for bump leaderboard."""
        await self.bumplb(ctx)  # reuse hybrid handler

    @commands.command(name="bst")
    async def bst(self, ctx: commands.Context):
        """Prefix-only alias for bump stats."""
        await self.bumpstats(ctx)

    @commands.command(name="topbump")
    async def topbump(self, ctx: commands.Context):
        """Prefix-only: show top 3 bumpers."""
        if ctx.guild is None:
            return await ctx.send("Server-only command.")

        await self.load_data()
        rows = await self.get_leaderboard(ctx.guild, limit=3)

        embed = discord.Embed(title="🥇 Top bumpers", color=discord.Color.blurple())
        if not rows or all(r.total_bumps == 0 for r in rows):
            embed.description = "No bumps recorded yet."
            return await ctx.send(embed=embed)

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows, start=1):
            lines.append(f"{medals[i-1]} <@{r.user_id}> — **{r.total_bumps}**")
        embed.add_field(name="Top 3", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    # ----------------------------
    # Error handling for prefix commands
    # ----------------------------

    @addbumps.error
    @removebumps.error
    @setbumpchannel.error
    async def _admin_prefix_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument. Please mention a valid user/channel and amount.")
            return
        await ctx.send("An error occurred while processing your command.")


async def setup(bot: commands.Bot):
    """Load the bump leaderboard cog."""
    await bot.add_cog(BumpLeaderboard(bot))
