
# ============================
# Raiko Discord Bot - MongoDB Version
# ============================



import threading
import discord
from discord import SelectOption
from discord.ext import commands, tasks
from itertools import cycle
from discord.ui import Button, View, Select
import random
from random import randint, choice
import asyncio
import json
import pytz
import string
import difflib
from difflib import get_close_matches
import html
import os
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta
from discord.ext.commands import MissingRequiredArgument
from motor.motor_asyncio import AsyncIOMotorClient
import sys
import traceback

CREATOR_IDS = [955882470690140200, 521399748687691810]

# ----------- BOT SETUP -----------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="-", intents=intents, case_insensitive=True)
bot.remove_command('help')

LOTTERY_TICKET_PRICE = 50000
LOTTERY_MAX_TICKETS = 5
LOTTERY_BASE_PRIZE = 50000
LOTTERY_BONUS_PER_TICKET = 25000
LOTTERY_CHANNEL_ID = 1398586067967414323


# ----------- MONGODB SETUP -----------
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URL")
client = AsyncIOMotorClient(MONGO_URI)

db = client["raiko"] 
users = db["users_test"]
bot_settings = db["bot_settingstest"]

# Block disabled commands
@bot.check
async def global_command_block(ctx):
    settings = await bot_settings.find_one({"_id": "disabled_commands"}) or {}
    disabled = settings.get("commands", [])
    return ctx.command.name not in disabled
    
async def test_mongodb():
    try:
        test_doc = await users.find_one()
        print("✅ MongoDB is connected and accessible.")
    except Exception as e:
        print(f"❌ MongoDB connection failed: {e}")


@bot.command()
async def clearbanks(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only bot creators can use this command.")

    result = await users.update_many(
        {"bank": {"$gt": 0}},  # Only users with something in bank
        [
            {
                "$set": {
                    "wallet": {"$add": ["$wallet", "$bank"]},
                    "bank": 0
                }
            }
        ]
    )

    await ctx.send(f"✅ Cleared bank balances for **{result.modified_count}** users.")


# ----------- USER UTILS -----------

async def ensure_user(user_id):
    user_id = str(user_id)
    user = await users.find_one({"_id": user_id})
    if not user:
        user = {
            "_id": user_id,
            "wallet": 0,
            "bank": 0,
            "notes": [],
            "stats": {
                "wins": 0,
                "losses": 0,
                "ttt": {"wins": 0, "losses": 0},
                "rps": {"wins": 0, "losses": 0},
                "blackjack": {"wins": 0, "losses": 0},
                "hangman": {"wins": 0, "losses": 0},
                "connect4": {"wins": 0, "losses": 0}
            },
            "cooldowns": {}
        }
        await users.insert_one(user)
    return user

async def get_user(user_id):
    return await users.find_one({"_id": str(user_id)})

async def update_user(user_id, updates):
    await users.update_one({"_id": str(user_id)}, {"$set": updates})

async def increment_user(user_id, field_path, amount):
    await users.update_one({"_id": str(user_id)}, {"$inc": {field_path: amount}})

# ----------- COOLDOWN UTILITY -----------

async def is_on_cooldown(user_id, command_name, cooldown_seconds):
    user = await users.find_one({"_id": str(user_id)})
    if not user:
        return False, 0

    cooldowns = user.get("cooldowns", {})
    last_used_str = cooldowns.get(command_name)

    if not last_used_str:
        return False, 0

    try:
        last_used = datetime.fromisoformat(last_used_str)
        now = datetime.utcnow()
        elapsed = (now - last_used).total_seconds()
        remaining = int(cooldown_seconds - elapsed)
        if remaining > 0:
            return True, remaining
    except Exception:
        pass

    return False, 0


# =================== SHOP =====================================

SHOP_ITEMS = {
    "🧲 Lucky Magnet": {"price": 30000, "description": "Boosts treasure odds."},
    "🎯 Target Scope": {"price": 50000, "description": "Dig 2 tiles in treasure hunt."},
    "💼 Bread Vault": {"price": 5000, "description": "Blocks 1 successful rob."},
    "🎲 Dice of Fortune": {"price": 25000, "description": "Random bread (1–50000)."},
    "💣 Fake Bomb": {"price": 20000, "description": "Cancels someone else's treasure hunt prize."},
    "🔑 Skeleton Key": {"price": 8000, "description": "50% to reset all cooldowns, 50% to break."},
    "🎟️ Lottery Ticket": {"price": 50000, "description": "Reserved for future lottery."},
    "🧱 Brick Wall": {"price": 7000, "description": "Blocks one trap."},
    "💪️ Wrench": {"price": 5000, "description": "Repairs one broken Skeleton Key."},
    "📦 Mystery Crate": {"price": 25000, "description": "Random item reward."},
    "📜 Contract": {"price": 30000, "description": "Steals random item from another user."},
    "🫓 Bread Juice": {"price": 4000, "description": "+500 🥖 and doubles next work payout."},
    "🔫 Gun": {"price": 8500, "description": "Next ;rob is 100% successful and steals double the bread😈 ARMED ROBBERY!"}
}

@bot.command()
async def shop(ctx):
    embed = discord.Embed(title="🏪 Shop", color=discord.Color.gold())
    for item, info in SHOP_ITEMS.items():
        embed.add_field(name=f"{item} - {info['price']} 🥖", value=info["description"], inline=False)
    await ctx.send(embed=embed)

# Buy command with confirmation
@bot.command()
async def buy(ctx, *, item_name):
    def normalize(name):
        return ''.join(c for c in name.lower() if c.isalnum())

    normalized_input = normalize(item_name)
    item_key = next((k for k in SHOP_ITEMS if normalize(k) == normalized_input), None)

    if not item_key:
        return await ctx.send("❌ That item doesn't exist.")

    item = SHOP_ITEMS[item_key]
    price = item["price"]
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})

    if not user or user.get("wallet", 0) < price:
        return await ctx.send("🚫 You don't have enough 🥖.")

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=15)

        @discord.ui.button(label="Confirm Purchase", style=discord.ButtonStyle.success)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            await users.update_one(
                {"_id": user_id},
                {"$inc": {"wallet": -price, f"inventory.{item_key}": 1}},
                upsert=True
            )
            await interaction.response.edit_message(content=f"✅ Purchased {item_key} for {price} 🥖!", view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            await interaction.response.edit_message(content="❌ Purchase cancelled.", view=None)

    await ctx.send(f"Are you sure you want to buy {item_key} for {price} 🥖?", view=ConfirmView())

# Inventory aliases
@bot.command(aliases=["inv", "items"])
async def inventory(ctx):
    user = await users.find_one({"_id": str(ctx.author.id)})
    inventory = user.get("inventory", {}) if user else {}
    if not inventory:
        return await ctx.send("📅 Your inventory is empty.")

    embed = discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", color=discord.Color.green())
    for item, count in inventory.items():
        embed.add_field(name=item, value=f"Quantity: {count}", inline=True)
    await ctx.send(embed=embed)

class ConfirmBuy(View):
    def __init__(self, item, user, ctx, price):
        super().__init__(timeout=15)
        self.item = item
        self.user = user
        self.ctx = ctx
        self.price = price

        self.add_item(Button(label="Confirm", style=discord.ButtonStyle.green, custom_id="confirm"))
        self.add_item(Button(label="Cancel", style=discord.ButtonStyle.red, custom_id="cancel"))

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, custom_id="confirm")
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
        await users.update_one(
            {"_id": str(self.user.id)},
            {"$inc": {"wallet": -self.price, f"inventory.{self.item}": 1}},
            upsert=True
        )
        await interaction.response.edit_message(content=f"✅ You bought **{self.item}** for {self.price} 🥖!", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="cancel")
    async def cancel_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
        await interaction.response.edit_message(content="❌ Purchase cancelled.", view=None)
        self.stop()

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def shop(self, ctx):
        embed = discord.Embed(title="🏍️ Shop Items", color=discord.Color.gold())
        for item, data in SHOP_ITEMS.items():
            embed.add_field(name=f"{item} - {data['price']} 🥖", value=data['description'], inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def buy(self, ctx, *, item_name):
        item_name = item_name.strip()
        match = None
        for item in SHOP_ITEMS:
            if item.lower() == item_name.lower():
                match = item
                break

        if not match:
            return await ctx.send("❌ Item not found in shop.")

        price = SHOP_ITEMS[match]["price"]
        user_id = str(ctx.author.id)
        user = await users.find_one({"_id": user_id})
        if not user or user.get("wallet", 0) < price:
            return await ctx.send("🚫 You don't have enough 🥖.")

        view = ConfirmBuy(match, ctx.author, ctx, price)
        await ctx.send(f"Are you sure you want to buy **{match}** for {price} 🥖?", view=view)



@bot.command(aliases=["useitem"])
async def use(ctx, *, item_name: str):
    """Use an item from your inventory (fuzzy matched)."""
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})

    if not user or "inventory" not in user or not user["inventory"]:
        return await ctx.send("❌ You don’t have that item.")

    inventory = user["inventory"]
    all_items = list(inventory.keys())

    # Normalize function (removes emoji, spaces, lowercase)
    def normalize(s):
        return ''.join(c.lower() for c in s if c.isalnum())

    # Try to match fuzzy input
    input_normalized = normalize(item_name)
    matched_key = None
    for key in all_items:
        if normalize(key) == input_normalized or input_normalized in normalize(key):
            matched_key = key
            break

    # Validate matched item
    if not matched_key or inventory.get(matched_key, 0) < 1:
        return await ctx.send("❌ You don’t have that item.")

    # === ITEM USAGE LOGIC ===
    if matched_key == "🎯 Target Scope":
        await users.update_one(
            {"_id": user_id},
            {
                "$inc": {f"inventory.{matched_key}": -1},
                "$set": {"active_buffs.target_scope": True}
            }
        )
        return await ctx.send("🎯 Target Scope activated! Your next Treasure Hunt will allow 2 digs.")

    # Fallback for unsupported items
    return await ctx.send("❌ That item cannot be used yet.")


    # === COOLDOWN HANDLING ===
    cooldown_items = ["🧲 Lucky Magnet", "🎯 Target Scope", "💣 Fake Bomb", "🔑 Skeleton Key", "📜 Contract", "🔫 Gun"]
    now = datetime.utcnow()
    cooldowns = user.get("cooldowns", {}).get("item_usage", {})

    last_used = cooldowns.get(matched_key)
    if matched_key in cooldown_items and last_used:
        if isinstance(last_used, str):
            try:
                last_used = datetime.fromisoformat(last_used)
            except Exception:
                last_used = now  # fallback
        delta = now - last_used
        if delta < timedelta(hours=24):
            remaining = timedelta(hours=24) - delta
            hours, rem = divmod(remaining.seconds, 3600)
            minutes = rem // 60
            return await ctx.send(f"⏳ You must wait {hours}h {minutes}m before using {matched_key} again.")

    # === ITEM EFFECTS ===
    msg = ""
    if matched_key == "🎲 Dice of Fortune":
        amount = random.randint(1, 50000)
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": amount}})
        msg = f"You rolled the dice and won **{amount} 🥖**!"

    elif matched_key == "🧃 Bread Juice":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.bread_juice": True}})
        msg = "You drank Bread Juice! Next `;work` will give **double 🥖**."

    elif matched_key == "🔫 Gun":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.gun": True}})
        msg = "🔫 You loaded your gun. Your next `;rob` is guaranteed to succeed and double the reward."

    elif matched_key == "💼 Bread Vault":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.vault": True}})
        msg = "💼 You activated your Bread Vault. It will block 1 successful rob attempt."

    elif matched_key == "🎯 Target Scope":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.scope": True}})
        msg = "🎯 You can dig 2 tiles in the next `;treasurehunt`!"

    elif matched_key == "🧲 Lucky Magnet":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.magnet": True}})
        msg = "🧲 You feel luckier... Treasure odds improved next game."

    elif matched_key == "🔑 Skeleton Key":
        if random.random() < 0.5:
            await users.update_one({"_id": user_id}, {"$unset": {"cooldowns": ""}})
            msg = "🔑 Your Skeleton Key reset **all cooldowns**!"
        else:
            msg = "💀 Your Skeleton Key broke into pieces... nothing happened."

    elif matched_key == "🛠️ Wrench":
        await users.update_one({"_id": user_id}, {"$unset": {"buffs.broken_key": ""}})
        msg = "🛠️ You repaired your broken Skeleton Key!"

    elif matched_key == "📦 Mystery Crate":
        prize = random.choice(list(SHOP_ITEMS.keys()))
        await users.update_one({"_id": user_id}, {"$inc": {f"inventory.{prize}": 1}})
        msg = f"📦 You opened the Mystery Crate and found **{prize}**!"

    elif matched_key == "📜 Contract":
        others = await users.find({"_id": {"$ne": user_id}, "inventory": {"$exists": True}}).to_list(100)
        targets = [u for u in others if u.get("inventory")]
        if not targets:
            msg = "📜 No valid targets to steal from."
        else:
            victim = random.choice(targets)
            victim_id = victim["_id"]
            inv = victim["inventory"]
            if inv:
                stolen = random.choice(list(inv.keys()))
                await users.update_one({"_id": user_id}, {"$inc": {f"inventory.{stolen}": 1}})
                await users.update_one({"_id": victim_id}, {"$inc": {f"inventory.{stolen}": -1}})
                msg = f"📜 You stole **{stolen}** from <@{victim_id}>!"

    elif matched_key == "💣 Fake Bomb":
        msg = "💣 You’re now ready to cancel someone else's treasure reward. Use it wisely (feature not yet active)."

    elif matched_key == "🎟️ Lottery Ticket":
        msg = "🎟️ You now own a Lottery Ticket. Stay tuned for the next jackpot draw!"

    elif matched_key == "🧱 Brick Wall":
        await users.update_one({"_id": user_id}, {"$set": {"buffs.brick": True}})
        msg = "🧱 You set up a Brick Wall to block the next trap."

    else:
        return await ctx.send("❌ That item cannot be used yet.")

    # Final update: remove item + set cooldown
    await users.update_one({"_id": user_id}, {
        "$inc": {f"inventory.{matched_key}": -1},
        "$set": {f"cooldowns.item_usage.{matched_key}": now}
    })

    await ctx.send(f"✅ {msg}")

async def setup(bot):
    await bot.add_cog(Shop(bot))

# =================================================================

# Load & shuffle the full question set once at startup
with open("trivia_questions.json", "r") as f:
    ALL_TRIVIA_QUESTIONS = json.load(f)
random.shuffle(ALL_TRIVIA_QUESTIONS)

# Questions draw-pile
unused_trivia_questions = ALL_TRIVIA_QUESTIONS.copy()

def global_except_hook(exc_type, exc_value, exc_traceback):
    print("❌ Uncaught exception:", file=sys.stderr)
    traceback.print_exception(exc_type, exc_value, exc_traceback)

sys.excepthook = global_except_hook

print("⚡ After global exception hook")

# =====================================
# =========== SOLO GAMBLING ===========
# =====================================

@bot.command(aliases=["cf"])
async def coinflip(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;coinflip <bet>`")

    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")

    class CoinFlipView(View):
        def __init__(self):
            super().__init__(timeout=15)
            self.value = None

        @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.green)
        async def heads(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your game.", ephemeral=True)
            self.value = "Heads"
            self.stop()

        @discord.ui.button(label="🔁 Tails", style=discord.ButtonStyle.blurple)
        async def tails(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your game.", ephemeral=True)
            self.value = "Tails"
            self.stop()

    view = CoinFlipView()
    await ctx.send(f"🎲 **Coin Flip!** Pick Heads or Tails for **{bet} 🥖**!", view=view)
    await view.wait()

    if not view.value:
        return await ctx.send("⏳ Timed out.")

    outcome = choice(["Heads", "Tails"])
    await ctx.send(f"🌀 Flipping... 🎲")
    await asyncio.sleep(2)
    if view.value == outcome:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": bet}})
        await ctx.send(f"🎉 It's **{outcome}**! You won **+{bet} 🥖**!")
    else:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})
        await ctx.send(f"💀 It's **{outcome}**. You lost **-{bet} 🥖**.")

@bot.command()
async def slot(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;slot <bet>`")

    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")

    symbols = ["🍒", "🍋", "🔔", "⭐", "💎"]
    await ctx.send("🎰 Spinning...")
    await asyncio.sleep(2)
    reel = [choice(symbols) for _ in range(3)]
    result = " | ".join(reel)
    await ctx.send(f"🎰 {result}")

    if reel[0] == reel[1] == reel[2]:
        win = bet * 5
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": win}})
        await ctx.send(f"🎉 Jackpot! You won **+{win} 🥖**!")
    elif reel[0] == reel[1] or reel[1] == reel[2]:
        win = int(bet * 1.5)
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": win}})
        await ctx.send(f"✨ Partial match! You won **+{win} 🥖**!")
    else:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})
        await ctx.send(f"💀 No match. You lost **-{bet} 🥖**.")

@bot.command()
async def dice(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;dice <bet>`")

    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")

    await ctx.send("🎲 Rolling dice...")
    await asyncio.sleep(2)
    user_roll = randint(1, 6)
    bot_roll = randint(1, 6)
    await ctx.send(f"🧍 You rolled **{user_roll}**\n🤖 Bot rolled **{bot_roll}**")

    if user_roll > bot_roll:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": bet}})
        await ctx.send(f"🎉 You win! +{bet} 🥖")
    elif user_roll < bot_roll:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})
        await ctx.send(f"💀 You lose! -{bet} 🥖")
    else:
        await ctx.send("😐 It's a tie! Your bet is returned.")

@bot.command()
async def roulette(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;roulette <bet>`")

    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")

    numbers_1 = [discord.SelectOption(label=str(i), value=str(i)) for i in range(0, 19)]
    numbers_2 = [discord.SelectOption(label=str(i), value=str(i)) for i in range(19, 37)] + [discord.SelectOption(label="00", value="00")]
    special_bets = [
        discord.SelectOption(label="🔴 Red", value="red"),
        discord.SelectOption(label="⚫ Black", value="black"),
        discord.SelectOption(label="Even", value="even"),
        discord.SelectOption(label="Odd", value="odd"),
        discord.SelectOption(label="0", value="0"),
        discord.SelectOption(label="00", value="00")
    ]

    class BetDropdown(discord.ui.Select):
        def __init__(self, options, placeholder):
            super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=len(options))

        async def callback(self, interaction: discord.Interaction):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your game.", ephemeral=True)
            self.view.bets.update(self.values)
            await interaction.response.send_message(f"✅ Selected: {', '.join(self.values)}", ephemeral=True)

    class RouletteView(View):
        def __init__(self):
            super().__init__(timeout=30)
            self.bets = set()
            self.add_item(BetDropdown(numbers_1, "Choose 0–18"))
            self.add_item(BetDropdown(numbers_2, "Choose 19–36 / 00"))
            self.add_item(BetDropdown(special_bets, "Choose red/black/odd/even"))

    view = RouletteView()
    await ctx.send(f"🎡 **Roulette** — Select your bets for **{bet} 🥖**", view=view)
    await view.wait()

    if not view.bets:
        return await ctx.send("⏳ Timed out or no bets made.")

    all_slots = [str(i) for i in range(0, 37)] + ["00"]
    spin_result = choice(all_slots)
    color_map = {
        "red": ['1', '3', '5', '7', '9', '12', '14', '16', '18', '19', '21', '23', '25', '27', '30', '32', '34', '36'],
        "black": ['2', '4', '6', '8', '10', '11', '13', '15', '17', '20', '22', '24', '26', '28', '29', '31', '33', '35']
    }

    win = False
    payout = 0

    if spin_result in view.bets:
        payout = 36 * bet  # Direct number match
        win = True
    elif "red" in view.bets and spin_result in color_map["red"]:
        payout += 2 * bet
        win = True
    elif "black" in view.bets and spin_result in color_map["black"]:
        payout += 2 * bet
        win = True
    elif "even" in view.bets and spin_result.isdigit() and int(spin_result) % 2 == 0:
        payout += 2 * bet
        win = True
    elif "odd" in view.bets and spin_result.isdigit() and int(spin_result) % 2 == 1:
        payout += 2 * bet
        win = True

    await ctx.send(f"🌀 Ball spins... lands on **{spin_result}**")
    if win:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": payout}})
        await ctx.send(f"🎉 You win **+{payout} 🥖**!")
    else:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})
        await ctx.send(f"💀 You lost **-{bet} 🥖**.")


# ============================
# COMMAND: LANDMINE (Fixed + Rewards + Streaks)
# ============================

@bot.command(aliases=["lm"])
async def landmine(ctx, bet: int):
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})

    if not user:
        await users.insert_one({"_id": user_id, "wallet": 0, "bank": 0, "stats": {"landmine_streak": 0}})
        user = await users.find_one({"_id": user_id})

    if bet <= 0:
        return await ctx.send("❗ Please enter a bet greater than 0 you moron.")

    if user.get("wallet", 0) < bet:
        return await ctx.send("❌ You don't even have enough bread to bet that, broke ass bitch.")

    wallet = user.get("wallet", 0)
    if wallet < 0:
        await users.update_one({"_id": user_id}, {"$set": {"wallet": 0}})
        wallet = 0

    # Deduct the bet after passing checks
    await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})

    win_streak = user.get("stats", {}).get("landmine_streak", 0)

    # Randomly pick 13 bomb tiles
    bomb_tiles = random.sample(range(25), 13)
    money_tiles = [i for i in range(25) if i not in bomb_tiles]

    class LandmineView(View):
        def __init__(self):
            super().__init__(timeout=30)
            self.clicked = False

            for i in range(25):
                self.add_item(self.TileButton(i, self))

        class TileButton(Button):
            def __init__(self, index, parent):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="🟫", row=index // 5)
                self.index = index
                self.parent = parent

            async def callback(self, interaction):
                if interaction.user != ctx.author:
                    return await interaction.response.send_message("🚫 Only the game starter can play!", ephemeral=True)

                if self.parent.clicked:
                    return await interaction.response.send_message("❗ You've already clicked a tile!", ephemeral=True)

                self.parent.clicked = True
                self.disabled = True

                if self.index in bomb_tiles:
                    self.emoji = "💣"
                    await users.update_one({"_id": user_id}, {
                        "$set": {"stats.landmine_streak": 0}
                    })
                    await interaction.response.edit_message(view=self.view)
                    return await ctx.send("💥 Boom! You hit a landmine and lost your win streak!")
                else:
                    self.emoji = "💰"
                    new_streak = win_streak + 1
                    winnings = bet * 2 * new_streak
                    await users.update_one({"_id": user_id}, {
                        "$inc": {"wallet": winnings},
                        "$set": {"stats.landmine_streak": new_streak}
                    })
                    await interaction.response.edit_message(view=self.view)
                    return await ctx.send(f"💰 You found a money bag! You won **{winnings:,} 🥖**!\n🔥 Current win streak: **{new_streak}**")

    embed = discord.Embed(
        title="💣 Landmine Game",
        description="Click a tile and try your luck! Avoid the bombs to win 🥖!",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed, view=LandmineView())



# ==========================
# ===== UNO GAME DATA ======
# ==========================

import traceback  # Needed for detailed error logging

active_uno_games = []

COLORS = ['🔴', '🟡', '🟢', '🔵']
VALUES = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '⏭️', '🔁', '+2']
WILD_CARDS = ['Wild', '+4']

CARD_EMOJIS = {
    '⏭️': '⏭️ Skip',
    '🔁': '🔁 Reverse',
    '+2': '+2 Draw',
    'Wild': '🌈 Wild',
    '+4': '+4 Draw',
}

def generate_deck():
    try:
        deck = [f"{color} {val}" for color in COLORS for val in VALUES * 2] + WILD_CARDS * 4
        print(f"[DEBUG] Generated deck with {len(deck)} cards.")
        return deck
    except Exception as e:
        print("[ERROR] Failed to generate deck.")
        traceback.print_exc()
        return []

def card_color(card):
    try:
        return card.split()[0] if card not in WILD_CARDS else None
    except Exception as e:
        print(f"[ERROR] card_color() failed on card: {card}")
        traceback.print_exc()

def card_value(card):
    try:
        return card.split()[-1]
    except Exception as e:
        print(f"[ERROR] card_value() failed on card: {card}")
        traceback.print_exc()

def is_valid_play(card, top_card, draw_stack, color_override=None):
    try:
        top = color_override or top_card

        # Draw stack is active
        if draw_stack:
            # Only allow stacking if the same type of draw card
            if '+2' in card and '+2' in top:
                return True
            if '+4' in card and '+4' in top:
                return True
            return False  # Can't play any other cards during draw stack

        # Always allow wilds
        if card in WILD_CARDS:
            return True

        card_parts = card.split()
        top_parts = top.split()

        if len(card_parts) < 2 or len(top_parts) < 2:
            print(f"[DEBUG] Invalid card format: {card} vs {top}")
            return False

        same_color = card_parts[0] == top_parts[0]
        same_value = card_parts[1] == top_parts[1]

        return same_color or same_value

    except Exception as e:
        print(f"[ERROR] is_valid_play() failed with card={card}, top_card={top_card}, draw_stack={draw_stack}")
        traceback.print_exc()
        return False


class ColorSelectView(discord.ui.View):
    def __init__(self, game, user):
        super().__init__(timeout=30)
        self.game = game
        self.user = user

        for color in ["🔴", "🟢", "🔵", "🟡"]:
            self.add_item(ColorButton(color, game, user))


class ColorButton(discord.ui.Button):
    def __init__(self, color, game, user):
        super().__init__(label=color, style=discord.ButtonStyle.primary)
        self.color = color
        self.game = game
        self.user = user

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("⛔ You're not the one choosing the color.", ephemeral=True)

        try:
            # Apply the chosen color
            self.game.color_override = self.color
            top_value = self.game.pile[-1].split(" ")[-1]  # Should be +4 or Wild
            self.game.pile[-1] = f"{self.color} {top_value}"

            # Turn ends after choosing color
            await interaction.followup.send(f"🎨 {interaction.user.mention} chose **{self.color}**!", ephemeral=False)
            self.game.advance_turn()
            await start_uno_game(interaction.client, self.game)

            # Clean up the color select buttons
            await interaction.message.edit(view=None)

        except Exception:
            print("[UNO ERROR] Color selection failed.")
            traceback.print_exc()
            

class UnoGame:
    def __init__(self, ctx, bet, players):
        try:
            print(f"[DEBUG] Initializing UNO game | Bet: {bet} | Players: {[p.display_name for p in players]}")
            self.discard_pile = []
            self.top_card = None
            self.timer_task = None 
            self.ctx = ctx
            self.bet = bet
            self.ended = False
            self.players = players
            self.deck = generate_deck()
            random.shuffle(self.deck)
            self.hands = {p: [self.deck.pop() for _ in range(7)] for p in players}
            print(f"[DEBUG] Hands dealt: {[len(self.hands[p]) for p in players]}")
            self.pile = [self.deck.pop()]
            self.top_card = self.pile[-1]  # ✅ Set the initial top card
            print(f"[DEBUG] First top card: {self.pile[-1]}")
            while self.pile[-1] in WILD_CARDS or any(x in self.pile[-1] for x in ['⏭️', '+2']):
                print(f"[DEBUG] Top card {self.pile[-1]} not allowed. Replacing...")
                self.deck.insert(0, self.pile.pop())
                self.pile.append(self.deck.pop())
                self.top_card = self.pile[-1]  # ✅ Update top_card again in case it changed
            print(f"[DEBUG] Valid starting top card: {self.pile[-1]}")
            self.current = 0
            self.direction = 1
            self.draw_stack = 0
            self.called_uno = {}
            self.color_override = None
            self.skip_next = False
            self.message = None
            self.draw_flag = False
            active_uno_games.append(self)
            print(f"[DEBUG] UNO game created successfully.")
        except Exception as e:
            print("[ERROR] Failed during UnoGame initialization")
            traceback.print_exc()

    def current_player(self):
        try:
            return self.players[self.current]
        except Exception as e:
            print(f"[ERROR] Failed to get current player at index {self.current}")
            traceback.print_exc()

    def next_player(self):
        try:
            next_index = (self.current + self.direction) % len(self.players)
            return self.players[next_index]
        except Exception as e:
            print(f"[ERROR] Failed to get next player from index {self.current} with direction {self.direction}")
            traceback.print_exc()

    
    def reset_draw_stack(self):
        self.draw_stack = 0


    def advance_turn(self):
        try:
            self.current = (self.current + self.direction) % len(self.players)
            print(f"[DEBUG] Turn advanced | New current: {self.players[self.current].display_name}")
        except Exception as e:
            print("[ERROR] Failed to advance turn")
            traceback.print_exc()

    def remove_game(self):
        try:
            if self in active_uno_games:
                active_uno_games.remove(self)
                print("[DEBUG] UNO game removed from active list.")
        except Exception as e:
            print("[ERROR] Failed to remove game from active list")
            traceback.print_exc()

    async def end_game(self, winner=None, reason=None):
        try:
            print(f"[DEBUG] Ending game | Winner: {winner.display_name if winner else None} | Reason: {reason}")
            if self.timer_task:
                self.timer_task.cancel()

            if winner:
                pool = self.bet * len(self.players)
                await users.update_one({"_id": str(winner.id)}, {"$inc": {"wallet": pool}}, upsert=True)
                await self.message.channel.send(f"🎉 {winner.mention} wins {pool} 🥖! Game Over.")
            elif reason:
                await self.message.channel.send(f"❌ Game ended: {reason}")
            self.remove_game()
        except Exception as e:
            print("[ERROR] Failed to end UNO game")
            traceback.print_exc()

    def apply_card_effect(self, card):
        try:
            print(f"[DEBUG] Applying card effect: {card}")
            if '+2' in card:
                self.draw_stack += 2
                print(f"[DEBUG] +2 card played. New draw stack: {self.draw_stack}")
            elif '+4' in card:
                self.draw_stack += 4
                print(f"[DEBUG] +4 card played. New draw stack: {self.draw_stack}")

            if '🔁' in card:
                if len(self.players) == 2:
                    self.skip_next = True
                    print("[DEBUG] Reverse used as Skip (2 players)")
                else:
                    self.direction *= -1
                    print(f"[DEBUG] Reverse card played. New direction: {self.direction}")

            if '⏭️' in card:
                self.skip_next = True
                print("[DEBUG] Skip card played. Next player will be skipped.")
        except Exception as e:
            print(f"[ERROR] apply_card_effect() failed for card: {card}")
            traceback.print_exc()

# ================================
# === UNO HELPER FUNCTIONS ======
# ================================

def generate_game_embed(game):
    try:
        top_card = game.pile[-1] if game.pile else "🂠"
        embed = discord.Embed(title="🎮 UNO Game", color=discord.Color.blue())
        embed.add_field(name="Top Card", value=top_card, inline=False)
        embed.add_field(name="Current Turn", value=game.current_player().mention, inline=True)

        next_index = (game.current + game.direction) % len(game.players)  # ✅ Use .current
        embed.add_field(name="Next", value=game.players[next_index].mention, inline=True)

        embed.set_footer(text=f"Pool: {game.bet * len(game.players)} 🥖")
        return embed
    except Exception:
        print("[UNO ERROR] Failed to generate game embed.")
        import traceback
        traceback.print_exc()
        return discord.Embed(title="🚨 Error generating game state")


def generate_game_view(game):
    try:
        view = View(timeout=None)
        view.add_item(DrawButton(game))
        view.add_item(HandDropdown(game))
        print("[DEBUG] Game view generated")
        return view
    except Exception as e:
        print("[ERROR] Failed to generate game view")
        traceback.print_exc()
        return View()

# ================================
# === UNO COMMAND REGISTRATION ===
# ================================

def register_uno_commands(bot):
    @bot.command()
    async def uno(ctx, bet: int):
        try:
            if any(g.message and g.message.channel.id == ctx.channel.id for g in active_uno_games):
                return await ctx.send("⚠️ A UNO game is already running in this channel.")

            if bet <= 0:
                return await ctx.send("❗ Usage: `;uno <bet>` — no mentions allowed. Use the join button.")

            players = [ctx.author]
            joined_ids = {p.id for p in players}

            for p in players:
                doc = await users.find_one({"_id": str(p.id)})
                if not doc or doc.get("wallet", 0) < bet:
                    return await ctx.send(f"🚫 {p.display_name} doesn't have enough 🥖.")

            view = View(timeout=30)

            class JoinButton(Button):
                def __init__(self):
                    super().__init__(label="Join UNO", style=discord.ButtonStyle.success)

                async def callback(self, interaction):
                    try:
                        uid = interaction.user.id
                        if uid in joined_ids:
                            return await interaction.response.send_message("❗ You already joined.", ephemeral=True)
                        if len(players) >= 6:
                            return await interaction.response.send_message("❗ Max 6 players.", ephemeral=True)
                        doc2 = await users.find_one({"_id": str(uid)})
                        if not doc2 or doc2.get("wallet", 0) < bet:
                            return await interaction.response.send_message("❗ Not enough 🥖.", ephemeral=True)

                        players.append(interaction.user)
                        joined_ids.add(uid)

                        await msg.edit(content=(
                            f"🎮 **UNO Game Starting!** Bet: {bet} 🥖\n"
                            f"Players: {', '.join(p.mention for p in players)}\n"
                            "Click below to join! ⏳ 30s..."
                        ), view=view)

                        await interaction.response.send_message(f"✅ {interaction.user.display_name} joined!", ephemeral=True)
                        print(f"[DEBUG] {interaction.user.display_name} joined UNO")
                    except Exception as e:
                        print("[ERROR] JoinButton callback failed")
                        traceback.print_exc()

            view.add_item(JoinButton())

            initial_text = (
                f"🎮 **UNO Game Starting!** Bet: {bet} 🥖\n"
                f"Players: {', '.join(p.mention for p in players)}\n"
            )

            msg = await ctx.send(initial_text + "Click below to join! ⏳ 30s...", view=view)

            for t in (20, 10, 5):
                joined_names = ', '.join(p.mention for p in players)
                await asyncio.sleep(10)
                await msg.edit(
                    content=f"🎮 **UNO Game Starting!** Bet: {bet} 🥖\n"
                            f"Players: {joined_names}\n"
                            f"... ⏳ {t}s left to join!"
                )

            await asyncio.sleep(5)

            if len(players) < 2:
                return await ctx.send("❗ Not enough players joined. Game cancelled.")

            for p in players:
                await users.update_one({"_id": str(p.id)}, {"$inc": {"wallet": -bet}}, upsert=True)

            game = UnoGame(ctx, bet, players)
            active_uno_games.append(game)

            try:
                await start_uno_game(bot, game)
            except Exception:
                print("[ERROR] Failed to start UNO game")
                traceback.print_exc()
                return await ctx.send("🚨 Oops—there was an error starting the UNO game. Check the logs for details.")

        except Exception as e:
            print("[ERROR] Exception in ;uno command")
            traceback.print_exc()
            await ctx.send("🚨 Critical error in UNO command.")


# =================================
# ======== UNO GAME MODULE ========
# =================================

class DrawButton(discord.ui.Button):
    def __init__(self, game, user):
        super().__init__(label="Draw", style=discord.ButtonStyle.secondary)
        self.game = game
        self.user = user

    async def callback(self, interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("❗ It’s not your turn.", ephemeral=True)

        try:
            drawn_card = self.game.deck.pop()
            self.game.hands[self.user].append(drawn_card)

            await interaction.response.send_message(f"🃏 You drew: `{drawn_card}`", ephemeral=True)

            # If it's playable, allow them to still play it manually
            if is_valid_play(drawn_card, self.game.top_card, self.game.draw_stack, self.game.color_override):
                await self.game.ctx.send(f"🔄 {self.user.display_name} drew a playable card.")
            else:
                self.game.advance_turn()
                self.game.top_card = self.game.pile[-1]  # ✅ Ensure top_card stays accurate
                await start_uno_game(interaction.client, self.game)

        except Exception:
            print("[UNO ERROR] Draw button failed.")
            traceback.print_exc()


class HandDropdown(Select):
    def __init__(self, game, user):
        self.game = game

        # Only show dropdown to the current player
        if user != game.current_player():
            # Empty options to prevent showing anything
            super().__init__(placeholder="Not your turn", options=[], disabled=True)
            return

        hand = game.hands[user]

        # Use unique values by appending index to duplicates
        options = []
        seen = {}
        for i, card in enumerate(hand):
            count = seen.get(card, 0)
            seen[card] = count + 1
            label = card
            value = f"{card}|{count}" if count > 0 else card  # Add suffix if it's a duplicate
            options.append(discord.SelectOption(label=label, value=value))

        super().__init__(placeholder="Select a card to play", min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        g = self.game
        try:
            if interaction.user != g.current_player():
                return await interaction.response.send_message("❗ It’s not your turn.", ephemeral=True)

            # Get actual card value without the "|0", "|1" suffix
            selected_raw = self.values[0].split("|")[0]

            # Validate
            if not is_valid_play(selected_raw, g.pile[-1], g.draw_stack, g.color_override):
                return await interaction.response.send_message("❌ Invalid move.", ephemeral=True)

            # Remove the first instance of that card in hand
            for i, card in enumerate(g.hands[interaction.user]):
                if card == selected_raw:
                    del g.hands[interaction.user][i]
                    break
            else:
                return await interaction.response.send_message("⚠️ Could not find the card in hand.", ephemeral=True)

            g.pile.append(selected_raw)
            g.top_card = selected_raw  # ✅ Update top card
            g.apply_card_effect(selected_raw)

            if selected_raw in WILD_CARDS:
                try:
                    await interaction.response.defer()
                except discord.errors.InteractionResponded:
                    pass
                await interaction.followup.send("🎨 Choose a color...", view=ColorSelectView(g, interaction.user), ephemeral=True)
                return


            # UNO call logic
            if len(g.hands[interaction.user]) == 1:
                g.called_uno[interaction.user] = None  # They need to call it soon

                async def uno_timer():
                    await asyncio.sleep(10)
                    if g.called_uno.get(interaction.user) is None and len(g.hands[interaction.user]) == 1:
                        g.hands[interaction.user].extend([g.deck.pop(), g.deck.pop()])
                        await g.ctx.send(f"❗ **{interaction.user.display_name}** didn’t call UNO! Drew 2 cards and turn ended.")
                    else:
                        await g.ctx.send(f"✅ **{interaction.user.display_name}** called UNO in time!")

                    g.advance_turn()
                    await start_uno_game(interaction.client, g)

                asyncio.create_task(uno_timer())
                return


            elif len(g.hands[interaction.user]) == 0:
                return await g.end_game(interaction.user)

            g.advance_turn()
            await start_uno_game(interaction.client, g)

        except Exception:
            print("[UNO ERROR] Failed inside HandDropdown callback.")
            traceback.print_exc()


async def start_uno_game(bot, game):
    if game.skip_next:
        game.skip_next = False
        game.advance_turn()
        return await start_uno_game(bot, game)

    try:
        print(f"[UNO DEBUG] Starting UNO game for {len(game.players)} players.")
        player = game.current_player()
        game.color_override = None
        print(f"[UNO DEBUG] Current player: {player.display_name}")

        embed = generate_game_embed(game)
        view = View(timeout=None)
        view.add_item(HandDropdown(game, game.current_player()))
        view.add_item(DrawButton(game, game.current_player()))

        game.message = await game.ctx.send(embed=embed, view=view)

        # Turn timer
        if hasattr(game, "timer_task") and game.timer_task:
            game.timer_task.cancel()
            
        async def turn_timer(p):
            try:
                # If player is under draw stack pressure
                if game.draw_stack > 0:
                    playable = [c for c in game.hands[p] if is_valid_play(c, game.top_card, game.draw_stack)]
                    if not playable:
                        # No stackable +2/+4, draw immediately and skip
                        drawn = [game.deck.pop() for _ in range(game.draw_stack)]
                        game.hands[p].extend(drawn)
                        await game.ctx.send(
                            f"❗ {p.mention} has no +2/+4 to stack and draws **{game.draw_stack} cards**. Turn skipped."
                        )
                        game.draw_stack = 0
                        game.advance_turn()
                        await start_uno_game(bot, game)
                        return  # Exit early
                    # Else wait 30s and let them stack manually

                # Countdown reminders
                for seconds_left in [20, 10, 5]:
                    await asyncio.sleep(30 - seconds_left)
                    await game.ctx.send(f"⏳ {p.mention}, {seconds_left}s left to play...")

                # Final check after timeout
                if p == game.current_player():
                    if game.draw_stack > 0:
                        drawn = [game.deck.pop() for _ in range(game.draw_stack)]
                        game.hands[p].extend(drawn)
                        await game.ctx.send(
                            f"❗ {p.mention} didn’t respond to +2/+4 stack and drew **{game.draw_stack} cards**. Turn skipped."
                        )
                        game.draw_stack = 0
                    else:
                        await game.ctx.send(f"⌛ {p.mention} took too long! Turn skipped.")

                    game.advance_turn()
                    await start_uno_game(bot, game)
            except Exception as e:
                print(f"[ERROR] in turn_timer for {p.display_name}")
                traceback.print_exc()


        game.timer_task = asyncio.create_task(turn_timer(player))

    except Exception:
        print("[UNO ERROR] Failed at game start.")
        traceback.print_exc()



# ======================
# === LOTTERY SYSTEM ===
# ======================


lottery_timezone = pytz.timezone("America/Toronto")
lottery_day = 6  # Sunday
lottery_hour = 12
lottery_minute = 0

lottery_cache = {
    "last_reminder": None,
    "last_draw": None
}

lottery_started = False  # Ensures the loop starts only once

@bot.command(aliases=["lottery"])
async def lotto(ctx):
    user = await get_user(ctx.author.id)
    now = datetime.now(lottery_timezone)
    next_draw = now.replace(hour=lottery_hour, minute=lottery_minute, second=0, microsecond=0)
    if now.weekday() > lottery_day or (now.weekday() == lottery_day and now.time() >= next_draw.time()):
        next_draw += timedelta(days=(7 - now.weekday() + lottery_day) % 7)
    elif now.weekday() < lottery_day:
        next_draw += timedelta(days=(lottery_day - now.weekday()))

    tickets = user.get("lottery_tickets", 0)
    buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
    total_tickets = sum(u.get("lottery_tickets", 0) for u in buyers)
    pool = 50000 + total_tickets * 25000

    embed = discord.Embed(title="🎟️ Weekly Lottery", color=discord.Color.gold())
    embed.add_field(name="Next Draw", value=f"<t:{int(next_draw.timestamp())}:R>", inline=False)
    embed.add_field(name="Prize Pool", value=f"{pool} 🥖", inline=False)
    embed.add_field(name="Your Tickets", value=f"{tickets}/5", inline=True)
    embed.add_field(name="Total Tickets", value=f"{total_tickets}", inline=True)
    embed.set_footer(text="Max 5 tickets per user per week. 50,000 🥖 each.")
    await ctx.send(embed=embed)

@bot.command()
async def lottobuy(ctx, amount: int):
    if amount < 1 or amount > 5:
        return await ctx.send("❗ You can buy between 1 and 5 tickets.")

    user = await get_user(ctx.author.id)
    current = user.get("lottery_tickets", 0)
    if current >= 5:
        return await ctx.send("❌ You already own 5 tickets this week.")
    if current + amount > 5:
        return await ctx.send(f"❌ You can only buy {5 - current} more ticket(s).")

    total_price = 50000 * amount
    if user.get("wallet", 0) < total_price:
        return await ctx.send("💸 Not enough 🥖.")

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.result = None

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
        async def confirm(self, interaction, button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            self.result = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, interaction, button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            self.result = False
            self.stop()

    view = ConfirmView()
    await ctx.send(f"🎟️ Buy {amount} ticket(s) for {total_price} 🥖?", view=view)
    await view.wait()

    if view.result:
        await update_user(ctx.author.id, {"$inc": {"wallet": -total_price, "lottery_tickets": amount}})
        await ctx.send(f"✅ You bought {amount} ticket(s)!")
    else:
        await ctx.send("❌ Purchase cancelled.")

async def run_lottery_draw():
    buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
    entries = []
    for u in buyers:
        entries.extend([u["_id"]] * u.get("lottery_tickets", 0))

    channel = bot.get_channel(1398586067967414323)
    if entries:
        winner_id = random.choice(entries)
        winner = await bot.fetch_user(int(winner_id))
        prize = 50000 + len(entries) * 25000
        await users.update_one({"_id": str(winner_id)}, {"$inc": {"wallet": prize}})
        await channel.send(f"🏆 Congratulations {winner.mention}! You won {prize} 🥖 from this week's lottery!")
    else:
        await channel.send("😞 No entries this week. Lottery cancelled.")

    await users.update_many({}, {"$set": {"lottery_tickets": 0}})
    lottery_cache["last_draw"] = datetime.now(lottery_timezone)

@bot.command()
async def forcelotto(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("❌ Only the bot owner can force a lottery draw.")

    await perform_lottery_draw()
    await ctx.send("🎯 Forced lottery draw executed.")

@tasks.loop(minutes=1)
async def lottery_check():
    now = datetime.now(lottery_timezone)

    # === Reminder every 48h ===
    if (lottery_cache["last_reminder"] is None or
        (now - lottery_cache["last_reminder"]).total_seconds() >= 48 * 3600):
        buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
        if buyers:
            total_tickets = sum(u.get("lottery_tickets", 0) for u in buyers)
            pool = 50000 + total_tickets * 25000
            lines = [f"<@{u['_id']}>: {u['lottery_tickets']} 🎟️" for u in buyers]
            embed = discord.Embed(title="⏳ Lottery Reminder", color=discord.Color.orange())
            embed.add_field(name="Prize Pool", value=f"{pool} 🥖", inline=False)
            embed.add_field(name="Participants", value="\n".join(lines), inline=False)
            embed.set_footer(text="Use ;lotto to check your tickets or ;lotto buy <amount> to join! Maximum of 5 tickets per participant!")

            channel = bot.get_channel(LOTTERY_CHANNEL_ID)
            if channel:
                await channel.send(embed=embed)
            else:
                print(f"[WARNING] Lottery reminder failed: Channel {LOTTERY_CHANNEL_ID} not found.")

            lottery_cache["last_reminder"] = now

    # === Draw time ===
    if now.weekday() == lottery_day and now.hour == lottery_hour and now.minute == lottery_minute:
        if lottery_cache["last_draw"] and lottery_cache["last_draw"].date() == now.date():
            return
        await run_lottery_draw()

@lottery_check.before_loop
async def before_lottery():
    await bot.wait_until_ready()


# ============================
# COMMAND: TREASURE HUNT/;TH
# ============================

@bot.command(aliases=["th"])
async def treasurehunt(ctx):
    user_id = str(ctx.author.id)
    now = datetime.utcnow()

    # Always fetch fresh
    user_data = await users.find_one({"_id": user_id})
    if not user_data:
        await users.insert_one({"_id": user_id, "wallet": 0, "bank": 0})
        user_data = await users.find_one({"_id": user_id})

    # Read target scope **after** confirming user exists
    has_target_scope = user_data.get("active_buffs", {}).get("target_scope", False)
    digs_allowed = 2 if has_target_scope else 1

    # Cooldown check
    cooldowns_data = user_data.get("cooldowns", {})
    last_used = cooldowns_data.get("treasurehunt")
    if last_used:
        if isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)
        elapsed = (now - last_used).total_seconds()
        if elapsed < 86400:
            hours = int((86400 - elapsed) // 3600)
            minutes = int(((86400 - elapsed) % 3600) // 60)
            return await ctx.send(f"⏳ You must wait {hours}h {minutes}m before digging again.")

    prizes = [
        ("👑", 1000000, 0.0005),
        ("💎", 100000, 0.01),
        ("🗿", 20000, 0.05),
        ("🧨", -100000, 0.05),
        ("🪦", "death", 0.005),
        ("💣", -25000, 0.05),
        ("🔑", "weekly_reset", 0.05),
        ("🪙", 500, 0.225),
        ("🥖", 2000, 0.27),
        ("🎁", 4000, 0.2995),
    ]

    prize_pool = []
    for emoji, value, prob in prizes:
        prize_pool += [(emoji, value)] * int(prob * 100000)

    class TreasureView(View):
        def __init__(self, author, digs_allowed):
            super().__init__(timeout=60)
            self.author = author
            self.digs_done = 0
            self.digs_allowed = digs_allowed
            self.clicked_indexes = set()
            for i in range(25):
                self.add_item(self.TreasureButton(i, self))

        class TreasureButton(Button):
            def __init__(self, index, parent):
                super().__init__(style=discord.ButtonStyle.secondary, label="🟫", row=index // 5)
                self.index = index
                self.parent = parent

            async def callback(self, interaction):
                if interaction.user != self.parent.author:
                    return await interaction.response.send_message("❗ Only the player who started this game can dig.", ephemeral=True)
                if self.index in self.parent.clicked_indexes:
                    return await interaction.response.send_message("❗ You already dug this tile!", ephemeral=True)
                if self.parent.digs_done >= self.parent.digs_allowed:
                    return await interaction.response.send_message("❗ You've used all your digs!", ephemeral=True)

                self.parent.clicked_indexes.add(self.index)
                self.parent.digs_done += 1
                prize = random.choice(prize_pool)
                emoji, value = prize
                self.label = emoji
                self.disabled = True
                await interaction.response.edit_message(view=self.view)

                if value == "death":
                    await users.update_one({"_id": user_id}, {"$set": {"wallet": 0}})
                    await ctx.send("💀 Haha, you dug your own grave and tripped. You died and lost all your bread loser.")
                elif value == "weekly_reset":
                    await users.update_one({"_id": user_id}, {"$unset": {"cooldowns.weekly": ""}})
                    await ctx.send(f"🔑 You found a **Key**! Your `;weekly` cooldown has been reset.")
                elif value > 0:
                    await users.update_one({"_id": user_id}, {"$inc": {"wallet": value}})
                    await ctx.send(f"🪓 You dug up a {emoji} and found **{value:,} 🥖**!")
                else:
                    await users.update_one({"_id": user_id}, {"$inc": {"wallet": value}})
                    await ctx.send(f"💥 You hit a {emoji} and **lost {-value:,} 🥖!**")

                if self.parent.digs_done == self.parent.digs_allowed:
                    await users.update_one({"_id": user_id}, {"$set": {"cooldowns.treasurehunt": now}})
                    if has_target_scope:
                        await users.update_one({"_id": user_id}, {"$unset": {"active_buffs.target_scope": ""}})

    view = TreasureView(ctx.author, digs_allowed)

    description = "Click a tile to dig for treasure! Some are good... some are deadly."
    if has_target_scope:
        description += "\n🎯 You have a **Target Scope** active! You get to dig **twice**!"

    embed = discord.Embed(
        title="🪓 Treasure Hunt",
        description=description,
        color=discord.Color.gold()
    )
    await ctx.send(embed=embed, view=view)



# ================================
# ======== FORCE LOTTERY =========
# ================================

@bot.command()
async def force_lotto(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("❌ You are not authorized to use this command.")
    await ctx.send("🧪 Forcing lottery draw now...")
    await run_lottery_draw()


# ============================
# COMMAND: disable/enable cmd
# ============================

@bot.command()
@commands.has_permissions(administrator=True)
async def disable(ctx, command_name: str):
    command = bot.get_command(command_name)
    if not command:
        return await ctx.send(f"❌ Command `{command_name}` not found.")
    if command_name in ["enable", "disable"]:
        return await ctx.send("❌ You cannot disable this command.")
    
    settings = await bot_settings.find_one({"_id": "disabled_commands"}) or {}
    disabled = settings.get("commands", [])
    if command_name in disabled:
        return await ctx.send("⚠️ Command is already disabled.")

    disabled.append(command_name)
    await bot_settings.update_one(
        {"_id": "disabled_commands"},
        {"$set": {"commands": disabled}},
        upsert=True
    )
    await ctx.send(f"🔒 Command `{command_name}` has been disabled.")

@bot.command()
@commands.has_permissions(administrator=True)
async def enable(ctx, command_name: str):
    settings = await bot_settings.find_one({"_id": "disabled_commands"}) or {}
    disabled = settings.get("commands", [])
    if command_name not in disabled:
        return await ctx.send("⚠️ Command is not disabled.")

    disabled.remove(command_name)
    await bot_settings.update_one(
        {"_id": "disabled_commands"},
        {"$set": {"commands": disabled}},
        upsert=True
    )
    await ctx.send(f"🔓 Command `{command_name}` has been enabled.")

@bot.command()
@commands.has_permissions(administrator=True)
async def disabled(ctx):
    settings = await bot_settings.find_one({"_id": "disabled_commands"}) or {}
    disabled = settings.get("commands", [])
    if not disabled:
        return await ctx.send("✅ No commands are currently disabled.")
    await ctx.send(f"🚫 Disabled commands:\n`{', '.join(disabled)}`")
    
# ────────────────────────────────────────────────────
# COMMAND: RESET ALL (Creator Only)
#────────────────────────────────────────────────────

@bot.command(name="resetall")
async def resetall(ctx):
    """Resets all user data: wallets, banks, cooldowns, and stats. Creator-only."""
    # Only the bot creator may run this
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot creator can use this command.")

    from discord.ui import View, Button

    class ConfirmResetAll(View):
        @discord.ui.button(label="✅ Confirm Reset All", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message(
                    "Only the command issuer can confirm.", ephemeral=True
                )
            # Reset economy and balances
            result = await users.update_many(
                {},
                {
                    "$set": {"wallet": 0, "bank": 0},
                    "$unset": {
                        "cooldowns": "",
                        "cooldowns.hangman": "",
                        "stats": ""
                    }
                }
            )
            # Clear in-memory leaderboards and games
            active_trivia.clear()
            trivia_answers.clear()

            await interaction.response.edit_message(
                content=(
                    f"♻️ Reset all user data for {result.modified_count} users (wallets, banks, cooldowns, stats)."
                ),
                view=None
            )

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message(
                    "Only the command issuer can cancel.", ephemeral=True
                )
            await interaction.response.edit_message(
                content="❎ Reset all cancelled.", view=None
            )

    # Prompt confirmation
    await ctx.send(
        "⚠️ This will wipe ALL wallets, banks, cooldowns, and stats. Are you sure?",
        view=ConfirmResetAll()
    )


# ============================
# ECONOMY COMMANDS
# ============================

@bot.command()
async def give(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        return await ctx.send("❗ Enter a positive amount.")

    author_data = users.find_one({"_id": str(ctx.author.id)})
    if not author_data or author_data.get("wallet", 0) < amount:
        return await ctx.send("❗ You don't have enough 🥖.")

    users.update_one({"_id": str(ctx.author.id)}, {"$inc": {"wallet": -amount}})
    users.update_one({"_id": str(member.id)}, {"$inc": {"wallet": amount}}, upsert=True)
    await ctx.send(f"✅ Gave {amount} 🥖 to {member.display_name}.")

@bot.command()
async def work(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'work', 3600)
    if on_cd:
        return await ctx.send(f"⏳ Come back in {remaining // 60}m {remaining % 60}s to work again!")

    earnings = random.randint(1000, 5000)
    await increment_user(ctx.author.id, "wallet", earnings)

    # ✅ Set cooldown after reward
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.work": datetime.utcnow().isoformat()}}
    )

    await ctx.send(f"💼 You worked and earned 🥖 {earnings}!")

    
@bot.command()
async def daily(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'daily', 86400)
    if on_cd:
        return await ctx.send(f"⏳ Daily already claimed! Wait {remaining // 3600}h {remaining % 3600 // 60}m.")

    earnings = random.randint(5000, 10000)
    await increment_user(ctx.author.id, "wallet", earnings)

    # ✅ Set cooldown after giving reward
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.daily": datetime.utcnow().isoformat()}}
    )

    await ctx.send(f"📆 You claimed your daily reward of 🥖 {earnings}!")


@bot.command()
async def weekly(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'weekly', 604800)
    if on_cd:
        days, rem = divmod(remaining, 86400)
        hours = rem // 3600
        return await ctx.send(f"⏳ Weekly already claimed! Wait **{days}d {hours}h**.")

    earnings = random.randint(10000, 20000)
    await increment_user(ctx.author.id, "wallet", earnings)

    # ✅ Set the cooldown timestamp
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.weekly": datetime.utcnow().isoformat()}}
    )

    await ctx.send(f"🧾 You claimed your weekly bonus of 🥖 {earnings}!")


@bot.command()
async def rob(ctx, target: discord.Member):
    if target.id == ctx.author.id:
        return await ctx.send("❌ You can't rob yourself, dumbass.")

    await ensure_user(ctx.author.id)
    await ensure_user(target.id)

    now = datetime.utcnow()
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'rob', 3600)
    if on_cd:
        return await ctx.send(f"⏳ Wait {remaining // 60}m {remaining % 60}s to rob again.")

    robber = await users.find_one({"_id": str(ctx.author.id)})
    victim = await users.find_one({"_id": str(target.id)})

    if victim["wallet"] <= 0:
        return await ctx.send("❌ That user has no 🥖 to steal.")

    if robber.get("buffs", {}).get("gun"):
        success = True
        double = True
        await users.update_one({"_id": str(ctx.author.id)}, {"$unset": {"buffs.gun": ""}})
    else:
        success = random.random() > 0.6  # 40% chance to succeed
        double = False

    if success:
        amount = random.randint(1000, min(5000, victim.get("wallet", 0)))
        if double:
            amount *= 2
        await increment_user(ctx.author.id, "wallet", amount)
        await increment_user(target.id, "wallet", -amount)
        await ctx.send(f"{ctx.author.mention}, you robbed {target.mention} and stole {amount} 🥖!")
    else:
        roast_lines = [
            "You tripped on your way to rob them. Embarrassing.",
            "They caught you and roasted your ass.",
            "You got smacked with a baguette 🥖 while robbing. RIP.",
            "Nice try. You got tackled by a security guard.",
        ]
        await ctx.send(random.choice(roast_lines))

    await users.update_one(
    {"_id": str(ctx.author.id)},
    {"$set": {"cooldowns.rob": now.isoformat()}}
)


# Helper to format cooldown display
def format_cd(name, last_ts, cd_sec, now):
    """Returns a formatted string showing remaining time or ready status."""
    if last_ts:
        try:
            cd_time = datetime.fromisoformat(last_ts)
            rem = int((cd_time - now).total_seconds())
            if rem > 0:
                days, rem2 = divmod(rem, 86400)
                hours = rem2 // 3600
                minutes = (rem2 % 3600) // 60
                parts = []
                if days:
                    parts.append(f"{days}d")
                if hours:
                    parts.append(f"{hours}h")
                if minutes:
                    parts.append(f"{minutes}m")
                time_str = " ".join(parts)
                return f"⏳ {name.capitalize()}: {time_str}"
        except Exception:
            pass
    return f"✅ {name.capitalize()}: Ready"

# ============================
# COMMAND: cooldowns / cd
# ============================

@bot.command(name="cooldowns", aliases=["cd"])
async def cooldowns_cmd(ctx):
    """Displays active cooldowns with pagination for commands and items."""
    await ensure_user(ctx.author.id)
    user = await users.find_one({"_id": str(ctx.author.id)}) or {}
    cds = user.get("cooldowns", {})
    now = datetime.utcnow()

    def format_cd(name, last_time, duration, now_time):
        if not last_time:
            return f"{name} — ✅ Ready"
        if isinstance(last_time, str):
            try:
                last_time = datetime.fromisoformat(last_time)
            except ValueError:
                return f"{name} — ❓ Error"
        elapsed = (now_time - last_time).total_seconds()
        if elapsed >= duration:
            return f"{name} — ✅ Ready"
        remaining = int(duration - elapsed)
        if name == "weekly":
            d, rem = divmod(remaining, 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            parts = []
            if d:
                parts.append(f"{d}d")
            if h:
                parts.append(f"{h}h")
            if m or not parts:
                parts.append(f"{m}m")
            return f"{name} — ⏳ `{' '.join(parts)}`"
        else:
            h, rem = divmod(remaining, 3600)
            m = rem // 60
            return f"{name} — ⏳ `{h}h {m}m`"


    # === Page 1: Command/Game cooldowns ===
    cd_definitions = {
        "work":    3600,
        "daily":   86400,
        "weekly":  604800,
        "rob":     3600,
        "trivia":  86400,
        "hangman": 10800,
    }
    command_lines = [format_cd(cmd, cds.get(cmd), dur, now) for cmd, dur in cd_definitions.items()]
    command_embed = discord.Embed(
        title="⌛ Your Cooldowns — Commands & Games",
        description="\n".join(command_lines),
        color=discord.Color.blurple()
    )

    # === Page 2: Item usage cooldowns ===
    item_usage = cds.get("item_usage", {})
    item_lines = []
    for item_name, last_used in item_usage.items():
        if isinstance(last_used, str):
            try:
                last_used = datetime.fromisoformat(last_used)
            except ValueError:
                continue
        elapsed = (now - last_used).total_seconds()
        if elapsed >= 86400:
            item_lines.append(f"{item_name} — ✅ Ready")
        else:
            remaining = int(86400 - elapsed)
            h, rem = divmod(remaining, 3600)
            m = rem // 60
            item_lines.append(f"{item_name} — ⏳ `{h}h {m}m`")

    item_embed = discord.Embed(
        title="🧃 Your Cooldowns — Items",
        description="\n".join(item_lines) or "You have no item cooldowns.",
        color=discord.Color.blurple()
    )

    # === View with Buttons ===
    class CDView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.current_page = 0
            self.embeds = [command_embed, item_embed]

        @discord.ui.button(label="🕹️Games & Commands", style=discord.ButtonStyle.secondary)
        async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your cooldowns!", ephemeral=True)
            self.current_page = (self.current_page - 1) % len(self.embeds)
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

        @discord.ui.button(label="🎒Items", style=discord.ButtonStyle.secondary)
        async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your cooldowns!", ephemeral=True)
            self.current_page = (self.current_page + 1) % len(self.embeds)
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    await ctx.send(embed=command_embed, view=CDView())



# ============================
# COMMAND: WITHDRAW
# ============================

@bot.command(aliases=["with"])
async def withdraw(ctx, amount: int):
    user_data = await get_user(ctx.author.id)
    bank = user_data["bank"]

    if amount <= 0:
        return await ctx.send("❌ Amount must be positive.")
    if amount > bank:
        return await ctx.send(f"❌ You don't have that much 🥖 in the bank. Current balance: {bank} 🥖.")

    bank = user_data.get("bank", 0)
    new_bank = bank + max_deposit


    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"wallet": new_wallet, "bank": new_bank}}
    )
    await ctx.send(f"💸 {ctx.author.mention} withdrew {amount} 🥖 from the bank.")


@bot.command(aliases=["dep"])
async def deposit(ctx, amount: int):
    user_data = await get_user(ctx.author.id)
    wallet = user_data.get("wallet", 0)
    bank = user_data.get("bank", 0)
    max_deposit = wallet // 2

    if amount <= 0:
        return await ctx.send("❌ Amount must be positive.")
    if amount > wallet:
        return await ctx.send("❌ You don't have that much 🥖.")
    if amount > max_deposit:
        return await ctx.send(f"❌ You can only deposit up to 50% of your wallet ({max_deposit} 🥖).")

    new_wallet = wallet - amount
    new_bank = bank + amount

    # 🛑 Block if bank would be more than 50% of remaining wallet after deposit
    if new_wallet == 0 or new_bank > new_wallet / 2:
        return await ctx.send("⚠️ You can't deposit that much — your bank would exceed 50% of your remaining wallet.")

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"wallet": new_wallet, "bank": new_bank}}
    )
    await ctx.send(f"🏦 {ctx.author.mention} deposited {amount} 🥖 into the bank.")


# ====================================
# COMMAND: DEPOSITMAX (50% OF BALANCE)
# ====================================

@bot.command(aliases=["depmax", "depall", "depositall"])
async def depositmax(ctx):
    user_data = await get_user(ctx.author.id)
    wallet = user_data.get("wallet", 0)
    bank = user_data.get("bank", 0)

    if wallet <= 0:
        return await ctx.send("❌ You don't have any 🥖 to deposit.")

    max_deposit = wallet // 2
    if max_deposit == 0:
        return await ctx.send("❌ You need at least 2 🥖 in your wallet to use `;deposit max`.")

    new_wallet = wallet - max_deposit
    new_bank = bank + max_deposit

    # ✅ Correct 50% check AFTER deposit
    if new_bank > new_wallet * 0.5:
        return await ctx.send("⚠️ This deposit would cause your bank to exceed 50% of your remaining wallet.")

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"wallet": new_wallet, "bank": new_bank}}
    )

    await ctx.send(f"🏦 {ctx.author.mention} deposited {max_deposit} 🥖 (50% of your wallet) into the bank.")


# ============================
# COMMAND: LEADERBOARD
# ============================

@bot.command()
async def leaderboard(ctx):
    users = await users_collection.find().to_list(length=100)
    sorted_users = sorted(users, key=lambda u: u.get("wallet", 0) + u.get("bank", 0), reverse=True)[:10]

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    for i, user in enumerate(sorted_users, start=1):
        member = await bot.fetch_user(int(user["_id"]))
        total = user.get("wallet", 0) + user.get("bank", 0)
        embed.add_field(name=f"{i}. {member.display_name}",
                        value=f"Total 🥖: {total}",
                        inline=False)
    await ctx.send(embed=embed)

# ============================
# GAME: TIC-TAC-TOE
# ============================

class TicTacToeButton(Button):
    def __init__(self, x, y, view):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=x)
        self.x = x
        self.y = y
        self.view_ref = view

    async def callback(self, interaction):
        view = self.view_ref
        if interaction.user != view.current_player:
            return await interaction.response.send_message("❌ Not your turn!", ephemeral=True)
        if view.board[self.x][self.y] != 0:
            return await interaction.response.send_message("❌ This cell is already taken!", ephemeral=True)

        mark = "❌" if view.current_player == view.player1 else "⭕"
        self.label = mark
        self.style = discord.ButtonStyle.danger if mark == "❌" else discord.ButtonStyle.success
        self.disabled = True
        view.board[self.x][self.y] = view.current_player.id

        winner = view.check_winner()
        if winner:
            await interaction.response.defer()  # Prevent "interaction failed"
            view.stop()
            stats = await get_user(winner.id)
            if "stats" not in stats:
                stats["stats"] = {}
            if "tictactoe" not in stats["stats"]:
                stats["stats"]["tictactoe"] = {"wins": 0, "losses": 0}
            stats["stats"]["tictactoe"]["wins"] += 1

            loser = view.player2 if winner == view.player1 else view.player1
            stats_loser = await get_user(loser.id)
            if "stats" not in stats_loser:
                stats_loser["stats"] = {}
            if "tictactoe" not in stats_loser["stats"]:
                stats_loser["stats"]["tictactoe"] = {"wins": 0, "losses": 0}
            stats_loser["stats"]["tictactoe"]["losses"] += 1


            stats["wallet"] += view.bet * 2

            await update_user(winner.id, stats)
            await update_user(loser.id, stats_loser)

            await interaction.message.edit(
                content=f"🎉 {winner.mention} wins! +{view.bet:,} 🥖", view=view)
        elif view.is_draw():
            await interaction.response.defer()
            view.stop()

            # Refund both
            p1_data = await get_user(view.player1.id)
            p2_data = await get_user(view.player2.id)
            p1_data["wallet"] += view.bet
            p2_data["wallet"] += view.bet
            await update_user(view.player1.id, p1_data)
            await update_user(view.player2.id, p2_data)

            await interaction.message.edit(content="🤝 It's a draw! Bets refunded.", view=view)
        else:
            view.current_player = view.player2 if view.current_player == view.player1 else view.player1
            await interaction.response.edit_message(content=f"{view.current_player.mention}'s turn", view=view)

class TicTacToeView(View):
    def __init__(self, ctx, player1, player2, bet):
        super().__init__()
        self.ctx = ctx
        self.player1 = player1
        self.player2 = player2
        self.current_player = player1
        self.board = [[0] * 3 for _ in range(3)]
        self.bet = bet
        for i in range(3):
            for j in range(3):
                self.add_item(TicTacToeButton(i, j, self))

    def check_winner(self):
        b = self.board
        lines = [
            [b[0][0], b[0][1], b[0][2]],
            [b[1][0], b[1][1], b[1][2]],
            [b[2][0], b[2][1], b[2][2]],
            [b[0][0], b[1][0], b[2][0]],
            [b[0][1], b[1][1], b[2][1]],
            [b[0][2], b[1][2], b[2][2]],
            [b[0][0], b[1][1], b[2][2]],
            [b[0][2], b[1][1], b[2][0]],
        ]
        for line in lines:
            if line[0] != 0 and all(cell == line[0] for cell in line):
                return self.player1 if line[0] == self.player1.id else self.player2
        return None

    def is_draw(self):
        return all(cell != 0 for row in self.board for cell in row)

@bot.command(aliases=["tictactoe"])
async def ttt(ctx, bet: int, member: discord.Member):
    if member == ctx.author:
        return await ctx.send("❌ You can't play against yourself.")
    if member.bot:
        return await ctx.send("❌ You can't play against bots.")

    p1_data = await get_user(ctx.author.id)
    p2_data = await get_user(member.id)

    if p1_data["wallet"] < bet:
        return await ctx.send("❌ You don't have enough 🥖 to place that bet.")
    if p2_data["wallet"] < bet:
        return await ctx.send(f"❌ {member.display_name} doesn't have enough 🥖 to accept the challenge.")

    class ConfirmView(View):
        def __init__(self, timeout=30):
            super().__init__(timeout=timeout)
            self.value = None

        @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: Button):
            if interaction.user != member:
                return await interaction.response.send_message("❌ You're not the challenged player!", ephemeral=True)
            self.value = True
            self.stop()

        @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: Button):
            if interaction.user != member:
                return await interaction.response.send_message("❌ You're not the challenged player!", ephemeral=True)
            self.value = False
            self.stop()

    view = ConfirmView()
    msg = await ctx.send(
        f"🎮 {ctx.author.mention} challenged {member.mention} to a Tic-Tac-Toe match for 🥖 **{bet:,}** each.\n"
        f"{member.mention}, do you accept?",
        view=view
    )

    await view.wait()

    if view.value is None:
        return await msg.edit(content="⌛ Challenge timed out. Game cancelled.", view=None)
    if view.value is False:
        return await msg.edit(content="❌ Challenge declined.", view=None)

   
    p1_data["wallet"] -= bet
    p2_data["wallet"] -= bet
    await update_user(ctx.author.id, p1_data)
    await update_user(member.id, p2_data)

    game_view = TicTacToeView(ctx, ctx.author, member, bet)
    await msg.edit(content=f"Tic-Tac-Toe: {ctx.author.mention} vs {member.mention}\n{ctx.author.mention}'s turn", view=game_view)
    
@bot.command()
async def tttleaderboard(ctx):
    users = await users_collection.find().to_list(length=100)
    ranked = sorted(users, key=lambda u: u.get("stats", {}).get("tictactoe", {}).get("wins", 0), reverse=True)[:10]
    embed = discord.Embed(title="🏆 Tic-Tac-Toe Leaderboard", color=discord.Color.blue())
    for i, user in enumerate(ranked, 1):
        member = await bot.fetch_user(int(user["_id"]))
        wins = user.get("stats", {}).get("tictactoe", {}).get("wins", 0)
        losses = user.get("stats", {}).get("tictactoe", {}).get("losses", 0)
        embed.add_field(name=f"{i}. {member.name}", value=f"Wins: {wins}, Losses: {losses}", inline=False)
    await ctx.send(embed=embed)


# ============================
# FUN COMMAND: ROAST
# ============================

@bot.command()
async def roast(ctx, member: discord.Member):
    roasts = [
        f"{member.mention}, you have something on your face… oh wait, that’s just your face.",
        f"{member.mention}, you have something special. It’s called bad taste.",
        f"{member.mention}, you’re not stupid; you just have bad luck thinking.",
        f"{member.mention}, I’d agree with you, but then we’d both be wrong.",
        f"{member.mention}, if you were any slower, you'd be going backward.",
        f"{member.mention}, your Wi-Fi signal has more strength than your personality.",
        f"{member.mention}, I’ve seen salads dress better than you.",
        f"{member.mention}, if I wanted to hear from someone irrelevant, I'd unmute you.",
        f"{member.mention}, you're proof that even evolution takes a break sometimes.",
        f"{member.mention}, you're like a cloud. When you disappear, it's a beautiful day.",
        f"{member.mention}, you're the human version of a participation trophy.",
        f"{member.mention}, your birth certificate is an apology letter from the hospital.",
        f"{member.mention}, you have something on your lip… oh wait, that’s failure.",
        f"{member.mention}, you're about as useful as a screen door on a submarine.",
        f"{member.mention}, the wheel is spinning, but the hamster’s definitely dead.",
        f"{member.mention}, you’re not ugly… but you’re not in the clear either.",
        f"{member.mention}, I’d roast you harder but I don’t want to bully the weak.",
        f"{member.mention}, you have the charisma of a wet sock.",
        f"{member.mention}, you make onions cry.",
        f"{member.mention}, even mirrors avoid reflecting you.",
        f"{member.mention}, you're like a software update—unwanted and annoying.",
        f"{member.mention}, you talk a lot for someone who says nothing.",
        f"{member.mention}, you're the background character of your own life.",
        f"{member.mention}, if laziness were an Olympic sport, you'd come in last just to avoid the podium.",
        f"{member.mention}, you're not even the main character in your dreams.",
        f"{member.mention}, if I had a dollar for every smart thing you said, I'd be broke.",
        f"{member.mention}, your secrets are always safe with me. I never even listen when you tell me them.",
        f"{member.mention}, you bring everyone so much joy… when you leave the room.",
        f"{member.mention}, you have something on your chin... no, the third one down.",
    ]
    await ctx.send(random.choice(roasts))

@bot.command()
async def hangman(ctx):
    await get_user(ctx.author.id)
    user_id = str(ctx.author.id)

    user = await users.find_one({"_id": user_id})
    now = datetime.now()

    if user and "cooldowns.hangman" in user:
        cooldown_end = datetime.strptime(user["cooldowns.hangman"], "%Y-%m-%d %H:%M:%S")
        if now < cooldown_end:
            remaining = cooldown_end - now
            hours, remainder = divmod(remaining.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            return await ctx.send(
                f"⏳ You must wait **{int(hours)}h {int(minutes)}m** before playing Hangman again."
            )

    import string
    HANGMAN_WORDS = [  # (shortened for clarity, full list unchanged)
        "able", "acid", "aged", "ally", "area", "atom", "auto", "avid", "baby", "bake", "ball",
        "base", "cool", "data", "duck", "even", "fail", "glue", "hair", "hope", "idea", "jack",
        "keen", "lamp", "mild", "note", "oval", "park", "quiz", "road", "safe", "tide", "user",
        "view", "wake", "xray", "yell", "zero"
    ]

    HANGMAN_PICS = [
        """ +---+\n |   |\n     |\n     |\n     |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n     |\n     |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n |   |\n     |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n/|   |\n     |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n/|\\  |\n     |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n/|\\  |\n/    |\n     |\n=========""",
        """ +---+\n |   |\n 💀   |\n/|\\  |\n/ \\  |\n     |\n========="""
    ]

    word = random.choice(HANGMAN_WORDS).lower()
    display = ["_" for _ in word]
    guessed = set()
    lives = len(HANGMAN_PICS) - 1

    class LetterView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            for letter in string.ascii_uppercase:
                if letter != 'Z':  # Exclude Z if needed
                    self.add_item(LetterButton(letter))

    class LetterButton(discord.ui.Button):
        def __init__(self, letter):
            super().__init__(label=letter, style=discord.ButtonStyle.secondary, custom_id=letter)

        async def callback(self, interaction: discord.Interaction):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("This isn't your game.", ephemeral=True)
            self.disabled = True
            view.guess = self.label.lower()
            await interaction.response.edit_message(view=view)
            view.stop()

    embed = discord.Embed(
        title="🎮 Hangman Started!",
        description=f"```{HANGMAN_PICS[0]}```\nWord: {' '.join(display)}\nLives left: {lives}",
        color=discord.Color.red()
    )

    view = LetterView()
    message = await ctx.send(embed=embed, view=view)

    while lives > 0 and "_" in display:
        view = LetterView()
        await message.edit(view=view)
        await view.wait()

        guess = getattr(view, "guess", None)
        if not guess or guess in guessed:
            continue

        guessed.add(guess)
        if guess in word:
            for i, c in enumerate(word):
                if c == guess:
                    display[i] = guess
        else:
            lives -= 1

        stage = HANGMAN_PICS[len(HANGMAN_PICS) - 1 - lives]
        embed.description = f"```{stage}```\nWord: {' '.join(display)}\nLives left: {lives}"
        await message.edit(embed=embed, view=None)

    cooldown_time = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    if "_" not in display:
        await users.update_one(
            {"_id": user_id},
            {"$inc": {"wallet": 10000}, "$set": {"cooldowns.hangman": cooldown_time}},
            upsert=True
        )
        await ctx.send(f"🎉 **You won!** The word was **{word}**\n💰 You earned **5000 🥖**!")
    else:
        await users.update_one(
            {"_id": user_id},
            {"$set": {"cooldowns.hangman": cooldown_time}},
            upsert=True
        )
        await ctx.send(f"💀 **Game Over!** The word was **{word}**.")
        
        await users.update_one(
    {"_id": str(ctx.author.id)},
    {"$set": {"cooldowns.hangman": datetime.utcnow().isoformat()}}
)



@bot.command(aliases=["command", "commands", "cmd"])
async def help(ctx):
    # === Page 1: Games ===
    games_embed = discord.Embed(
        title="🎮 Game Commands",
        description="Play games to win 🥖 or challenge friends!",
        color=discord.Color.green()
    )
    games_embed.add_field(name="🃏 Competitive Games", value="""
`;uno <bet> @user1 @user2...` - Play UNO with betting  
`;blackjack <bet>` - Blackjack vs bot  
`;rps <bet> @user` - Rock Paper Scissors vs user  
`;tictactoe <bet> @user` - Tic-Tac-Toe duel  
`;connect4 <bet> @user` - Connect 4 with bread bets  

""", inline=False)
    games_embed.add_field(name="🧠 Guessing & Trivia", value="""
`;hangman` - Guess a 4-letter word in 6 tries  
`;trivia` - Answer trivia questions to win 🥖  
""", inline=False)

    # === Page 2: Gambling ===
    gambling_embed = discord.Embed(
        title="🎰 Solo Gambling Games",
        description="Test your luck and win 🥖!",
        color=discord.Color.gold()
    )
    gambling_embed.add_field(name="🎰 Solo Games", value="""
`;slot <bet>` - Spin the slot machine for prizes  
`;landmine <bet>` - Click tiles and cash out before hitting a mine!  
`;roulette <bet>` - Bet on red, black, even, or specific numbers  
`;coinflip <bet>` - Heads or tails?  
`;dice <bet>` - Roll a die against the bot  
`;treasurehunt` or `;th` - Dig up random treasures daily. Be careful! This could be deadly!  
""", inline=False)

    # === Page 3: Economy ===
    economy_embed = discord.Embed(
        title="💰 Economy Commands",
        description="Manage your bread and grind the economy!",
        color=discord.Color.blurple()
    )
    economy_embed.add_field(name="💸 Bread Economy", value="""
`;work` - Earn 1000–5000 🥖 every hour  
`;daily` - Earn 5000–10000 🥖 every 24h  
`;weekly` - Earn 10000–20000 🥖 every 7d  
`;rob @user` - Attempt to rob (60% fail rate)  
`;pay @user <amount>` - Pay someone  
`;deposit <amount>` - Deposit up to 50% of wallet  
`;depositmax` - Deposit full 50% automatically  
`;withdraw <amount> ` - Withdraw bread from bank
`;balance` - Check wallet & bank  
`;cooldowns` - View your active cooldowns  
""", inline=False)

    # === Page 4: Fun & Extras ===
    fun_embed = discord.Embed(
        title="🎲 Fun & Extra Commands",
        description="Non-economy extras and cool tools.",
        color=discord.Color.purple()
    )
    fun_embed.add_field(name="🎲 Fun & Extras", value="""
`;fact` - Get a random fun fact  
`;8ball <question>` - Ask the magic 8-ball  
`;roast @user` - Roast a user  
`;inv` / `;items` / `;inventory` - View your items  
`;buy <item>` - Purchase an item from the shop  
`;use <item>` - Use one of your items  
`;lotto` - View lottery info and prize pool  
`;lottobuy <#>` - Buy weekly lottery tickets (max 5)  
""", inline=False)

    # === View with Buttons ===
    class HelpView(View):
        def __init__(self):
            super().__init__(timeout=None)
            self.embeds = [games_embed, gambling_embed, economy_embed, fun_embed]

        @discord.ui.button(label="🎮 Games", style=discord.ButtonStyle.green)
        async def show_games(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("❌ Not your help menu.", ephemeral=True)
            await interaction.response.edit_message(embed=self.embeds[0], view=self)

        @discord.ui.button(label="🎰 Gambling", style=discord.ButtonStyle.blurple)
        async def show_gambling(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("❌ Not your help menu.", ephemeral=True)
            await interaction.response.edit_message(embed=self.embeds[1], view=self)

        @discord.ui.button(label="💰 Economy", style=discord.ButtonStyle.gray)
        async def show_economy(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("❌ Not your help menu.", ephemeral=True)
            await interaction.response.edit_message(embed=self.embeds[2], view=self)

        @discord.ui.button(label="🎲 Fun + Extra", style=discord.ButtonStyle.red)
        async def show_fun(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("❌ Not your help menu.", ephemeral=True)
            await interaction.response.edit_message(embed=self.embeds[3], view=self)

    await ctx.send(embed=games_embed, view=HelpView())


# ================================
# ======= CREATOR_IDS ONLY =======
# ================================

@bot.command()
async def admin(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    embed = discord.Embed(
        title="🔒 Raiko Owner Commands",
        description="These commands are only available to bot creators.",
        color=discord.Color.red()
    )

    embed.add_field(name="🔇 User Control", value="""
`;raikomute @user` — Mute a user from using the bot  
`;raikoum @user` — Unmute a user  
`;muted` — List all muted users  
""", inline=False)

    embed.add_field(name="🚫 Global Control", value="""
`;lockdown` — Disable all bot commands for everyone  
`;unlock` — Re-enable all commands  
""", inline=False)

    embed.add_field(name="🔁 Cooldown Management", value="""
`;resetcd` — Reset all cooldowns for all users  
`;resetweekly` — Reset just the `;weekly` cooldown  
""", inline=False)

    embed.add_field(name="🎟️ Lottery Tools", value="""
`;forcelotto` — Force a lottery draw manually  
""", inline=False)

    embed.add_field(name="⚙️ Command Toggles", value="""
`;disable <command>` — Disable a specific command  
`;enable <command>` — Re-enable a disabled command  
""", inline=False)

    await ctx.send(embed=embed)

# === Global Lockdown + Mute Middleware ===

@bot.command()
async def muted(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    # Query all muted users
    muted_users = users.find({"muted": True})
    mentions = []

    async for user in muted_users:
        user_id = int(user["_id"])
        mentions.append(f"<@{user_id}>")

    if not mentions:
        return await ctx.send("✅ No users are currently muted.")

    # Send list in an embed
    embed = discord.Embed(
        title="🔇 Muted Users",
        description="\n".join(mentions),
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)


@bot.check
async def globally_block_commands(ctx):
    # always allow these admin commands
    if ctx.command and ctx.command.name in (
        "raikomute", "raikoum",
        "lockdown", "unlock",
        "resetcd", "resetweekly"
    ):
        return True

    # check lockdown flag
    settings = await bot_settings.find_one({"_id":"config"}) or {}
    if settings.get("lockdown", False) and ctx.author.id not in CREATOR_IDS:
        await ctx.send("Bot is currently in lockdown!")
        return False

    # mute check
    is_muted = await users.find_one({"_id": str(ctx.author.id), "muted": True})
    if is_muted:
        raise commands.CheckFailure("❌ You are muted and cannot use bot commands.")
    return True


# === Mute User ===
@bot.command()
async def raikomute(ctx, member: discord.Member):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    if member.id in CREATOR_IDS:
        return await ctx.send("bro you tryna mute my creator? go fuck yourself haha")

    await users.update_one(
        {"_id": str(member.id)},
        {"$set": {"muted": True}},
        upsert=True
    )
    await ctx.send(f"🔇 {member.mention} has been muted from using bot commands.")

# === Unmute User ===
@bot.command()
async def raikoum(ctx, member: discord.Member):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    await users.update_one({"_id": str(member.id)}, {"$unset": {"muted": ""}})
    await ctx.send(f"🔊 {member.mention} can now use bot commands again.")


# === Lockdown (Global Disable) ===
@bot.command()
async def lockdown(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    settings = await bot_settings.find_one({"_id": "config"})
    if settings and settings.get("lockdown", False):
        return await ctx.send("🚫 The bot is already in **lockdown** mode.")

    class ConfirmLockdown(View):
        @discord.ui.button(label="✅ Confirm Lockdown", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can confirm.", ephemeral=True)
            await bot_settings.update_one({"_id": "config"}, {"$set": {"lockdown": True}}, upsert=True)
            await interaction.response.edit_message(content="🚫 Bot is now in **lockdown**. Only creators can use commands.", view=None)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can cancel.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Lockdown cancelled.", view=None)

    await ctx.send("⚠️ Are you sure you want to enable **lockdown** mode?", view=ConfirmLockdown())


# === Unlock (Global Enable) ===
@bot.command()
async def unlock(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    settings = await bot_settings.find_one({"_id": "config"})
    if settings and not settings.get("lockdown", False):
        return await ctx.send("🔓 The bot is already **unlocked**.")

    class ConfirmUnlock(View):
        @discord.ui.button(label="✅ Confirm Unlock", style=discord.ButtonStyle.success)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can confirm.", ephemeral=True)
            await bot_settings.update_one({"_id": "config"}, {"$set": {"lockdown": False}}, upsert=True)
            await interaction.response.edit_message(content="✅ Bot is now **unlocked**. Everyone can use commands again.", view=None)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can cancel.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Unlock cancelled.", view=None)

    await ctx.send("⚠️ Are you sure you want to **unlock** the bot?", view=ConfirmUnlock())


# === Reset All Cooldowns ===
@bot.command()
async def resetcd(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    class ConfirmResetCD(View):
        @discord.ui.button(label="✅ Reset All Cooldowns", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can confirm.", ephemeral=True)

            await interaction.response.defer()  # Prevent "interaction failed"

            result = await users.update_many({}, {
                "$unset": {
                    "cooldowns": ""
                }
            })

            await interaction.edit_original_response(
                content=f"♻️ Reset **all cooldowns** for {result.modified_count} users.",
                view=None
            )

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can cancel.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Cooldown reset cancelled.", view=None)

    await ctx.send("⚠️ Are you sure you want to reset **all user cooldowns**?", view=ConfirmResetCD())


# === Reset Only Weekly Cooldown ===
@bot.command()
async def resetweekly(ctx):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot owner(s) can use this command.")

    class ConfirmResetWeekly(View):
        @discord.ui.button(label="✅ Confirm Reset Weekly", style=discord.ButtonStyle.success)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can confirm.", ephemeral=True)
            count = users.update_many({}, {"$unset": {
                "cooldowns.weekly": ""
            }}).modified_count
            await interaction.response.edit_message(content=f"🔁 Reset `;weekly` cooldown for {count} users.", view=None)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Only the command issuer can cancel.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Weekly cooldown reset cancelled.", view=None)

    await ctx.send("⚠️ Are you sure you want to reset all **weekly** cooldowns?", view=ConfirmResetWeekly())



# ============================
# COMMAND: 8BALL
# ============================

@bot.command(name="8ball")
async def eight_ball(ctx, *, question: str):
    responses = [
        "Yes.", "No.", "Absolutely!", "Never.", "Maybe.", "Ask again later.",
        "It is certain.", "Very doubtful.", "Without a doubt.", "Better not tell you now."
    ]
    await ctx.send(f"🎱 {random.choice(responses)}")

# ============================
# FACTS / JOKES / FUN FACTS
# ============================

@bot.command()
async def fact(ctx):
    facts = [
        "Octopuses have three hearts.",
        "Bananas are berries, but strawberries aren't.",
        "Honey never spoils.",
        "Wombat poop is cube-shaped.",
        "There are more stars in the universe than grains of sand on Earth.",
        "A group of flamingos is called a 'flamboyance'.",
        "Humans share 60% of their DNA with bananas.",
        "Honey never spoils. Archaeologists have found pots of honey in ancient tombs that are still edible."
    ]
    await ctx.send(f"📚 Fun Fact: {random.choice(facts)}")


@bot.command()
async def joke(ctx):
    jokes = [
        "Why don’t skeletons fight each other? They don’t have the guts.",
        "What did the ocean say to the beach? Nothing, it just waved.",
        "Why don’t scientists trust atoms? Because they make up everything!",
        "I'm reading a book on anti-gravity. It's impossible to put down."
    ]
    await ctx.send("😂 " + random.choice(jokes))


@bot.command(aliases=["bj"])
async def blackjack(ctx, bet: int):
    import traceback

    print(f"[DEBUG] Blackjack started by {ctx.author.display_name}, bet = {bet}")

    user_data = await get_user(ctx.author.id)
    if bet <= 0:
        return await ctx.send("❌ Bet must be a positive number.")
    if user_data["wallet"] < bet:
        return await ctx.send("❌ You don't have enough 🥖 to place that bet.")

    print(f"[DEBUG] {ctx.author.display_name} wallet before deduction: {user_data['wallet']}")
    user_data["wallet"] -= bet
    await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"wallet": user_data["wallet"]}})
    print(f"[DEBUG] Deducted {bet} 🥖. New wallet: {user_data['wallet']}")

    values = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    suits = ['♦️', '♣️', '♥️', '♠️']
    def draw_card():
        card = random.choice(values) + random.choice(suits)
        print(f"[DEBUG] Drew card: {card}")
        return card
    def extract_value(card):
        for v in values:
            if card.startswith(v):
                return v
        return card[0]
    def calc(hand):
        total = 0
        aces = 0
        for card in hand:
            val = extract_value(card)
            if val in ['J', 'Q', 'K']:
                total += 10
            elif val == 'A':
                total += 11
                aces += 1
            else:
                total += int(val)
        while total > 21 and aces:
            total -= 10
            aces -= 1
        return total

    player = [draw_card(), draw_card()]
    dealer = [draw_card(), draw_card()]
    print(f"[DEBUG] Player hand: {player} ({calc(player)}), Dealer hand: {dealer} ({calc(dealer)})")

    view = View()
    msg = await ctx.send("⏳ Starting Blackjack...")

    async def update_message():
        try:
            await msg.edit(
                content=f"🃏 Your hand: {' '.join(player)} ({calc(player)})\nDealer shows: {dealer[0]}",
                view=view
            )
            print("[DEBUG] Updated game message with current player hand.")
        except Exception as e:
            print(f"[ERROR] Failed to update message in update_message: {e}")
            traceback.print_exc()

    async def end_game(result):
        try:
            print(f"[DEBUG] Entered end_game with result: {result}")
            player_total = calc(player)
            dealer_total = calc(dealer)
            print(f"[DEBUG] Final player: {player_total}, dealer: {dealer_total}")
            print(f"[DEBUG] Final player hand: {player}, dealer hand: {dealer}")
            
            if "stats" not in user_data:
                user_data["stats"] = {}

            if "blackjack" not in user_data["stats"]:
                user_data["stats"]["blackjack"] = {"wins": 0, "losses": 0}
                
            stats = user_data["stats"]["blackjack"]


            content = (
                f"🃏 Final hands:\n"
                f"**You:** {' '.join(player)} (**{player_total}**)\n"
                f"**Dealer:** {' '.join(dealer)} (**{dealer_total}**)\n"
            )

            if result == "win":
                stats["wins"] += 1
                user_data["wallet"] += bet * 2
                CONTENT += f"\n🎉 You win! Gained 🥖 **{bet:,}**"
                print("[DEBUG] Player wins. Bread added.")
            elif result == "lose":
                stats["losses"] += 1
                content += f"\n😢 You lose! Lost 🥖 **{bet:,}**"
                print("[DEBUG] Player loses. No refund.")
            else:
                user_data["wallet"] += bet
                content += "\n🤝 It's a tie! Bet refunded."
                print("[DEBUG] Tie. Bet refunded.")

            await update_user(ctx.author.id, {
                "wallet": user_data["wallet"],
                "stats.blackjack": user_data["stats"]["blackjack"]
            })

            print("[DEBUG] User data updated in DB.")

            for child in view.children:
                child.disabled = True

            await msg.edit(content=content, view=view)
            print("[DEBUG] Final game message sent successfully.")
        except Exception as e:
            print(f"[ERROR] Exception in end_game: {e}")
            traceback.print_exc()

    async def hit(interaction):
        if interaction.user != ctx.author:
            return await interaction.response.send_message("It's not your game!", ephemeral=True)
        await interaction.response.defer()
        player.append(draw_card())
        score = calc(player)
        print(f"[DEBUG] Player score after hit: {score}")
        if score > 21:
            print("[DEBUG] Player busted! Calling end_game('lose')")
            await end_game("lose")
        elif score == 21:
            print("[DEBUG] Player hit 21! Auto-stand triggered.")
            await stand(interaction)
        else:
            await update_message()

    async def stand(interaction):
        if interaction.user != ctx.author:
            return await interaction.response.send_message("It's not your game!", ephemeral=True)
        await interaction.response.defer()
        print(f"[DEBUG] {ctx.author.display_name} stands. Dealer reveals hand.")

        try:
            while calc(dealer) < 17:
                new_card = draw_card()
                dealer.append(new_card)
                print(f"[DEBUG] Dealer drew {new_card}. Dealer total now: {calc(dealer)}")

            p = calc(player)
            d = calc(dealer)

            print(f"[DEBUG] Comparing final hands. Player: {p}, Dealer: {d}")
            if d > 21 or p > d:
                await end_game("win")
            elif p < d:
                await end_game("lose")
            else:
                await end_game("tie")
        except Exception as e:
            print(f"[ERROR] Exception in stand(): {e}")
            traceback.print_exc()

    btn_hit = Button(label="Hit", style=discord.ButtonStyle.success)
    btn_stand = Button(label="Stand", style=discord.ButtonStyle.primary)
    btn_hit.callback = hit
    btn_stand.callback = stand
    view.add_item(btn_hit)
    view.add_item(btn_stand)

    try:
        await msg.edit(
            content=f"🃏 Your hand: {' '.join(player)} ({calc(player)})\nDealer shows: {dealer[0]}",
            view=view
        )
        print("[DEBUG] Blackjack game started. Message sent with buttons.")
    except Exception as e:
        print(f"[ERROR] Failed to send initial game message: {e}")
        traceback.print_exc()
    


@bot.command()
async def rps(ctx, bet: int, opponent: discord.Member):
    print(f"[DEBUG] RPS command invoked by {ctx.author.display_name} vs {opponent.display_name}, bet = {bet}")

    if opponent.bot or opponent == ctx.author:
        return await ctx.send("❌ Invalid opponent.")

    if bet <= 0:
        return await ctx.send("❗ Usage: `;rps @user <bet>` — bet must be positive.")

    author_data = await get_user(ctx.author.id)
    opponent_data = await get_user(opponent.id)
    print(f"[DEBUG] {ctx.author.display_name} wallet: {author_data['wallet']}")
    print(f"[DEBUG] {opponent.display_name} wallet: {opponent_data['wallet']}")

    if author_data["wallet"] < bet:
        return await ctx.send("❌ You don’t have enough 🥖.")
    if opponent_data["wallet"] < bet:
        return await ctx.send(f"❌ {opponent.display_name} doesn’t have enough 🥖.")

    class ConfirmView(View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
        async def accept(self, interaction: discord.Interaction, button: Button):
            if interaction.user != opponent:
                return await interaction.response.send_message("❌ You're not the invited player.", ephemeral=True)
            self.value = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="✅ Match accepted!", view=self)
            self.stop()

        @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
        async def decline(self, interaction: discord.Interaction, button: Button):
            if interaction.user != opponent:
                return await interaction.response.send_message("❌ You're not the invited player.", ephemeral=True)
            self.value = False
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Match declined.", view=self)
            self.stop()

    confirm = ConfirmView()
    confirm_msg = await ctx.send(
        f"🎮 {opponent.mention}, do you accept the RPS match with a **{bet:,} 🥖** bet?",
        view=confirm
    )
    await confirm.wait()

    if confirm.value is None:
        return await confirm_msg.edit(content="⌛ No response. Game canceled.", view=None)
    if confirm.value is False:
        return await confirm_msg.edit(content=f"❌ {opponent.mention} declined the match.", view=None)

    # Deduct bets up front
    author_data["wallet"] -= bet
    opponent_data["wallet"] -= bet
    await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"wallet": author_data["wallet"]}})
    await users.update_one({"_id": str(opponent.id)}, {"$set": {"wallet": opponent_data["wallet"]}})
    print(f"[DEBUG] Deducted {bet} 🥖 from both players.")

    results = {}
    choices = ["🪨", "📄", "✂️"]

    async def decide():
        try:
            c1 = results[ctx.author.id]
            c2 = results[opponent.id]
            print(f"[DEBUG] {ctx.author.display_name} = {c1}, {opponent.display_name} = {c2}")

            outcome = {
                ("🪨", "✂️"): ctx.author,
                ("✂️", "📄"): ctx.author,
                ("📄", "🪨"): ctx.author,
                ("✂️", "🪨"): opponent,
                ("📄", "✂️"): opponent,
                ("🪨", "📄"): opponent,
            }

            if c1 == c2:
                author_data["wallet"] += bet
                opponent_data["wallet"] += bet
                await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"wallet": author_data["wallet"]}})
                await users.update_one({"_id": str(opponent.id)}, {"$set": {"wallet": opponent_data["wallet"]}})
                await game_msg.edit(content=f"🤝 It’s a tie! Both chose {c1}", view=None)
                print(f"[DEBUG] Tie — both refunded.")
            else:
                winner = outcome.get((c1, c2))
                loser = opponent if winner == ctx.author else ctx.author
                print(f"[DEBUG] Winner: {winner.display_name}, Loser: {loser.display_name}")

                winner_data = await get_user(winner.id)
                loser_data = await get_user(loser.id)

                winner_data.setdefault("stats", {}).setdefault("rps", {}).setdefault("wins", 0)
                loser_data.setdefault("stats", {}).setdefault("rps", {}).setdefault("losses", 0)

                winner_data["stats"]["rps"]["wins"] += 1
                loser_data["stats"]["rps"]["losses"] += 1

                await users.update_one(
                    {"_id": str(winner.id)},
                    {
                        "$inc": {
                            "wallet": bet * 2,
                            "stats.rps.wins": 1
                        }
                    },
                    upsert=True
                )
                await users.update_one(
                    {"_id": str(loser.id)},
                    {
                        "$inc": {
                            "stats.rps.losses": 1
                        }
                    },
                    upsert=True
                )


                await game_msg.edit(content=f"🏆 {winner.mention} won **{bet * 2:,} 🥖** {c1} vs {c2}", view=None)
                print(f"[DEBUG] Sent win message to channel.")

            view.stop()
            print("[DEBUG] View stopped.")
        except Exception as e:
            print(f"[ERROR] Exception during RPS result resolution: {e}")

    class RPSButton(Button):
        def __init__(self, emoji):
            super().__init__(emoji=emoji, style=discord.ButtonStyle.primary)
            self.choice = emoji

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id not in [ctx.author.id, opponent.id]:
                return await interaction.response.send_message("❌ You're not part of this game.", ephemeral=True)

            if interaction.user.id in results:
                return await interaction.response.send_message("❗ You've already made your choice.", ephemeral=True)

            results[interaction.user.id] = self.choice
            print(f"[DEBUG] {interaction.user.display_name} clicked {self.choice}")
            await interaction.response.send_message(f"✅ You picked {self.choice}", ephemeral=True)

            if len(results) == 2:
                print(f"[DEBUG] Both players made choices: {results}")
                await decide()

    view = View(timeout=30)
    for emoji in choices:
        view.add_item(RPSButton(emoji))

    game_msg = await ctx.send(
        f"🎮 **Rock Paper Scissors**\n"
        f"{ctx.author.mention} vs {opponent.mention}\n"
        f"Choose your move!",
        view=view
    )



active_trivia = {}
trivia_answers = {}

# ────────────────────────────────────────────────────
# COMMAND: STOP TRIVIA (Admin Only)
# ────────────────────────────────────────────────────
@bot.command()
@commands.has_permissions(administrator=True)
async def stoptrivia(ctx):
    """Stops any active trivia in this channel."""
    chan = ctx.channel.id
    if chan not in active_trivia:
        return await ctx.send("⚠️ No active trivia to stop here.")

    # Remove game state
    del active_trivia[chan]
    trivia_answers.pop(chan, None)
    await ctx.send("🛑 Trivia game has been stopped by an administrator.")

    
# ────────────────────────────────────────────────────
# COMMAND: STOP UNO (Admin Only)
# ────────────────────────────────────────────────────

@bot.command()
@commands.has_permissions(administrator=True)
async def stopuno(ctx):
    """
    Forcefully stops the UNO game in the current channel.
    """
    try:
        for game in active_uno_games:
            if game.ctx.channel.id == ctx.channel.id:
                # Cancel turn timer if it's running
                if hasattr(game, "timer_task") and game.timer_task:
                    game.timer_task.cancel()

                # Mark the game as ended
                game.ended = True

                # Disable interaction view if it's still visible
                if game.message:
                    if game.message.components:
                        try:
                            for item in game.message.components[0].children:
                                item.disabled = True
                            await game.message.edit(view=None)
                        except:
                            pass

                # Remove game from active list
                active_uno_games.remove(game)

                await ctx.send("🛑 UNO game has been forcefully stopped.")
                return

        await ctx.send("❌ No active UNO game in this channel.")

    except Exception:
        print("[UNO ERROR] stopuno command failed.")
        import traceback
        traceback.print_exc()
        await ctx.send("⚠️ Failed to stop the game due to an internal error.")


# ────────────────────────────────────────────────────
# COMMAND: TRIVIA (with previous-round winners embed and bread mention)
# ────────────────────────────────────────────────────

@bot.command()
async def trivia(ctx):
    if ctx.channel.id != 1399899594757767340:
        return await ctx.send("❌ Trivia can only be played in <#1399899594757767340>.")

    global unused_trivia_questions
    messages_to_delete = []

    # 1) Load local JSON questions
    try:
        with open("trivia_questions.json", "r") as f:
            trivia_data = json.load(f)
        if not trivia_data or not isinstance(trivia_data, list):
            msg = await ctx.send("❗ `trivia_questions.json` must contain a non-empty array of questions.")
            messages_to_delete.append(msg)
            return
    except FileNotFoundError:
        msg = await ctx.send("❗ Couldn’t find `trivia_questions.json`.")
        messages_to_delete.append(msg)
        return
    except json.JSONDecodeError as e:
        msg = await ctx.send(f"❗ Error parsing `trivia_questions.json`: {e}")
        messages_to_delete.append(msg)
        return

    # 2) Cooldown check
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id}) or {}
    cds = user.get("cooldowns", {})
    now = datetime.utcnow()
    if "trivia" in cds:
        try:
            cd_time = datetime.fromisoformat(cds["trivia"])
            if now < cd_time:
                sec = int((cd_time - now).total_seconds())
                h, m = sec // 3600, (sec % 3600) // 60
                msg = await ctx.send(f"⏳ Wait **{h}h {m}m** before starting Trivia again.")
                messages_to_delete.append(msg)
                return
        except:
            pass

    # 3) Prevent concurrent games
    if ctx.channel.id in active_trivia:
        msg = await ctx.send("⚠️ A trivia game is already running here.")
        messages_to_delete.append(msg)
        return

    # 4) Join phase
    players = {ctx.author.id: 0}
    view = View(timeout=None)
    class JoinButton(Button):
        def __init__(self):
            super().__init__(label="Join Trivia", style=discord.ButtonStyle.green)

        async def callback(self, interaction):
            uid = interaction.user.id
            if uid not in players:
                players[uid] = 0
                await interaction.response.send_message("✅ Joined trivia!", ephemeral=True)
            else:
                await interaction.response.send_message("❗ Already joined.", ephemeral=True)
    view.add_item(JoinButton())

    msg = await ctx.send("🎮 Trivia starts in **30s**! Click to join.", view=view)
    messages_to_delete.append(msg)
    await asyncio.sleep(10); msg = await ctx.send("⏳ 20s left to join!"); messages_to_delete.append(msg)
    await asyncio.sleep(10); msg = await ctx.send("⏳ 10s left to join!"); messages_to_delete.append(msg)
    await asyncio.sleep(10)
    msg = await ctx.send(f"✅ Starting with {len(players)} player(s)! Use `;a <A|B|C|D>`.")
    messages_to_delete.append(msg)

    # 5) Mark active & set cooldown
    active_trivia[ctx.channel.id] = {
        "players": players,
        "answers": {},
        "host": ctx.author.id,
        "last_winners": []
    }

    # 6) Question loop
    for q_num in range(1, 21):
        active_trivia[ctx.channel.id]["answers"] = {}

        if not unused_trivia_questions:
            unused_trivia_questions = ALL_TRIVIA_QUESTIONS.copy()
            random.shuffle(unused_trivia_questions)

        qobj = unused_trivia_questions.pop()
        question = html.unescape(qobj["question"])
        correct  = html.unescape(qobj["answer"])

        options = [html.unescape(opt) for opt in qobj.get("options", [])]
        random.shuffle(options)

        letters        = ["A", "B", "C", "D"]
        mapping        = dict(zip(letters, options))
        correct_letter = letters[options.index(correct)]

        class AnswerSelect(Select):
            def __init__(self, mapping):
                opts = [
                    SelectOption(label=text, value=letter)
                    for letter, text in mapping.items()
                ]
                super().__init__(
                    placeholder="Choose your answer…",
                    min_values=1, max_values=1,
                    options=opts
                )

            async def callback(self, interaction):
                chan = interaction.channel.id
                uid  = interaction.user.id
                game = active_trivia.get(chan)
                if not game or uid not in game["players"]:
                    return await interaction.response.send_message("❌ You’re not in this trivia.", ephemeral=True)
                if uid in game["answers"]:
                    return await interaction.response.send_message("❗ You already answered.", ephemeral=True)
                choice = self.values[0]
                game["answers"][uid] = choice
                await interaction.response.send_message(f"✅ You chose **{mapping[choice]}**", ephemeral=True)

        view = View(timeout=30)
        view.add_item(AnswerSelect(mapping))

        opts_text = "\n".join(mapping[L] for L in letters)
        embed = discord.Embed(
            title=f"Question {q_num}/20",
            description=question,
            color=discord.Color.blue()
        )
        embed.add_field(name="Choices", value=opts_text, inline=False)
        msg = await ctx.send(embed=embed, view=view)
        messages_to_delete.append(msg)

        await asyncio.sleep(10); msg = await ctx.send("⏳ 20s left to answer!"); messages_to_delete.append(msg)
        await asyncio.sleep(10); msg = await ctx.send("⏳ 10s left to answer!"); messages_to_delete.append(msg)
        await asyncio.sleep(10)

        winners = []
        for uid in players:
            ans = active_trivia[ctx.channel.id]["answers"].get(uid, "")
            if ans == correct_letter:
                players[uid] += 1
                await users.update_one(
                    {"_id": str(uid)},
                    {"$inc": {"wallet": qobj.get("points", 1000)}},
                    upsert=True
                )
                winners.append(f"<@{uid}> (+{qobj.get('points', 1000)} 🥖)")

        active_trivia[ctx.channel.id]["last_winners"] = winners
        win_text = ", ".join(winners) if winners else "No one"
        msg = await ctx.send(f"✅ Correct: **{correct_letter}** — Winners: {win_text}")
        messages_to_delete.append(msg)

    final = sorted(players.items(), key=lambda x: x[1], reverse=True)
    board = "\n".join(f"{i+1}. <@{uid}> — **{pts}**" for i, (uid, pts) in enumerate(final))
    msg = await ctx.send("🏁 **Trivia Over!**\n" + board)
    messages_to_delete.append(msg)

    if len(players) >= 3:
        prizes = [10000, 6000, 3000]
        podium = []
        for i in range(min(3, len(final))):
            uid, _ = final[i]
            await users.update_one(
                {"_id": str(uid)},
                {"$inc": {"wallet": prizes[i]}}, upsert=True
            )
            podium.append(f"{['🥇','🥈','🥉'][i]} +{prizes[i]} 🥖")
        msg = await ctx.send("🏆 Podium Prizes:\n" + "\n".join(podium))
        messages_to_delete.append(msg)

        del active_trivia[ctx.channel.id]
    trivia_answers.pop(ctx.channel.id, None)

    await users.update_one(
        {"_id": user_id},
        {"$set": {"cooldowns.trivia": now.isoformat()}},
        upsert=True
    )

    await asyncio.sleep(5)
    try:
        def not_pinned(m):
            return not m.pinned

        await ctx.channel.purge(limit=1000, check=not_pinned)
    except Exception as e:
        print(f"[ERROR] Failed to purge messages: {e}")



@bot.command()
async def pay(ctx, member: discord.Member, amount: int):
    if member.bot or member == ctx.author:
        return await ctx.send("❌ Invalid recipient.")

    sender_data = await get_user(ctx.author.id)
    receiver_data = await get_user(member.id)

    if sender_data["wallet"] < amount or amount <= 0:
        return await ctx.send("❌ Not enough 🥖 or invalid amount.")

    sender_new_wallet = sender_data["wallet"] - amount
    receiver_new_wallet = receiver_data["wallet"] + amount

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"wallet": sender_new_wallet}}
    )
    await users.update_one(
        {"_id": str(member.id)},
        {"$set": {"wallet": receiver_new_wallet}}
    )

    await ctx.send(f"✅ {ctx.author.mention} paid {member.mention} 🥖 {amount}.")
    

# ============================
# COMMAND: ;gen (Genereate bread, CREATOR_IDS only)
# ============================

@bot.command(name="gen")
async def gen(ctx, amount: int, member: discord.Member = None):
    """
    Allows the bot creator to generate an unlimited amount of 🥖 bread.
    Usage: ;gen <amount> [@user]
    If no user is mentioned, bread is generated for the command issuer.
    """
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot creator can generate bread.")

    target = member or ctx.author

    await users.update_one(
        {"_id": str(target.id)},
        {"$inc": {"wallet": amount}},
        upsert=True
    )

    await ctx.send(f"✅ Granted **{amount} 🥖** to {target.mention}!")

# ============================
# COMMAND: ;top
# ============================

@bot.command()
async def top(ctx):
    top_users = users.find().sort("wallet", -1).limit(20)
    leaderboard = []
    rank = 1
    async for user_data in top_users:
        member = ctx.guild.get_member(int(user_data["_id"]))
        name = member.display_name if member else f"<@{user_data['_id']}>"
        balance = user_data.get("wallet", 0)
        leaderboard.append(f"**{rank}.** {name} - 🥖 {balance:,}")
        rank += 1

    embed = discord.Embed(title="🏆 Top 20 Richest Users", description="\n".join(leaderboard), color=0xFFD700)
    await ctx.send(embed=embed)


# ============================
# COMMAND: ;beg <@user>
# ============================

@bot.command()
async def beg(ctx, target: discord.Member):
    if target.bot or target == ctx.author:
        return await ctx.send("❌ Invalid target.")

    view = View()
    accepted = []

    class Accept(Button):
        def __init__(self):
            super().__init__(label="yes", style=discord.ButtonStyle.success)

        async def callback(self, interaction):
            if interaction.user != target:
                return await interaction.response.send_message("Not your button.", ephemeral=True)
            accepted.append(True)
            await interaction.response.send_message("🥖 Take my bread...", ephemeral=True)
            giver = await get_user(target.id)
            receiver = await get_user(ctx.author.id)
            amount = random.randint(1000, 5000)
            if giver["wallet"] >= amount:
                giver["wallet"] -= amount
                receiver["wallet"] += amount
                await update_user(target.id, giver)
                await update_user(ctx.author.id, receiver)
                await ctx.send(f"{ctx.author.mention} begged and received 🥖 {amount} from {target.mention}")
            else:
                await ctx.send(f"{target.mention} is too broke to give you bread.")

    class Decline(Button):
        def __init__(self):
            super().__init__(label="fuck u", style=discord.ButtonStyle.danger)

        async def callback(self, interaction):
            if interaction.user != target:
                return await interaction.response.send_message("Not your button.", ephemeral=True)
            await interaction.response.send_message("❌ Denied.", ephemeral=True)
            await ctx.send(f"{target.mention} told {ctx.author.mention}: fuck u")

    view.add_item(Accept())
    view.add_item(Decline())

    await ctx.send(f"{ctx.author.mention} is begging {target.mention} for 🥖...\nRespond below:", view=view)

# ============================
# COMMAND: ;balance / ;bal
# ============================

@bot.command(aliases=["bal", "cash", "bread"])
async def balance(ctx, member: discord.Member = None):
    user = member or ctx.author
    user_data = await get_user(user.id)

    wallet = user_data.get("wallet", 0)
    bank = user_data.get("bank", 0)
    total = wallet + bank

    embed = discord.Embed(
        title=f"{user.display_name}'s Balance",
        description=f"💰 Wallet: 🥖 {wallet}\n🏦 Bank: 🥖 {bank}\n📊 Total: 🥖 {total}",
        color=discord.Color.gold()
    )
    await ctx.send(embed=embed)


# ============================
# COMMAND: ;8ball
# ============================

@bot.command()
async def eightball(ctx, *, question):
    responses = [
        "Yes.", "No.", "Maybe.", "Absolutely.", "Definitely not.",
        "Try again later.", "Without a doubt.", "I don't think so.",
        "It is certain.", "My reply is no."
    ]
    await ctx.send(f"🎱 {random.choice(responses)}")


# ============================
# GAME: Connect 4
# ============================

class Connect4View(View):
    def __init__(self, ctx, p1, p2, bet):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.p1 = p1
        self.p2 = p2
        self.bet = bet
        self.turn = p1
        self.board = [[0] * 7 for _ in range(6)]
        self.symbols = {p1.id: "🔴", p2.id: "🟡"}
        self.message = None

    def check_winner(self, row, col, player_id):
        print(f"[DEBUG] Running check_winner for row {row}, col {col}, player {player_id}")
        def count_direction(dr, dc):
            count = 1
            for step in (1, -1):
                r, c = row, col
                while True:
                    r += dr * step
                    c += dc * step
                    if 0 <= r < 6 and 0 <= c < 7 and self.board[r][c] == player_id:
                        count += 1
                    else:
                        break
            return count

        # Check all directions
        return any(
            count_direction(dr, dc) >= 4
            for dr, dc in [(0, 1), (1, 0), (1, 1), (1, -1)]
        )

    def is_full(self):
        return all(self.board[0][c] != 0 for c in range(7))

    async def update_message(self):
        rows = []
        for row in self.board:
            row_str = "".join(self.symbols.get(cell, "⚪") for cell in row)
            rows.append(row_str)
        content = "\n".join(rows)
        content += f"\n\n{self.turn.mention}'s turn ({self.symbols[self.turn.id]})"
        await self.message.edit(content=content, view=self)

    async def interaction_check(self, interaction):
        return interaction.user == self.turn

    async def make_move(self, col, interaction):
        print(f"[DEBUG] {self.turn.display_name} attempting to place in column {col}")

        for r in range(5, -1, -1):
            if self.board[r][col] == 0:
                self.board[r][col] = self.turn.id
                placed_row = r
                print(f"[DEBUG] Placed disc at row {r}, col {col}, player {self.turn.id}")
                break
        else:
            await interaction.response.send_message("❗ Column is full.", ephemeral=True)
            return
    
        print(f"[DEBUG] Running check_winner for row {placed_row}, col {col}, player {self.turn.id}")
        if self.check_winner(placed_row, col, self.turn.id):
            print(f"[DEBUG] Winner found: {self.turn.display_name}")
            try:
                await interaction.response.defer()
                print("[DEBUG] Deferred interaction")

                winner_user = self.turn
                loser = self.p2 if winner_user == self.p1 else self.p1
                print(f"[DEBUG] Winner: {winner_user.display_name}, Loser: {loser.display_name}")

                await increment_user(winner_user.id, "stats.connect4.wins", 1)
                print("[DEBUG] Updated winner stats")
                await increment_user(loser.id, "stats.connect4.losses", 1)
                print("[DEBUG] Updated loser stats")
                await increment_user(winner_user.id, "wallet", self.bet * 2)
                print("[DEBUG] Bread given to winner")

                await self.message.edit(
                    content=f"🏆 {winner_user.mention} wins Connect 4! +{self.bet * 2:,} 🥖",
                    view=None
                )
                print("[DEBUG] Message edited with win message")
    
                self.stop()
                print("[DEBUG] View stopped")
                return

            except Exception as e:
                print(f"[ERROR] Exception during win logic: {e}")
                return

        # Tie check
        if self.is_full():
            print("[DEBUG] Board is full. It's a tie.")
            await interaction.response.defer()
            await increment_user(self.p1.id, "wallet", self.bet)
            await increment_user(self.p2.id, "wallet", self.bet)
            await self.message.edit(content="🤝 It's a tie! Bets refunded.", view=None)
            self.stop()
            return

        # Switch turn
        self.turn = self.p1 if self.turn == self.p2 else self.p2
        print(f"[DEBUG] Turn switched to: {self.turn.display_name}")
        await self.update_message()
        await interaction.response.defer()




    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary)
    async def c1(self, interaction, button): await self.make_move(0, interaction)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary)
    async def c2(self, interaction, button): await self.make_move(1, interaction)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary)
    async def c3(self, interaction, button): await self.make_move(2, interaction)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary)
    async def c4(self, interaction, button): await self.make_move(3, interaction)

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary)
    async def c5(self, interaction, button): await self.make_move(4, interaction)

    @discord.ui.button(label="6", style=discord.ButtonStyle.secondary)
    async def c6(self, interaction, button): await self.make_move(5, interaction)

    @discord.ui.button(label="7", style=discord.ButtonStyle.secondary)
    async def c7(self, interaction, button): await self.make_move(6, interaction)

@bot.command(aliases=["c4"])
async def connect4(ctx, bet: int, opponent: discord.Member):
    if bet <= 0:
        return await ctx.send("❌ Bet must be a positive number.")
    if opponent == ctx.author:
        return await ctx.send("❌ You can't play against yourself.")

    p1_data = await get_user(ctx.author.id)
    p2_data = await get_user(opponent.id)

    if p1_data["wallet"] < bet:
        return await ctx.send("❌ You don't have enough 🥖 to place that bet.")
    if p2_data["wallet"] < bet:
        return await ctx.send(f"❌ {opponent.display_name} doesn't have enough 🥖 to accept the challenge.")

    class ConfirmConnect4(View):
        def __init__(self):
            super().__init__(timeout=30)
            self.accepted = False

        @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: Button):
            if interaction.user != opponent:
                return await interaction.response.send_message("Only the challenged player can accept.", ephemeral=True)
            self.accepted = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="✅ Challenge accepted! Starting game...", view=self)
            self.stop()

        @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: Button):
            if interaction.user != opponent:
                return await interaction.response.send_message("Only the challenged player can decline.", ephemeral=True)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="❌ Challenge declined.", view=self)
            self.stop()

    view = ConfirmConnect4()
    msg = await ctx.send(
        f"🎮 {opponent.mention}, do you accept the Connect 4 challenge from {ctx.author.mention} for {bet:,} 🥖?",
        view=view
    )
    await view.wait()

    if not view.accepted:
        return

    await increment_user(ctx.author.id, "wallet", -bet)
    await increment_user(opponent.id, "wallet", -bet)

    game_view = Connect4View(ctx, ctx.author, opponent, bet)
    content = f"Connect 4: {ctx.author.mention} (🔴) vs {opponent.mention} (🟡)\n{ctx.author.mention}'s turn (🔴)"
    msg = await ctx.send(content, view=game_view)
    game_view.message = msg


@bot.command()
async def test(ctx):
    await ctx.send("✅ test works")
    print("✅ test fired")

# =============================
# ======== LOOT CHEST =========
# =============================

active_chests = {}  # Global chest tracker

CHEST_TYPES = [
    {
        "key": "silver",
        "name": "Silver Chest",
        "color": 0xC0C0C0,
        "min": 500,
        "max": 1500,
        "weight": 40,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401441966813614141/dgifuzi-4e29b16f-aa74-4f1a-b597-f935e67e61a1.png?ex=68904a0a&is=688ef88a&hm=9da9b4a42e8d6884a8bed676aac30fb5a265ca35b1920938ad734e9d1c352ac3&"
    },
    {
        "key": "gold",
        "name": "Gold Chest",
        "color": 0xFFD700,
        "min": 1500,
        "max": 3000,
        "weight": 30,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401441996035588216/1000146375-removebg-preview.png?ex=68904a11&is=688ef891&hm=f0be893b204af79c3a7f90f7975115c5fab1c49ca3544bafa6cdb92e74eba620&"
    },
    {
        "key": "diamond",
        "name": "Diamond Chest",
        "color": 0x00E5FF,
        "min": 3000,
        "max": 6000,
        "weight": 20,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401442029652803696/1000146365-removebg-preview.png?ex=68904a19&is=688ef899&hm=26829d4a91f71bb4e180d14f411eedfe32c4b1efc6dc13993a1459b9c87539a0&"
    },
    {
        "key": "cursed",
        "name": "Cursed Chest",
        "color": 0x8B0000,
        "min": -3000,
        "max": -500,
        "weight": 10,
        "cursed": True,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401442089543270492/1000146368-removebg-preview.png?ex=68904a27&is=688ef8a7&hm=7b4d14b48237e60d3f027cd37e3b171b806300124e4e18c1bfa5baede9673f62&"
    },
]

def choose_chest():
    weights = [c["weight"] for c in CHEST_TYPES]
    return random.choices(CHEST_TYPES, weights=weights, k=1)[0]


@bot.command(name="forcechest")
@commands.has_permissions(administrator=True)
async def forcechest(ctx):
    global active_chests

    if ctx.channel.id in active_chests:
        return await ctx.send("⚠️ A chest is already active in this channel.")

    chest = choose_chest()

    embed = discord.Embed(
        title=f"A {chest['name']} appeared!",
        description="A chest just spawned! Type `!pick` first to claim it!",
        color=chest["color"]
    )
    embed.set_image(url=chest["image"])
    await ctx.send(embed=embed)

    active_chests[ctx.channel.id] = {
        **chest,
        "claimed": False
    }

    async def timeout_cleanup():
        await asyncio.sleep(30)
        if ctx.channel.id in active_chests and not active_chests[ctx.channel.id]["claimed"]:
            await ctx.send("⏳ The chest vanished. Nobody claimed it in time.")
            del active_chests[ctx.channel.id]

    asyncio.create_task(timeout_cleanup())


# ============================
# Start the bot (on_ready event)
# ============================

@bot.event
async def on_message(message):
    if not message.guild or message.author.bot:
        return

    raw = message.content.strip()
    content = raw.lower()

    # ========================
    # UNO "call uno" detection
    # ========================
    if content == "uno":
        for game in active_uno_games:
            if game.message and game.message.channel.id == message.channel.id:
                if message.author in game.players:
                    hand_size = len(game.hands.get(message.author, []))
                    if hand_size == 1:
                        if game.called_uno.get(message.author) is None:
                            game.called_uno[message.author] = True
                            await message.channel.send(f"📢 **{message.author.display_name}** called **UNO!!**")
                    elif hand_size > 1:
                        game.hands[message.author].append(game.deck.pop())
                        await message.channel.send(f"❌ **{message.author.display_name}** falsely called UNO and drew 1 penalty card.")
                        game.advance_turn()
                        await start_uno_game(bot, game)
                    return

    # ========================
    # Loot Chest System
    # ========================
    global active_chests
    channel = message.channel

    if content == "!pick":
        if channel.id in active_chests:
            chest = active_chests[channel.id]
            if chest["claimed"]:
                if message.author.id == chest.get("claimed_by"):
                    return  # don't roast the winner again
                if chest["cursed"]:
                    await channel.send(f"{message.author.mention} thank god you slow af, pay attention to what type of lootboxes are being spawned dickhead")
                else:
                    await channel.send(f"{message.author.mention} you were slow af on that one haha loser")
            else:
                chest["claimed"] = True
                chest["claimed_by"] = message.author.id  # track who claimed it

                claimer = message.author
                if chest["cursed"]:
                    amount = -random.randint(abs(chest["max"]), abs(chest["min"]))
                else:
                    amount = random.randint(chest["min"], chest["max"])


                await users.update_one(
                    {"_id": str(claimer.id)},
                    {"$inc": {"wallet": amount}},
                    upsert=True
                )

                if chest["cursed"]:
                    await channel.send(
                        f"{claimer.mention} you just got fucked haha that was a cursed chest you idiot, "
                        f"you lost **{abs(amount):,} 🥖**"
                    )
                else:
                    await channel.send(
                        f"{claimer.mention} was the fastest and won **{amount:,} 🥖**!"
                    )

                async def delayed_delete():
                    await asyncio.sleep(10)
                    if channel.id in active_chests:
                        del active_chests[channel.id]

                asyncio.create_task(delayed_delete())

        await bot.process_commands(message)
        return


    # 1% spawn chance
    if channel.id not in active_chests and random.randint(1, 100) == 1:
        chest = choose_chest()

        embed = discord.Embed(
            title=f"A loot chest appeared!",
            description=f"A {chest['name']} just spawned! Type `!pick` quickly to pick it up!",
            color=chest["color"]
        )
        embed.set_image(url=chest["image"])
        await channel.send(embed=embed)

        active_chests[channel.id] = {
            **chest,
            "claimed": False
        }

        async def timeout_cleanup():
            await asyncio.sleep(30)
            if channel.id in active_chests and not active_chests[channel.id]["claimed"]:
                await channel.send("⏳ The chest vanished. Nobody claimed it in time.")
                del active_chests[channel.id]

        asyncio.create_task(timeout_cleanup())

    await bot.process_commands(message)


@tasks.loop(minutes=1)
async def change_status():
    activities = [
        discord.Activity(type=discord.ActivityType.playing, name="with your ego 😈"),
        discord.Activity(type=discord.ActivityType.playing, name="with your last brain cell 🧠"),
        discord.Activity(type=discord.ActivityType.playing, name="with your pride and winning 😈"),
        discord.Activity(type=discord.ActivityType.playing, name="on hard mode — your brain 🧠"),
        discord.Activity(type=discord.ActivityType.playing, name="in clown tournaments 🤡"),
        discord.Activity(type=discord.ActivityType.playing, name="with your confidence 💅"),
        discord.Activity(type=discord.ActivityType.playing, name="with fate's deck 🎴"),
        discord.Activity(type=discord.ActivityType.watching, name="you lose bets 💸"),
        discord.Activity(type=discord.ActivityType.watching, name="your hopes disappear 💨"),
        discord.Activity(type=discord.ActivityType.watching, name="you crumble slowly 🍿"),
        discord.Activity(type=discord.ActivityType.listening, name="your regrets 🎧"),
        discord.Activity(type=discord.ActivityType.listening, name="your excuses 🎤"),
        discord.Activity(type=discord.ActivityType.listening, name="the sound of defeat 🔊"),
        discord.Activity(type=discord.ActivityType.playing, name="UNO with your destiny 🎲"),
        discord.Activity(type=discord.ActivityType.playing, name="games you can’t win 🕹️"),
        discord.Activity(type=discord.ActivityType.playing, name="the long con 🎭"),
    ]
    await bot.change_presence(activity=random.choice(activities), status=discord.Status.online)

# Startup confirmation
@bot.event
async def on_ready():
    global lottery_started
    if not lottery_started:
        lottery_check.start()
        lottery_started = True
    change_status.start()
    await bot.change_presence(status=discord.Status.online)
    print(f"🤖 Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"✅ Registered commands: {[cmd.name for cmd in bot.commands]}")

@bot.event
async def on_command_error(ctx, error):
    print(f"❌ Command `{ctx.command}` raised: {error}")

# Webserver
async def run_webserver():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.add_routes([web.get("/", lambda request: web.Response(text="Bot is running"))])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Webserver running on port {port}")
    
register_uno_commands(bot)

# Main function
async def main():
    print("🔧 Inside async main()")
    await run_webserver()
    await test_mongodb()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ DISCORD_BOT_TOKEN is missing or empty!")
        return

    try:
        print("🟢 About to start bot...")
        await bot.start(token)
        print("🔁 Bot loop should never reach here unless it disconnects.")
    except Exception as e:
        print(f"❌ Exception in bot.start(): {e}")
        
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
