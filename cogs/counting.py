import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from utils.codebuddy_database import DB_PATH
import ast
import operator
import random
import asyncio
import time
from typing import Optional

class Counting(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Cache for counting channels: guild_id -> channel_id
        self.counting_channels = {}
        # Protect against occasional duplicate MESSAGE_CREATE dispatches or accidental double-processing.
        # Key: message_id, Value: monotonic timestamp
        self._recent_message_ids: dict[int, float] = {}
        # Throttle reaction API calls to avoid Discord rate limits in fast counting channels.
        self._reaction_queue: asyncio.Queue[tuple[discord.Message, str]] = asyncio.Queue()
        self._pending_reactions: set[tuple[int, str]] = set()
        self._reaction_worker_task: Optional[asyncio.Task[None]] = None

    async def cog_unload(self) -> None:
        if self._reaction_worker_task and not self._reaction_worker_task.done():
            self._reaction_worker_task.cancel()

    async def _reaction_worker(self) -> None:
        # A small delay between reaction requests keeps us under the common reaction route limits.
        # Reactions may appear slightly delayed, but they will still be added.
        while True:
            message, emoji = await self._reaction_queue.get()
            try:
                try:
                    await message.add_reaction(emoji)
                except Exception:
                    pass
                await asyncio.sleep(0.35)
            finally:
                self._pending_reactions.discard((message.id, emoji))
                self._reaction_queue.task_done()

    def _enqueue_reaction(self, message: discord.Message, emoji: str) -> None:
        key = (message.id, emoji)
        if key in self._pending_reactions:
            return
        self._pending_reactions.add(key)
        try:
            self._reaction_queue.put_nowait((message, emoji))
        except Exception:
            self._pending_reactions.discard(key)

    async def cog_load(self):
        """Load counting channels into memory on startup"""
        if self._reaction_worker_task is None or self._reaction_worker_task.done():
            self._reaction_worker_task = asyncio.create_task(self._reaction_worker())
        try:
            async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                # Ensure auxiliary tables exist
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS counting_warnings (
                        guild_id INTEGER,
                        user_id INTEGER,
                        warnings INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (guild_id, user_id)
                    )
                """)

                await db.execute("""
                    CREATE TABLE IF NOT EXISTS counting_active_highscore (
                        guild_id INTEGER PRIMARY KEY,
                        message_id INTEGER
                    )
                """)

                await db.execute("""
                    CREATE TABLE IF NOT EXISTS counting_highscore_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        score INTEGER NOT NULL,
                        user_id INTEGER,
                        message_id INTEGER,
                        timestamp INTEGER
                    )
                """)

                await db.commit()

                try:
                    async with db.execute("SELECT guild_id, channel_id FROM counting_config") as cursor:
                        rows = await cursor.fetchall()
                        for guild_id, channel_id in rows:
                            self.counting_channels[guild_id] = channel_id
                    print(f"Loaded {len(self.counting_channels)} counting channels")
                except aiosqlite.OperationalError:
                    print("counting_config table not found during cog load (likely first run)")
        except Exception as e:
            print(f"Error loading counting channels: {e}")

    async def _get_warning_count(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute(
                "SELECT warnings FROM counting_warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _set_warning_count(self, guild_id: int, user_id: int, warnings: int) -> None:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            if warnings <= 0:
                await db.execute(
                    "DELETE FROM counting_warnings WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO counting_warnings (guild_id, user_id, warnings)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET warnings = excluded.warnings
                    """,
                    (guild_id, user_id, warnings),
                )
            await db.commit()

    async def _clear_all_warnings(self, guild_id: int) -> None:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("DELETE FROM counting_warnings WHERE guild_id = ?", (guild_id,))
            await db.commit()

    async def _get_active_highscore_message_id(self, guild_id: int) -> Optional[int]:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute(
                "SELECT message_id FROM counting_active_highscore WHERE guild_id = ?",
                (guild_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def _set_active_highscore_message_id(self, guild_id: int, message_id: Optional[int]) -> None:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            if message_id is None:
                await db.execute("DELETE FROM counting_active_highscore WHERE guild_id = ?", (guild_id,))
            else:
                await db.execute(
                    """
                    INSERT INTO counting_active_highscore (guild_id, message_id)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET message_id = excluded.message_id
                    """,
                    (guild_id, message_id),
                )
            await db.commit()

    async def _remove_bot_reactions(self, channel: discord.TextChannel, message_id: int) -> None:
        try:
            msg = await channel.fetch_message(message_id)
        except Exception:
            return

        if not self.bot.user:
            return
        try:
            await msg.remove_reaction("✅", self.bot.user)
        except Exception:
            pass
        try:
            await msg.remove_reaction("🏆", self.bot.user)
        except Exception:
            pass

    async def _mark_highscore_message(
        self,
        message: discord.Message,
        new_count: int,
        previous_high_score: int,
    ) -> None:
        """Add ✅+🏆 to the message.

        Note: Reactions, once added by the bot, should never be removed.
        """
        if not message.guild or not isinstance(message.channel, discord.TextChannel):
            return

        guild_id = message.guild.id
        channel = message.channel

        # Only add the trophy here.
        # The ✅ reaction is added for all valid counts in the main handler;
        # adding it again here causes extra API calls and rate limits.
        self._enqueue_reaction(message, "🏆")

        # Track the latest highscore/tie message ID for bookkeeping.
        # (We no longer remove reactions from older messages.)
        await self._set_active_highscore_message_id(guild_id, message.id)

        # Record history only if it is a NEW record
        if new_count > previous_high_score:
            async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                await db.execute(
                    """
                    INSERT INTO counting_highscore_history (guild_id, score, user_id, message_id, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (guild_id, new_count, message.author.id, message.id, int(time.time())),
                )
                await db.commit()

    async def _clear_highscore_marker_if_any(self, guild_id: int, channel: discord.TextChannel) -> None:
        marker_id = await self._get_active_highscore_message_id(guild_id)
        if marker_id:
            await self._set_active_highscore_message_id(guild_id, None)

    @app_commands.command(name="setcountingchannel", description="Set the channel for the counting game")
    @app_commands.checks.has_permissions(administrator=True)
    async def setcountingchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        # Slash command interactions must be acknowledged quickly.
        # DB operations can take >3s (locks, slow disks), so defer immediately.
        if interaction.response.is_done():
            # Extremely defensive; normally false here.
            pass
        else:
            await interaction.response.defer(ephemeral=True)

        if interaction.guild_id is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        retries = 3
        while retries > 0:
            try:
                async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                    await db.execute(
                        """
                        INSERT INTO counting_config (guild_id, channel_id)
                        VALUES (?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id
                        """,
                        (interaction.guild_id, channel.id),
                    )
                    await db.commit()
                break
            except aiosqlite.OperationalError as e:
                if "locked" in str(e).lower():
                    retries -= 1
                    await asyncio.sleep(0.5)
                    continue
                raise

        # Update cache
        self.counting_channels[interaction.guild_id] = channel.id

        await interaction.followup.send(f"Counting channel set to {channel.mention}", ephemeral=True)

    def safe_eval(self, expr):
        operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.BitXor: operator.pow, # Allow ^ for power
            ast.USub: operator.neg
        }

        def eval_node(node):
            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                raise TypeError("Not a number")
            elif isinstance(node, ast.BinOp):
                op = type(node.op)
                if op in operators:
                    left = eval_node(node.left)
                    right = eval_node(node.right)
                    if op in (ast.Pow, ast.BitXor):
                        if right > 100: # Limit exponent
                            raise ValueError("Exponent too large")
                    return operators[op](left, right)
            elif isinstance(node, ast.UnaryOp):
                op = type(node.op)
                if op in operators:
                    return operators[op](eval_node(node.operand))
            raise TypeError("Unsupported type")

        try:
            tree = ast.parse(expr, mode='eval')
            return eval_node(tree.body)
        except Exception:
            return None

    def _extract_leading_expression(self, text: str) -> Optional[str]:
        """Extract a safe-eval compatible expression from the start of a message.

        This allows messages like "43 is next" or "6*7 nice" to count as 43/42.
        Only a leading run of digits/operators/parentheses is considered.
        """
        if not text:
            return None

        # Normalize whitespace/newlines.
        s = text.replace("\n", " ").strip()
        if not s:
            return None

        # Allow common markdown wrappers before the number (e.g. `43`, **43**).
        s = s.lstrip()
        while s and s[0] in "`*_~":
            s = s[1:].lstrip()

        allowed = set("0123456789+-*/^(). ")
        expr_chars: list[str] = []
        for ch in s:
            if ch in allowed:
                expr_chars.append(ch)
            else:
                break

        expr = "".join(expr_chars).strip()
        return expr or None

    def _parse_count_number(self, message_content: str) -> Optional[int]:
        expr = self._extract_leading_expression(message_content)
        if not expr:
            return None

        number = self.safe_eval(expr)
        if number is None:
            return None

        if isinstance(number, float):
            if number.is_integer():
                return int(number)
            return None
        if isinstance(number, int):
            return number
        return None

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        # 1. OPTIMIZATION: Check cache first before touching DB
        if message.guild.id not in self.counting_channels:
            return
        
        if message.channel.id != self.counting_channels[message.guild.id]:
            return

        # Deduplicate processing of the same message ID within this process.
        # This prevents duplicate warnings/messages if Discord or the bot dispatches the event twice.
        now = time.monotonic()
        last_seen = self._recent_message_ids.get(message.id)
        if last_seen is not None and (now - last_seen) < 30:
            return
        self._recent_message_ids[message.id] = now
        if len(self._recent_message_ids) > 5000:
            cutoff = now - 120
            self._recent_message_ids = {mid: ts for mid, ts in self._recent_message_ids.items() if ts >= cutoff}

        # 2. Process the message logic
        # Wrap DB operations in retry loop for robustness
        retries = 3
        while retries > 0:
            try:
                async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                    async with db.execute("SELECT current_count, last_user_id, high_score FROM counting_config WHERE guild_id = ?", (message.guild.id,)) as cursor:
                        config = await cursor.fetchone()
                    
                    if not config:
                        # Should not happen if in cache, but possible if DB was manually cleared
                        return

                    current_count, last_user_id, high_score = config

                    # Try to parse the number
                    content = message.content.strip()
                    if not content:
                        return

                    # Evaluate math expression (supports extra trailing text like "43 is next")
                    number = self._parse_count_number(content)
                    if number is None:
                        return  # Not a valid number/expression at the start

                    # Check rules
                    next_count = current_count + 1
                    
                    if number != next_count:
                        await self.fail_count(message, current_count, "Wrong number!")
                        return

                    if message.author.id == last_user_id:
                        # Warn instead of instant ruin. 3 warnings ruins the count.
                        # Use an atomic increment in the SAME connection to avoid races and DB-lock retries.
                        await db.execute(
                            """
                            INSERT INTO counting_warnings (guild_id, user_id, warnings)
                            VALUES (?, ?, 1)
                            ON CONFLICT(guild_id, user_id) DO UPDATE SET warnings = warnings + 1
                            """,
                            (message.guild.id, message.author.id),
                        )
                        async with db.execute(
                            "SELECT warnings FROM counting_warnings WHERE guild_id = ? AND user_id = ?",
                            (message.guild.id, message.author.id),
                        ) as cursor:
                            row = await cursor.fetchone()
                        warnings = int(row[0]) if row else 1
                        await db.commit()

                        if warnings >= 3:
                            await self.fail_count(message, current_count, "Too many warnings (counted twice in a row 3 times)!")
                            return

                        self._enqueue_reaction(message, "⚠️")

                        await message.channel.send(
                            f"You can't count twice in a row, {message.author.mention}. "
                            f"You have **{warnings}/3** warnings.",
                            delete_after=12,
                        )
                        return

                    # Valid count - Update DB
                    new_high_score = max(high_score, next_count)
                    
                    # Update configuration tables
                    await db.execute("""
                        UPDATE counting_config 
                        SET current_count = ?, last_user_id = ?, high_score = ?
                        WHERE guild_id = ?
                    """, (next_count, message.author.id, new_high_score, message.guild.id))
                    
                    # Update user stats
                    await db.execute("""
                        INSERT INTO counting_stats (user_id, guild_id, total_counts, ruined_counts)
                        VALUES (?, ?, 1, 0)
                        ON CONFLICT(user_id, guild_id) DO UPDATE SET total_counts = total_counts + 1
                    """, (message.author.id, message.guild.id))

                    # Reset warnings for this user on a valid count (in the same transaction).
                    await db.execute(
                        "DELETE FROM counting_warnings WHERE guild_id = ? AND user_id = ?",
                        (message.guild.id, message.author.id),
                    )
                    
                    await db.commit()

                    # Side effects after commit to avoid duplicate reactions on retries.
                    self._enqueue_reaction(message, "✅")

                    # Highscore marker: react ✅+🏆 when reaching/topping the record
                    if next_count >= high_score:
                        await self._mark_highscore_message(message, next_count, high_score)
                    return # Success
            
            except aiosqlite.OperationalError as e:
                # If specifically locked, retry
                if "locked" in str(e):
                    retries -= 1
                    if retries == 0:
                        print(f"Database locked repeatedly in counting for msg {message.id}")
                        # Don't crash bot, just ignore or log
                        return
                    await asyncio.sleep(0.1 * (4 - retries)) # backoff
                else:
                    raise # Re-raise other operational errors

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Announce when someone deletes a counting number message."""
        if message.author.bot or not message.guild:
            return

        if message.guild.id not in self.counting_channels:
            return

        if message.channel.id != self.counting_channels[message.guild.id]:
            return

        content = (message.content or "").strip()
        if not content:
            return

        number = self._parse_count_number(content)
        if number is None:
            return

        await message.channel.send(
            f"{message.author.mention} deleted a number **{number}**.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def fail_count(self, message, current_count, reason):
        # 1. Send initial message
        await message.add_reaction("❌")
        status_msg = await message.channel.send(
            f"{reason} {message.author.mention} messed up at {current_count}!\n"
            "🎲 **Rolling the Dice of Fate...**\n"
            "React with 🎲 to help roll! (Need 2 reactions in 60s)"
        )
        await status_msg.add_reaction("🎲")

        # 2. Wait for reactions
        reactions_collected = False
        try:
            end_time = asyncio.get_event_loop().time() + 60
            while True:
                # Check current count
                status_msg = await message.channel.fetch_message(status_msg.id)
                reaction = discord.utils.get(status_msg.reactions, emoji="🎲")
                
                # If bot reacted, count is at least 1. We need 2 total.
                if reaction and reaction.count >= 2:
                    reactions_collected = True
                    break
                
                timeout = end_time - asyncio.get_event_loop().time()
                if timeout <= 0:
                    break
                
                try:
                    # Wait for any reaction on this message
                    await self.bot.wait_for(
                        'reaction_add', 
                        check=lambda r, u: r.message.id == status_msg.id and str(r.emoji) == "🎲", 
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    break
        except Exception:
            pass # Proceed if something fails

        # 3. Determine Outcome
        outcome_msg = ""
        new_count = 0
        new_last_user_id = None
        
        dice_db_ops = [] # List of DB operations to perform (query, args)

        if not reactions_collected:
            # TIMEOUT / NOT ENOUGH REACTIONS -> RESET
            new_count = 0
            new_last_user_id = None
            outcome_msg = "⏳ **Time's up!** Not enough people helped roll the dice.\n💥 **Reset!** The count goes back to 0."
            
            dice_db_ops.append(("""
                UPDATE counting_config 
                SET current_count = 0, last_user_id = NULL
                WHERE guild_id = ?
            """, (message.guild.id,)))

            dice_db_ops.append(("""
                INSERT INTO counting_stats (user_id, guild_id, total_counts, ruined_counts)
                VALUES (?, ?, 0, 1)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET ruined_counts = ruined_counts + 1
            """, (message.author.id, message.guild.id)))

        else:
            # REACTIONS COLLECTED -> ROLL DICE
            dice_roll = random.randint(1, 6)
            outcome_msg = f"🎲 **Dice Roll: {dice_roll}**\n"
            
            if dice_roll in [2, 4, 6]:
                # SAVE
                new_count = current_count
                outcome_msg += "✨ **Saved!** The count continues!"
                # No update to config needed except maybe verifying it? 
                # Actually if saved, we do NOTHING to counting_config.
            elif dice_roll == 3:
                # RESET
                new_count = 0
                new_last_user_id = None
                outcome_msg += "💥 **Reset!** The count goes back to 0."
            elif dice_roll == 1:
                # -10 Penalty
                new_count = max(0, current_count - 10)
                new_last_user_id = None
                outcome_msg += "🔻 **-10 Penalty!** The count drops by 10."
            elif dice_roll == 5:
                # -5 Penalty
                new_count = max(0, current_count - 5)
                new_last_user_id = None
                outcome_msg += "🔻 **-5 Penalty!** The count drops by 5."

            if dice_roll not in [2, 4, 6]:
                dice_db_ops.append(("""
                    UPDATE counting_config 
                    SET current_count = ?, last_user_id = ?
                    WHERE guild_id = ?
                """, (new_count, new_last_user_id, message.guild.id)))
            
            dice_db_ops.append(("""
                INSERT INTO counting_stats (user_id, guild_id, total_counts, ruined_counts)
                VALUES (?, ?, 0, 1)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET ruined_counts = ruined_counts + 1
            """, (message.author.id, message.guild.id)))

        # EXECUTE DB OPS with Retry
        if dice_db_ops:
            retries = 3
            while retries > 0:
                try:
                    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
                        for sql, args in dice_db_ops:
                            await db.execute(sql, args)
                        await db.commit()
                    break # Success
                except aiosqlite.OperationalError as e:
                    if "locked" in str(e):
                        retries -= 1
                        await asyncio.sleep(0.5)
                    else:
                        print(f"Error saving count fail state: {e}")
                        break

        # 4. Edit message
        await status_msg.edit(content=f"{reason} {message.author.mention} messed up at {current_count}!\n{outcome_msg}\nNext number is **{new_count + 1}**.")

        # If the count was actually changed (ruined/reset/penalty), clear warnings and remove highscore marker.
        count_ruined = new_count != current_count
        if count_ruined and message.guild and isinstance(message.channel, discord.TextChannel):
            await self._clear_all_warnings(message.guild.id)
            await self._clear_highscore_marker_if_any(message.guild.id, message.channel)

    @commands.hybrid_command(name="highscoretable", aliases=["highscores"], help="Show recent counting highscores")
    async def highscore_table(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("Server only command.")

        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute(
                """
                SELECT score, user_id, timestamp
                FROM counting_highscore_history
                WHERE guild_id = ?
                ORDER BY score DESC
                LIMIT 10
                """,
                (ctx.guild.id,),
            ) as cursor:
                rows = await cursor.fetchall()

            async with db.execute(
                "SELECT current_count, high_score FROM counting_config WHERE guild_id = ?",
                (ctx.guild.id,),
            ) as cursor:
                config = await cursor.fetchone()

        current = config[0] if config else 0
        high = config[1] if config else 0

        embed = discord.Embed(title="Counting Highscores", color=discord.Color.gold())
        embed.add_field(name="Current Count", value=str(current), inline=True)
        embed.add_field(name="All-Time High", value=str(high), inline=True)

        if not rows:
            embed.description = "No highscore history yet."
            return await ctx.send(embed=embed)

        lines = []
        for i, (score, user_id, ts) in enumerate(rows, 1):
            when = f"<t:{int(ts)}:R>" if ts else ""
            lines.append(f"{i}. **{score}** by <@{user_id}> {when}")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.command(name="mcl", aliases=["tc"])
    async def most_count_leaderboard(self, ctx):
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute("""
                SELECT user_id, total_counts 
                FROM counting_stats 
                WHERE guild_id = ? 
                ORDER BY total_counts DESC 
                LIMIT 10
            """, (ctx.guild.id,)) as cursor:
                rows = await cursor.fetchall()
        
        if not rows:
            await ctx.send("No counting stats yet.")
            return

        embed = discord.Embed(title="Most Count Leaderboard", color=discord.Color.blue())
        description = ""
        for i, (user_id, count) in enumerate(rows, 1):
            description += f"{i}. <@{user_id}>: {count}\n"
        embed.description = description
        await ctx.send(embed=embed)

    @commands.command(name="mrl")
    async def most_ruined_leaderboard(self, ctx):
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute("""
                SELECT user_id, ruined_counts 
                FROM counting_stats 
                WHERE guild_id = ? 
                ORDER BY ruined_counts DESC 
                LIMIT 10
            """, (ctx.guild.id,)) as cursor:
                rows = await cursor.fetchall()
        
        if not rows:
            await ctx.send("No ruined stats yet.")
            return

        embed = discord.Embed(title="Most Ruined Leaderboard", color=discord.Color.red())
        description = ""
        for i, (user_id, count) in enumerate(rows, 1):
            description += f"{i}. <@{user_id}>: {count}\n"
        embed.description = description
        await ctx.send(embed=embed)

    @commands.command(name="scs")
    async def server_count_stats(self, ctx):
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            async with db.execute("SELECT current_count, high_score FROM counting_config WHERE guild_id = ?", (ctx.guild.id,)) as cursor:
                row = await cursor.fetchone()
        
        if not row:
            await ctx.send("Counting channel not set up or no data.")
            return
            
        current, high = row
        embed = discord.Embed(title="Server Count Stats", color=discord.Color.green())
        embed.add_field(name="Current Count", value=str(current))
        embed.add_field(name="High Score", value=str(high))
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Counting(bot))
