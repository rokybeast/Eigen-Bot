import discord
from discord.ext import commands
from discord.ui import Button, View
from discord import app_commands
import asyncio
import time
import random
from typing import Optional, Union

EMOJIS = {
    "rock": "🪨",
    "paper": "📄",
    "scissors": "✂️"
}
CHECK_MARK = "✅"
EMPTY_MARK = "⠀⠀"  # Gleich breit wie CHECK_MARK
SKULL = "☠"
HEART = "❤️"

class RockPaperScissorsGame:
    def __init__(self, player1, player2, ai_mode=False):
        self.players = [player1, player2]
        self.lives = [3, 3]
        self.choices = [None, None]
        self.rounds = []
        self.game_over = False
        self.winner = None
        self.ai_mode = ai_mode

    def set_choice(self, player_index, choice):
        if self.choices[player_index] is not None:
            return False
        self.choices[player_index] = choice
        return True

    def both_chosen(self):
        return self.choices[0] is not None and self.choices[1] is not None

    def determine_winner_of_round(self):
        p1 = self.choices[0]
        p2 = self.choices[1]
        if p1 is None or p2 is None:
            return -1  # Draw if either choice is None
        if p1 == p2:
            return -1  # Draw
        wins = {
            "rock": "scissors",
            "scissors": "paper",
            "paper": "rock"
        }
        if wins[p1] == p2:
            return 0
        else:
            return 1

    def end_round(self):
        winner = self.determine_winner_of_round()
        self.rounds.append((self.choices[0], self.choices[1], winner))
        if winner != -1:
            loser = 1 - winner
            self.lives[loser] -= 1
        self.choices = [None, None]

        if self.lives[0] == 0:
            self.game_over = True
            self.winner = 1
        elif self.lives[1] == 0:
            self.game_over = True
            self.winner = 0

class RockPaperScissorsView(View):
    def __init__(self, game, interaction, cog):
        super().__init__(timeout=30)
        self.game = game
        self.interaction = interaction
        self.cog = cog
        self.timeout_task = None
        self.timeout_until = int(time.time()) + 30  # Zeitstempel für Timeout

        for choice in ["rock", "paper", "scissors"]:
            btn = Button(label="", emoji=EMOJIS[choice], style=discord.ButtonStyle.primary, custom_id=choice)
            btn.callback = self.make_choice_callback(choice)
            self.add_item(btn)

    def make_choice_callback(self, choice):
        async def callback(interaction: discord.Interaction):
            if self.game.game_over:
                return

            player_index = None
            for idx, player in enumerate(self.game.players):
                if player.id == interaction.user.id:
                    player_index = idx
                    break
            if player_index is None:
                await interaction.response.defer()
                return

            if self.game.choices[player_index] is not None:
                await interaction.response.defer()
                return

            self.game.set_choice(player_index, choice)
            await interaction.response.defer()

            # Timeout NICHT zurücksetzen, sondern erst nach beiden Zügen

            # Wenn gegen Bot: Bot wählt sofort nach dem Spieler
            if self.game.ai_mode and player_index == 0 and not self.game.choices[1]:
                await asyncio.sleep(0.5)
                self.game.choices[1] = self.bot_choice()
                await self.update_message()

            if self.game.both_chosen():
                self.game.end_round()
                # Timeout jetzt zurücksetzen!
                if self.timeout_task and not self.timeout_task.done():
                    self.timeout_task.cancel()
                self.timeout_until = int(time.time()) + 30
                self.timeout_task = asyncio.create_task(self.player_timeout())

            await self.update_message()

            if self.game.game_over:
                self.clear_items()
                await self.update_message()
                self.cog.active_players.discard(self.game.players[0].id)
                if not self.game.ai_mode:
                    self.cog.active_players.discard(self.game.players[1].id)
                if self.timeout_task and not self.timeout_task.done():
                    self.timeout_task.cancel()

        return callback

    async def player_timeout(self):
        await asyncio.sleep(30)
        if not self.game.game_over:
            self.clear_items()
            await self.interaction.edit_original_response(content=self.format_message(cancelled=True), view=self)
            self.cog.active_players.discard(self.game.players[0].id)
            if not self.game.ai_mode:
                self.cog.active_players.discard(self.game.players[1].id)

    def bot_choice(self):
        return random.choice(["rock", "paper", "scissors"])

    def format_lives(self, player_index):
        lives = self.game.lives[player_index]
        return HEART * lives if lives > 0 else SKULL

    def format_player_line(self, player_index):
        mark = CHECK_MARK if self.game.choices[player_index] is not None else EMPTY_MARK
        player = self.game.players[player_index]
        lives = self.format_lives(player_index)
        return f"⠀ {mark} {player.mention} {lives}"

    def format_player_line_endgame(self, player_index):
        player = self.game.players[player_index]
        lives = self.format_lives(player_index)
        return f"{player.display_name} {lives}"

    def format_rounds(self):
        if not self.game.rounds:
            return ""
        lines = []
        for p1_choice, p2_choice, winner in self.game.rounds:
            if winner == 0:
                winner_name = f" [{self.game.players[0].display_name}]"
            elif winner == 1:
                winner_name = f" [{self.game.players[1].display_name}]"
            else:
                winner_name = " [Draw]"
            lines.append(f"``{EMOJIS[p1_choice]} vs {EMOJIS[p2_choice]}{winner_name}``")
        return "\n".join(lines)

    def format_message(self, cancelled=False):
        header = f"**Rock Paper Scissors [**{self.game.players[0].display_name} vs {self.game.players[1].display_name}**]**"
        rounds = self.format_rounds()
        # Timer immer anzeigen, außer bei Game Over/Timeout
        timer_line = ""
        if not cancelled and not self.game.game_over:
            timer_line = f"\nTimeout <t:{self.timeout_until}:R>"

        if cancelled:
            body = "\n**Timeout.**"
            body += f"\n\n{self.format_player_line_endgame(0)}\n{self.format_player_line_endgame(1)}"
            if rounds:
                body += f"\n\n{rounds}"
        elif self.game.game_over:
            winner_id = self.game.winner
            if winner_id is not None:
                winner = self.game.players[winner_id]
                body = f"\n{winner.mention} has **won!**\n\n"
            else:
                body = "Draw!\n\n"
            body += f"{self.format_player_line_endgame(0)}\n{self.format_player_line_endgame(1)}"
            if rounds:
                body += f"\n\n{rounds}"
        else:
            if rounds:
                body = f"\n\n{self.format_player_line(0)}\n{self.format_player_line(1)}\n\n{rounds}"
            else:
                body = f"\n\n{self.format_player_line(0)}\n{self.format_player_line(1)}\n\n⠀"

        # Timer über das Spielfeld, unter dem Header
        return f"{header}{timer_line}{body}"

    async def on_timeout(self):
        if not self.game.game_over:
            self.clear_items()
            await self.interaction.edit_original_response(content=self.format_message(cancelled=True), view=self)
            self.cog.active_players.discard(self.game.players[0].id)
            if not self.game.ai_mode:
                self.cog.active_players.discard(self.game.players[1].id)

    async def update_message(self):
        await self.interaction.edit_original_response(content=self.format_message(), view=self)

class RockPaperScissorsChallengeView(View):
    def __init__(self, challenger, opponent, cog, message):
        super().__init__(timeout=31)
        self.challenger = challenger
        self.opponent = opponent
        self.cog = cog
        # For slash command flows, the underlying message is an InteractionMessage.
        # We set it later once the original response is available.
        self.message: Optional[Union[discord.Message, discord.InteractionMessage]] = message

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.opponent.id:
            embed = discord.Embed(
                description=f"Only {self.opponent.mention} can accept this challenge.",
                colour=discord.Colour.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()
        button.disabled = True

        # If the view was created before we had the original response, fall back to the
        # message that triggered this component interaction.
        if self.message is None and interaction.message is not None:
            self.message = interaction.message

        game = RockPaperScissorsGame(self.challenger, self.opponent)
        game_view = RockPaperScissorsView(game, interaction, self.cog)

        if self.message is not None and self.message.id in self.cog.challenges:
            self.cog.challenges[self.message.id]["accepted"] = True

        if self.message is not None:
            await self.message.edit(
                content=game_view.format_message(),
                view=game_view
            )

class RockPaperScissorsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.challenges = {}
        self.active_players = set()

    @app_commands.command(name="rockpaperscissors", description="Challenge another player or the bot to Rock Paper Scissors")
    async def rockpaperscissors(self, interaction: discord.Interaction, opponent: discord.User):
        challenger = interaction.user

        def error_embed(msg):
            return discord.Embed(description=msg, colour=discord.Colour.red())

        if challenger.id in self.active_players:
            return await interaction.response.send_message(embed=error_embed("You are already in a game."), ephemeral=True)
        if opponent.id in self.active_players and opponent != self.bot.user:
            return await interaction.response.send_message(embed=error_embed("That player is already in a game."), ephemeral=True)
        if opponent.id == challenger.id:
            return await interaction.response.send_message(embed=error_embed("You cannot challenge yourself."), ephemeral=True)

        # --- Bot Mode ---
        if opponent == self.bot.user:
            try:
                bot_member = interaction.guild.get_member(self.bot.user.id) if interaction.guild else self.bot.user
                game = RockPaperScissorsGame(challenger, bot_member, ai_mode=True)
                view = RockPaperScissorsView(game, interaction, self)
                await interaction.response.send_message(
                    view.format_message(),
                    view=view
                )
                self.active_players.add(challenger.id)  # <-- Jetzt erst eintragen!
            except Exception as e:
                print(f"Error starting RockPaperScissors vs Bot: {e}")
            return

        try:
            eta_timestamp = int(time.time()) + 31
            challenge_view = RockPaperScissorsChallengeView(challenger, opponent, self, None)

            await interaction.response.send_message(
                f"{opponent.mention}, you have been challenged to **Rock Paper Scissors**!\nThe challenge will expire <t:{eta_timestamp}:R>.",
                view=challenge_view
            )
            msg = await interaction.original_response()

            challenge_view.message = msg

            self.active_players.add(challenger.id)
            self.active_players.add(opponent.id)

            self.challenges[msg.id] = {
                "challenger": challenger,
                "opponent": opponent,
                "accepted": False,
                "message": msg,
            }

            async def challenge_timeout():
                await asyncio.sleep(31)
                if msg.id in self.challenges and not self.challenges[msg.id]["accepted"]:
                    try:
                        await msg.delete()
                    except discord.NotFound:
                        pass
                    self.active_players.discard(challenger.id)
                    self.active_players.discard(opponent.id)
                    del self.challenges[msg.id]

            asyncio.create_task(challenge_timeout())
        except Exception as e:
            print(f"Error starting RockPaperScissors challenge: {e}")

async def setup(bot):
    await bot.add_cog(RockPaperScissorsCog(bot))
