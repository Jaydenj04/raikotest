import discord
from discord.ext import commands
from discord.ui import Button, View
import asyncio
import random
from PIL import Image, ImageDraw, ImageFont
import io

MONOPOLY_STARTING_MONEY = 1500000
EMOJI_TOKENS = ['🐶', '👠', '🎩', '🚗']
HOUSE = '🏠'
HOTEL = '🏨'

# All 40 Monopoly board tiles
PROPERTY_TILES = [
    {"name": "GO", "type": "go"},
    {"name": "Mediterranean Avenue", "type": "property", "color": "brown", "price": 60_000, "rent": 2_000},
    {"name": "Community Chest", "type": "chest"},
    {"name": "Baltic Avenue", "type": "property", "color": "brown", "price": 60_000, "rent": 4_000},
    {"name": "Income Tax", "type": "tax", "amount": 200_000},
    {"name": "Reading Railroad", "type": "railroad", "price": 200_000, "rent": 25_000},
    {"name": "Oriental Avenue", "type": "property", "color": "lightblue", "price": 100_000, "rent": 6_000},
    {"name": "Chance", "type": "chance"},
    {"name": "Vermont Avenue", "type": "property", "color": "lightblue", "price": 100_000, "rent": 6_000},
    {"name": "Connecticut Avenue", "type": "property", "color": "lightblue", "price": 120_000, "rent": 8_000},
    {"name": "Jail", "type": "jail"},
    {"name": "St. Charles Place", "type": "property", "color": "pink", "price": 140_000, "rent": 10_000},
    {"name": "Electric Company", "type": "utility", "price": 150_000},
    {"name": "States Avenue", "type": "property", "color": "pink", "price": 140_000, "rent": 10_000},
    {"name": "Virginia Avenue", "type": "property", "color": "pink", "price": 160_000, "rent": 12_000},
    {"name": "Pennsylvania Railroad", "type": "railroad", "price": 200_000, "rent": 25_000},
    {"name": "St. James Place", "type": "property", "color": "orange", "price": 180_000, "rent": 14_000},
    {"name": "Community Chest", "type": "chest"},
    {"name": "Tennessee Avenue", "type": "property", "color": "orange", "price": 180_000, "rent": 14_000},
    {"name": "New York Avenue", "type": "property", "color": "orange", "price": 200_000, "rent": 16_000},
    {"name": "Free Parking", "type": "freeparking"},
    {"name": "Kentucky Avenue", "type": "property", "color": "red", "price": 220_000, "rent": 18_000},
    {"name": "Chance", "type": "chance"},
    {"name": "Indiana Avenue", "type": "property", "color": "red", "price": 220_000, "rent": 18_000},
    {"name": "Illinois Avenue", "type": "property", "color": "red", "price": 240_000, "rent": 20_000},
    {"name": "B&O Railroad", "type": "railroad", "price": 200_000, "rent": 25_000},
    {"name": "Atlantic Avenue", "type": "property", "color": "yellow", "price": 260_000, "rent": 22_000},
    {"name": "Ventnor Avenue", "type": "property", "color": "yellow", "price": 260_000, "rent": 22_000},
    {"name": "Water Works", "type": "utility", "price": 150_000},
    {"name": "Marvin Gardens", "type": "property", "color": "yellow", "price": 280_000, "rent": 24_000},
    {"name": "Go To Jail", "type": "gotojail"},
    {"name": "Pacific Avenue", "type": "property", "color": "green", "price": 300_000, "rent": 26_000},
    {"name": "North Carolina Avenue", "type": "property", "color": "green", "price": 300_000, "rent": 26_000},
    {"name": "Community Chest", "type": "chest"},
    {"name": "Pennsylvania Avenue", "type": "property", "color": "green", "price": 320_000, "rent": 28_000},
    {"name": "Short Line", "type": "railroad", "price": 200_000, "rent": 25_000},
    {"name": "Chance", "type": "chance"},
    {"name": "Park Place", "type": "property", "color": "darkblue", "price": 350_000, "rent": 35_000},
    {"name": "Luxury Tax", "type": "tax", "amount": 100_000},
    {"name": "Boardwalk", "type": "property", "color": "darkblue", "price": 400_000, "rent": 50_000},
]

TOTAL_TILES = len(PROPERTY_TILES)

class MonopolyPlayer:
    def __init__(self, user, token):
        self.user = user
        self.token = token
        self.money = MONOPOLY_STARTING_MONEY
        self.position = 0
        self.properties = []
        self.in_jail = False
        self.jail_turns = 0
        self.houses = {}  # tile index → house count
        self.hotels = {}  # tile index → True if hotel
        self.eliminated = False
      
class MonopolyGame:
    def __init__(self, ctx):
        self.ctx = ctx
        self.players = []
        self.current_index = 0
        self.started = False
        self.board = PROPERTY_TILES.copy()
        self.game_message = None
        self.turn_message = None
        self.skip_votes = set()
        self.double_rolls = 0  # for jail logic
        self.free_parking_pool = 0

    def add_player(self, user, token):
        if self.started or any(p.user.id == user.id for p in self.players):
            return False
        self.players.append(MonopolyPlayer(user, token))
        return True

    def get_player(self, user):
        return next((p for p in self.players if p.user.id == user.id), None)

    def current_player(self):
        return self.players[self.current_index]

    def advance_turn(self):
        # Move to next non-eliminated player
        for _ in range(len(self.players)):
            self.current_index = (self.current_index + 1) % len(self.players)
            if not self.players[self.current_index].eliminated:
                break

    def move_player(self, player, steps):
        passed_go = False
        for _ in range(steps):
            player.position = (player.position + 1) % TOTAL_TILES
            if player.position == 0:
                passed_go = True
        if passed_go:
            player.money += 200_000
            return "💰 Passed GO! Collected 200,000 $"
        return None

    def property_owner(self, tile_index):
        for p in self.players:
            if tile_index in p.properties:
                return p
        return None

    def check_complete_color_set(self, player, color):
        props = [i for i, tile in enumerate(PROPERTY_TILES) if tile.get("color") == color]
        return all(i in player.properties for i in props)

    def is_game_over(self):
        alive = [p for p in self.players if not p.eliminated]
        return len(alive) == 1

      async def handle_turn(self, interaction: discord.Interaction):
        if self.players[self.current_turn].eliminated:
            await self.advance_turn(interaction)
            return

        player = self.players[self.current_turn]
        embed = discord.Embed(title=f"🎲 {player.user.display_name}'s Turn", color=discord.Color.gold())
        embed.add_field(name="Balance", value=f"${player.money:,}", inline=False)
        embed.add_field(name="Location", value=f"{PROPERTY_TILES[player.position]['name']} (Tile {player.position})", inline=False)

        view = RollOrSkipView(self)
        self.message = await interaction.channel.send(embed=embed, view=view)

    async def advance_turn(self, interaction: discord.Interaction):
        self.current_turn = (self.current_turn + 1) % len(self.players)
        await self.handle_turn(interaction)

    async def move_player(self, player, steps):
        old_position = player.position
        player.position = (player.position + steps) % TOTAL_TILES

        if player.position < old_position:
            player.money += 200_000  # Passed GO
            await self.channel.send(f"💰 {player.user.mention} passed **GO** and collected $200,000!")

        current_tile = PROPERTY_TILES[player.position]
        await self.channel.send(f"🚩 {player.user.mention} landed on **{current_tile['name']}**")

        await self.handle_tile_action(player, current_tile)

    async def handle_tile_action(self, player, tile):
        if tile["type"] == "property":
            await self.offer_property(player, tile)
        elif tile["type"] == "tax":
            player.money -= tile["amount"]
            await self.channel.send(f"💸 {player.user.mention} paid a tax of ${tile['amount']:,}.")
        elif tile["type"] == "gotojail":
            await self.send_to_jail(player)
        elif tile["type"] == "chest":
            await self.channel.send(f"📦 {player.user.mention} drew a Community Chest card. (Effect TBD)")
        elif tile["type"] == "chance":
            await self.channel.send(f"🎲 {player.user.mention} drew a Chance card. (Effect TBD)")
        elif tile["type"] == "freeparking":
            await self.channel.send(f"🅿️ {player.user.mention} is taking a break at Free Parking.")
        elif tile["type"] == "jail":
            await self.channel.send(f"🚔 {player.user.mention} is just visiting Jail.")
        elif tile["type"] == "utility" or tile["type"] == "railroad":
            await self.offer_property(player, tile)

    async def offer_property(self, player, tile):
        owner = self.ownership.get(tile["name"])
        if owner is None:
            view = BuyPropertyView(self, player, tile)
            await self.channel.send(
                f"💰 {player.user.mention}, do you want to buy **{tile['name']}** for ${tile['price']:,}?",
                view=view
            )
        elif owner != player.user.id:
            rent = tile.get("rent", 25_000)
            player.money -= rent
            for p in self.players:
                if p.user.id == owner:
                    p.money += rent
                    break
            await self.channel.send(
                f"🏠 {player.user.mention} paid ${rent:,} rent to <@{owner}> for landing on **{tile['name']}**."
            )

class MonopolyView(discord.ui.View):
    def __init__(self, game, player):
        super().__init__(timeout=60)
        self.game = game
        self.player = player

        self.add_item(RollButton(game, player))
        self.add_item(SkipButton(game, player))
        self.add_item(BuyButton(game, player))

class RollButton(discord.ui.Button):
    def __init__(self, game, player):
        super().__init__(label="🎲 Roll", style=discord.ButtonStyle.primary)
        self.game = game
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("❌ It's not your turn.", ephemeral=True)

        if self.game.rolled_this_turn:
            return await interaction.response.send_message("🎲 You already rolled this turn.", ephemeral=True)

        steps = random.randint(1, 12)
        await self.game.move_player(self.player, steps)
        self.game.rolled_this_turn = True
        await interaction.response.send_message(f"🎲 {self.player.user.mention} rolled a **{steps}**!")

        current_tile = PROPERTY_TILES[self.player.position]
        await self.game.handle_tile_action(self.player, current_tile)

class SkipButton(discord.ui.Button):
    def __init__(self, game, player):
        super().__init__(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
        self.game = game
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("❌ Only the current player can skip.", ephemeral=True)

        self.game.rolled_this_turn = False
        await interaction.response.send_message(f"⏭️ {self.player.user.mention} skipped their turn.")
        await self.game.advance_turn(interaction)

class BuyButton(discord.ui.Button):
    def __init__(self, game, player):
        super().__init__(label="💰 Buy", style=discord.ButtonStyle.success)
        self.game = game
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("❌ Only the current player can buy.", ephemeral=True)

        current_tile = PROPERTY_TILES[self.player.position]
        if current_tile["type"] != "property":
            return await interaction.response.send_message("❗ This tile can't be purchased.", ephemeral=True)

        if self.player.money < current_tile["price"]:
            return await interaction.response.send_message("💸 You don't have enough money.", ephemeral=True)

        self.player.money -= current_tile["price"]
        self.player.properties.append(self.player.position)
        await interaction.response.send_message(
            f"✅ {self.player.user.mention} bought **{current_tile['name']}** for {current_tile['price']:,}!"
        )
        await self.game.advance_turn(interaction)


monopoly_games = {1400971551351898216}

def register_monopoly_commands(bot):

    @bot.command(name="mjoin")
    async def mjoin(ctx):
        game = monopoly_games.get(ctx.channel.id)

        if game is None:
            game = MonopolyGame(ctx)
            monopoly_games[ctx.channel.id] = game

        if game.started:
            return await ctx.send("🎲 The game has already started.")

        if any(p.user.id == ctx.author.id for p in game.players):
            return await ctx.send("❗ You already joined.")

        if len(game.players) >= len(EMOJI_TOKENS):
            return await ctx.send("🚫 Maximum players reached.")

        token = EMOJI_TOKENS[len(game.players)]
        player = MonopolyPlayer(ctx.author, token)
        game.players.append(player)

        await ctx.send(f"✅ {ctx.author.mention} joined the Monopoly game as {token}!")

    @bot.command(name="mstart")
    async def mstart(ctx):
        game = monopoly_games.get(ctx.channel.id)
        if not game:
            return await ctx.send("❗ No players have joined yet.")

        if game.started:
            return await ctx.send("🚫 The game has already started.")

        if len(game.players) < 2:
            return await ctx.send("⚠️ At least 2 players are required.")

        await game.start_game()
        await game.send_board()
        await game.send_turn_prompt()

    @bot.command(name="mbal", aliases=["mbalance"])
    async def mbal(ctx):
        game = monopoly_games.get(ctx.channel.id)
        if not game:
            return await ctx.send("❗ No Monopoly game running in this channel.")

        player = game.get_player(ctx.author)
        if not player:
            return await ctx.send("❗ You're not part of this game.")

        await ctx.send(f"💰 {ctx.author.mention}, you have **${player.money:,}**.")

    @bot.command(name="mstatus")
    async def mstatus(ctx):
        game = monopoly_games.get(ctx.channel.id)
        if not game:
            return await ctx.send("❗ No Monopoly game running.")

        lines = []
        for p in game.players:
            lines.append(
                f"{p.token} {p.user.mention} - ${p.money:,} - 🧭 Position: **{PROPERTY_TILES[p.position]['name']}**"
            )
        await ctx.send("📊 **Game Status:**\n" + "\n".join(lines))

    @bot.command(name="mreset")
    async def mreset(ctx):
        monopoly_games.pop(ctx.channel.id, None)
        await ctx.send("🔁 Monopoly game reset in this channel.")

# === Views and Buttons ===

class RollView(discord.ui.View):
    def __init__(self, game, player):
        super().__init__(timeout=30)
        self.game = game
        self.player = player

    @discord.ui.button(label="🎲 Roll Dice", style=discord.ButtonStyle.green)
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("⛔ It's not your turn!", ephemeral=True)

        await interaction.response.defer()
        await self.game.roll_and_move(self.player)

class BuyPropertyView(discord.ui.View):
    def __init__(self, game, player, tile_index):
        super().__init__(timeout=30)
        self.game = game
        self.player = player
        self.tile_index = tile_index

    @discord.ui.button(label="💰 Buy", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("⛔ You're not the current player!", ephemeral=True)

        success = await self.game.buy_property(self.player, self.tile_index)
        if success:
            await interaction.response.send_message(f"✅ You bought **{PROPERTY_TILES[self.tile_index]['name']}**!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Couldn't buy the property.", ephemeral=True)

        await self.game.next_turn()

    @discord.ui.button(label="❌ Skip", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.player.user:
            return await interaction.response.send_message("⛔ You're not the current player!", ephemeral=True)

        await interaction.response.send_message("⏭️ You skipped the property.", ephemeral=True)
        await self.game.next_turn()

# === Global State ===
active_monopoly_games = {}

# === Command to start a Monopoly game ===
@bot.command(name="mstart")
async def monopoly_start(ctx):
    if ctx.channel.id in active_monopoly_games:
        return await ctx.send("⚠️ A Monopoly game is already ongoing!")

monopoly_games = {}

class JoinMonopolyView(View):
    def __init__(self, ctx, host):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.host = host
        self.joined = [host]

    @discord.ui.button(label="Join", style=discord.ButtonStyle.green)
    async def join(self, interaction: discord.Interaction, button: Button):
        user = interaction.user
        if user in self.joined:
            await interaction.response.send_message("❗ You already joined!", ephemeral=True)
            return
        if len(self.joined) >= 4:
            await interaction.response.send_message("❗ Max 4 players already joined.", ephemeral=True)
            return
        self.joined.append(user)
        await interaction.response.send_message(f"✅ {user.mention} joined the game!", ephemeral=False)

    async def on_timeout(self):
        if len(self.joined) < 2:
            await self.ctx.send("❗ Not enough players joined. Game canceled.")
            return

        players = []
        for i, user in enumerate(self.joined):
            token = EMOJI_TOKENS[i]
            players.append(MonopolyPlayer(user, token))

        game = MonopolyGame(self.ctx, players)
        monopoly_games[self.ctx.channel.id] = game
        await game.start_turn()

@bot.command(name="mstart")
async def mstart(ctx):
    if ctx.channel.id in monopoly_games:
        return await ctx.send("⚠️ A Monopoly game is already running in this channel.")

    view = JoinMonopolyView(ctx, ctx.author)
    await ctx.send(f"🎲 Monopoly game starting! {ctx.author.mention} is the host.\nClick to join (30s)...", view=view)

@bot.command(name="mroll")
async def mroll(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No game running in this channel.")
    await game.roll_dice(ctx.author)

@bot.command(name="mskip")
async def mskip(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No game running in this channel.")
    await game.skip_turn(ctx.author)

@bot.command(name="mbuy")
async def mbuy(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No game running in this channel.")
    await game.buy_property(ctx.author)

@bot.command(name="mbal")
async def mbal(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No game running in this channel.")
    await game.show_balance(ctx.author)
@bot.command(name="mupgrade")
async def mupgrade(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No Monopoly game running here.")
    player = game.get_player(ctx.author)
    if not player or player != game.players[game.turn_index]:
        return await ctx.send("❗ It's not your turn or you're not in the game.")

    tile = PROPERTY_TILES[player.position]
    if tile["type"] != "property":
        return await ctx.send("❗ You’re not on an upgradable property.")
    if tile["name"] not in player.properties:
        return await ctx.send("❗ You don’t own this property.")
    
    # Must own all of same color
    group = [t for t in PROPERTY_TILES if t.get("color") == tile.get("color")]
    group_names = [t["name"] for t in group]
    if not all(name in player.properties for name in group_names):
        return await ctx.send("❗ You must own all properties of this color to upgrade.")

    tile_index = player.position
    if player.hotels.get(tile_index):
        return await ctx.send("❗ This property already has a hotel.")

    house_count = player.houses.get(tile_index, 0)
    upgrade_cost = 50_000  # Flat cost per house/hotel

    if house_count < 4:
        if player.money < upgrade_cost:
            return await ctx.send("💸 Not enough money to add a house.")
        player.money -= upgrade_cost
        player.houses[tile_index] = house_count + 1
        await ctx.send(f"🏠 {player.user.mention} added a house to **{tile['name']}**.")
    else:
        # Upgrade to hotel
        if player.money < upgrade_cost:
            return await ctx.send("💸 Not enough money to build hotel.")
        player.money -= upgrade_cost
        player.hotels[tile_index] = True
        del player.houses[tile_index]
        await ctx.send(f"🏨 {player.user.mention} built a hotel on **{tile['name']}**.")

@bot.command(name="mprops")
async def mprops(ctx):
    game = monopoly_games.get(ctx.channel.id)
    if not game:
        return await ctx.send("❗ No Monopoly game running.")
    player = game.get_player(ctx.author)
    if not player:
        return await ctx.send("❗ You’re not in the current game.")

    if not player.properties:
        return await ctx.send("📦 You don’t own any properties.")
    
    prop_list = []
    for name in player.properties:
        i = next((i for i, t in enumerate(PROPERTY_TILES) if t["name"] == name), None)
        if i is not None:
            house = player.houses.get(i, 0)
            hotel = player.hotels.get(i, False)
            upgrades = f"{HOUSE*house}{HOTEL if hotel else ''}"
            prop_list.append(f"• {name} {upgrades}")
    
    props = "\n".join(prop_list)
    await ctx.send(f"🏘️ {ctx.author.mention} owns:\n{props}")

def handle_rent(game, player, tile, tile_index):
    for p in game.players:
        if p == player:
            continue
        if tile["name"] in p.properties:
            rent = tile["rent"]
            if p.hotels.get(tile_index):
                rent *= 5
            elif p.houses.get(tile_index):
                rent *= 1 + p.houses[tile_index]
            player.money -= rent
            p.money += rent
            return f"💰 {player.user.mention} paid {rent:,} to {p.user.mention} for landing on **{tile['name']}**."

    return None

def check_elimination(game):
    for p in game.players:
        if not p.eliminated and p.money < -100_000:
            p.eliminated = True
            game.ctx.send(f"☠️ {p.user.mention} was eliminated due to bankruptcy!")

    remaining = [p for p in game.players if not p.eliminated]
    if len(remaining) == 1:
        winner = remaining[0]
        game.ctx.send(f"🏆 {winner.user.mention} wins Monopoly with ${winner.money:,}!")
        del monopoly_games[game.ctx.channel.id]

