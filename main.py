# ============================
# Raiko Discord Bot - MongoDB Version
# ============================


import threading
import discord

print(f"[INFO] discord.py version: {discord.__version__}")
from discord import Embed, Interaction, ButtonStyle, ui, SelectOption
from discord.ext import commands, tasks
from itertools import cycle
from discord.ui import Button, View, Select, button
import random
from random import randint, choice
import asyncio
import json
import pytz
import math
from pprint import pformat
import string
import difflib
import re
import uuid
from difflib import get_close_matches
from collections import defaultdict
import html
import os
import time
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta, timezone
from discord.ext.commands import MissingRequiredArgument
from motor.motor_asyncio import AsyncIOMotorClient
import sys
import traceback
from pymongo import ReturnDocument

CREATOR_IDS = [955882470690140200, 521399748687691810]

# ----------- BOT SETUP -----------

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)
bot.remove_command('help')

LOTTERY_TICKET_PRICE = 50000
LOTTERY_MAX_TICKETS = 5
LOTTERY_BASE_PRIZE = 50000
LOTTERY_BONUS_PER_TICKET = 50000
LOTTERY_CHANNEL_ID = 977201441146040362

CHEST_CHANNEL_ID = 977201441146040362


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ----------- MONGODB SETUP -----------
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URL")
client = AsyncIOMotorClient(MONGO_URI)

db = client["raiko"]
users = db["users"]
bot_settings = db["bot_settings"]


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


# ----------- USER UTILS -----------

# === Global No-CD Helpers (persistent toggle in DB) ===
async def _get_meta_collection():
    # use the same database as your `users` collection
    return users.database.get_collection("meta")


async def _is_global_nocd(cmd_name: str) -> bool:
    meta = await _get_meta_collection()
    doc = await meta.find_one({"_id": "cooldown_toggles"}) or {}
    return bool(doc.get("nocd", {}).get(cmd_name.lower(), False))


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
            "cooldowns": {},
            "inventory": {}
        }
        await users.insert_one(user)
    else:
        updates = {}
        if "wallet" not in user:
            updates["wallet"] = 0
        if "bank" not in user:
            updates["bank"] = 0
        if "notes" not in user:
            updates["notes"] = []
        if "cooldowns" not in user:
            updates["cooldowns"] = {}
        if "inventory" not in user:
            updates["inventory"] = {}
        if "stats" not in user:
            updates["stats"] = {
                "wins": 0, "losses": 0,
                "ttt": {"wins": 0, "losses": 0},
                "rps": {"wins": 0, "losses": 0},
                "blackjack": {"wins": 0, "losses": 0},
                "hangman": {"wins": 0, "losses": 0},
                "connect4": {"wins": 0, "losses": 0}
            }
        elif "blackjack" not in user["stats"]:
            user["stats"]["blackjack"] = {"wins": 0, "losses": 0}
            updates["stats"] = user["stats"]

        if updates:
            await users.update_one({"_id": user_id}, {"$set": updates})
            user.update(updates)

    return user


async def get_user(user_id):
    return await users.find_one({"_id": str(user_id)})


async def update_user(user_id, updates):
    await users.update_one({"_id": str(user_id)}, {"$set": updates})


async def increment_user(user_id, field_path, amount):
    await users.update_one({"_id": str(user_id)}, {"$inc": {field_path: amount}})


def compute_active_set_breakdown(equipment: dict):
    """
    Returns:
      breakdown: { set_name: {"hp": X, "atk": Y, "def": Z} } for fully active sets
      total: {"hp": sum, "atk": sum, "def": sum}
    """
    import traceback
    from pprint import pformat

    try:
        print("[PROFILE DEBUG] [compute_active_set_breakdown] start")

        # Quick offender scan in equipment (dicts that have 'slot' but no 'slots')
        def _find_offenders(obj, path="$"):
            offenders = []
            if isinstance(obj, dict):
                if "slot" in obj and "slots" not in obj:
                    offenders.append((path, obj))
                for k, v in obj.items():
                    offenders.extend(_find_offenders(v, f"{path}.{k}"))
            elif isinstance(obj, list):
                for i, x in enumerate(obj):
                    offenders.extend(_find_offenders(x, f"{path}[{i}]"))
            return offenders

        offenders = _find_offenders(equipment or {})
        print("[PROFILE DEBUG] [compute_active_set_breakdown] equipment offenders:", len(offenders))
        for p, o in offenders[:5]:
            print("[PROFILE DEBUG] [compute_active_set_breakdown] offender at", p, "->", o)

        breakdown = {}
        total = {"hp": 0, "atk": 0, "def": 0}

        # --- Canonicalize slot names locally so 'helmet'==head, 'armor'==chest, etc. ---
        _ALIASES = {
            "helmet": "head",
            "helm": "head",
            "armor": "chest",
            "chestplate": "chest",
            "pants": "legs",
            "leggings": "legs",
            "boots": "feet",
        }

        def _canon_slot(s):
            try:
                # use your normalize_slot if present; otherwise just lowercase/strip
                base = normalize_slot(s) if 'normalize_slot' in globals() else str(s).lower().strip()
            except Exception:
                base = str(s).lower().strip()
            return _ALIASES.get(base, base)

        # -------------------------------------------------------------------------------

        # Iterate sets with targeted try/except so we know exactly where 'slots' blows up
        for set_name, cfg in SET_BONUSES.items():
            try:
                set_key = str(set_name).lower()
                req = {_canon_slot(s) for s in (cfg.get("slots") or cfg.get("slot") or [])}
            except Exception:
                print("[PROFILE DEBUG] [compute_active_set_breakdown] BAD SET ENTRY:", set_name, "->", cfg)
                print("[PROFILE TRACEBACK compute_active_set_breakdown:req]\n" + traceback.format_exc())
                raise

            try:
                # NORMALIZE both the item set name (lowercase) and the slot names (aliases)
                have = {
                    _canon_slot(slot)
                    for slot, item in (equipment or {}).items()
                    if item and str(item.get("set", "")).lower() == set_key
                }
            except Exception:
                print("[PROFILE TRACEBACK compute_active_set_breakdown:have]\n" + traceback.format_exc())
                raise

            print(f"[PROFILE DEBUG] [compute_active_set_breakdown] set='{set_name}' req={req} have={have}")

            if have >= req:
                try:
                    if "bonus" not in cfg:
                        print("[PROFILE DEBUG] SET_BONUSES entry missing 'bonus':", set_name, "->", cfg)
                    b = cfg.get("bonus", {})
                    b_hp = int(b.get("hp", 0))
                    b_atk = int(b.get("atk", 0))
                    b_def = int(b.get("def", 0))
                except Exception:
                    print("[PROFILE TRACEBACK compute_active_set_breakdown:bonus]\n" + traceback.format_exc())
                    raise

                breakdown[set_name] = {"hp": b_hp, "atk": b_atk, "def": b_def}
                total["hp"] += b_hp
                total["atk"] += b_atk
                total["def"] += b_def

        print("[PROFILE DEBUG] [compute_active_set_breakdown] result breakdown:", pformat(breakdown))
        print("[PROFILE DEBUG] [compute_active_set_breakdown] result total:", total)
        print("[PROFILE DEBUG] [compute_active_set_breakdown] end")
        return breakdown, total

    except Exception:
        print("[PROFILE TRACEBACK compute_active_set_breakdown]\n" + traceback.format_exc())
        raise


# ---- Trash Pool helpers (server-wide) ----
TRASH_MAX_PAYOUT = 200_000  # hard cap per find


async def _get_trash_pool() -> int:
    doc = await bot_settings.find_one({"_id": "trash_pool"}) or {}
    return int(doc.get("amount", 0))


async def _inc_trash_pool(delta: int) -> None:
    await bot_settings.update_one({"_id": "trash_pool"}, {"$inc": {"amount": int(delta)}}, upsert=True)


# ---- Trash Pool info ----
async def _get_trash_info() -> dict:
    """Return {'amount': int, 'last_user_id': str|None, 'last_amount': int|0, 'last_at': iso|None}"""
    doc = await bot_settings.find_one({"_id": "trash_pool"}) or {}
    return {
        "amount": int(doc.get("amount", 0)),
        "last_user_id": doc.get("last_user_id"),
        "last_amount": int(doc.get("last_amount", 0) or 0),
        "last_at": doc.get("last_at"),
    }


# === Anti-farm guardrails (FIND payouts, not deposits) ===
MAIN_CHANNEL_ID = 977201441146040362  # main-channel ID

TRASH_FIND_COOLDOWN_SEC = 5 * 60  # per-user cooldown after a find (5 min.)
TRASH_GUILD_DELAY_SEC = 10  # global “cooldown” between any two finds (server-wide)
TRASH_DAILY_MAX_CLAIMS = 10  # max number of find payouts per user per day
TRASH_DAILY_BREAD_CAP = 300_000  # max bread from finds per user per day


# ----------- COOLDOWN UTILITY -----------

async def is_on_cooldown(user_id, command_name, cooldown_seconds):
    # Global toggle (if nocd is ON for this command, always bypass)
    if await _is_global_nocd(command_name):
        return False, 0

    user = await users.find_one({"_id": str(user_id)})
    if not user:
        return False, 0

    cooldowns = user.get("cooldowns", {})
    value = cooldowns.get(command_name)
    if value is None:
        return False, 0

    # ---- Parse value to a datetime 't' robustly
    t = None
    try:
        from datetime import datetime, timezone
        if isinstance(value, datetime):
            t = value
        elif isinstance(value, str):
            v = value.strip()
            if v.endswith("Z"):
                v = v[:-1]
            t = datetime.fromisoformat(v)
        elif isinstance(value, (int, float)):
            t = datetime.utcfromtimestamp(value).replace(tzinfo=timezone.utc)
    except Exception:
        t = None

    if t is None:
        return False, 0

    # Normalize to aware UTC for safe math
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    remaining_expiry = int((t - now).total_seconds())
    remaining_lastused = int((t + timedelta(seconds=cooldown_seconds) - now).total_seconds())

    remaining = max(remaining_expiry, remaining_lastused)
    if remaining > 0:
        return True, remaining
    return False, 0


async def _is_global_nocd(command_name: str) -> bool:
    """
    Returns True if nocd is enabled for this command (i.e., cooldowns disabled).
    """
    meta = await _get_meta_collection()
    doc = await meta.find_one({"_id": "cooldown_toggles"}) or {}
    nocd = (doc.get("nocd") or {})
    return bool(nocd.get(command_name.strip().lower()))


@bot.command(name="nocd")
@commands.has_permissions(administrator=True)
async def nocd_toggle(ctx, cmd_name: str):
    key = cmd_name.strip().lower()
    meta = await _get_meta_collection()

    doc = await meta.find_one({"_id": "cooldown_toggles"}) or {"_id": "cooldown_toggles", "nocd": {}}
    nocd = doc.get("nocd", {})
    new_state = not bool(nocd.get(key, False))  # True means cooldowns are disabled
    nocd[key] = new_state

    await meta.update_one(
        {"_id": "cooldown_toggles"},
        {"$set": {"nocd": nocd}},
        upsert=True
    )

    # new_state=True => cooldowns OFF; new_state=False => cooldowns ON
    state_txt = "OFF (bypassed for everyone)" if new_state else "ON (enforced)"
    await ctx.send(f"🛠️ Cooldowns for **{key}** are now **{state_txt}**.")


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


# ===== Leveling config =====

ALLOWED_XP_CHANNELS = {977201441146040362, 1399446341012422797}

XP_MULTIPLIER = 0.5  # 50% of normal XP

XP_PER_MESSAGE_RANGE = (8, 15)  # random per valid message
XP_PER_COMMAND_RANGE = (15, 30)  # random per completed command
MSG_XP_COOLDOWN_SECONDS = 5  # per-user anti-spam
CMD_XP_COOLDOWN_SECONDS = 5  # per-user anti-spam
EXP_BAR_SIZE = 22  # characters in the progress bar


def bread_fmt(amount: int) -> str:
    return f"🥖{amount:,}"


def now_utc():
    return datetime.utcnow()


def xp_for_level(level: int) -> int:
    # Quadratic curve: 100 * level^2 (Level 1 => 100, L10 => 10,000 etc.)
    return 100 * (level ** 2)


def level_from_total_xp(total_xp: int) -> int:
    # inverse of 100*l^2 <= total_xp
    l = int((total_xp / 100) ** 0.5)
    return max(l, 0)


def level_progress(total_xp: int):
    """Return (level, cur, need, pct_float)."""
    lvl = level_from_total_xp(total_xp)
    cur_floor = xp_for_level(lvl)
    next_need = xp_for_level(lvl + 1)
    cur = total_xp - cur_floor
    need = max(next_need - cur_floor, 1)
    pct = cur / need
    return lvl, cur, need, pct


def exp_bar(pct: float, size: int = EXP_BAR_SIZE) -> str:
    filled = int(round(pct * size))
    filled = min(max(filled, 0), size)
    return "█" * filled + "░" * (size - filled)


async def _ensure_user_doc(user_id: int):
    uid = str(user_id)
    doc = await users.find_one({"_id": uid})
    if not doc:
        doc = {
            "_id": uid,
            "wallet": 0,
            "bank": 0,
            "total_xp": 0,
            "commands_used": 0,
            "messages_sent": 0,
            "flags": {},
            "cooldowns": {},
            "profile_bio": "",
            "married_to": None,  # user id or None
            "married_since": None,  # datetime or None
            "guild_xp": {},  # {guild_id_str: xp}
            "xp_cooldowns": {},  # {"msg": iso, "cmd": iso}
        }
        await users.insert_one(doc)
    return doc


def _cd_ready(doc: dict, key: str, cd_seconds: int) -> bool:
    iso = (doc.get("xp_cooldowns") or {}).get(key)
    if not iso:
        return True
    try:
        last = datetime.fromisoformat(iso)
    except Exception:
        return True
    return (now_utc() - last).total_seconds() >= cd_seconds


def _set_cd(update_doc: dict, key: str):
    xpcd = update_doc.get("xp_cooldowns") or {}
    xpcd[key] = now_utc().isoformat()
    update_doc["xp_cooldowns"] = xpcd


# =================== SHOP =====================================

SHOP_ITEMS = {
    # ----- Power/Buff items -----
    "🧲 Lucky Magnet": {"price": 30000, "description": "Boosts Treasure Hunt odds (CD: 48h)"},
    "🎯 Target Scope": {"price": 50000, "description": "Dig 2 tiles in next Treasure Hunt (CD: 48h)"},
    "💼 Bread Vault": {"price": 75000, "description": "Blocks next 3 robs for 24h (CD: 48h)"},
    "🛡️ Rob Shield": {"price": 25000, "description": "Blocks next 1 rob (CD: 24h)"},
    "🧃 Bread Juice": {"price": 4000, "description": "Doubles next ;work payout (CD: 24h)"},
    "🔫 Gun": {"price": 50000, "description": "Next ;rob is 100% + double (CD: 48h)"},

    # ----- Wedding rings -----
    "💍 Gualmar Wedding Ring": {"price": 15000, "description": "Cheapest wedding ring."},
    "🥉 Copper Wedding Ring": {"price": 50000, "description": "A modest copper ring."},
    "🥇 Gold Wedding Ring": {"price": 100000, "description": "Shiny golden ring."},
    "💎 Diamond Wedding Ring": {"price": 500000, "description": "A brilliant diamond ring."},
    "♾️ Eternity Wedding Ring": {"price": 5000000, "description": "An eternal bond."}
}


@bot.command()
async def shop(ctx):
    # Build two separate embeds: Items vs Rings (using one unified SHOP_ITEMS dict)
    items_embed = discord.Embed(title="🏪 Shop — Items", color=discord.Color.gold())
    rings_embed = discord.Embed(title="💍 Shop — Wedding Rings", color=discord.Color.gold())

    for item, info in SHOP_ITEMS.items():
        if "Wedding Ring" in item:
            rings_embed.add_field(
                name=f"{item} — {info['price']} 🥖",
                value=info["description"],
                inline=False
            )
        else:
            items_embed.add_field(
                name=f"{item} — {info['price']} 🥖",
                value=info["description"],
                inline=False
            )

    class ShopView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.embeds = [items_embed, rings_embed]
            self.current = 0

        @discord.ui.button(label="🛒 Items", style=discord.ButtonStyle.secondary)
        async def items_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your shop view.", ephemeral=True)
            self.current = 0
            await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

        @discord.ui.button(label="💍 Rings", style=discord.ButtonStyle.secondary)
        async def rings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("Not your shop view.", ephemeral=True)
            self.current = 1
            await interaction.response.edit_message(embed=self.embeds[self.current], view=self)

    await ctx.send(embed=items_embed, view=ShopView())


# ====================================================================
# ========================= TRASH SYSTEM =============================
# ====================================================================

@bot.command(name="trash")
async def trash(ctx, amount: str | None = None):
    """Throw bread from your wallet into the server's Public Trash Can (TRASH_POOL). 7d cooldown."""
    # View-only: show pool status if no amount provided (NO cooldown)
    await ensure_user(ctx.author.id)
    if amount is None:
        info = await bot_settings.find_one({"_id": "trash_pool"}) or {}
        pool_amt = int(info.get("amount", 0))
        last_user_id = info.get("last_user_id")
        last_amount = int(info.get("last_amount", 0) or 0)
        last_at = info.get("last_at")

        # Try to resolve mention
        last_user_mention = "—"
        if last_user_id:
            member = ctx.guild.get_member(int(last_user_id)) if ctx.guild else None
            if member:
                last_user_mention = member.mention
            else:
                u = bot.get_user(int(last_user_id))
                last_user_mention = u.mention if u else f"<@{last_user_id}>"

        embed = discord.Embed(title="🗑️ Public Trash Can", color=discord.Color.dark_grey())
        embed.add_field(name="Current Pool", value=f"**{pool_amt:,} 🥖**", inline=False)
        if last_amount > 0 and last_user_id:
            when = f" • {last_at}" if last_at else ""
            embed.add_field(name="Last Deposit", value=f"{last_user_mention} — **{last_amount:,} 🥖**{when}",
                            inline=False)
        else:
            embed.add_field(name="Last Deposit", value="No deposits yet.", inline=False)
        return await ctx.send(embed=embed)

    # Cooldown: 7 days (applies only for deposits)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'trash', 7 * 24 * 3600)
    if on_cd:
        m, s = divmod(remaining, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        return await ctx.send(f"⏳ You can use **!trash** again in {d}d {h}h {m}m {s}s.")

    user = await users.find_one({"_id": str(ctx.author.id)}) or {}
    wallet = int(user.get("wallet", 0))

    if wallet <= 0:
        return await ctx.send("🗑️ Your wallet is already empty.")

    # Parse amount
    target_amt = None
    if amount.strip().lower() == "all":
        target_amt = wallet
    else:
        try:
            target_amt = int(amount.replace(",", ""))
        except:
            return await ctx.send("❌ Please provide a valid amount (e.g., `!trash 5000`) or `!trash all`.")
    if target_amt is None or target_amt <= 0:
        return await ctx.send("❌ Amount must be greater than 0.")
    if target_amt > wallet:
        return await ctx.send(f"❌ You only have **{wallet} 🥖** in your wallet.")

    # Confirm UI
    class TrashConfirm(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=20)

        @discord.ui.button(label="✅ Yes, throw it away", style=discord.ButtonStyle.success)
        async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)

            # re-check cooldown & wallet just before committing
            fresh = await users.find_one({"_id": str(ctx.author.id)}) or {}
            fresh_wallet = int(fresh.get("wallet", 0))

            on_cd2, _ = await is_on_cooldown(ctx.author.id, 'trash', 7 * 24 * 3600)
            if on_cd2:
                return await interaction.response.send_message("⏳ You're on cooldown for **!trash**.", ephemeral=True)

            amt = min(target_amt, fresh_wallet)
            if amt <= 0:
                return await interaction.response.send_message("🗑️ Your wallet is empty now.", ephemeral=True)

            # Move to pool, remove from wallet
            await increment_user(ctx.author.id, "wallet", -amt)
            await _inc_trash_pool(+amt)

            # set cooldown + record last deposit metadata
            now = datetime.utcnow().isoformat()
            await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"cooldowns.trash": now}}, upsert=True)
            await bot_settings.update_one(
                {"_id": "trash_pool"},
                {"$set": {"last_user_id": str(ctx.author.id), "last_amount": int(amt), "last_at": now}},
                upsert=True
            )

            await interaction.response.edit_message(
                content=(f"🗑️ You threw **{amt} 🥖** into the **Public Trash Can**.\n"
                         f"*(Your bank balance wasn’t affected.)*"),
                view=None
            )

        @discord.ui.button(label="❌ No, keep it", style=discord.ButtonStyle.danger)
        async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Cancelled. You kept your bread.", view=None)

    prompt = (f"Are you sure you want to throw **{target_amt} 🥖** "
              f"into the **Public Trash Can**? This action **cannot** be undone.\n"
              f"**Your bank balance will not be affected.**")
    await ctx.send(prompt, view=TrashConfirm())


TRASH_FIND_BASE_CHANCE = 0.03  # 3% per command; set to 0.01~0.05

# (min, max, weight)
TRASH_BRACKETS = [
    (1_000, 10_000, 70),
    (10_000, 50_000, 20),
    (50_000, 100_000, 8),
    (100_000, 200_000, 2),
]

TRASH_MESSAGES = [
    "{user} You were so lucky you found 🥖{amount} in a trash can at the park!",
    "{user} Some random on the street gave you 🥖{amount} for no reason",
    "{user} You found a big brown envelope with nothing written on it and 🥖{amount} inside. You took the bread while no one was watching.",
]

TRASH_MEGA_MESSAGE = (
    "You found a container full of bread! You stole a forklift and left with the container, gaining 🥖{amount}"
)


@bot.listen("on_command_completion")
async def _trash_pool_roll(ctx: commands.Context):
    try:
        # Only in guilds, only for humans
        if not ctx.guild or ctx.author.bot:
            return

        # === Channel restriction: only allow finds from the main channel
        if ctx.channel.id != MAIN_CHANNEL_ID:
            return

        # Must have a pool
        pool = await _get_trash_pool()
        if pool <= 0:
            return

        # === Per-user cooldown on "find" payouts (silent skip if on CD)
        on_cd, _remaining = await is_on_cooldown(ctx.author.id, "trash_find", TRASH_FIND_COOLDOWN_SEC)
        if on_cd:
            return

        # === Guild flood-guard: small global delay between any two finds
        settings = await bot_settings.find_one({"_id": "global"}) or {"_id": "global"}
        now = datetime.now(timezone.utc)
        lock_ts = settings.get("trash_find_lock_until")
        if lock_ts and isinstance(lock_ts, datetime) and now < lock_ts:
            return

        # Chance gate (unchanged)
        if random.random() >= TRASH_FIND_BASE_CHANCE:
            return

        # Choose an affordable bracket (unchanged logic)
        affordable = []
        for lo, hi, w in TRASH_BRACKETS:
            capped_hi = min(hi, TRASH_MAX_PAYOUT, pool)
            if capped_hi >= lo:
                affordable.append((lo, capped_hi, w))
        if not affordable:
            return

        total_w = sum(w for _, _, w in affordable)
        roll = random.uniform(0, total_w)
        upto = 0
        chosen = affordable[-1]
        for br in affordable:
            upto += br[2]
            if roll <= upto:
                chosen = br
                break

        lo, hi, _ = chosen
        amount = random.randint(lo, hi)
        amount = min(amount, pool)
        if amount <= 0:
            return

        # === Daily caps per user (claims & bread)
        uid = str(ctx.author.id)
        u = await users.find_one({"_id": uid}) or {"_id": uid}
        stats = (u.get("trash_find_stats") or {})
        today = _today_str()

        if stats.get("date") != today:
            stats = {"date": today, "claims": 0, "earned": 0}

        # If already at limits, skip silently
        if stats["claims"] >= TRASH_DAILY_MAX_CLAIMS:
            return
        if stats["earned"] >= TRASH_DAILY_BREAD_CAP:
            return

        # Clamp payout to remaining daily bread cap
        remaining_cap = TRASH_DAILY_BREAD_CAP - stats["earned"]
        if amount > remaining_cap:
            amount = max(0, remaining_cap)
        if amount <= 0:
            return

        # === Commit: set guild lock first to throttle bursts
        new_lock = now + timedelta(seconds=TRASH_GUILD_DELAY_SEC)
        await bot_settings.update_one(
            {"_id": "global"},
            {"$set": {"trash_find_lock_until": new_lock}},
            upsert=True
        )

        # Pay user & drain pool (unchanged calls)
        await increment_user(ctx.author.id, "wallet", +amount)
        await _inc_trash_pool(-amount)

        # Update daily stats & set per-user find cooldown
        now_iso = datetime.utcnow().isoformat()
        await users.update_one(
            {"_id": uid},
            {
                "$set": {
                    "trash_find_stats.date": today,
                    "cooldowns.trash_find": now_iso
                },
                "$inc": {
                    "trash_find_stats.claims": 1,
                    "trash_find_stats.earned": int(amount)
                }
            },
            upsert=True
        )

        # Message (your original logic/messages)
        if amount >= 100_000:
            msg = TRASH_MEGA_MESSAGE.format(amount=amount)
            await ctx.send(f"🎉 {ctx.author.mention} {msg}")
        else:
            msg = random.choice(TRASH_MESSAGES).format(user=ctx.author.mention, amount=amount)
            await ctx.send(f"🎉 {msg}")

    except Exception:
        print("[TRASH_POOL] passive payout error:\n" + traceback.format_exc(), flush=True)


# ====================================================================
# ====================================================================

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

    # 🔒 Allow each wedding ring only once (but other rarities can still be bought once each)
    WEDDING_RING_KEYS = [k for k in SHOP_ITEMS.keys() if "Wedding Ring" in k]
    is_wedding_ring = item_key in WEDDING_RING_KEYS
    if is_wedding_ring:
        inv = (user.get("inventory") or {})
        if inv.get(item_key, 0) > 0:
            return await ctx.send("💍 You already own this wedding ring. You can’t buy the same one twice.")

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=15)

        @discord.ui.button(label="Confirm Purchase", style=discord.ButtonStyle.success)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Not your confirmation.", ephemeral=True)

            # Re-check funds and same-ring ownership at confirm time
            fresh = await users.find_one({"_id": user_id}) or {}
            if fresh.get("wallet", 0) < price:
                return await interaction.response.send_message("🚫 You don't have enough 🥖.", ephemeral=True)

            if is_wedding_ring:
                fresh_inv = (fresh.get("inventory") or {})
                if fresh_inv.get(item_key, 0) > 0:
                    return await interaction.response.send_message(
                        "💍 You already own this wedding ring. You can’t buy the same one twice.", ephemeral=True)
                # For wedding rings, force quantity to 1 (no stacking), still deduct wallet
                await users.update_one(
                    {"_id": user_id},
                    {
                        "$inc": {"wallet": -price},
                        "$set": {f"inventory.{item_key}": 1}
                    },
                    upsert=True
                )
            else:
                # Non-ring items keep normal stacking behavior
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
async def inventory(ctx, member: discord.Member | None = None):
    target = member or ctx.author
    user = await users.find_one({"_id": str(target.id)})
    inventory = user.get("inventory", {}) if user else {}
    if not inventory:
        return await ctx.send(f"📅 {target.display_name}'s inventory is empty.")

    embed = discord.Embed(title=f"🎒 {target.display_name}'s Inventory", color=discord.Color.green())
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


_CANONICAL = {
    "🧲 Lucky Magnet": {"aliases": ["luckymagnet", "magnet", "lm"]},
    "🎯 Target Scope": {"aliases": ["targetscope", "scope", "ts"]},
    "💼 Bread Vault": {"aliases": ["breadvault", "vault", "bv"]},
    "🛡️ Rob Shield": {"aliases": ["robshield", "shield", "rs"]},
    "🧃 Bread Juice": {"aliases": ["breadjuice", "juice", "bj"]},
    "🔫 Gun": {"aliases": ["gun", "pistol"]},
}

# Reverse alias map
_ALIAS_TO_CANON = {}


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


for canon, meta in _CANONICAL.items():
    _ALIAS_TO_CANON[_norm(canon)] = canon
    for a in meta["aliases"]:
        _ALIAS_TO_CANON[_norm(a)] = canon

# 24h cooldown for all supported items
_COOLDOWN_24H = set(_CANONICAL.keys())


def _resolve_item_name(inv_keys: list[str], user_input: str) -> tuple[str | None, str | None]:
    """
    Returns (matched_inventory_key, canonical_name) or (None, None).
    """
    ni = _norm(user_input)

    # 1) Exact normalized match to an inventory key
    for k in inv_keys:
        if _norm(k) == ni:
            canon = _ALIAS_TO_CANON.get(_norm(k))
            if not canon:
                cands = [c for c in _CANONICAL if _norm(c) in _norm(k)]
                canon = cands[0] if cands else None
            return k, canon or k

    # 2) Alias exact match
    if ni in _ALIAS_TO_CANON:
        canon = _ALIAS_TO_CANON[ni]
        for k in inv_keys:
            if _norm(canon) in _norm(k):
                return k, canon

    # 3) Prefix/substring fuzzy
    candidates = []
    for k in inv_keys:
        nk = _norm(k)
        if nk.startswith(ni) or ni in nk:
            for canon in _CANONICAL:
                if _norm(canon) in nk:
                    candidates.append((k, canon))
                    break

    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        return candidates[0]
    else:
        return None, None


@bot.command(aliases=["useitem"])
async def use(ctx, *, item_name: str):
    """Use a supported shop item (Lucky Magnet, Target Scope, Bread Vault, Rob Shield, Bread Juice, Gun)."""
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id}) or {"_id": user_id}

    inv = (user.get("inventory") or {})
    if not inv:
        return await ctx.send("❌ You don’t have that item.")

    matched_key, canonical = _resolve_item_name(list(inv.keys()), item_name)
    if not matched_key or inv.get(matched_key, 0) < 1:
        return await ctx.send("❌ You don’t have that item.")

    if canonical not in _CANONICAL:
        return await ctx.send("❌ That item cannot be used.")

    # --- Cooldown check (24h) ---
    now = datetime.utcnow()
    item_cds = ((user.get("cooldowns") or {}).get("item_usage") or {})
    last_used = item_cds.get(matched_key)

    if canonical in _COOLDOWN_24H and last_used:
        if isinstance(last_used, str):
            try:
                last_used = datetime.fromisoformat(last_used)
            except Exception:
                last_used = now
        if isinstance(last_used, datetime):
            elapsed = now - last_used
            if elapsed < timedelta(hours=24):
                remaining = timedelta(hours=24) - elapsed
                total = int(remaining.total_seconds())
                hh = total // 3600
                mm = (total % 3600) // 60
                return await ctx.send(f"⏳ You must wait {hh}h {mm}m before using **{matched_key}** again.")

    # --- Effects ---
    updates = {}
    msg = ""
    buffs = (user.get("buffs") or {})

    if canonical == "🧲 Lucky Magnet":
        updates["$set"] = {"buffs.magnet": True}
        msg = "🧲 You feel luckier… Treasure odds improved for your next hunt."

    elif canonical == "🎯 Target Scope":
        updates["$set"] = {"buffs.scope": True}
        msg = "🎯 Target Scope equipped! Your next `;treasurehunt` allows **2 digs**."

    elif canonical == "💼 Bread Vault":
        current_vault = int(buffs.get("vault", 0) or 0)
        new_vault = current_vault + 3
        updates["$set"] = {"buffs.vault": new_vault}
        msg = f"💼 Bread Vault fortified! It will block the **next 3 rob attempts** (charges now: **{new_vault}**)."

    elif canonical == "🛡️ Rob Shield":
        updates["$set"] = {"buffs.robshield": 1}
        msg = "🛡️ Rob Shield activated! It will block the **next rob attempt** against you."

    elif canonical == "🧃 Bread Juice":
        inc = updates.get("$inc", {})
        inc["wallet"] = inc.get("wallet", 0) + 500
        updates["$inc"] = inc
        set_ = updates.get("$set", {})
        set_["buffs.bread_juice"] = True
        updates["$set"] = set_
        msg = "🧃 You drank Bread Juice! **+500 🥖 now** and your **next `;work` pays double**."

    elif canonical == "🔫 Gun":
        updates["$set"] = {"buffs.gun": True}
        msg = "🔫 Gun loaded. Your **next `;rob` is guaranteed** and **pays double**."

    else:
        return await ctx.send("❌ That item cannot be used.")

    # --- Consume item unless it's a wedding ring ---
    inc2 = updates.get("$inc", {})

    if "Wedding Ring" not in matched_key:  # don't consume rings
        inc2[f"inventory.{matched_key}"] = inc2.get(f"inventory.{matched_key}", 0) - 1

    if inc2:
        updates["$inc"] = inc2

    set2 = updates.get("$set", {})
    set2[f"cooldowns.item_usage.{matched_key}"] = now.isoformat()
    updates["$set"] = set2

    await users.update_one({"_id": user_id}, updates)
    return await ctx.send(f"✅ {msg}")


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


# ------- LEVELING, PROFILE, BIO, BACKGROUND ETC --------

async def _server_rank(guild: discord.Guild, user_id: int) -> int | None:
    gid = str(guild.id)
    # Pull top users with this guild’s xp
    cursor = users.find({f"guild_xp.{gid}": {"$gt": 0}}, {f"guild_xp.{gid}": 1})
    entries = []
    async for d in cursor:
        entries.append((d["_id"], int(d.get("guild_xp", {}).get(gid, 0))))
    if not entries:
        return None
    entries.sort(key=lambda x: x[1], reverse=True)
    ids = [uid for uid, _ in entries]
    uid = str(user_id)
    return (ids.index(uid) + 1) if uid in ids else None


@bot.command(name="setbio")
async def set_bio_cmd(ctx: commands.Context, *, text: str):
    bio = text.strip()
    if len(bio) > 200:
        return await ctx.send("❌ Bio must be **200 characters or fewer**.")
    uid = str(ctx.author.id)
    await _ensure_user_doc(ctx.author.id)
    await users.update_one({"_id": uid}, {"$set": {"profile_bio": bio}})
    await ctx.send("✅ Bio updated.")


@bot.command(name="profile")
async def profile_cmd(ctx: commands.Context, member: discord.Member = None):
    target = member or ctx.author
    await _ensure_user_doc(target.id)
    doc = await get_user(target.id)

    username = f"{target.name}#{target.discriminator}" if hasattr(target, "discriminator") else target.name
    avatar_url = target.display_avatar.url if target.display_avatar else None
    bio = (doc.get("profile_bio") or "").strip()

    total_xp = int(doc.get("total_xp", 0))
    lvl, cur, need, pct = level_progress(total_xp)
    bar = exp_bar(pct)
    cmds = int(doc.get("commands_used", 0))
    msgs = int(doc.get("messages_sent", 0))

    wallet = int(doc.get("wallet", 0))
    bank = int(doc.get("bank", 0))
    total_bal = wallet + bank

    # Marriage
    married_to = doc.get("married_to")
    married_since = doc.get("married_since")
    marriage_line = "None"
    if married_to:
        try:
            spouse = ctx.guild.get_member(int(married_to)) or await bot.fetch_user(int(married_to))
            name = getattr(spouse, "mention", f"<@{married_to}>")
        except Exception:
            name = f"<@{married_to}>"
        duration = ""
        if married_since:
            try:
                dt = married_since if isinstance(married_since, datetime) else datetime.fromisoformat(married_since)
                delta = now_utc() - dt
                days = delta.days
                hrs = int((delta.total_seconds() // 3600) % 24)
                duration = f" — {days}d {hrs}h"
            except Exception:
                pass
        marriage_line = f"{name}{duration}"

    # Server rank
    rank = await _server_rank(ctx.guild, target.id)
    rank_text = f"#{rank}" if rank else "—"

    # Build embed
    e = Embed(title=f"👤 {username}", color=0x00ACEE)
    e.set_thumbnail(url=avatar_url)  # avatar

    # Level & EXP bar
    e.add_field(
        name=f"Level {lvl} • EXP {cur}/{need}",
        value=f"`{bar}`  {int(pct * 100)}%",
        inline=False
    )

    # Activity
    e.add_field(
        name="Activity",
        value=f"Messages: **{msgs:,}** • Commands: **{cmds:,}**\nTotal EXP: **{total_xp:,}** • Server Rank: **{rank_text}**",
        inline=False
    )

    # balances
    e.add_field(
        name="Balances",
        value=(
            f"💼 Wallet: {bread_fmt(wallet)}\n"
            f"🏦 Bank:   {bread_fmt(bank)}\n"
            f"🧮 Total:  {bread_fmt(total_bal)}"
        ),
        inline=True
    )

    # marriage
    if married_to:
        partner = marriage_line.split(' — ')[0]
        since = marriage_line.split(' — ')[1] if ' — ' in marriage_line else "—"
        e.add_field(
            name="Marriage",
            value=f"💍 Partner: {partner}\n⏳ Since:   {since}",
            inline=True
        )
    else:
        e.add_field(
            name="Marriage",
            value="💍 Partner: —\n⏳ Since:   —",
            inline=True
        )

    # Bio / About
    e.add_field(
        name="Bio",
        value=bio if bio else "*No bio set. Use `!setbio <text>`*",
        inline=False
    )

    e.set_footer(text=f"Requested by {ctx.author.display_name}")

    await ctx.send(embed=e)


# =====================================
# =========== SOLO GAMBLING ===========
# =====================================

# ============================
# PLINKO (buttons, MongoDB bets)
# Command: ;plinko <bet>
# ============================

import random
import math
import asyncio
import discord
from discord.ext import commands
from discord.ui import View, Button

# ---- Config ----
PLINKO_ROWS = 12  # number of peg rows
PLINKO_COLS = PLINKO_ROWS + 1  # number of bottom bins/columns
PLINKO_MIN_BET = 100
PLINKO_MAX_BET = 50000

# Risk profiles (multipliers per bin; center index ~ COLS//2).
# Keep symmetric so left/right are equivalent.
# RTP ~ ~95% when starting near the center. Tweak to taste.
PLINKO_MULTIPLIERS = {
    "low": [0.5, 0.7, 0.8, 0.9, 1, 1.2, 1.5, 1.2, 1, 0.9, 0.8, 0.7, 0.5],
    "normal": [0.2, 0.5, 0.8, 1.0, 1.5, 2.5, 5.0, 2.5, 1.5, 1.0, 0.8, 0.5, 0.2],
    "high": [0.1, 0.2, 0.4, 0.8, 1.2, 2.0, 9.0, 2.0, 1.2, 0.8, 0.4, 0.2, 0.1],
}

# Safety: make sure list length matches PLINKO_COLS
for k, v in PLINKO_MULTIPLIERS.items():
    if len(v) != PLINKO_COLS:
        raise RuntimeError(f"PLINKO_MULTIPLIERS['{k}'] must have {PLINKO_COLS} entries")

# Track users currently playing to prevent concurrent runs per user
_ACTIVE_PLINKO = set()


def _center_col():
    return PLINKO_COLS // 2  # integer center


def _simulate_drop(start_col: int) -> tuple[int, list[int]]:
    """
    Simulate a chip drop:
    - start_col: 0..PLINKO_COLS-1
    Returns (final_col, path_cols_per_row)
    """
    col = max(0, min(PLINKO_COLS - 1, start_col))
    path = []
    # At each row the chip "bounces" left or right with 0.5 probability.
    for _ in range(PLINKO_ROWS):
        step = -1 if random.random() < 0.5 else +1
        col = max(0, min(PLINKO_COLS - 1, col + step))
        path.append(col)
    return col, path


def _render_board_preview(current_col: int, row_idx: int) -> str:
    """
    Lightweight text visualization: shows current row (1-based) and a line with a pointer to current column.
    Keeps messages compact to avoid Discord edit spam.
    """
    cols = PLINKO_COLS
    cells = ["·"] * cols
    cells[current_col] = "⬇️"
    line = "".join(cells)
    return f"Row {row_idx + 1}/{PLINKO_ROWS}\n`{line}`"


class PlinkoView(View):
    def __init__(self, author_id: int, bet: int, risk: str, multipliers: list[float]):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.bet = bet
        self.risk = risk
        self.multipliers = multipliers
        self.message: discord.Message | None = None

        # Build 13 column buttons in 3 rows (5 + 5 + 3)
        for i in range(PLINKO_COLS):
            label = str(i + 1)
            btn = Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"plinko_col_{i}")
            # Avoid >5 per row by adding in order
            self.add_item(btn)

    async def on_timeout(self):
        # Disable all buttons on timeout
        for item in self.children:
            if isinstance(item, Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("🚫 Only the player who started this Plinko can choose the column.",
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Center", style=discord.ButtonStyle.primary, row=0)
    async def center_button(self, interaction: discord.Interaction, button: Button):
        # Add a convenience "Center" (maps to middle column)
        await self._handle_drop(interaction, _center_col())

    async def _handle_drop(self, interaction: discord.Interaction, start_col: int):
        # Disable all controls immediately
        for item in self.children:
            if isinstance(item, Button):
                item.disabled = True

        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # Animate the drop in the same message embed
        try:
            embed = self.message.embeds[0] if (self.message and self.message.embeds) else None
        except Exception:
            embed = None

        final_col, path = _simulate_drop(start_col)
        # quick step-through animation (fast so it doesn't rate-limit)
        if embed and self.message:
            for r, col in enumerate(path):
                preview = _render_board_preview(col, r)
                embed.set_field_at(0, name="Path", value=preview, inline=False)
                try:
                    await self.message.edit(embed=embed)
                    await asyncio.sleep(0.15)
                except Exception:
                    break

        mult = self.multipliers[final_col]
        win_total = int(math.floor(self.bet * mult))
        net = win_total - self.bet

        # Payout handled by command body; we only show result here.
        result_text = (
            f"🎯 **Final Bin:** #{final_col + 1}  |  ✖️ **Multiplier:** x{mult}\n"
            f"💰 **Result:** {'+' if net >= 0 else ''}{net} 🥖  "
            f"(won {win_total} 🥖{' incl. bet' if win_total else ''})"
        )

        if embed and self.message:
            try:
                embed.remove_field(0)
            except Exception:
                pass
            embed.add_field(name="Result", value=result_text, inline=False)
            try:
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass


def register_plinko_command(bot: commands.Bot):
    @bot.command(name="plinko")
    async def plinko_cmd(ctx, bet: int, risk: str = "normal"):
        """
        Play Plinko with a bet from your wallet.
        Usage: ;plinko <bet> [risk]
        risk ∈ {low, normal, high}, default=normal
        """
        global _ACTIVE_PLINKO

        risk = risk.lower()
        if risk not in PLINKO_MULTIPLIERS:
            return await ctx.send("❌ Invalid risk. Use one of: low, normal, high.")

        if bet < PLINKO_MIN_BET:
            return await ctx.send(f"❌ Minimum bet is {PLINKO_MIN_BET} 🥖.")
        if bet > PLINKO_MAX_BET:
            return await ctx.send(f"❌ Maximum bet is {PLINKO_MAX_BET:,} 🥖.")

        uid = str(ctx.author.id)
        if uid in _ACTIVE_PLINKO:
            return await ctx.send("⏳ You already have a Plinko game running.")

        # --- DB: check funds and deduct bet up front ---
        users = globals().get("users")
        if users is None:
            return await ctx.send("⚠️ Database not ready. Try again later.")

        user = await users.find_one({"_id": uid}) or {"_id": uid, "wallet": 0, "bank": 0}
        wallet = int(user.get("wallet", 0))

        if bet > wallet:
            return await ctx.send("❌ You don't have that much 🥖.")

        # Deduct bet atomically
        res = await users.update_one(
            {"_id": uid, "wallet": {"$gte": bet}},
            {"$inc": {"wallet": -bet}}
        )
        if res.modified_count == 0:
            return await ctx.send("❌ Balance changed — not enough 🥖 now.")

        _ACTIVE_PLINKO.add(uid)
        try:
            multipliers = PLINKO_MULTIPLIERS[risk]
            # Intro embed
            embed = discord.Embed(
                title="🟡 Plinko",
                description=(
                    f"**Bet:** {bet:,} 🥖   •   **Rows:** {PLINKO_ROWS}   •   **Risk:** {risk.capitalize()}\n"
                    "Pick a starting **column** below (or press **Center**)."
                ),
                color=0xFFD54F
            )
            # Placeholder field for animation path
            embed.add_field(name="Path", value="Waiting for a column...", inline=False)
            # Multipliers line
            mult_line = " | ".join([f"#{i + 1}: x{m}" for i, m in enumerate(multipliers)])
            embed.add_field(name="Bins & Multipliers", value=mult_line, inline=False)

            view = PlinkoView(ctx.author.id, bet, risk, multipliers)

            msg = await ctx.send(embed=embed, view=view)
            view.message = msg

            # Wait until user presses a button OR timeout
            timeout = await view.wait()

            # If timed out before choosing, refund bet
            if timeout and msg and (uid in _ACTIVE_PLINKO):
                # Refund because no interaction occurred
                await users.update_one({"_id": uid}, {"$inc": {"wallet": bet}})
                for item in view.children:
                    if isinstance(item, Button):
                        item.disabled = True
                try:
                    embed = msg.embeds[0]
                    embed.description = "⌛ Timed out. Bet refunded."
                    await msg.edit(embed=embed, view=view)
                except Exception:
                    pass
                _ACTIVE_PLINKO.discard(uid)
                return

            try:
                e = msg.embeds[0]
                result_field = next((f for f in e.fields if f.name == "Result"), None)
                if not result_field:
                    # Shouldn't happen, but refund if no result produced
                    await users.update_one({"_id": uid}, {"$inc": {"wallet": bet}})
                    _ACTIVE_PLINKO.discard(uid)
                    return
                # Extract multiplier from text "Multiplier: x{mult}"
                import re
                m = re.search(r"Multiplier:\s*x([0-9]*\.?[0-9]+)", result_field.value)
                mult = float(m.group(1)) if m else 1.0
            except Exception:
                mult = 1.0

            win_total = int(math.floor(bet * mult))
            if win_total > 0:
                await users.update_one({"_id": uid}, {"$inc": {"wallet": win_total}})

            _ACTIVE_PLINKO.discard(uid)

        except Exception as e:
            _ACTIVE_PLINKO.discard(uid)

            try:
                await users.update_one({"_id": uid}, {"$inc": {"wallet": bet}})
            except Exception:
                pass
            raise e


@commands.cooldown(1, 5, commands.BucketType.user)
@bot.command(aliases=["cf"])
async def coinflip(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;coinflip <bet>`")

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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


@commands.cooldown(1, 5, commands.BucketType.user)
@bot.command()
async def slots(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `!slots <bet>`")

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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


@bot.command(aliases=["gtf"])
async def guesstheflag(ctx):
    flags = {
        "United States": "https://flagcdn.com/w320/us.png",
        "Canada": "https://flagcdn.com/w320/ca.png",
        "United Kingdom": "https://flagcdn.com/w320/gb.png",
        "Germany": "https://flagcdn.com/w320/de.png",
        "France": "https://flagcdn.com/w320/fr.png",
        "Italy": "https://flagcdn.com/w320/it.png",
        "Spain": "https://flagcdn.com/w320/es.png",
        "Mexico": "https://flagcdn.com/w320/mx.png",
        "Brazil": "https://flagcdn.com/w320/br.png",
        "Argentina": "https://flagcdn.com/w320/ar.png",
        "Chile": "https://flagcdn.com/w320/cl.png",
        "Colombia": "https://flagcdn.com/w320/co.png",
        "Peru": "https://flagcdn.com/w320/pe.png",
        "Venezuela": "https://flagcdn.com/w320/ve.png",
        "Ecuador": "https://flagcdn.com/w320/ec.png",
        "Bolivia": "https://flagcdn.com/w320/bo.png",
        "Paraguay": "https://flagcdn.com/w320/py.png",
        "Uruguay": "https://flagcdn.com/w320/uy.png",
        "Panama": "https://flagcdn.com/w320/pa.png",
        "Costa Rica": "https://flagcdn.com/w320/cr.png",
        "Cuba": "https://flagcdn.com/w320/cu.png",
        "Dominican Republic": "https://flagcdn.com/w320/do.png",
        "Haiti": "https://flagcdn.com/w320/ht.png",
        "Jamaica": "https://flagcdn.com/w320/jm.png",
        "Trinidad and Tobago": "https://flagcdn.com/w320/tt.png",
        "Puerto Rico": "https://flagcdn.com/w320/pr.png",
        "Australia": "https://flagcdn.com/w320/au.png",
        "New Zealand": "https://flagcdn.com/w320/nz.png",
        "China": "https://flagcdn.com/w320/cn.png",
        "Japan": "https://flagcdn.com/w320/jp.png",
        "South Korea": "https://flagcdn.com/w320/kr.png",
        "India": "https://flagcdn.com/w320/in.png",
        "Pakistan": "https://flagcdn.com/w320/pk.png",
        "Bangladesh": "https://flagcdn.com/w320/bd.png",
        "Thailand": "https://flagcdn.com/w320/th.png",
        "Vietnam": "https://flagcdn.com/w320/vn.png",
        "Malaysia": "https://flagcdn.com/w320/my.png",
        "Philippines": "https://flagcdn.com/w320/ph.png",
        "Indonesia": "https://flagcdn.com/w320/id.png",
        "Saudi Arabia": "https://flagcdn.com/w320/sa.png",
        "Iran": "https://flagcdn.com/w320/ir.png",
        "Iraq": "https://flagcdn.com/w320/iq.png",
        "Israel": "https://flagcdn.com/w320/il.png",
        "Egypt": "https://flagcdn.com/w320/eg.png",
        "South Africa": "https://flagcdn.com/w320/za.png",
        "Kenya": "https://flagcdn.com/w320/ke.png",
        "Nigeria": "https://flagcdn.com/w320/ng.png",
        "Ghana": "https://flagcdn.com/w320/gh.png",
        "Algeria": "https://flagcdn.com/w320/dz.png",
        "Morocco": "https://flagcdn.com/w320/ma.png",
        "Tunisia": "https://flagcdn.com/w320/tn.png",
        "Russia": "https://flagcdn.com/w320/ru.png",
        "Ukraine": "https://flagcdn.com/w320/ua.png",
        "Poland": "https://flagcdn.com/w320/pl.png",
        "Netherlands": "https://flagcdn.com/w320/nl.png",
        "Belgium": "https://flagcdn.com/w320/be.png",
        "Sweden": "https://flagcdn.com/w320/se.png",
        "Norway": "https://flagcdn.com/w320/no.png",
        "Finland": "https://flagcdn.com/w320/fi.png",
        "Denmark": "https://flagcdn.com/w320/dk.png",
        "Switzerland": "https://flagcdn.com/w320/ch.png",
        "Austria": "https://flagcdn.com/w320/at.png",
        "Portugal": "https://flagcdn.com/w320/pt.png",
        "Greece": "https://flagcdn.com/w320/gr.png",
        "Czech Republic": "https://flagcdn.com/w320/cz.png",
        "Slovakia": "https://flagcdn.com/w320/sk.png",
        "Hungary": "https://flagcdn.com/w320/hu.png",
        "Romania": "https://flagcdn.com/w320/ro.png",
        "Turkey": "https://flagcdn.com/w320/tr.png",
        "Kazakhstan": "https://flagcdn.com/w320/kz.png",
        "Georgia": "https://flagcdn.com/w320/ge.png",
        "Armenia": "https://flagcdn.com/w320/am.png",
        "Azerbaijan": "https://flagcdn.com/w320/az.png",
        "Iceland": "https://flagcdn.com/w320/is.png",
        "Ireland": "https://flagcdn.com/w320/ie.png",
        "Luxembourg": "https://flagcdn.com/w320/lu.png",
        "Estonia": "https://flagcdn.com/w320/ee.png",
        "Latvia": "https://flagcdn.com/w320/lv.png",
        "Lithuania": "https://flagcdn.com/w320/lt.png"
    }

    country, image_url = random.choice(list(flags.items()))

    embed = discord.Embed(
        title="🌍 Guess the Flag!",
        description="You have 30 seconds to guess the country!",
        color=discord.Color.orange()
    )
    embed.set_image(url=image_url)

    await ctx.send(embed=embed)

    def check(m):
        return (
                m.channel == ctx.channel
                and country.lower() in m.content.lower()
        )

    try:
        guess = await bot.wait_for("message", timeout=30.0, check=check)
        await ctx.send(f"✅ Correct! {guess.author.mention} guessed **{country}**!")
    except asyncio.TimeoutError:
        await ctx.send(f"⏳ Time's up! The correct answer was **{country}**.")


@commands.cooldown(1, 5, commands.BucketType.user)
@bot.command()
async def dice(ctx, bet: int):
    """Rolls three dice sequentially with real emojis and shows total scores. Max bet: 50,000 🥖."""
    await ensure_user(ctx.author.id)
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")
    if bet <= 0:
        return await ctx.send("❗ Usage: `;dice <bet>`")
    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

    dice_emojis = {
        1: "🎲1", 2: "🎲2", 3: "🎲3",
        4: "🎲4", 5: "🎲5", 6: "🎲6"
    }

    embed = discord.Embed(
        title="🎲 Dice Duel",
        description=f"Rolling dice for **{bet:,} 🥖**...",
        color=discord.Color.green()
    )
    msg = await ctx.send(embed=embed)

    user_rolls, bot_rolls = [], []

    for i in range(3):
        await asyncio.sleep(1)
        u = randint(1, 6)
        b = randint(1, 6)
        user_rolls.append(u)
        bot_rolls.append(b)

        user_dice = "  ".join(dice_emojis[r] for r in user_rolls)
        bot_dice = "  ".join(dice_emojis[r] for r in bot_rolls)
        user_total = str(sum(user_rolls))
        bot_total = str(sum(bot_rolls))

        embed.description = (
            f"{ctx.author.mention}    {user_dice:<20}  **{user_total}**\n"
            f"{bot.user.mention}    {bot_dice:<20}  **{bot_total}**"
        )
        await msg.edit(embed=embed)

    await asyncio.sleep(1)
    total_user = sum(user_rolls)
    total_bot = sum(bot_rolls)

    if total_user > total_bot:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": bet}})
        await ctx.send(f"🎉 {ctx.author.mention} wins! +{bet} 🥖")
    elif total_user < total_bot:
        await users.update_one({"_id": user_id}, {"$inc": {"wallet": -bet}})
        await ctx.send(f"💀 {ctx.author.mention} loses! -{bet} 🥖")
    else:
        await ctx.send(f"😐 It's a tie! No 🥖 lost.")


@commands.cooldown(1, 10, commands.BucketType.user)
@bot.command()
async def roulette(ctx, bet: int):
    if bet <= 0:
        return await ctx.send("❗ Usage: `;roulette <bet>`")

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})
    if not user or user.get("wallet", 0) < bet:
        return await ctx.send("💸 You don’t have enough 🥖 to bet.")

    numbers_1 = [discord.SelectOption(label=str(i), value=str(i)) for i in range(0, 19)]
    numbers_2 = [discord.SelectOption(label=str(i), value=str(i)) for i in range(19, 37)] + [
        discord.SelectOption(label="00", value="00")]
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
        "black": ['2', '4', '6', '8', '10', '11', '13', '15', '17', '20', '22', '24', '26', '28', '29', '31', '33',
                  '35']
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

@commands.cooldown(1, 1800, commands.BucketType.user)
@bot.command(aliases=["lm"])
async def landmine(ctx, bet: int):
    user_id = str(ctx.author.id)
    user = await users.find_one({"_id": user_id})

    if not user:
        await users.insert_one({"_id": user_id, "wallet": 0, "bank": 0, "stats": {"landmine_streak": 0}})
        user = await users.find_one({"_id": user_id})

    if bet <= 0:
        # 🔧 Don't consume cooldown on invalid bet
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❗ Please enter a bet greater than 0 you moron.")

    if bet > 50000:
        # 🔧 Don't consume cooldown on invalid bet
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

    if user.get("wallet", 0) < bet:
        # 🔧 Don't consume cooldown when they can't afford it
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ You don't even have enough bread to bet that, broke ass bitch.")

    wallet = user.get("wallet", 0)
    if wallet < 0:
        await users.update_one({"_id": user_id}, {"$set": {"wallet": 0}})
        wallet = 0

    await users.update_one(
        {"_id": user_id},
        {"$set": {"cooldowns.landmine": datetime.utcnow() + timedelta(minutes=30)}},
        upsert=True
    )

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
                    winnings = int(bet * 1.5 * new_streak)  # 🔧 cast to int to avoid .0
                    await users.update_one({"_id": user_id}, {
                        "$inc": {"wallet": winnings},
                        "$set": {"stats.landmine_streak": new_streak}
                    })
                    await interaction.response.edit_message(view=self.view)
                    return await ctx.send(
                        f"💰 You found a money bag! You won **{winnings:,} 🥖**!\n🔥 Current win streak: **{new_streak}**")

    embed = discord.Embed(
        title="💣 Landmine Game",
        description="Click a tile and try your luck! Avoid the bombs to win 🥖!",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed, view=LandmineView())


@landmine.error
async def landmine_error(ctx, error):
    # 🔧 Handle cooldown cleanly (no duplicate messages)
    if isinstance(error, commands.CommandOnCooldown):
        # Mirror the remaining time to MongoDB so !cooldowns shows it
        await users.update_one(
            {"_id": str(ctx.author.id)},
            {"$set": {"cooldowns.landmine": datetime.utcnow() + timedelta(seconds=int(error.retry_after))}},
            upsert=True
        )
        remaining = int(error.retry_after)
        m, s = divmod(remaining, 60)
        h, m = divmod(m, 60)
        pretty = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s")
        return await ctx.send(f"⏳ You’re on Landmine cooldown. Try again in **{pretty}**.")

    # 🔧 If they forgot the bet or passed a non-number, show usage and REFUND cooldown
    if isinstance(error, commands.MissingRequiredArgument) or isinstance(error, commands.BadArgument):
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("Usage: `!landmine <bet>`")

    # let other errors bubble
    raise error


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

        if draw_stack:
            # Only allow stacking if the card matches the current draw type
            if '+2' in card and '+2' in top:
                return True
            if '+4' in card and '+4' in top:
                return True
            return False  # No other cards allowed during draw stack

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
            print(f"[DEBUG] First top card: {self.pile[-1]}")
            while self.pile[-1] in WILD_CARDS or any(x in self.pile[-1] for x in ['⏭️', '+2']):
                print(f"[DEBUG] Top card {self.pile[-1]} not allowed. Replacing...")
                self.deck.insert(0, self.pile.pop())
                self.pile.append(self.deck.pop())
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

            if bet > 50000:
                return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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

                        await interaction.response.send_message(f"✅ {interaction.user.display_name} joined!",
                                                                ephemeral=True)
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
            if is_valid_play(drawn_card, self.game.pile[-1], self.game.draw_stack, self.game.color_override):
                await self.game.ctx.send(f"🔄 {self.user.display_name} drew a playable card.")
            else:
                self.game.advance_turn()
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
            g.apply_card_effect(selected_raw)

            if selected_raw in WILD_CARDS:
                try:
                    await interaction.response.defer()
                except discord.errors.InteractionResponded:
                    pass
                await interaction.followup.send("🎨 Choose a color...", view=ColorSelectView(g, interaction.user),
                                                ephemeral=True)
                return

            # UNO call logic
            if len(g.hands[interaction.user]) == 1:
                g.called_uno[interaction.user] = None  # They need to call it soon

                async def uno_timer():
                    await asyncio.sleep(10)
                    if g.called_uno.get(interaction.user) is None and len(g.hands[interaction.user]) == 1:
                        g.hands[interaction.user].extend([g.deck.pop(), g.deck.pop()])
                        await g.ctx.send(
                            f"❗ **{interaction.user.display_name}** didn’t call UNO! Drew 2 cards and turn ended.")
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
                for seconds_left in [20, 10, 5]:
                    await asyncio.sleep(30 - seconds_left)
                    await game.ctx.send(f"⏳ {p.mention}, {seconds_left}s left to play...")

                if p == game.current_player():
                    # Check if player is under draw pressure
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

            except Exception:
                print("[UNO ERROR] Turn timer failed.")
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
    pool = 50000 + total_tickets * 50000

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
            await interaction.response.defer()
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
        await users.update_one(
            {"_id": str(ctx.author.id)},
            {"$inc": {"wallet": -total_price, "lottery_tickets": amount}}
        )

        await ctx.send(f"✅ You bought {amount} ticket(s)!")
    else:
        await ctx.send("❌ Purchase cancelled.")


async def run_lottery_draw():
    buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
    entries = []
    for u in buyers:
        entries.extend([u["_id"]] * u.get("lottery_tickets", 0))

    channel = bot.get_channel(977201441146040362)
    if entries:
        winner_id = random.choice(entries)
        winner = await bot.fetch_user(int(winner_id))
        prize = 50000 + len(entries) * 50000
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

    await run_lottery_draw()
    await ctx.send("🎯 Forced lottery draw executed.")


@tasks.loop(minutes=1)
async def lottery_check():
    now = datetime.now(lottery_timezone)

    # === Reminder every 48h ===
    if (lottery_cache["last_reminder"] is None or
            (now - lottery_cache["last_reminder"]).total_seconds() >= 48 * 3600):
        buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
        # === Reminder every 48h ===
        if (lottery_cache["last_reminder"] is None or
                (now - lottery_cache["last_reminder"]).total_seconds() >= 48 * 3600):

            buyers = await users.find({"lottery_tickets": {"$gt": 0}}).to_list(length=100)
            total_tickets = sum(u.get("lottery_tickets", 0) for u in buyers)
            pool = LOTTERY_BASE_PRIZE + total_tickets * LOTTERY_BONUS_PER_TICKET

            if buyers:
                participant_lines = [f"<@{u['_id']}>: {u['lottery_tickets']} 🎟️" for u in buyers]
            else:
                participant_lines = ["❌ No participants yet! Be the first to join."]

            embed = discord.Embed(title="⏳ Lottery Reminder", color=discord.Color.orange())
            embed.add_field(name="Prize Pool", value=f"{pool} 🥖", inline=False)
            embed.add_field(name="Participants", value="\n".join(participant_lines), inline=False)
            embed.set_footer(
                text="Use ;lotto to check your tickets or ;lottobuy <amount> to join! Maximum of 5 tickets per participant!")

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


# put near your imports
from datetime import datetime


async def record_wordle_best(user_id, tries_used, mode):
    """
    Stores the lowest (best) number of tries a user needed to solve Wordle.
    Paths used:
      users._id = str(user_id)
      users.wordle.best.<mode>.tries = int
      users.wordle.best.<mode>.updatedAt = ISO timestamp
    """
    users_col = globals().get("users")
    if users_col is None:
        return  # DB not ready; avoid crashing

    mode = (str(mode or "normal").strip().lower())
    if mode not in ("normal", "expert"):
        mode = "normal"

    uid = str(user_id)
    tries = int(max(1, tries_used))  # clamp to sane minimum
    now_iso = datetime.utcnow().isoformat()

    # $min sets the field if missing or if the new value is lower (better)
    await users_col.update_one(
        {"_id": uid},
        {
            "$setOnInsert": {"_id": uid},
            "$min": {f"wordle.best.{mode}.tries": tries},
            "$set": {f"wordle.best.{mode}.updatedAt": now_iso},
        },
        upsert=True,
    )


# ============================
# DAILY WORDLE GAME (WITH MODES)
# ============================


# WORD_LIST = [
#    "crane", "slate", "grind", "sword", "plant", "bloom", "frame", "glide", "shock", "pound",
#   "brave", "swept", "price", "climb", "tiger", "stone", "light", "float", "sweep", "cloud",
#  "sugar", "clock", "spice", "grape", "chase", "crazy", "lemon", "paint", "trick", "proud",
#   "bread", "shine", "trace", "jumpy", "drink", "latch", "chair", "speak", "flame", "brush",
#    "smile", "quiet", "lunch", "track", "music", "swift", "mount", "skate", "trace", "night",
#    "water", "match", "candy", "liver", "grace", "clean", "happy", "sunny", "chill", "flock",
#  "crack", "shelf", "medal", "spend", "crisp", "plump", "skill", "grass", "build", "plant",
# "reach", "shiny", "feast", "frost", "crush", "greet", "mimic", "judge", "sling",
# "swing", "party", "prank", "glare", "fable", "ghost", "bride", "groom", "nurse",
#    "salty", "straw", "bulge", "rumor", "panic", "title", "river", "event", "lucky", "teach"
# ]

# WORD_LIST_EXPERT = [
#   "cryptic", "zephyrs", "phantom", "awkward", "gargoyle", "quibble", "nymphic", "blitzed", "waltzed", "jackpot",
#  "vaccine", "jigsaw", "jubilee", "keyhole", "mystify", "gazette", "voyager", "bizarre", "nymphos", "flaxman",
# "haphazard", "oxymoron", "melancholy", "subtext", "plummet", "javelin", "buffalo", "fiancé", "detoxify", "fuchsia",
# "glisten", "klutzy", "overkill", "rejoice", "squalor", "vagrant", "wrestle", "hijinks", "jubilee", "silicon",
#    "toppled", "worship", "yearned", "zeniths", "bewitch", "corrupt", "dwarfed", "frazzle", "gnostic", "hazards",
#   "iceberg", "jackals", "leprosy", "mortals", "notably", "opacity", "pixels", "quaking", "rivalry", "scarabs",
#  "thwarts", "uncanny", "verbose", "waffled", "xylitol", "yardman", "zealous", "alchemy", "banshee", "chimera",
# "dwindle", "empathy", "forsake", "grumble", "hostile", "incisor", "juggler", "kindled", "lattice", "mislead",
# "nomadic", "obscure", "pitfall", "quicken", "ruffian", "siphons", "turbine", "undying", "vexedly", "wicking",
# "xenonox", "yawning", "zillion", "baffled", "clunker", "dizzily", "epochal", "fissure", "gizzard", "hexagon"
# ]

with open("words_normal.txt", "r", encoding="utf-8") as f:
    WORD_LIST = [w.strip().lower() for w in f if w.strip()]

with open("words_expert.txt", "r", encoding="utf-8") as f:
    WORD_LIST_EXPERT = [w.strip().lower() for w in f if w.strip()]

REWARDS_NORMAL = {i: 10000 - (i - 1) * 1000 for i in range(1, 11)}
REWARDS_EXPERT = {i: 30000 - (i - 1) * 2500 for i in range(1, 11)}


def wordle_feedback(guess, answer):
    feedback = []
    used = list(answer)
    for i in range(len(guess)):
        if i < len(answer) and guess[i] == answer[i]:
            feedback.append("🟩")
            used[i] = None
        else:
            feedback.append(None)
    for i in range(len(guess)):
        if feedback[i] is not None:
            continue
        if guess[i] in used:
            feedback[i] = "🟨"
            used[used.index(guess[i])] = None
        else:
            feedback[i] = "⬛"
    return "".join(feedback)


def generate_hint(word):
    hints = [
        f"The word starts with **{word[0].upper()}**.",
        f"The word ends with **{word[-1].upper()}**.",
        f"The word contains the letter **{random.choice(word).upper()}**.",
        f"The word has **{len(set(word))} unique letters**.",
        f"The word rhymes with '**{word[-3:]}**'."
    ]
    return random.choice(hints)


async def get_definition(word):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}") as r:
                data = await r.json()
                if isinstance(data, list):
                    return data[0]["meanings"][0]["definitions"][0]["definition"]
    except Exception as e:
        print(f"[ERROR] get_definition: {e}")
        return None


class WordleGuessModal(ui.Modal):
    guess = ui.TextInput(label="Enter your guess", max_length=8)

    def __init__(self, bot, mode, word, user_data, interaction, message_id: int, channel_id: int):
        super().__init__(title="Wordle Guess")
        self.bot = bot
        self.mode = mode
        self.word = word
        self.user_data = user_data
        self.ctx = interaction
        self.message_id = message_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: Interaction):
        print("[DEBUG] Modal submitted")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        today_key = f"{today}:{self.mode}"  # ✅ fix: use self.mode

        # Always respond once to the submit interaction
        try:
            await interaction.response.defer(thinking=False, ephemeral=True)
        except Exception as e:
            print(f"[WARN] Modal defer failed: {e}")

        guess = (self.guess.value or "").strip().lower()
        if not guess.isalpha() or len(guess) != len(self.word):
            return await interaction.followup.send(
                f"❌ Invalid guess length. This word has **{len(self.word)}** letters.",
                ephemeral=True
            )

        wordlist = [w.lower() for w in (WORD_LIST_EXPERT if self.mode == "expert" else WORD_LIST)]
        if guess not in wordlist:
            return await interaction.followup.send("❌ Word not recognized.", ephemeral=True)

        users = globals().get("users")
        if users is None:
            return await interaction.followup.send("⚠️ Database is not initialized. Try again later.", ephemeral=True)

        user = await users.find_one({"_id": str(interaction.user.id)}) or {"_id": str(interaction.user.id)}
        wordle_data = user.get("wordle", {})
        today_data = wordle_data.get(today_key,
                                     {"word": self.word, "guesses": [], "completed": False, "mode": self.mode})

        if today_data.get("completed"):
            return await interaction.followup.send("✅ You've already completed today's Wordle.", ephemeral=True)

        feedback = wordle_feedback(guess, self.word)
        today_data["guesses"].append((guess, feedback))

        # Persist a hint for normal mode so it doesn't change every submit
        if self.mode == "normal" and not today_data.get("hint"):
            today_data["hint"] = generate_hint(self.word)

        reward = 0
        completed = (guess == self.word)

        if completed:
            attempt = len(today_data["guesses"])
            reward = (REWARDS_EXPERT if self.mode == "expert" else REWARDS_NORMAL).get(attempt, 1000)
            try:
                definition = await asyncio.wait_for(get_definition(self.word), timeout=2.5)
            except Exception as e:
                print(f"[WARN] definition fetch failed: {e}")
                definition = None
            def_text = f"\n📚 Definition: *{definition}*" if definition else ""
            result_msg = f"🎉 Correct! You solved it in **{attempt}** tries and earned **{reward} 🥖**!{def_text}"
            today_data["completed"] = True
        elif len(today_data["guesses"]) >= 10:
            result_msg = f"❌ Out of attempts! The word was **{self.word.upper()}**."
            today_data["completed"] = True
        else:
            result_msg = feedback

        wordle_data[today_key] = today_data  # ✅ use the same key you read with

        update_doc = {"$set": {"wordle": wordle_data}}
        if reward:
            update_doc["$inc"] = {"wallet": int(reward)}

        try:
            await users.update_one({"_id": str(interaction.user.id)}, update_doc, upsert=True)
        except Exception as e:
            print(f"[ERROR] Mongo update failed in WordleGuessModal.on_submit: {e}")

        # Build embed
        history = "\n".join(f"`{g.upper()}` → {f}" for g, f in today_data["guesses"])
        hint_line = f"**Hint:** {today_data.get('hint', '')}\n" if self.mode == "normal" and today_data.get(
            "hint") else ""
        desc = f"{hint_line}**Word length:** **{len(self.word)}** letters\n\n{history}\n\n{result_msg}"

        embed = Embed(
            title=f"🟩 Wordle - {self.mode.capitalize()} Mode",
            description=desc,
            color=0x2ecc71 if self.mode == "normal" else 0xe74c3c
        )

        try:
            channel = interaction.client.get_channel(self.channel_id) or await interaction.client.fetch_channel(
                self.channel_id)
            msg = await channel.fetch_message(self.message_id)
            view = None if today_data.get("completed") else GuessButton(self.bot, self.mode, self.word, self.user_data,
                                                                        interaction.user)
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"[ERROR] Failed to edit original Wordle message: {e}")
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e2:
                print(f"[ERROR] followup send also failed: {e2}")


class ModeSelect(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        print("[DEBUG] ModeSelect View initialized")

    @ui.button(label="Normal Mode", style=ButtonStyle.success, custom_id="wordle_normal")
    async def normal(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[DEBUG] Normal Mode clicked by {interaction.user}")
        await start_wordle_game(interaction, "normal")

    @ui.button(label="Expert Mode", style=ButtonStyle.danger, custom_id="wordle_expert")
    async def expert(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[DEBUG] Expert Mode clicked by {interaction.user}")
        await start_wordle_game(interaction, "expert")


@bot.command()
async def wordle(ctx):
    print(f"[DEBUG] Wordle command triggered by {ctx.author}")
    await ctx.send("🟩 Choose your difficulty:", view=ModeSelect())


class GuessButton(ui.View):
    def __init__(self, bot, mode, word, user_data, author):
        super().__init__(timeout=None)
        self.bot = bot
        self.mode = mode
        self.word = word
        self.user_data = user_data
        self.author = author

    @ui.button(label="✍️ Submit Guess", style=ButtonStyle.primary)
    async def guess(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ This isn't your game.", ephemeral=True)
        # ✅ pass the message & channel IDs so the modal can edit the right message
        modal = WordleGuessModal(
            self.bot, self.mode, self.word, self.user_data, interaction,
            message_id=interaction.message.id,
            channel_id=interaction.channel.id
        )
        await interaction.response.send_modal(modal)


async def start_wordle_game(interaction: Interaction, mode: str):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_key = f"{today}:{mode}"

    print(f"[DEBUG] start_wordle_game triggered by {interaction.user} - Mode: {mode}")
    try:
        await interaction.response.defer(thinking=False)
        print("[DEBUG] Interaction deferred successfully")
    except Exception as e:
        print(f"[ERROR] Could not defer interaction: {e}\n{traceback.format_exc()}")

    try:
        users = globals().get("users")
        if users is None:
            print("[ERROR] Global 'users' collection is not initialized.")
            return await interaction.followup.send("⚠️ Database is not initialized. Try again later.", ephemeral=True)

        uid = str(interaction.user.id)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        now = datetime.utcnow()

        user = await users.find_one({"_id": uid}) or {"_id": uid}

        # === Prevent multiple active games (safe + migrate legacy keys) ===
        wordle_data = user.get("wordle", {})
        if not isinstance(wordle_data, dict):
            wordle_data = {}

        # Migrate legacy "YYYY-MM-DD" key to "YYYY-MM-DD:mode" if present
        legacy_key = today  # e.g., "2025-08-08"
        if legacy_key in wordle_data and isinstance(wordle_data[legacy_key], dict):
            legacy = wordle_data[legacy_key]
            # only migrate if legacy is for this mode or unspecified
            if legacy.get("mode") in (None, mode):
                wordle_data[today_key] = legacy
                del wordle_data[legacy_key]

        today_data = wordle_data.get(today_key)

        if today_data:
            # A game exists for this date+mode
            if not today_data.get("completed", False):
                # Check 2-minute inactivity expiry
                start_time_str = today_data.get("start_time")
                start_time = None
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str)
                    except Exception:
                        start_time = None

                if start_time and (now - start_time).total_seconds() < 120:
                    return await interaction.followup.send(
                        "⚠️ You already have a Wordle game in progress. Finish it before starting a new one.",
                        ephemeral=True
                    )
                else:
                    # Expired or invalid -> clear the slot so a new game can start
                    print("[DEBUG] Expired or invalid Wordle game found - resetting.")
                    del wordle_data[today_key]

        # === Enforce 24h cooldown after completion (per mode) ===
        completed_dates_this_mode = []
        for k, data in wordle_data.items():
            # keys look like "YYYY-MM-DD:normal" or "YYYY-MM-DD:expert"
            if k.endswith(f":{mode}") and data.get("completed", False):
                date_str = k.split(":")[0]
                try:
                    completed_dates_this_mode.append(datetime.strptime(date_str, "%Y-%m-%d"))
                except:
                    pass

        if completed_dates_this_mode:
            last_date = max(completed_dates_this_mode)
            if (now - last_date).total_seconds() < 28800:
                return await interaction.followup.send(
                    f"⏳ You must wait 8 hours after finishing your last **{mode.capitalize()}** game to start a new one.",
                    ephemeral=True
                )

        cooldowns = user.get("cooldowns", {})
        cd_key = f"wordle_{mode}"
        if cd_key in cooldowns and datetime.fromisoformat(cooldowns[cd_key]) > now:
            remaining = datetime.fromisoformat(cooldowns[cd_key]) - now
            hours, remainder = divmod(remaining.seconds, 3600)
            minutes = remainder // 60
            print(f"[DEBUG] Cooldown active for {interaction.user}: {hours}h {minutes}m left")
            try:
                await interaction.edit_original_response(
                    content=f"🕒 You already played {mode} mode. Try again in **{hours}h {minutes}m**.",
                    embed=None, view=None
                )
            except Exception as e2:
                print(f"[ERROR] edit_original_response failed (cooldown): {e2}\n{traceback.format_exc()}")
                await interaction.followup.send(
                    f"🕒 You already played {mode} mode. Try again in **{hours}h {minutes}m**.",
                    ephemeral=True
                )
            return

        # === Pick word or resume existing ===
        if today_key not in wordle_data:
            word = random.choice(WORD_LIST_EXPERT if mode == "expert" else WORD_LIST)
            stored_hint = generate_hint(word) if mode == "normal" else ""
            wordle_data[today_key] = {
                "word": word,
                "guesses": [],
                "completed": False,
                "mode": mode,
                "start_time": now.isoformat(),
                "hint": stored_hint
            }
        else:
            word = wordle_data[today_key]["word"]
            stored_hint = wordle_data[today_key].get("hint", generate_hint(word) if mode == "normal" else "")
            wordle_data[today_key]["hint"] = stored_hint

        print(f"[DEBUG] Word selected: {word} for {interaction.user}")

        guesses = wordle_data[today_key]["guesses"]
        history = "\n".join(f"`{g.upper()}` → {f}" for g, f in guesses) if guesses else "*No guesses yet.*"
        hint_line = f"**Hint:** {stored_hint}\n" if mode == "normal" and stored_hint else ""
        embed = Embed(
            title=f"🟩 Wordle - {mode.capitalize()} Mode",
            description=f"{hint_line}*(Word length: {len(word)} letters)*\n\n{history}",
            color=0x2ecc71 if mode == "normal" else 0xe74c3c
        )

        if guesses and guesses[-1][0] == word:  # last guess equals the word
            tries_used = len(guesses)
            await record_wordle_best(interaction.user.id, tries_used, mode)

        cooldowns[cd_key] = (now + timedelta(hours=8)).isoformat()
        await users.update_one(
            {"_id": uid},
            {"$set": {"cooldowns": cooldowns, "wordle": wordle_data}},
            upsert=True
        )

        view = GuessButton(interaction.client, mode, word, user, interaction.user)
        print(f"[DEBUG] Prepared view; attempting to edit original response...")

        await interaction.edit_original_response(embed=embed, view=view)
        print("[DEBUG] edit_original_response succeeded")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] start_wordle_game crashed: {e}\n{tb}")
        try:
            await interaction.edit_original_response(
                content="⚠️ Something went wrong starting the game. Check logs.",
                embed=None, view=None
            )
        except Exception as e2:
            print(f"[ERROR] edit_original_response failed (error fallback): {e2}\n{traceback.format_exc()}")
            try:
                await interaction.followup.send("⚠️ Something went wrong starting the game. Check logs.",
                                                ephemeral=True)
            except Exception as e3:
                print(f"[ERROR] followup.send also failed: {e3}\n{traceback.format_exc()}")


# ============================
# BLACK HOLE EFFECT
# ============================

class BlackHoleEffect:
    GIF_URL = "https://cdn.discordapp.com/attachments/1010943160207294527/1403555907182526484/ezgif-5565d271897a6e.gif?ex=6897facd&is=6896a94d&hm=0fa8bea1c955f2392569577c3699d40a0304384c415de343839a682106332bcf&"

    @staticmethod
    async def trigger(ctx, users_col, winner_id: int, max_victims: int = 3, percent: float = 0.10):
        """
        Steals `percent` of wallet from up to `max_victims` random users (wallet>0, not the winner),
        credits total to winner, and sends an embed. Works in DMs or guilds (DB-first sampling).
        """
        try:
            uid = str(winner_id)
            stolen_total = 0
            details = []
            victims_used = 0

            # DB-first sampling
            pipeline = [
                {"$match": {"_id": {"$ne": uid}, "wallet": {"$gte": 50000}}},
                {"$sample": {"size": max_victims * 3}},
                {"$project": {"_id": 1, "wallet": 1}},
            ]
            victims_docs = []
            async for doc in users_col.aggregate(pipeline):
                victims_docs.append(doc)

            # Optional fallback to guild members
            if not victims_docs and getattr(ctx, "guild", None):
                print("[TH][BH] DB sample empty; falling back to guild members.")
                import random
                members = [m for m in ctx.guild.members if not m.bot and m.id != winner_id]
                random.shuffle(members)
                for m in members[:max_victims * 3]:
                    d = await users_col.find_one({"_id": str(m.id)}, {"wallet": 1})
                    if d and int(d.get("wallet", 0)) >= 50000:
                        victims_docs.append(d)

            # Execute steals
            for v in victims_docs:
                if victims_used >= max_victims:
                    break

                vid = v["_id"]
                vdoc = await users_col.find_one({"_id": vid}, {"wallet": 1})
                vw = int(vdoc.get("wallet", 0)) if vdoc else 0
                take = int(vw * percent)
                if take <= 0:
                    print(f"[BH] Skip {vid}: wallet={vw}, take={take}")
                    continue

                res = await users_col.update_one(
                    {"_id": vid, "wallet": {"$gte": take}},
                    {"$inc": {"wallet": -take}}
                )
                if res.modified_count == 1:
                    victims_used += 1
                    stolen_total += take
                    mention = f"<@{vid}>"
                    if getattr(ctx, "guild", None):
                        m = ctx.guild.get_member(int(vid))
                        if m:
                            mention = m.mention
                    details.append(f"- {mention} lost **{take:,} 🥖**")
                    print(f"[BH] Stole {take} from {vid}. Total={stolen_total}")
                else:
                    print(f"[BH] Could not steal from {vid} (race/insufficient).")

            # Credit winner
            if stolen_total > 0:
                await users_col.update_one({"_id": uid}, {"$inc": {"wallet": stolen_total}}, upsert=True)

            # Build & send embed
            import discord
            msg = (
                f"🕳️ A **Black Hole** devoured 🥖 from **{victims_used}** random users "
                f"and gave <@{uid}> **{stolen_total:,} 🥖**! 😈"
            )
            if details:
                msg += "\n\n" + "\n".join(details)
            else:
                msg += "\n\nNo one has over 50,000 bread right now... yall broke af😢"

            embed = discord.Embed(
                title="🕳️ BLACK HOLE",
                description=msg,
                color=discord.Color.dark_purple()
            )
            embed.set_image(url=BlackHoleEffect.GIF_URL)

            await ctx.send(embed=embed)
            print(f"[BH] Done. Winner={uid}, stolen_total={stolen_total}, victims_used={victims_used}")

        except Exception as e:
            import traceback
            print(f"[BH][ERROR] {e}\n{traceback.format_exc()}")
            try:
                await ctx.send("⚠️ Black Hole encountered an error. Check logs.")
            except Exception:
                pass


# ============================
# COMMAND: USE BLACK HOLE
# ============================

@bot.command(name="useblackhole", aliases=["usebh", "bh", "blackhole"])
async def use_black_hole(ctx):
    uid = str(ctx.author.id)
    users_col = globals().get("users")
    if users_col is None:
        users_col = bot.db["users"]
    now = datetime.utcnow()

    # === Cooldown check ===
    user_data = await users_col.find_one({"_id": uid}, {"cooldowns": 1, "inventory": 1})
    cooldowns_data = user_data.get("cooldowns", {}) if user_data else {}
    last_used = cooldowns_data.get("black_hole")

    if last_used:
        if isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)
        elapsed = (now - last_used).total_seconds()
        cooldown_seconds = 24 * 3600  # 1 day
        if elapsed < cooldown_seconds:
            remaining = cooldown_seconds - elapsed
            days = int(remaining // 86400)
            hours = int((remaining % 86400) // 3600)
            minutes = int((remaining % 3600) // 60)
            return await ctx.send(
                f"⏳ You must wait {days}d {hours}h {minutes}m before using another **Black Hole**."
            )

    # === Inventory check ===
    inv = (user_data or {}).get("inventory", {})
    count = int(inv.get("black_hole", 0))
    if count <= 0:
        return await ctx.send("❌ You don't have a **Black Hole** in your inventory.")

    # === Confirmation prompt ===
    class ConfirmBlackHole(discord.ui.View):
        def __init__(self, author):
            super().__init__(timeout=30)
            self.author = author
            self.result = None

        @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.success)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != self.author:
                return await interaction.response.send_message("❗ Only you can confirm this.", ephemeral=True)
            self.result = True
            self.stop()
            await interaction.response.defer()

        @discord.ui.button(label="❌ No", style=discord.ButtonStyle.danger)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user != self.author:
                return await interaction.response.send_message("❗ Only you can cancel this.", ephemeral=True)
            self.result = False
            self.stop()
            await interaction.response.defer()

    view = ConfirmBlackHole(ctx.author)
    await ctx.send(
        f"🕳️ You have **{count}x Black Hole**.\n"
        "Do you want to activate one now? This will steal from up to 3 random users.",
        view=view
    )
    await view.wait()

    if view.result is None:
        return await ctx.send("⏳ Timed out. Black Hole not used.")
    if not view.result:
        return await ctx.send("❌ Black Hole usage cancelled.")

    # === Consume item & set cooldown ===
    res = await users_col.update_one(
        {"_id": uid, "inventory.black_hole": {"$gte": 1}},
        {
            "$inc": {"inventory.black_hole": -1},
            "$set": {"cooldowns.item_usage.🕳 Black Hole": now.isoformat()}
        }
    )
    if res.modified_count != 1:
        return await ctx.send("❌ Couldn't use it right now. Try again.")

    # === Trigger effect ===
    await BlackHoleEffect.trigger(ctx, users_col, ctx.author.id, max_victims=3, percent=0.10)


# ==============================
# ====== GIVE / GET_ITEM =======
# ==============================

def _norm_item_key(raw: str) -> str:
    return str(raw).strip().lower().replace(' ', '_').replace('-', '_')


@bot.command(name="get", aliases=["gi"])
async def get_item(ctx, item: str, amount: int = 1):
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot creators can use this.")
    if amount <= 0:
        return await ctx.send("❌ Amount must be a positive integer.")

    item_key = _norm_item_key(item)

    users_col = globals().get("users")
    if users_col is None:
        users_col = bot.db["users"]

    uid = str(ctx.author.id)

    await users_col.update_one(
        {"_id": uid},
        {"$inc": {f"inventory.{item_key}": int(amount)}},
        upsert=True
    )

    await ctx.send(f"✅ Minted **{amount}× {item_key}** to your inventory, {ctx.author.mention}.")


@bot.command(aliases=["itemgive", "giveitem", "igive", "givei"])
async def give_item(ctx, item: str, member: discord.Member, amount: int = 1):
    """
    Creator-only. Gives (mints) <amount> of <item> directly to the target user.
    Usage: !give <item> @user [amount]
    """
    if ctx.author.id not in CREATOR_IDS:
        return await ctx.send("🚫 Only the bot creators can use this.")

    item_key = _norm_item_key(item)
    if amount <= 0:
        return await ctx.send("❌ Amount must be a positive integer.")

    users_col = globals().get("users")
    if users_col is None:
        users_col = bot.db["users"]

    tid = str(member.id)

    await users_col.update_one(
        {"_id": tid},
        {"$inc": {f"inventory.{item_key}": int(amount)}},
        upsert=True
    )

    await ctx.send(f"🎁 Gave **{amount}× {item_key}** to {member.mention}.")


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

    # Read target scope (support both old and new flags)
    has_target_scope_old = user_data.get("active_buffs", {}).get("target_scope", False)
    has_target_scope_new = user_data.get("buffs", {}).get("scope", False)
    has_target_scope = bool(has_target_scope_old or has_target_scope_new)
    digs_allowed = 2 if has_target_scope else 1

    # Lucky Magnet (new flag)
    has_magnet = user_data.get("buffs", {}).get("magnet", False)

    # Cooldown check
    cooldowns_data = user_data.get("cooldowns", {})
    last_used = cooldowns_data.get("treasurehunt")
    if last_used:
        if isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)
        elapsed = (now - last_used).total_seconds()
        if elapsed < 21600:
            hours = int((21600 - elapsed) // 3600)
            minutes = int(((21600 - elapsed) % 3600) // 60)
            return await ctx.send(f"⏳ You must wait {hours}h {minutes}m before digging again.")

    # Base prize table (emoji, value, probability)
    prizes = [
        ("👑", 1000000, 0.005),  # Jackpot (very rare)
        ("💎", 100000, 0.045),  # Rare
        ("🗿", 200000, 0.04),  # Big prize
        ("🧨", -200000, 0.04),  # Big loss
        ("🪦", "death", 0.005),  # Lose all 🥖
        ("💣", -30000, 0.05),  # Minor trap
        ("🕳️", "black_hole", 0.15),  # Inventory item
        ("🪙", 5000, 0.165),  # Common bread
        ("🥖", 10000, 0.27),  # Common bread
        ("🎁", 20000, 0.23),  # Common prize
    ]

    # 🔸 Apply Lucky Magnet odds tweak (modest)
    if has_magnet:
        # Slightly boost rare/high-pay & slightly reduce common
        def adjust_prob(emoji, p):
            rare_boost = 1.25 if emoji in ("👑", "💎", "🗿") else 1.0
            common_nerf = 0.92 if emoji in ("🪙", "🥖", "🎁") else 1.0
            return p * rare_boost * common_nerf

        adjusted = [(e, v, adjust_prob(e, p)) for (e, v, p) in prizes]
        total = sum(p for (_, _, p) in adjusted)
        prizes = [(e, v, p / total) for (e, v, p) in adjusted]  # renormalize

        # consume the magnet
        await users.update_one({"_id": user_id}, {"$unset": {"buffs.magnet": ""}})

    # Build prize pool from (possibly) adjusted probabilities
    prize_pool = []
    for emoji, value, prob in prizes:
        prize_pool += [(emoji, value)] * max(1, int(prob * 100000))

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
                    return await interaction.response.send_message("❗ Only the player who started this game can dig.",
                                                                   ephemeral=True)
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
                    await ctx.send(
                        "💀 Haha, you dug your own grave and tripped. You died and lost all your bread loser.")
                elif value == "black_hole":
                    # Grant an inventory item instead of triggering immediately
                    await users.update_one(
                        {"_id": user_id},
                        {"$inc": {"inventory.black_hole": 1}},
                        upsert=True
                    )
                    await ctx.send(
                        "🕳️ **You found a Black Hole!**\n"
                        "It was added to your inventory. Use it anytime with **`;useblackhole`** to siphon bread 😈"
                    )

                elif value > 0:
                    await users.update_one({"_id": user_id}, {"$inc": {"wallet": value}})
                    await ctx.send(f"🪓 You dug up a {emoji} and found **{value:,} 🥖**!")
                else:
                    await users.update_one({"_id": user_id}, {"$inc": {"wallet": value}})
                    await ctx.send(f"💥 You hit a {emoji} and **lost {-value:,} 🥖!**")

                if self.parent.digs_done == self.parent.digs_allowed:
                    await users.update_one({"_id": user_id}, {"$set": {"cooldowns.treasurehunt": now}})
                    # consume Target Scope whichever flag was used
                    if has_target_scope_old:
                        await users.update_one({"_id": user_id}, {"$unset": {"active_buffs.target_scope": ""}})
                    if has_target_scope_new:
                        await users.update_one({"_id": user_id}, {"$unset": {"buffs.scope": ""}})

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


# ============================
# CREATOR BYPASS FOR DISABLED CMDS
# ============================

@bot.check
async def disabled_commands_check(ctx: commands.Context):
    # no command (e.g., bare prefix) -> allow
    if not ctx.command:
        return True

    name = ctx.command.qualified_name  # actual invoked command name
    # Always allow these admin utilities
    if name in {"enable", "disable", "disabled"}:
        return True

    settings = await bot_settings.find_one({"_id": "disabled_commands"}) or {}
    disabled = set(settings.get("commands", []))

    if name not in disabled:
        return True

    # Disabled: creators bypass (with reminder), others blocked
    if ctx.author.id in CREATOR_IDS:
        try:
            await ctx.send(f"⚠️ `{name}` is currently **disabled**. (creator bypass active)", delete_after=6)
        except Exception:
            pass
        return True

    # Non-creator: block by raising DisabledCommand (your handler can format this)
    raise commands.DisabledCommand(f"`{name}` is currently disabled.")


# ────────────────────────────────────────────────────
# COMMAND: RESET ALL (Creator Only)
# ────────────────────────────────────────────────────

@bot.command(name="resetall")
async def resetall(ctx):
    """Resets all user data: wallets, banks, cooldowns, and items, but keeps long-term streaks/records."""
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

            try:
                await interaction.response.defer()  # Prevent "interaction failed"

                # 1) Reset economy, cooldowns, and inventory (KEEP stats/streaks)
                result_bal_cd_inv = await users.update_many(
                    {},
                    {
                        # bread balances
                        "$set": {
                            "wallet": 0,
                            "bank": 0,
                            # wipe all cooldowns (commands, items, wordle, etc.)
                            "cooldowns": {},
                            # wipe all items to zero by clearing the inventory map
                            "inventory": {}
                        },
                        # remove any legacy/stray cooldown roots so everything starts clean
                        "$unset": {
                            "cooldown": "",
                            "daily_cooldown": "",
                            "weekly_cooldown": "",
                            "wordle": "",
                            # DO NOT TOUCH "stats": keep streaks/records intact
                        }
                    }
                )

                # 2) Downgrade wedding ring rarity to Gualmar for married users (preserve marriages)
                result_rings = await users.update_many(
                    {
                        "$or": [
                            {"married_to": {"$exists": True}},  # user is/was married
                            {"marriage_ring": {"$exists": True}},  # ring field exists
                        ]
                    },
                    {
                        "$set": {"marriage_ring": "gualmar"}  # internal key used by your ring boosts
                    }
                )

                # Clear in-memory data you already track
                active_trivia.clear()
                trivia_answers.clear()

                await interaction.edit_original_response(
                    content=(
                        "♻️ Reset complete.\n"
                        f"• Users updated: {result_bal_cd_inv.modified_count}\n"
                        f"• Rings downgraded to Gualmar: {result_rings.modified_count}\n\n"
                        "What changed:\n"
                        "• Wallets & banks → 0\n"
                        "• All cooldowns cleared\n"
                        "• Inventory cleared (all items to 0)\n"
                        "• Wedding rings set to Gualmar (marriages preserved)\n"
                        "What stayed:\n"
                        "• Streaks/records like marriage duration, joint daily/weekly claims, Wordle bests, etc."
                    ),
                    view=None
                )

            except Exception:
                print("[RESETALL ERROR] Reset failed.")
                traceback.print_exc()
                await interaction.followup.send("❌ Something went wrong during reset.", ephemeral=True)

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user != ctx.author:
                return await interaction.response.send_message(
                    "Only the command issuer can cancel.", ephemeral=True
                )
            await interaction.response.edit_message(
                content="❎ Reset all cancelled.", view=None
            )

    await ctx.send(
        "⚠️ This will reset everything including banks, wallets, inventories, etc, "
        "while keeping stats and streaks. "
        "Wedding rings will be downgraded to Gualmar. Proceed?",
        view=ConfirmResetAll()
    )


# ============================
# ECONOMY COMMANDS
# ============================

# -------- WORKOUT HELPERS ---------

def pct(x: float) -> str:
    return f"{int(round(x * 100))}%"


def build_workout_prob_fields():
    """
    Build two concise fields:
    - Standard Workouts (Legs/Arms/Chest/Cardio)
    - HIIT
    Uses values from WORKOUTS and SPECIAL_EVENT.
    """
    # use one representative for standard workouts (they share the same config)
    std_key = next(k for k in WORKOUTS if k != "hiit")
    std = WORKOUTS[std_key]
    hiit = WORKOUTS["hiit"]

    std_value = (
        f"**Lose:** {pct(std['lose_chance'])} "
        f"({bread_fmt(std['lose_range'][0])}–{bread_fmt(std['lose_range'][1])})\n"
        f"**Win:** {pct(std['win_chance'])} "
        f"({bread_fmt(std['win_range'][0])}–{bread_fmt(std['win_range'][1])})\n"
        f"**Neutral:** {pct(1.0 - std['lose_chance'] - std['win_chance'])}\n"
        f"**Special:** {pct(SPECIAL_EVENT['chance'])} jackpot {bread_fmt(SPECIAL_EVENT['amount'])}"
    )

    hiit_value = (
        f"**Lose:** {pct(hiit['lose_chance'])} "
        f"({bread_fmt(hiit['lose_range'][0])}–{bread_fmt(hiit['lose_range'][1])})\n"
        f"**Win:** {pct(hiit['win_chance'])} "
        f"({bread_fmt(hiit['win_range'][0])}–{bread_fmt(hiit['win_range'][1])})\n"
        f"**Neutral:** {pct(max(0.0, 1.0 - hiit['lose_chance'] - hiit['win_chance']))}\n"
        f"**Special:** {pct(SPECIAL_EVENT['chance'])} jackpot {bread_fmt(SPECIAL_EVENT['amount'])}"
    )

    return [
        ("📊 Standard Workouts (Legs / Arms / Chest / Cardio)", std_value),
        ("⚡ HIIT (High Risk / High Reward)", hiit_value),
    ]


# ======================
# ====== WORKOUT =======
# ======================

WORKOUT_COOLDOWN_HOURS = 2

WORKOUTS = {
    "legs": {
        "label": "Legs",
        "emoji": "🦵",
        "style": ButtonStyle.success,
        "lose_chance": 0.40,
        "win_chance": 0.50,
        "lose_range": (5_000, 20_000),
        "win_range": (5_000, 40_000),
        "muscle": "quads",
    },
    "arms": {
        "label": "Arms",
        "emoji": "💪",
        "style": ButtonStyle.primary,
        "lose_chance": 0.40,
        "win_chance": 0.50,
        "lose_range": (5_000, 20_000),
        "win_range": (5_000, 40_000),
        "muscle": "biceps",
    },
    "chest": {
        "label": "Chest",
        "emoji": "🏋️",
        "style": ButtonStyle.secondary,
        "lose_chance": 0.40,
        "win_chance": 0.50,
        "lose_range": (5_000, 20_000),
        "win_range": (5_000, 40_000),
        "muscle": "pecs",
    },
    "cardio": {
        "label": "Cardio",
        "emoji": "🏃",
        "style": ButtonStyle.danger,
        "lose_chance": 0.40,
        "win_chance": 0.50,
        "lose_range": (5_000, 20_000),
        "win_range": (5_000, 40_000),
        "muscle": "calves",
    },
    "hiit": {
        "label": "HIIT",
        "emoji": "🔥",
        "style": ButtonStyle.danger,
        "lose_chance": 0.60,
        "win_chance": 0.40,
        "lose_range": (15_000, 50_000),
        "win_range": (25_000, 100_000),
        "muscle": "whole body",
    },
}

SPECIAL_EVENT = {
    "chance": 0.05,
    "amount": 200_000,
    "messages": [
        "You got featured on national TV for your **{workout}** grind and earned {bread}!",
        "A fitness sponsor loved your **{workout}** montage and dropped {bread} on you!",
        "Your insane **{workout}** record broke the internet — payout: {bread}!",
        "A famous athlete shouted you out for your **{workout}** session. Bonus {bread}!",
        "Your **{workout}** streak landed you a massive grant worth {bread}!",
    ]
}


def bread_fmt(amount: int) -> str:
    return f"🥖{amount:,}"


LOSS_MESSAGES = [
    "You strained your **{muscle}** and had to see a physiotherapist, costing {bread}.",
    "You pulled your **{muscle}** mid set. Recovery treatments cost {bread}.",
    "You twisted your ankle badly during training. Medical bills cost {bread}.",
    "You fractured a rib while pushing too hard. Doctor’s visit cost {bread}.",
    "You hit your head on the barbell rack. The ER charged you {bread}.",
    "You blacked out mid-set and needed first aid. It cost you {bread}.",
    "You dropped a dumbbell on your foot — the clinic visit cost {bread}.",
    "A weight plate slipped and smashed your toe. You paid {bread} for treatment.",
    "You slipped on sweat near the treadmill. Medical expenses cost {bread}.",
    "The bench press bar pinned you down. Spotters called for help, costing {bread}.",
    "You forgot the safety clips. The plates made a run for it — there goes {bread}.",
    "You flexed too hard in the mirror and pulled nothing but your dignity. Hospital bill: {bread}.",
    "Bro skipped warmup, skipped gains, skipped {bread} too.",
    "Tried to impress the gym crush, failed the lift, succeeded at losing {bread}.",
]

WIN_MESSAGES = [
    "You beat your personal record at **{workout}** and earned {bread} in prize money!",
    "You crushed a local competition in **{workout}** and won {bread}!",
    "Your insane **{workout}** session went viral — sponsors gave you {bread}!",
    "You impressed the crowd with your **{workout}** strength and won {bread}!",
    "Your dedication to **{workout}** training paid off. You collected {bread} in rewards!",
    "You dominated a **{workout}** challenge and claimed {bread}!",
    "Someone mistook you for a personal trainer and tipped you {bread}.",
    "A protein brand gave you free samples and {bread} for posting a selfie.",
    "Your **{workout}** form became a meme. Strangely, you earned {bread} from it.",
    "You spotted a stranger at the gym and they rewarded you {bread} for saving their set.",
    "The gym staff bet against you finishing your set. Joke’s on them — you won {bread}.",
    "You flexed in the mirror and someone threw {bread} at you like a stripper. Respect.",
]

NEUTRAL_MESSAGES = [
    "You finished your **{workout}** and felt the burn. No bread gained or lost.",
    "Decent **{workout}** — progress made, wallet unchanged.",
    "You focused on form today. No bread, but solid fundamentals.",
    "Stretching and mobility day. Your future self says thanks (no bread).",
]


class WorkoutView(View):
    def __init__(self, ctx: commands.Context, user_id: int):
        super().__init__(timeout=45)
        self.ctx = ctx
        self.user_id = user_id
        self.is_processing = False
        for key, data in WORKOUTS.items():
            label = f"{data['emoji']} {data['label']}"
            self.add_item(WorkoutButton(key, data, self, label=label))

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True
        try:
            await self.ctx.message.edit(view=self)
        except Exception:
            pass


class WorkoutButton(Button):
    def __init__(self, workout_key: str, workout_data: dict, parent_view: WorkoutView, label: str):
        super().__init__(label=label, style=workout_data["style"])
        self.workout_key = workout_key
        self.workout_data = workout_data
        self.parent_view = parent_view

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.parent_view.user_id:
            return await interaction.response.send_message(
                "❌ Only the command user can choose a workout.", ephemeral=True
            )
        if self.parent_view.is_processing:
            return await interaction.response.send_message(
                "⏳ Processing your workout...", ephemeral=True
            )
        self.parent_view.is_processing = True

        try:
            uid = str(interaction.user.id)
            user = await get_user(interaction.user.id)

            now = datetime.utcnow()
            cd_until = (user.get("cooldowns") or {}).get("workout")
            if cd_until and cd_until > now:
                return await interaction.response.send_message(
                    f"⏳ You can workout again in **{hm(cd_until - now)}**.", ephemeral=True
                )

            data = self.workout_data

            if random.random() < SPECIAL_EVENT["chance"]:
                amount = SPECIAL_EVENT["amount"]
                msg = random.choice(SPECIAL_EVENT["messages"]).format(
                    workout=data["label"], bread=bread_fmt(amount)
                )
                delta = amount
                new_wallet = int(user.get("wallet", 0)) + delta
                cd_new = now + timedelta(hours=WORKOUT_COOLDOWN_HOURS)
                await users.update_one(
                    {"_id": uid},
                    {"$set": {"wallet": new_wallet, "cooldowns.workout": cd_new}}
                )
                result = Embed(
                    title=f"💪 Workout Result: {data['label']}",
                    description=f"{msg}\n\n**Net Change:** {bread_fmt(delta)}",
                    color=0x9B59B6,
                )
                for child in self.parent_view.children:
                    if isinstance(child, Button):
                        child.disabled = True
                return await interaction.response.edit_message(embed=result, view=self.parent_view)

            roll = random.random()
            outcome = None
            delta = 0

            if roll < data["lose_chance"]:
                loss = random.randint(*data["lose_range"])
                delta = -loss
                template = random.choice(LOSS_MESSAGES)
                outcome = template.format(muscle=data["muscle"], bread=bread_fmt(loss))
            elif roll < data["lose_chance"] + data["win_chance"]:
                gain = random.randint(*data["win_range"])
                delta = gain
                template = random.choice(WIN_MESSAGES)
                outcome = template.format(workout=data["label"], bread=bread_fmt(gain))
            else:
                outcome = random.choice(NEUTRAL_MESSAGES).format(workout=data["label"])

            new_wallet = int(user.get("wallet", 0)) + delta
            cd_new = now + timedelta(hours=WORKOUT_COOLDOWN_HOURS)
            await users.update_one(
                {"_id": uid},
                {"$set": {"wallet": new_wallet, "cooldowns.workout": cd_new}}
            )

            result = Embed(
                title=f"💪 Workout Result: {data['label']}",
                description=f"{outcome}\n\n**Net Change:** {bread_fmt(delta)}",
                color=0x2ECC71 if delta > 0 else (0xE74C3C if delta < 0 else 0x95A5A6)
            )

            for child in self.parent_view.children:
                if isinstance(child, Button):
                    child.disabled = True

            await interaction.response.edit_message(embed=result, view=self.parent_view)

        finally:
            self.parent_view.is_processing = True


@bot.command(name="workout")
async def workout_cmd(ctx: commands.Context):
    user = await get_user(ctx.author.id)

    now = datetime.utcnow()
    cd_until = (user.get("cooldowns") or {}).get("workout")
    if cd_until and cd_until > now:
        return await ctx.send(f"⏳ You can workout again in **{hm(cd_until - now)}**.")

    embed = Embed(
        title="💪 Choose Your Workout",
        description="Pick a workout type below. Beware of injuries or glory!",
        color=0x3498DB
    )

    for name, value in build_workout_prob_fields():
        embed.add_field(name=name, value=value, inline=False)

    view = WorkoutView(ctx, ctx.author.id)
    sent = await ctx.send(embed=embed, view=view)
    view.message = sent


# ============================
# COMMAND: FISH
# ============================
import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import Embed, ButtonStyle, Interaction
from discord.ui import View, Button

try:
    bread_fmt
except NameError:
    def bread_fmt(amount: int) -> str:
        return f"🥖{amount:,}"

try:
    hm
except NameError:
    def hm(td: timedelta) -> str:
        total = int(td.total_seconds())
        hours, rem = divmod(total, 3600)
        mins = rem // 60
        if hours and mins: return f"{hours}h {mins}m"
        if hours: return f"{hours}h"
        return f"{mins}m"

FISH_COOLDOWN_HOURS = 2
MISS_CHANCE = 0.30  # 30% miss for all lures
FISHING_GIF_URL = "https://cdn.discordapp.com/attachments/962847107318951976/1408817151166644285/ezgif-333c37a7499d09.gif?ex=68ab1eb7&is=68a9cd37&hm=0b38a55bba9b00231778b52c4d88e92f506a6383ac32e39b398171f0c2474381&"  # <- put your GIF here

# Result GIFs
MISS_GIF_URL = "https://cdn.discordapp.com/attachments/962847107318951976/1408831697444929638/ezgif-7e899bc9e4918f.gif?ex=68ab2c43&is=68a9dac3&hm=10a83206c950d0a7ee710e481aafdd0d067b8e323091f929b1755868fe74d45b&"
JACKPOT_GIF_URL = "https://cdn.discordapp.com/attachments/962847107318951976/1408831696689823854/ve2ikofm8bs81-ezgif.com-crop.gif?ex=68ab2c43&is=68a9dac3&hm=69c10580ddc5390417b8c85542afe8a02faa1b71607195b3a998e313fbc4b489&"
CATCH_GIF_URL = "https://cdn.discordapp.com/attachments/962847107318951976/1408831697025503363/ezgif-7d7107a1efbf33.gif?ex=68ab2c43&is=68a9dac3&hm=cebd8d2aa0d79694f5b504dddf69b939b57b072220e46bac3a2c8cf023be0191&"

# ---------- Lure Config ----------
# Mythic chances: Nightcrawler 1%, Spinners 2%, Frog 3%, Rubber 5%
# Tier weights will be normalized automatically; they represent relative preference.
LURES = {
    "nightcrawler": {
        "label": "Nightcrawler",
        "emoji": "🪱",
        "style": ButtonStyle.success,
        "cost": 5_000,
        "mythic": 0.01,
        "weights": {  # relative weights for Common..Legend
            "common": 0.35,
            "uncommon": 0.18,
            "rare": 0.10,
            "epic": 0.04,
            "legend": 0.02,
        },
        "blurb": "Beginner’s classic. Cheap and reliable.",
    },
    "spinners": {
        "label": "Spinners",
        "emoji": "🌀",
        "style": ButtonStyle.primary,
        "cost": 10_000,
        "mythic": 0.02,
        "weights": {
            "common": 0.32,
            "uncommon": 0.18,
            "rare": 0.12,
            "epic": 0.05,
            "legend": 0.031,  # ensure Legendary > Mythic
        },
        "blurb": "Shiny and tempting. Better odds for bigger fish.",
    },
    "frog": {
        "label": "Frog Lures",
        "emoji": "🐸",
        "style": ButtonStyle.secondary,
        "cost": 15_000,
        "mythic": 0.03,
        "weights": {
            "common": 0.28,
            "uncommon": 0.18,
            "rare": 0.12,
            "epic": 0.06,
            "legend": 0.031,  # ensure Legendary > Mythic
        },
        "blurb": "Topwater thrills. Serious chance at rares.",
    },
    "rubber": {
        "label": "Rubber Bait",
        "emoji": "🎣",
        "style": ButtonStyle.danger,
        "cost": 20_000,
        "mythic": 0.05,
        "weights": {
            "common": 0.24,
            "uncommon": 0.16,
            "rare": 0.11,
            "epic": 0.07,
            "legend": 0.07,
        },
        "blurb": "Premium bait. Highest chance for epic + mythic.",
    },
}

# ---------- Fish Names By Tier (real-ish names) ----------
FISH_NAMES = {
    "common": ["Sunfish", "Carp", "Perch", "Bluegill", "Minnow"],
    "uncommon": ["Trout", "Catfish", "Bass", "Tilapia", "Crucian Carp"],
    "rare": ["Salmon", "Pike", "Walleye", "Mahi-Mahi", "Snapper"],
    "epic": ["Golden Koi", "Electric Eel", "Giant Grouper", "Swordfish", "Arctic Char"],
    "legend": ["Ancient Sturgeon", "Leviathan Minnow", "Ghost Barracuda", "Royal Marlin"],
    "mythic": ["Celestial Whale", "Dragon Carp", "Abyssal Seraphfish"],
}

# ---------- Reward Ranges ----------
REWARD_RANGES = {
    "common": (5_000, 15_000),
    "uncommon": (15_000, 30_000),
    "rare": (30_000, 60_000),
    "epic": (60_000, 100_000),
    "legend": (100_000, 150_000),
    "mythic": (200_000, 200_000),  # fixed jackpot
}

MISS_MESSAGES = [
    "You dropped the fishing rod like a dumbass. The fish applauded. You caught nothing.",
    "The fish outsmarted you and stole your bait. You reeled in pure disappointment.",
    "A sudden wave splashed your reel — line snapped, zero catch.",
    "You got a nibble… then tangled your line around your neck. No fish.",
    "A seagull swooped down and stole your bait. You’re baitless and fishless.",
    "You slipped on a wet rock. The fish laughed and swam away.",
]

# ---------- Catch flavor by tier ----------
CATCH_LINES = {
    "common": "You reeled in a **{fish}**. Not bad!",
    "uncommon": "Nice pull — a **{fish}**!",
    "rare": "Great catch! A **{fish}** leaps onto the deck.",
    "epic": "Epic haul! The mighty **{fish}** fights to the end.",
    "legend": "LEGENDARY catch! A **{fish}** stuns the crowd.",
    "mythic": "MYTHIC CATCH!!! The fabled **{fish}** has answered your call!",
}


# ---------- Probability helpers ----------
def compute_effective_probs(lure: dict, lure_key: str | None = None) -> dict:
    """
    Return effective probabilities (out of 1.0) for:
    miss, mythic, common, uncommon, rare, epic, legend
    given global MISS_CHANCE and lure's mythic + weights.
    For 'frog' and 'rubber' lures, force Common = 0% and move that share to Uncommon.
    """
    miss = MISS_CHANCE
    mythic = float(lure["mythic"])
    remaining = 1.0 - miss - mythic
    weights = dict(lure["weights"])  # copy

    # --- Adjustment: frog/rubber => 0% common, boost uncommon
    if lure_key in {"frog", "rubber"}:
        common_w = weights.get("common", 0.0)
        weights["common"] = 0.0
        weights["uncommon"] = weights.get("uncommon", 0.0) + common_w

    total_w = sum(weights.values())
    eff = {
        "miss": miss,
        "mythic": mythic,
    }
    if remaining < 0:
        remaining = 0.0
    for tier, w in weights.items():
        share = (w / total_w) * remaining if total_w > 0 else 0.0
        eff[tier] = share
    # Ensure explicit zeros exist
    for tier in ["common", "uncommon", "rare", "epic", "legend"]:
        eff.setdefault(tier, 0.0)
    return eff


def pick_tier_for_lure(lure_key: str) -> str | None:
    """Sample according to effective probabilities."""
    lure = LURES[lure_key]
    eff = compute_effective_probs(lure, lure_key=lure_key)
    r = random.random()
    cumulative = 0.0
    cumulative += eff["miss"]
    if r < cumulative:
        return None
    cumulative += eff["mythic"]
    if r < cumulative:
        return "mythic"
    for tier in ["common", "uncommon", "rare", "epic", "legend"]:
        cumulative += eff.get(tier, 0.0)
        if r < cumulative:
            return tier
    return "common"


def random_reward_for_tier(tier: str) -> int:
    low, high = REWARD_RANGES[tier]
    return low if low == high else random.randint(low, high)


def pct(x: float) -> str:
    return f"{round(x * 100, 2)}%"


def build_lure_table() -> str:
    """
    Build a compact monospaced 'table' of odds per lure, showing only
    Mythic and Legendary probabilities (and jackpot/range).
    """
    lines = []
    for key in ["nightcrawler", "spinners", "frog", "rubber"]:
        d = LURES[key]
        eff = compute_effective_probs(d, lure_key=key)
        block = [
            f"{d['emoji']} {d['label']} — {bread_fmt(d['cost'])}",
            f"  Legendary: {pct(eff.get('legend', 0)):>7}  {bread_fmt(REWARD_RANGES['legend'][0])}–{bread_fmt(REWARD_RANGES['legend'][1])}",
            f"  Mythic:    {pct(eff['mythic']):>7}  (jackpot {bread_fmt(200_000)})",
        ]
        lines.append("\n".join(block))
    return "```\n" + "\n\n".join(lines) + "\n```"


# ---- View & Buttons
class FishView(View):
    def __init__(self, ctx: commands.Context, user_id: int):
        super().__init__(timeout=45)
        self.ctx = ctx
        self.user_id = user_id
        self.is_processing = False
        for key in ["nightcrawler", "spinners", "frog", "rubber"]:
            data = LURES[key]
            label = f"{data['emoji']} {data['label']} — {bread_fmt(data['cost'])}"
            self.add_item(FishButton(key, data, self, label=label))

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True
        try:
            await self.ctx.message.edit(view=self)
        except Exception:
            pass


class FishButton(Button):
    def __init__(self, lure_key: str, lure_data: dict, parent_view: FishView, label: str):
        super().__init__(label=label, style=lure_data["style"])
        self.lure_key = lure_key
        self.lure_data = lure_data
        self.parent_view = parent_view

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.parent_view.user_id:
            return await interaction.response.send_message(
                "❌ Only the command user can choose a lure.", ephemeral=True
            )
        if self.parent_view.is_processing:
            return await interaction.response.send_message(
                "⏳ Processing your cast...", ephemeral=True
            )
        self.parent_view.is_processing = True

        try:
            uid = str(interaction.user.id)
            user = await get_user(interaction.user.id)

            now = datetime.utcnow()
            cd_until = (user.get("cooldowns") or {}).get("fish")
            if cd_until and cd_until > now:
                return await interaction.response.send_message(
                    f"⏳ You can fish again in **{hm(cd_until - now)}**.", ephemeral=True
                )

            cost = int(self.lure_data["cost"])
            wallet = int(user.get("wallet", 0))
            if wallet < cost:
                return await interaction.response.send_message(
                    f"❌ You need at least {bread_fmt(cost)} for **{self.lure_data['label']}**.",
                    ephemeral=True
                )

            # charge cost + set cooldown
            wallet -= cost
            cd_new = now + timedelta(hours=FISH_COOLDOWN_HOURS)
            await users.update_one(
                {"_id": uid},
                {"$set": {"wallet": wallet, "cooldowns.fish": cd_new}}
            )

            # roll outcome (using normalized effective probabilities)
            tier = pick_tier_for_lure(self.lure_key)
            used_lure_display = f"{self.lure_data['emoji']} {self.lure_data['label']}"
            net_delta = -cost  # we already paid cost; add reward below if any

            if tier is None:
                # Miss!
                miss_text = random.choice(MISS_MESSAGES)
                winnings = 0
                desc = (
                    f"**Lure:** {used_lure_display}\n"
                    f"**Result:** {miss_text}\n"
                    f"**Winnings:** {bread_fmt(winnings)}\n"
                    f"**Net:** {bread_fmt(net_delta)}"
                )
                color = 0x95A5A6
                result_gif = MISS_GIF_URL
            else:
                fish_name = random.choice(FISH_NAMES[tier])
                reward = random_reward_for_tier(tier)
                wallet += reward
                await users.update_one({"_id": uid}, {"$set": {"wallet": wallet}})
                net_delta += reward

                catch_line = CATCH_LINES[tier].format(fish=fish_name)
                desc = (
                    f"**Lure:** {used_lure_display}\n"
                    f"**Result:** {catch_line}\n"
                    f"**Winnings:** {bread_fmt(reward)}\n"
                    f"**Net:** {bread_fmt(net_delta)}"
                )

                if tier in {"legend", "mythic"}:
                    color = 0x9B59B6
                    result_gif = JACKPOT_GIF_URL
                else:
                    color = 0x1ABC9C
                    result_gif = CATCH_GIF_URL

            # Improved Result Embed with GIF and clean layout
            result = Embed(title="🎣 Fishing Result", description=desc, color=color)
            result.set_image(url=result_gif)

            for child in self.parent_view.children:
                if isinstance(child, Button):
                    child.disabled = True

            await interaction.response.edit_message(embed=result, view=self.parent_view)

        finally:
            self.parent_view.is_processing = True


# ---- Command
@bot.command(name="fish")
async def fish_cmd(ctx: commands.Context):
    user = await get_user(ctx.author.id)

    now = datetime.utcnow()
    cd_until = (user.get("cooldowns") or {}).get("fish")
    if cd_until and cd_until > now:
        return await ctx.send(f"⏳ You can fish again in **{hm(cd_until - now)}**.")

    # Build embed with compact per-lure odds table (Legendary + Mythic only)
    intro = (
        "Choose your **lure** below. Higher lures improve your chances for better fish.\n\n"
        "Odds per lure:"
    )
    table = build_lure_table()

    embed = Embed(title="🎣 Pick Your Lure", description=intro, color=0x3498DB)
    embed.add_field(name="Legendary & Mythic Odds", value=table, inline=False)
    if FISHING_GIF_URL:
        embed.set_image(url=FISHING_GIF_URL)  # Global GIF on start

    view = FishView(ctx, ctx.author.id)
    sent = await ctx.send(embed=embed, view=view)
    view.message = sent


# ============================
# COMMAND: EXPLORE
# ============================
import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import Embed, ButtonStyle, Interaction
from discord.ui import View, Button

# Assumes you already have:
# - bot (commands.Bot)
# - users (Motor collection)
# - get_user(user_id) -> dict

EXPLORE_COST = 10_000
EXPLORE_COOLDOWN_HOURS = 1

# location configs
EXPLORE_LOCATIONS = {
    "sahara": {
        "label": "Sahara Desert",
        "emoji": "🏜️",
        "color": ButtonStyle.success,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1408801278632530061/sahara-desert-spiritual-blog.jpg?ex=68ab0fef&is=68a9be6f&hm=0b5ce76bf5a8fa1e1d32e07869ceb3ec041560f3ab7db714d4394d05e5de032b&",
        "miss_chance": 0.30,
        "mythic_chance": 0.03,
        "miss_texts": [
            "A sandstorm swallowed your trail. You returned empty-handed.",
            "Mirages fooled you for hours… nothing but endless dunes."
        ],
        "common_items": [
            "Sun-Bleached Coin",
            "Sandstone Figurine",
            "Ancient Pottery Shard",
            "Caravan Tally Stick",
            "Glittering Glass Bead"
        ],
        "mythic_item": "Pharaoh’s Scarab",
    },
    "ruins": {
        "label": "Ancient Ruins",
        "emoji": "🏛️",
        "color": ButtonStyle.primary,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1408801277248405695/90.jpeg?ex=68ab0fee&is=68a9be6e&hm=21c2a72878837f054c98094e1ade8bfdbf8e6952e8a00e824cd76adacd61ab1c&",
        "miss_chance": 0.30,
        "mythic_chance": 0.03,
        "miss_texts": [
            "A collapsing wall nearly crushed you — you fled with nothing.",
            "You triggered a dart trap and bailed before finding anything."
        ],
        "common_items": [
            "Cracked Idol",
            "Bronze Tablet",
            "Stone Mask Fragment",
            "Inscribed Brick",
            "Temple Seal"
        ],
        "mythic_item": "Primeval Relic",
    },
    "forest": {
        "label": "Dark Forest",
        "emoji": "🌲",
        "color": ButtonStyle.secondary,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1408801278120956087/forest_fog_trees_147805_1600x900.jpg?ex=68ab0fee&is=68a9be6e&hm=3bf76a02d21e1d7a61ed093696dcff4124e1294f71ad0de5db28ffaf9c5dcf56&",
        "miss_chance": 0.30,
        "mythic_chance": 0.03,
        "miss_texts": [
            "Wolves howled from all directions — you retreated empty-handed.",
            "A strange fae fog disoriented you; you found nothing."
        ],
        "common_items": [
            "Luminous Mushroom",
            "Antler Charm",
            "Glowing Root",
            "Witch-Knot Twig",
            "Moonlit Fern"
        ],
        "mythic_item": "Eldergrove Heartwood",
    },
    "house": {
        "label": "Abandoned Victorian House",
        "emoji": "🏚️",
        "color": ButtonStyle.danger,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1408801277768372304/abandoned-haunted-house-refuge-of-spirits-moonlit-royalty-free-image-1633983690.jpg?ex=68ab0fee&is=68a9be6e&hm=d480d45e74c21e1da40a531b597543344c770b04b92934ccfcbe39e2e58ecae4&",
        "miss_chance": 0.30,
        "mythic_chance": 0.03,
        "miss_texts": [
            "A ghost chased you! You ran away empty-handed like a pussy.",
            "Floorboards gave way — you bolted out with nothing."
        ],
        "common_items": [
            "Antique Locket",
            "Silver Candlestick",
            "Dusty Portrait",
            "Music Box Key",
            "Cracked Mirror Shard"
        ],
        "mythic_item": "Haunted Cameo",
    },
}


# number formatting: 🥖(amount)
def bread_fmt(amount: int) -> str:
    return f"🥖{amount:,}"


# cooldown format: hours + minutes
def hm(td: timedelta) -> str:
    total = int(td.total_seconds())
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


class ExploreView(View):
    def __init__(self, ctx: commands.Context, user_id: int):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.user_id = user_id
        self.is_processing = False  # anti double-click spam
        for key, data in EXPLORE_LOCATIONS.items():
            self.add_item(ExploreButton(key, data, self))

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, Button):
                child.disabled = True
        try:
            await self.ctx.message.edit(view=self)
        except Exception:
            pass


class ExploreButton(Button):
    def __init__(self, loc_key: str, loc_data: dict, parent_view: ExploreView):
        super().__init__(label=f"{loc_data['emoji']} {loc_data['label']}",
                         style=loc_data['color'])
        self.loc_key = loc_key
        self.loc_data = loc_data
        self.parent_view = parent_view

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.parent_view.user_id:
            return await interaction.response.send_message(
                "❌ Only the command user can choose a location.", ephemeral=True
            )
        if self.parent_view.is_processing:
            return await interaction.response.send_message(
                "⏳ Processing your exploration...", ephemeral=True
            )
        self.parent_view.is_processing = True

        try:
            uid = str(interaction.user.id)
            user = await get_user(interaction.user.id)

            now = datetime.utcnow()
            cd_until = (user.get("cooldowns") or {}).get("explore")
            if cd_until and cd_until > now:
                remaining = cd_until - now
                return await interaction.response.send_message(
                    f"⏳ You can explore again in **{hm(remaining)}**.", ephemeral=True
                )

            wallet = int(user.get("wallet", 0))
            if wallet < EXPLORE_COST:
                return await interaction.response.send_message(
                    f"❌ You need at least {bread_fmt(EXPLORE_COST)} to explore.",
                    ephemeral=True
                )

            wallet -= EXPLORE_COST
            cd_new = now + timedelta(hours=EXPLORE_COOLDOWN_HOURS)
            await users.update_one(
                {"_id": uid},
                {"$set": {"wallet": wallet, "cooldowns.explore": cd_new}}
            )

            roll = random.random()
            miss_chance = float(self.loc_data.get("miss_chance", 0.30))
            mythic_chance = float(self.loc_data.get("mythic_chance", 0.03))

            outcome_text = ""
            reward = 0

            if roll < miss_chance:
                outcome_text = random.choice(self.loc_data["miss_texts"])
            elif roll < miss_chance + mythic_chance:
                reward = 200_000
                item = self.loc_data["mythic_item"]
                outcome_text = (
                    f"You explored the {self.loc_data['label']} and found a **{item}**! "
                    f"You sold it for {bread_fmt(reward)}."
                )
            else:
                reward = random.randint(5_000, 75_000)
                item = random.choice(self.loc_data["common_items"])
                outcome_text = (
                    f"You explored the {self.loc_data['label']} and found a **{item}** "
                    f"worth {bread_fmt(reward)}."
                )

            if reward > 0:
                wallet += reward
                await users.update_one({"_id": uid}, {"$set": {"wallet": wallet}})

            result = Embed(
                title=f"Exploration Result: {self.loc_data['label']}",
                description=outcome_text,
                color=0xF1C40F
            )
            result.set_image(url=self.loc_data["image"])

            for child in self.parent_view.children:
                if isinstance(child, Button):
                    child.disabled = True

            await interaction.response.edit_message(embed=result, view=self.parent_view)

        finally:
            self.parent_view.is_processing = True


@bot.command(name="explore")
async def explore_cmd(ctx: commands.Context):
    user = await get_user(ctx.author.id)

    now = datetime.utcnow()
    cd_until = (user.get("cooldowns") or {}).get("explore")
    if cd_until and cd_until > now:
        remaining = cd_until - now
        return await ctx.send(f"⏳ You can explore again in **{hm(remaining)}**.")

    wallet = int(user.get("wallet", 0))
    if wallet < EXPLORE_COST:
        return await ctx.send(f"❌ You need at least {bread_fmt(EXPLORE_COST)} to explore.")

    embed = Embed(
        title="🌍 Choose Your Exploration",
        description=(
            f"Exploring costs {bread_fmt(EXPLORE_COST)}.\n\n"
            "Pick a location below to see what you discover!"
        ),
        color=0x3498DB
    )
    view = ExploreView(ctx, ctx.author.id)
    sent = await ctx.send(embed=embed, view=view)
    view.message = sent


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


MARRIAGE_RING_BOOSTS = {
    "gualmar": (0.10, "Gualmar Wedding Ring"),
    "copper": (0.20, "Copper Wedding Ring"),
    "gold": (0.30, "Gold Wedding Ring"),
    "diamond": (0.50, "Diamond Wedding Ring"),
    "eternity": (1.00, "Eternity Wedding Ring"),
}


def _get_marriage_ring_boost(user_doc: dict) -> tuple[float, str | None]:
    key = str(user_doc.get("marriage_ring") or "").strip().lower()
    if key in MARRIAGE_RING_BOOSTS:
        mult, display = MARRIAGE_RING_BOOSTS[key]
        return mult, display
    return 0.0, None


@bot.command()
async def work(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'work', 3600)
    if on_cd:
        return await ctx.send(f"⏳ Come back in {remaining // 60}m {remaining % 60}s to work again!")

    earnings = random.randint(1000, 5000)

    # 🔺 Ring-based couple perk (if married & ring matches table)
    doc = await users.find_one({"_id": str(ctx.author.id)}) or {}
    partner_id = doc.get("married_to")
    ring_mult, ring_name = (0.0, None)
    if partner_id:
        ring_mult, ring_name = _get_marriage_ring_boost(doc)

    ring_bonus = int(earnings * ring_mult) if ring_mult > 0 else 0
    earnings += ring_bonus  # add ring bonus before Bread Juice

    # 🔸 Apply Bread Juice if active (double once, then consume)
    user_doc = doc  # reuse the same doc we already fetched
    if user_doc.get("buffs", {}).get("breadjuice"):
        earnings *= 2
        # if doubled, the ring bonus is effectively doubled too; update display value
        ring_bonus *= 2
        await users.update_one({"_id": str(ctx.author.id)}, {"$unset": {"buffs.breadjuice": ""}})

    await increment_user(ctx.author.id, "wallet", earnings)

    # ✅ Set cooldown after reward
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.work": datetime.utcnow().isoformat()}}
    )

    # 📨 Message
    if ring_bonus > 0 and ring_name:
        pct = int(ring_mult * 100)  # ✅ use ring_mult
        await ctx.send(f"💼 You worked and earned 🥖 {earnings:,}! (+{ring_bonus:,} from 💍 {ring_name} +{pct}%)")
    else:
        await ctx.send(f"💼 You worked and earned 🥖 {earnings:,}!")


@bot.command()
async def daily(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'daily', 86400)
    if on_cd:
        return await ctx.send(f"⏳ Daily already claimed! Wait {remaining // 3600}h {remaining % 3600 // 60}m.")

    earnings = random.randint(5000, 10000)
    now = datetime.utcnow()
    today_key = now.date().isoformat()

    me = await users.find_one({"_id": str(ctx.author.id)}) or {}
    partner_id = me.get("married_to")

    # 🔺 Ring-based couple perk (if married & ring matches table)
    ring_mult, ring_name = (0.0, None)
    if partner_id:
        ring_mult, ring_name = _get_marriage_ring_boost(me)
    ring_bonus = int(earnings * ring_mult) if ring_mult > 0 else 0
    earnings += ring_bonus

    joint_bonus = 0

    # mark my claim date
    await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"last_claim.daily_date": today_key}})

    if partner_id:
        partner = await users.find_one({"_id": partner_id}) or {}
        # if partner already claimed today, award joint bonus ONCE per day
        if partner.get("last_claim", {}).get("daily_date") == today_key:
            me_last = (me.get("marriage_stats", {}) or {}).get("last_joint_daily")
            partner_last = (partner.get("marriage_stats", {}) or {}).get("last_joint_daily")
            if me_last != today_key or partner_last != today_key:
                joint_bonus = 500  # tweak as you want
                # set markers on BOTH and increment BOTH counters once
                await users.update_one({"_id": str(ctx.author.id)},
                                       {"$set": {"marriage_stats.last_joint_daily": today_key},
                                        "$inc": {"marriage_stats.joint_dailies": 1}})
                await users.update_one({"_id": partner_id}, {"$set": {"marriage_stats.last_joint_daily": today_key},
                                                             "$inc": {"marriage_stats.joint_dailies": 1}})

    total_reward = earnings + joint_bonus
    await increment_user(ctx.author.id, "wallet", total_reward)

    # ✅ Set cooldown after giving reward
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.daily": datetime.utcnow().isoformat()}}
    )

    # 📨 Message (single send)
    parts = [f"✅ Daily claimed for **{total_reward:,} 🥖**"]
    if ring_bonus > 0 and ring_name:
        pct = int(ring_mult * 100)  # ✅ use ring_mult
        parts.append(f"(+{ring_bonus:,} from 💍 {ring_name} +{pct}%)")
    if joint_bonus:
        parts.append(f"(+{joint_bonus:,} 🥖 couple bonus)")
    await ctx.send(" ".join(parts))


@bot.command()
async def weekly(ctx):
    await ensure_user(ctx.author.id)
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'weekly', 604800)
    if on_cd:
        days, rem = divmod(remaining, 86400)
        hours = rem // 3600
        return await ctx.send(f"⏳ Weekly already claimed! Wait **{days}d {hours}h**.")

    earnings = random.randint(5000, 20000)
    now = datetime.utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"

    me = await users.find_one({"_id": str(ctx.author.id)}) or {}
    partner_id = me.get("married_to")

    # 🔺 Ring-based couple perk (if married & ring matches table)
    ring_mult, ring_name = (0.0, None)
    if partner_id:
        ring_mult, ring_name = _get_marriage_ring_boost(me)
    ring_bonus = int(earnings * ring_mult) if ring_mult > 0 else 0
    earnings += ring_bonus

    joint_bonus = 0

    # mark my weekly claim
    await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"last_claim.weekly_key": week_key}})

    if partner_id:
        partner = await users.find_one({"_id": partner_id}) or {}
        # if partner already claimed this week, award joint bonus ONCE per week
        if partner.get("last_claim", {}).get("weekly_key") == week_key:
            me_last = (me.get("marriage_stats", {}) or {}).get("last_joint_weekly")
            partner_last = (partner.get("marriage_stats", {}) or {}).get("last_joint_weekly")
            if me_last != week_key or partner_last != week_key:
                joint_bonus = 2000  # tweak as you want
                # set markers on BOTH and increment BOTH counters once
                await users.update_one({"_id": str(ctx.author.id)},
                                       {"$set": {"marriage_stats.last_joint_weekly": week_key},
                                        "$inc": {"marriage_stats.joint_weeklies": 1}})
                await users.update_one({"_id": partner_id}, {"$set": {"marriage_stats.last_joint_weekly": week_key},
                                                             "$inc": {"marriage_stats.joint_weeklies": 1}})

    total_reward = earnings + joint_bonus
    await increment_user(ctx.author.id, "wallet", total_reward)

    # ✅ Set the cooldown timestamp
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"cooldowns.weekly": datetime.utcnow().isoformat()}}
    )

    # 📨 Message (single send)
    parts = [f"🧾 You claimed your weekly bonus of 🥖 {total_reward:,}"]
    if ring_bonus > 0 and ring_name:
        pct = int(ring_mult * 100)  # ✅ use ring_mult
        parts.append(f"(+{ring_bonus:,} from 💍 {ring_name} +{pct}%)")
    if joint_bonus:
        parts.append(f"(+{joint_bonus:,} 🥖 couple bonus)")
    await ctx.send(" ".join(parts))


@bot.command()
async def rob(ctx, member: discord.Member = None):
    # --- Fallback: if a mention was provided but the converter failed, use the first mention ---
    if member is None and ctx.message.mentions:
        m = ctx.message.mentions[0]
        if isinstance(m, discord.Member) and m.id != ctx.author.id and not m.bot:
            member = m

    # Track whether this was a targeted attempt
    was_targeted = member is not None

    # Build candidate list (used for random victim AND for fail-recipient in random mode)
    candidates = [m for m in ctx.guild.members if not m.bot and m.id != ctx.author.id]

    victim_member = None
    victim_data = None

    if member is None:
        # === RANDOM MODE: pick a random eligible victim (wallet > 100,000) ===
        if not candidates:
            return await ctx.send("❌ No one has 🥖 to steal right now.")

        random.shuffle(candidates)
        for m in candidates:
            await ensure_user(m.id)
            data = await users.find_one({"_id": str(m.id)})
            if data and data.get("wallet", 0) >= 100000:
                victim_member = m
                victim_data = data
                break

        if not victim_member:
            return await ctx.send("❌ Nobody has above 🥖 100,000 to steal right now.")
    else:
        # === TARGETED MODE: rob a specific user ===
        if member.id == ctx.author.id:
            return await ctx.send("❌ You can't rob yourself, dumbass.")
        await ensure_user(member.id)
        data = await users.find_one({"_id": str(member.id)})
        if not data or data.get("wallet", 0) <= 0:
            return await ctx.send("❌ That user has no 🥖 to steal.")
        victim_member = member
        victim_data = data

    # === original logic from here (cooldown + success rules) ===
    await ensure_user(ctx.author.id)

    now = datetime.utcnow()
    on_cd, remaining = await is_on_cooldown(ctx.author.id, 'rob', 1800)
    if on_cd:
        return await ctx.send(f"⏳ Wait {remaining // 60}m {remaining % 60}s to rob again.")

    robber = await users.find_one({"_id": str(ctx.author.id)})
    victim = victim_data  # use chosen victim's data

    # 🚫 Prevent robbing if robber has 0 🥖
    if robber.get("wallet", 0) <= 0:
        return await ctx.send("❌ You need some 🥖 yourself before you can rob anyone.")

    # --- PROTECTION CHECKS on the VICTIM (Bread Vault / Rob Shield) ---
    def _ts_to_dt(val):
        if not val:
            return None
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
            except:
                return None
        return val  # already datetime

    victim_buffs = victim.get("buffs", {})

    # Expiry cleanup for Vault
    vault_exp = _ts_to_dt(victim_buffs.get("vault_expire"))
    if victim_buffs.get("vault") and vault_exp and now > vault_exp:
        await users.update_one({"_id": str(victim_member.id)},
                               {"$unset": {"buffs.vault": "", "buffs.vault_expire": ""}})
        victim_buffs["vault"] = 0

    # Expiry cleanup for Rob Shield
    shield_exp = _ts_to_dt(victim_buffs.get("robshield_expire"))
    if victim_buffs.get("robshield") and shield_exp and now > shield_exp:
        await users.update_one({"_id": str(victim_member.id)},
                               {"$unset": {"buffs.robshield": "", "buffs.robshield_expire": ""}})
        victim_buffs["robshield"] = 0

    # ✅ Active Bread Vault blocks this attempt (consumes 1)
    if int(victim_buffs.get("vault", 0) or 0) > 0:
        new_blocks = max(int(victim_buffs.get("vault", 0)) - 1, 0)
        await users.update_one(
            {"_id": str(victim_member.id)},
            {"$inc": {"buffs.vault": -1}}
        )
        await ctx.send(
            f"💼 {victim_member.mention}'s **Bread Vault** blocked your robbery, {ctx.author.mention}! (blocks left: {new_blocks})"
        )
        # put robber on cooldown
        await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"cooldowns.rob": now.isoformat()}})
        return

    # ✅ Active Rob Shield blocks this attempt (consumes 1) + 3% penalty to victim
    if int(victim_buffs.get("robshield", 0) or 0) > 0:
        # consume 1 shield
        await users.update_one(
            {"_id": str(victim_member.id)},
            {"$inc": {"buffs.robshield": -1}}
        )

        # penalize robber 3% like a fail and give it to victim
        robber_wallet = int(robber.get("wallet", 0))
        fail_amount = int(robber_wallet * 3 / 100)
        if robber_wallet > 0:
            fail_amount = max(1, fail_amount)
        fail_amount = min(fail_amount, robber_wallet)

        if fail_amount > 0:
            await increment_user(ctx.author.id, "wallet", -fail_amount)
            await increment_user(victim_member.id, "wallet", fail_amount)

        await ctx.send(
            f"🛡️ {victim_member.mention}'s **Rob Shield** blocked your robbery, {ctx.author.mention}! "
            + (f"You paid them **{fail_amount} 🥖**." if fail_amount > 0 else "Good news? You had nothing to lose.")
        )

        # put robber on cooldown
        await users.update_one({"_id": str(ctx.author.id)}, {"$set": {"cooldowns.rob": now.isoformat()}})
        return

    # --- SUCCESS/FAIL LOGIC ---
    if robber.get("buffs", {}).get("gun"):
        success = True
        double = True
        await users.update_one({"_id": str(ctx.author.id)}, {"$unset": {"buffs.gun": ""}})
    else:
        success = random.random() > 0.5  # 50% chance to succeed
        double = False

    if success:
        # 1%–5% of victim's wallet
        pct = random.randint(1, 5)
        amount = max(1, int(victim.get("wallet", 0) * pct / 100))

        if double:
            amount *= 2

        # cap at victim's wallet
        amount = min(amount, victim.get("wallet", 0))

        # apply balance changes
        await increment_user(ctx.author.id, "wallet", amount)
        await increment_user(victim_member.id, "wallet", -amount)

        if double:
            await ctx.send(
                f"{ctx.author.mention} robbed {victim_member.mention} for **{amount} 🥖**. 🔫 You pulled out your glock and "
                f"{victim_member.mention} was so scared they gave you double the amount.. what a pussy haha"
            )
        else:
            await ctx.send(
                f"{ctx.author.mention} robbed {victim_member.mention} for **{amount} 🥖**"
            )
    else:
        # Failure: pay 3% of robber's wallet
        robber_wallet = int(robber.get("wallet", 0))
        fail_amount = int(robber_wallet * 3 / 100)
        if robber_wallet > 0:
            fail_amount = max(1, fail_amount)
        fail_amount = min(fail_amount, robber_wallet)

        # Recipient: victim if targeted, otherwise random server member
        if was_targeted:
            fail_recipient = victim_member
        else:
            fail_recipient = random.choice(candidates) if candidates else None

        if fail_amount > 0:
            await increment_user(ctx.author.id, "wallet", -fail_amount)
            if fail_recipient:
                # ensure user doc if random mode; targeted victim already ensured
                if not was_targeted:
                    await ensure_user(fail_recipient.id)
                await increment_user(fail_recipient.id, "wallet", fail_amount)

        roast_lines = [
            "You tripped on your way to rob them. Embarrassing.",
            "They caught you and roasted your ass.",
            "You got smacked with a baguette 🥖 while robbing. RIP.",
            "Nice try. You got tackled by a security guard.",
        ]
        base_msg = random.choice(roast_lines)
        if fail_amount > 0 and fail_recipient:
            await ctx.send(
                f"{ctx.author.mention} failed to rob anyone. {base_msg} "
                f"{fail_recipient.mention} called the cops on yo ass and sued you. you had to pay them **{fail_amount} 🥖**."
            )
        else:
            await ctx.send(
                f"{ctx.author.mention} failed to rob anyone. {base_msg} "
                f"Good news? You had nothing to lose."
            )

    # final: set robber cooldown
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

ITEM_COOLDOWNS = {
    "🧲 Lucky Magnet": 24,
    "🎯 Target Scope": 24,
    "💼 Bread Vault": 24,
    "🛡️ Rob Shield": 24,
    "🧃 Bread Juice": 24,
    "🔫 Gun": 24,
    " 🕳 Black Hole": 24,
}


@bot.command(name="cooldowns", aliases=["cd", "cds"])
async def cooldowns_cmd(ctx):
    """Displays active cooldowns with pagination for commands and items."""
    await ensure_user(ctx.author.id)
    user = await users.find_one({"_id": str(ctx.author.id)}) or {}
    cds = user.get("cooldowns", {})
    now = datetime.utcnow()

    def format_cd(name, stored_value, duration, now_time):
        if not stored_value:
            return f"{name} — ✅ Ready"

        try:
            dt = datetime.fromisoformat(stored_value) if isinstance(stored_value, str) else stored_value
        except Exception:
            return f"{name} — ❓ Error"

        # Decide mode
        if dt > now_time:
            # Treat as EXPIRY timestamp
            remaining = int((dt - now_time).total_seconds())
        else:
            # Treat as LAST-USED timestamp
            expiry = dt + timedelta(seconds=duration)
            remaining = int((expiry - now_time).total_seconds())

        if remaining <= 0:
            return f"{name} — ✅ Ready"

        # Pretty formatting (weekly keeps days)
        if name == "weekly":
            d, rem = divmod(remaining, 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            parts = []
            if d: parts.append(f"{d}d")
            if h: parts.append(f"{h}h")
            if m or not parts: parts.append(f"{m}m")
            return f"{name} — ⏳ `{' '.join(parts)}`"
        else:
            h, rem = divmod(remaining, 3600)
            m = rem // 60
            return f"{name} — ⏳ `{h}h {m}m`"

    # === Page 1: Command/Game cooldowns ===
    cd_definitions = {

        "work": 3600,  # 1h
        "daily": 86400,  # 24h
        "weekly": 604800,  # 7d
        "explore": 3600,  # 1h
        "fish": 7200,  # 2h
        "workout": 7200,  # 2h
        "rob": 1800,  # 30m
        "trivia": 86400,  # 24h
        "hangman": 10800,  # 3h
        "depositall": 172800,  # 48h
        "treasurehunt": 21600,  # 6h
        "landmine": 1800,  # 30m
        "wordle_normal": 28800,  # 8h
        "wordle_expert": 28800  # 8h
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
        # Convert timestamp
        if isinstance(last_used, str):
            try:
                last_used = datetime.fromisoformat(last_used)
            except ValueError:
                continue

        # Look up per-item cooldown hours (default 24h if not found)
        hours = ITEM_COOLDOWNS.get(item_name, 24)
        duration_sec = hours * 3600

        elapsed = (now - last_used).total_seconds()
        if elapsed >= duration_sec:
            item_lines.append(f"{item_name} — ✅ Ready")
        else:
            remaining = int(duration_sec - elapsed)
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
# ====== MARRY/DIVORCE =======
# ============================

RING_QUALITIES = {
    "gualmar": ("💍 Gualmar Wedding Ring",
                "https://cdn.discordapp.com/attachments/962847107318951976/1407180834959659112/71BJSFag5OL._UF350_350_QL80_-removebg-preview.png?ex=68a52ac7&is=68a3d947&hm=9791dd6193f9c5e776e5252b0c48e059bcb545eb206bbfefcf5bfd32ec750df6&"),
    "copper": ("🥉 Copper Wedding Ring",
               "https://cdn.discordapp.com/attachments/962847107318951976/1407181119400443955/s-l400-removebg-preview.png?ex=68a52b0b&is=68a3d98b&hm=7c65f78d425cad368ce8495d9de4562c0c1e0de8d3840001a02d9319decc2a4b&"),
    "gold": ("🥇 Gold Wedding Ring",
             "https://cdn.discordapp.com/attachments/962847107318951976/1407180684836999178/celestial-gold-ring-486903.jpg-removebg-preview.png?ex=68a52aa3&is=68a3d923&hm=f3cfc46919b42fe32d78a1b2c53fe239cebbad221a8f5fcc7517ffde24fb688d&"),
    "diamond": ("💎 Diamond Wedding Ring",
                "https://cdn.discordapp.com/attachments/962847107318951976/1407181323654922280/pngtree-diamond-engagement-ring-clip-art-shiny-jewelry-illustration-png-image_14084822-removebg-preview.png?ex=68a52b3b&is=68a3d9bb&hm=abb5647d0a03fe32d500df231ce1d4006eb5a6ad1ee182e5f266ddbe6eb3586e&"),
    "eternity": ("♾️ Eternity Wedding Ring",
                 "https://cdn.discordapp.com/attachments/962847107318951976/1407181015998402782/One_Ring_Blender_Render-removebg-preview.png?ex=68a52af2&is=68a3d972&hm=fbc571cf4828de07fba11376f9f226172baac6dee0aad5efebc913723cc68b66&"),
}


def _normalize_quality(q: str) -> str:
    """Accepts partials like 'dia', 'gol', 'eter' and returns the canonical key."""
    q = (q or "").strip().lower()
    for key in RING_QUALITIES.keys():
        if q == key or key.startswith(q):
            return key
    return ""


# Ring order for quality comparisons (lowest -> highest)
RING_ORDER = ["gualmar", "copper", "gold", "diamond", "eternity"]


def ring_rank(key: str) -> int:
    key = (key or "").lower()
    return RING_ORDER.index(key) if key in RING_ORDER else -1


@bot.command(name="marry")
async def marry_cmd(ctx, member: discord.Member = None, *, ring_quality: str = ""):
    await ensure_user(ctx.author.id)
    user_id = str(ctx.author.id)
    now = datetime.utcnow()

    me = await users.find_one({"_id": user_id}) or {"_id": user_id}

    # Already married? -> show status or try VOW RENEWAL if args provided
    if me.get("married_to"):
        partner_id = me["married_to"]
        since_raw = me.get("married_since")
        try:
            since_dt = datetime.fromisoformat(since_raw) if isinstance(since_raw, str) else since_raw
        except Exception:
            since_dt = now
        days = max(0, (now.date() - (since_dt or now).date()).days)

        # Count stats (safe defaults)
        stats = me.get("marriage_stats", {})
        joint_dailies = int(stats.get("joint_dailies", 0))
        joint_weeklies = int(stats.get("joint_weeklies", 0))

        # If no args -> just show embed
        if member is None and not ring_quality:
            ring_key = (me.get("marriage_ring") or "").lower()
            # Show only quality label (short)
            quality_label = ring_key.title() if ring_key else "Unknown"
            image_url = None
            for key, (_inv_name, url) in RING_QUALITIES.items():
                if key == ring_key:
                    image_url = url
                    break

            embed = discord.Embed(
                title="💞 Marriage Status",
                description=f"You are married to <@{partner_id}>",
                color=discord.Color.pink()
            )
            embed.add_field(name="Days Married", value=f"{days} days", inline=True)
            embed.add_field(name="Ring Quality", value=quality_label, inline=True)
            embed.add_field(name="Joint Dailies", value=str(joint_dailies), inline=True)
            embed.add_field(name="Joint Weeklies", value=str(joint_weeklies), inline=True)
            if image_url:
                embed.set_thumbnail(url=image_url)  # show actual image instead of link
            return await ctx.send(embed=embed)

        # If args are given, allow VOW RENEWAL only with the current spouse
        if not member or str(member.id) != partner_id:
            return await ctx.send("❌ You can only renew vows with your current spouse.")

        # Validate new ring quality
        qkey = _normalize_quality(ring_quality)
        if not qkey:
            return await ctx.send("❌ Specify a ring quality: gualmar, copper, gold, diamond, eternity.")

        # New ring must be strictly higher than current
        current_key = (me.get("marriage_ring") or "").lower()
        if ring_rank(qkey) <= ring_rank(current_key):
            return await ctx.send("❌ Vow renewal requires a **higher quality ring** than your current one.")

        inv_item_name, _image_url = RING_QUALITIES[qkey]
        inv = me.get("inventory", {})
        if inv.get(inv_item_name, 0) < 1:
            return await ctx.send(f"❌ You need **{inv_item_name}** in your inventory to renew vows.")

        # Confirmation: spouse must accept
        class RenewView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.message = None

            async def on_timeout(self):
                for c in self.children:
                    c.disabled = True
                if self.message:
                    try:
                        await self.message.edit(content="⌛ Vow renewal expired.", view=self)
                    except:
                        pass

            @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
            async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != int(partner_id):
                    return await interaction.response.send_message("Not your proposal.", ephemeral=True)

                # Re-check both docs & inventory at accept time
                fresh_me = await users.find_one({"_id": user_id}) or {}
                fresh_partner = await users.find_one({"_id": partner_id}) or {}
                if fresh_me.get("married_to") != partner_id or fresh_partner.get("married_to") != user_id:
                    return await interaction.response.edit_message(content="❌ Marriage state changed. Try again.",
                                                                   view=None)

                inv2 = fresh_me.get("inventory", {})
                if inv2.get(inv_item_name, 0) < 1:
                    return await interaction.response.edit_message(
                        content=f"❌ {ctx.author.mention} no longer has **{inv_item_name}**.", view=None)

                # Consume ring & upgrade on both
                await users.update_one({"_id": user_id}, {"$inc": {f"inventory.{inv_item_name}": -1}})
                await users.update_one({"_id": user_id}, {"$set": {"marriage_ring": qkey}})
                await users.update_one({"_id": partner_id}, {"$set": {"marriage_ring": qkey}})

                for c in self.children:
                    c.disabled = True
                await interaction.response.edit_message(
                    content=f"💍 Vow renewal complete! {ctx.author.mention} upgraded the ring to **{inv_item_name}** with <@{partner_id}>. 💖",
                    view=self)

            @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
            async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != int(partner_id):
                    return await interaction.response.send_message("Not your proposal.", ephemeral=True)
                for c in self.children:
                    c.disabled = True
                await interaction.response.edit_message(content="❌ Vow renewal declined.", view=self)

        view = RenewView()
        msg = await ctx.send(
            f"💍 {ctx.author.mention} wants to **renew vows** with {member.mention} using **{inv_item_name}**. {member.mention}, do you accept?",
            view=view)
        view.message = msg
        return

    # Not married yet:
    if member is None:
        return await ctx.send("you're not married you dumbahh, does anyone even want you lmao")
    if member.bot:
        return await ctx.send("❌ You can't marry a bot.")
    if member.id == ctx.author.id:
        return await ctx.send("❌ You can't marry yourself.")

    await ensure_user(member.id)
    target_doc = await users.find_one({"_id": str(member.id)}) or {}
    if target_doc.get("married_to"):
        return await ctx.send(f"❌ {member.mention} is already married.")

    # Validate ring to initiate marriage
    qkey = _normalize_quality(ring_quality)
    if not qkey:
        return await ctx.send("❌ Specify a ring quality: gualmar, copper, gold, diamond, eternity.")
    inv_item_name, _image_url = RING_QUALITIES[qkey]

    inv = me.get("inventory", {})
    if inv.get(inv_item_name, 0) < 1:
        return await ctx.send(f"❌ You need **{inv_item_name}** in your inventory to marry.")

    # Confirmation: target must accept
    class MarryView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.message = None

        async def on_timeout(self):
            for c in self.children:
                c.disabled = True
            if self.message:
                try:
                    await self.message.edit(content="⌛ Marriage proposal expired.", view=self)
                except:
                    pass

        @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
        async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != member.id:
                return await interaction.response.send_message("Not your proposal.", ephemeral=True)

            # Re-check everything at accept time
            fresh_proposer = await users.find_one({"_id": user_id}) or {}
            fresh_target = await users.find_one({"_id": str(member.id)}) or {}

            if fresh_proposer.get("married_to") or fresh_target.get("married_to"):
                return await interaction.response.edit_message(
                    content="❌ Someone is already married now. Proposal void.", view=None)

            inv2 = fresh_proposer.get("inventory", {})
            if inv2.get(inv_item_name, 0) < 1:
                return await interaction.response.edit_message(
                    content=f"❌ {ctx.author.mention} no longer has **{inv_item_name}**.", view=None)

            # Consume ring and marry both
            since_iso = datetime.utcnow().isoformat()
            await users.update_one({"_id": user_id}, {"$inc": {f"inventory.{inv_item_name}": -1}})
            base_fields = {"married_to": str(member.id), "married_since": since_iso, "marriage_ring": qkey}
            await users.update_one({"_id": user_id}, {"$set": base_fields}, upsert=True)
            await users.update_one({"_id": str(member.id)},
                                   {"$set": {"married_to": user_id, "married_since": since_iso, "marriage_ring": qkey}},
                                   upsert=True)
            # init stats containers if missing
            await users.update_one({"_id": user_id},
                                   {"$setOnInsert": {"marriage_stats": {"joint_dailies": 0, "joint_weeklies": 0}}},
                                   upsert=True)
            await users.update_one({"_id": str(member.id)},
                                   {"$setOnInsert": {"marriage_stats": {"joint_dailies": 0, "joint_weeklies": 0}}},
                                   upsert=True)

            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(
                content=f"💍 {ctx.author.mention} married {member.mention} with a **{inv_item_name}**! Congrats 🎉",
                view=self)

        @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
        async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != member.id:
                return await interaction.response.send_message("Not your proposal.", ephemeral=True)
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(content="❌ Marriage proposal declined.", view=self)

    view = MarryView()
    msg = await ctx.send(
        f"💍 {ctx.author.mention} wants to marry {member.mention} using **{inv_item_name}**. {member.mention}, do you accept?",
        view=view)
    view.message = msg


@bot.command(name="divorce")
async def divorce_cmd(ctx):
    """Break the marriage and reset streak (clears marriage fields) for both users."""
    await ensure_user(ctx.author.id)
    me = await users.find_one({"_id": str(ctx.author.id)}) or {}
    if not me.get("married_to"):
        return await ctx.send("❌ You're not married. Nothing to divorce.")

    partner_id = me["married_to"]

    # Clear both sides
    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$unset": {"married_to": "", "married_since": "", "marriage_ring": ""}}
    )
    await users.update_one(
        {"_id": str(partner_id)},
        {"$unset": {"married_to": "", "married_since": "", "marriage_ring": ""}}
    )

    await ctx.send(f"💔 {ctx.author.mention} divorced <@{partner_id}>. Marriage streak reset.")


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

    new_wallet = user_data["wallet"] + amount
    new_bank = user_data["bank"] - amount

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$set": {"wallet": new_wallet, "bank": new_bank}}
    )
    await ctx.send(f"💸 {ctx.author.mention} withdrew {amount} 🥖 from the bank.")


# ============================
# COMMAND: DEPOSIT
# ============================

@bot.command(alises=["dep"])
async def deposit(ctx, amount: int):
    user = await get_user(ctx.author.id)
    wallet = user.get("wallet", 0)
    bank = user.get("bank", 0)

    if amount <= 0:
        return await ctx.send("❗ You must deposit a positive amount.")

    if amount > wallet:
        return await ctx.send("💸 You don’t have that much in your wallet!")

    new_wallet = wallet - amount
    new_bank = bank + amount

    # ❌ Block if bank would be more than 50% of new wallet
    if new_wallet == 0 or new_bank > new_wallet / 2:
        return await ctx.send(f"⚠️ You can't deposit that much — your bank would exceed 50% of your remaining wallet.")

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {"$inc": {"wallet": -amount, "bank": amount}}
    )

    await ctx.send(f"🏦 You deposited {amount} 🥖 into your bank.")


# ====================================
# COMMAND: DEPOSITMAX (50% OF BALANCE)
# ====================================

@bot.command(aliases=["depmax", "depall", "depositall"])
async def depositmax(ctx):
    user_data = await get_user(ctx.author.id)
    now = datetime.utcnow()

    # Check 48h cooldown
    deposit_cd = user_data.get("cooldowns", {}).get("depositall")
    if deposit_cd and now < deposit_cd + timedelta(hours=48):
        remaining = deposit_cd + timedelta(hours=48) - now
        hours, minutes = divmod(int(remaining.total_seconds()) // 60, 60)
        return await ctx.send(f"⏳ You can use `;depositall` again in **{hours}h {minutes}m**.")

    wallet = user_data["wallet"]

    if wallet <= 0:
        return await ctx.send("❌ You don't have any 🥖 to deposit.")

    max_deposit = wallet // 2
    if max_deposit == 0:
        return await ctx.send("❌ You need at least 2 🥖 in your wallet to use `;deposit max`.")

    new_wallet = wallet - max_deposit
    new_bank = user_data["bank"] + max_deposit

    await users.update_one(
        {"_id": str(ctx.author.id)},
        {
            "$set": {"wallet": new_wallet, "bank": new_bank, "cooldowns.depositall": now}
        }
    )
    await ctx.send(f"🏦 {ctx.author.mention} deposited {max_deposit} 🥖 (50% of your wallet) into the bank.")


# ============================
# COMMAND: LEADERBOARD (with buttons)
# ============================

# Fallbacks if these helpers aren't already defined
try:
    bread_fmt
except NameError:
    def bread_fmt(amount: int) -> str:
        return f"🥖{amount:,}"

try:
    level_from_total_xp
except NameError:
    def level_from_total_xp(total_xp: int) -> int:
        # inverse of xp_for_level = 100 * L^2
        return int((total_xp / 100) ** 0.5)


# ---- Data fetchers ----
async def _lb_balance():
    # Top 10 by (wallet + bank)
    cursor = users.find({}, {"wallet": 1, "bank": 1})
    docs = await cursor.to_list(length=1000)
    rows = []
    for d in docs:
        w = int(d.get("wallet", 0))
        b = int(d.get("bank", 0))
        total = w + b
        if total > 0:
            rows.append((d["_id"], total, w, b))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:10]


async def _lb_levels():
    # Top 10 by level derived from total_xp
    cursor = users.find({}, {"total_xp": 1})
    docs = await cursor.to_list(length=1000)
    rows = []
    for d in docs:
        txp = int(d.get("total_xp", 0))
        lvl = level_from_total_xp(txp)
        if txp > 0:
            rows.append((d["_id"], lvl, txp))
    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return rows[:10]


async def _lb_landmine():
    # Try best-known field names, fall back safely
    cursor = users.find({}, {"landmine_best_streak": 1, "landmine_streak": 1})
    docs = await cursor.to_list(length=1000)
    rows = []
    for d in docs:
        best = d.get("landmine_best_streak")
        if best is None:
            best = d.get("landmine_streak", 0)
        best = int(best or 0)
        if best > 0:
            rows.append((d["_id"], best))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:10]


async def _render_wordle_embed(bot, rows):
    embed = discord.Embed(title="🏆 Leaderboard — Wordle (Lowest Guesses)", color=discord.Color.green())
    if not rows:
        embed.description = "No data yet."
        return embed
    for i, (uid, best, label) in enumerate(rows, start=1):
        user = await bot.fetch_user(int(uid))
        tries = "try" if best == 1 else "tries"
        embed.add_field(
            name=f"{i}. {user.display_name}",
            value=f"Best: **{best} {tries}** • *{label}*",
            inline=False
        )
    return embed


# ---- Renderers ----
async def _render_balance_embed(bot, rows):
    embed = discord.Embed(title="🏆 Leaderboard — Balance", color=discord.Color.gold())
    if not rows:
        embed.description = "No data yet."
        return embed
    for i, (uid, total, w, b) in enumerate(rows, start=1):
        user = await bot.fetch_user(int(uid))
        embed.add_field(
            name=f"{i}. {user.display_name}",
            value=f"Total: {bread_fmt(total)}\nWallet: {bread_fmt(w)} • Bank: {bread_fmt(b)}",
            inline=False
        )
    return embed


async def _render_levels_embed(bot, rows):
    embed = discord.Embed(title="🏆 Leaderboard — Levels", color=discord.Color.blurple())
    if not rows:
        embed.description = "No data yet."
        return embed
    for i, (uid, lvl, txp) in enumerate(rows, start=1):
        user = await bot.fetch_user(int(uid))
        embed.add_field(
            name=f"{i}. {user.display_name}",
            value=f"Level **{lvl}** • Total EXP **{txp:,}**",
            inline=False
        )
    return embed


async def _render_landmine_embed(bot, rows):
    embed = discord.Embed(title="🏆 Leaderboard — Landmine Streaks", color=discord.Color.red())
    if not rows:
        embed.description = "No data yet."
        return embed
    for i, (uid, best) in enumerate(rows, start=1):
        user = await bot.fetch_user(int(uid))
        embed.add_field(
            name=f"{i}. {user.display_name}",
            value=f"Best Streak: **{best}**",
            inline=False
        )
    return embed


async def _render_wordle_embed(bot, rows):
    embed = discord.Embed(title="🏆 Leaderboard — Wordle (Lowest Guesses)", color=discord.Color.green())
    if not rows:
        embed.description = "No data yet."
        return embed
    for i, (uid, best) in enumerate(rows, start=1):
        user = await bot.fetch_user(int(uid))
        tries = "try" if best == 1 else "tries"
        embed.add_field(
            name=f"{i}. {user.display_name}",
            value=f"Best: **{best} {tries}**",
            inline=False
        )
    return embed


# ---- Buttons/View ----
class LeaderboardView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx

    async def on_timeout(self):
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

    async def _ensure_author(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("❌ Only the command user can switch this leaderboard.",
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💰 Balance", style=discord.ButtonStyle.success)
    async def lb_balance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_author(interaction): return
        rows = await _lb_balance()
        embed = await _render_balance_embed(self.ctx.bot, rows)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⭐ Levels", style=discord.ButtonStyle.primary)
    async def lb_levels_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_author(interaction): return
        rows = await _lb_levels()
        embed = await _render_levels_embed(self.ctx.bot, rows)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💥 Landmine", style=discord.ButtonStyle.danger)
    async def lb_landmine_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_author(interaction): return
        rows = await _lb_landmine()
        embed = await _render_landmine_embed(self.ctx.bot, rows)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔤 Wordle", style=discord.ButtonStyle.secondary)
    async def lb_wordle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_author(interaction): return
        rows = await _lb_wordle()
        embed = await _render_wordle_embed(self.ctx.bot, rows)
        await interaction.response.edit_message(embed=embed, view=self)


# ---- Command ----
@bot.command(aliases=["leaderboards", "lb", "levels", "ranks", "rank", "ranking", "rankings"])
async def leaderboard(ctx):
    # Default view: Balance
    rows = await _lb_balance()
    embed = await _render_balance_embed(bot, rows)
    view = LeaderboardView(ctx)
    msg = await ctx.send(embed=embed, view=view)
    view.message = msg


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

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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
    await msg.edit(content=f"Tic-Tac-Toe: {ctx.author.mention} vs {member.mention}\n{ctx.author.mention}'s turn",
                   view=game_view)


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


active_hangman_games = set()


@bot.command(aliases=["hm"])
async def hangman(ctx):
    await get_user(ctx.author.id)
    user_id = str(ctx.author.id)
    if user_id in active_hangman_games:
        return await ctx.send("⚠️ You're already in a Hangman game. Finish it before starting another.")

    active_hangman_games.add(user_id)

    async def hangman_timeout():
        await asyncio.sleep(300)  # 5 minutes = 300 seconds
        if user_id in active_hangman_games:
            active_hangman_games.discard(user_id)
            try:
                await ctx.send(f"⌛ {ctx.author.mention}, your Hangman game expired due to inactivity.")
            except:
                pass

    asyncio.create_task(hangman_timeout())

    user = await users.find_one({"_id": user_id})

    now = datetime.now()

    # ——— Fixed cooldown check ———
    last_cd_str = user.get("cooldowns", {}).get("hangman") if user else None
    if last_cd_str:
        try:
            # Try ISO format first
            cooldown_end = datetime.fromisoformat(last_cd_str)
        except ValueError:
            # Fallback to legacy format
            cooldown_end = datetime.strptime(last_cd_str, "%Y-%m-%d %H:%M:%S")
        if now < cooldown_end:
            remaining = cooldown_end - now
            hours, rem_secs = divmod(remaining.total_seconds(), 3600)
            minutes, _ = divmod(rem_secs, 60)
            return await ctx.send(
                f"⏳ You must wait **{int(hours)}h {int(minutes)}m** before playing Hangman again."
            )

    import string
    HANGMAN_WORDS = [  # (shortened for clarity, full list unchanged)
        "able", "acid", "aged", "ally", "area", "atom", "auto", "avid", "baby", "bake", "ball",
        "base", "cool", "data", "duck", "even", "fail", "glue", "hair", "hope", "idea", "jack",
        "keen", "lamp", "mild", "note", "oval", "park", "quiz", "road", "safe", "tide", "user",
        "view", "wake", "xray", "yell", "arch", "barn", "bark", "bell", "bend", "bind", "bite",
        "blow", "blue", "bold", "boot", "born", "brow", "buck", "bulk", "burn", "bush", "busy",
        "calm", "card", "care", "carp", "case", "cast", "cash", "cost", "crew", "crop", "cube",
        "cure", "curb", "curl", "cute", "dart", "dash", "date", "dawn", "deep", "deed", "deer",
        "dent", "dial", "dice", "dine", "dirt", "dive", "dock", "does", "doll", "dome"
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

    formatted_word = " ".join(f"`{char}`" for char in display)
    embed = discord.Embed(
        title="🎮 Hangman Started!",
        description=f"```{HANGMAN_PICS[0]}```\nWord: {formatted_word}\nLives left: {lives}",
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
        formatted_word = " ".join(f"`{char}`" for char in display)
        embed.description = f"```{stage}```\nWord: {formatted_word}\nLives left: {lives}"
        await message.edit(embed=embed, view=None)

    cooldown_time = (datetime.now() + timedelta(hours=3)).isoformat()
    if "_" not in display:
        await users.update_one(
            {"_id": user_id},
            {"$inc": {"wallet": 10000}, "$set": {"cooldowns.hangman": cooldown_time}},
            upsert=True
        )
        await ctx.send(f"🎉 **You won!** The word was **{word}**\n💰 You earned **10,000 🥖**!")
    else:
        await users.update_one(
            {"_id": user_id},
            {"$set": {"cooldowns.hangman": cooldown_time}},
            upsert=True
        )
        await ctx.send(f"💀 **Game Over!** The word was **{word}**.")

    active_hangman_games.discard(user_id)


@bot.command(aliases=["command", "commands", "cmd"])
async def help(ctx):
    # === Page 1: Games ===
    games_embed = discord.Embed(
        title="🎮 Game Commands",
        description="Play games to win 🥖 or challenge friends!",
        color=discord.Color.green()
    )
    games_embed.add_field(name="🃏 Competitive Games", value="""
`!uno <bet> @user1 @user2...` - Play UNO with betting  
`!blackjack <bet>` - Blackjack vs bot  
`!rps <bet> @user` - Rock Paper Scissors vs user  
`!tictactoe <bet> @user` - Tic-Tac-Toe duel  
`!connect4 <bet> @user` - Connect 4 with bread bets  

""", inline=False)
    games_embed.add_field(name="🧠 Guessing & Trivia", value="""
`!hangman` - Guess a 4-letter word in 6 tries  
`!trivia` - Answer trivia questions to win 🥖  
""", inline=False)

    # === Page 2: Gambling ===
    gambling_embed = discord.Embed(
        title="🎰 Solo Gambling Games",
        description="Test your luck and win 🥖!",
        color=discord.Color.gold()
    )
    gambling_embed.add_field(name="🎰 Solo Games", value="""
`!slot <bet>` - Spin the slot machine for prizes  
`!landmine <bet>` - Click tiles and cash out before hitting a mine!  
`!roulette <bet>` - Bet on red, black, even, or specific numbers  
`!coinflip <bet>` - Heads or tails?  
`!dice <bet>` - Roll a die against the bot  
`!treasurehunt` or `;th` - Dig up random treasures daily. Be careful! This could be deadly!  
""", inline=False)

    # === Page 3: Economy ===
    economy_embed = discord.Embed(
        title="💰 Economy Commands",
        description="Manage your bread and grind the economy!",
        color=discord.Color.blurple()
    )
    economy_embed.add_field(name="💸 Bread Economy", value="""
`!work` - Earn 1000–5000 🥖 every hour  
`!daily` - Earn 5000–10000 🥖 every 24h  
`!weekly` - Earn 10000–20000 🥖 every 7d  
`!rob @user` - Attempt to rob (60% fail rate)  
`!pay @user <amount>` - Pay someone  
`!deposit <amount>` - Deposit up to 50% of wallet  
`!depositmax` - Deposit full 50% automatically  
`!withdraw <amount> ` - Withdraw bread from bank
`!balance` - Check wallet & bank  
`!cooldowns` - View your active cooldowns  
""", inline=False)

    # === Page 4: Fun & Extras ===
    fun_embed = discord.Embed(
        title="🎲 Fun & Extra Commands",
        description="Non-economy extras and cool tools.",
        color=discord.Color.purple()
    )
    fun_embed.add_field(name="🎲 Fun & Extras", value="""
`!fact` - Get a random fun fact  
`!8ball <question>` - Ask the magic 8-ball  
`!roast @user` - Roast a user  
`!inv` / `;items` / `;inventory` - View your items  
`!buy <item>` - Purchase an item from the shop  
`!use <item>` - Use one of your items  
`!lotto` - View lottery info and prize pool  
`!lottobuy <#>` - Buy weekly lottery tickets (max 5)  
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
`;mute @user` — Mute a user from using the bot  
`;unmute @user` — Unmute a user  
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
            "mute", "unmute",
            "lockdown", "unlock",
            "resetcd", "resetweekly"
    ):
        return True

    # check lockdown flag
    settings = await bot_settings.find_one({"_id": "config"}) or {}
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
async def mute(ctx, member: discord.Member):
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
async def unmute(ctx, member: discord.Member):
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
            await interaction.response.edit_message(
                content="🚫 Bot is now in **lockdown**. Only creators can use commands.", view=None)

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
            await interaction.response.edit_message(
                content="✅ Bot is now **unlocked**. Everyone can use commands again.", view=None)

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

    from discord.ui import View, Button  # ensure imports exist
    from datetime import datetime

    class ConfirmResetCD(View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="✅ Reset All Cooldowns", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Only the command issuer can confirm.", ephemeral=True)

            await interaction.response.defer()  # avoids "interaction failed"

            # 0) Clear all decorator-based cooldown buckets in memory (e.g., @commands.cooldown)
            cleared_commands = 0
            try:
                for cmd in bot.walk_commands():
                    buckets = getattr(cmd, "_buckets", None)
                    if buckets and hasattr(buckets, "_cache"):
                        buckets._cache.clear()
                        cleared_commands += 1
            except Exception:
                pass

            # 1) Clear ALL cooldowns in DB (commands + items) by replacing the object
            today = datetime.utcnow().strftime("%Y-%m-%d")
            res1 = await users.update_many({}, {"$set": {"cooldowns": {}}})

            # 2) Also remove today's Wordle flags wherever they might exist
            wordle_unset = {
                f"cooldowns.wordle.{today}:normal": "",
                f"cooldowns.wordle.{today}:expert": "",
                f"wordle.{today}:normal": "",
                f"wordle.{today}:expert": "",
            }
            res2 = await users.update_many({}, {"$unset": wordle_unset})

            await interaction.edit_original_response(
                content=(
                    "♻️ Reset **all cooldowns** (commands + items) and cleared today's Wordle state.\n"
                    f"• In-memory decorator cooldowns cleared for **{cleared_commands}** commands\n"
                    f"• DB cooldowns reset in **{res1.modified_count}** users\n"
                    f"• Wordle keys cleared in **{res2.modified_count}** users"
                ),
                view=None
            )

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != ctx.author.id:
                return await interaction.response.send_message("Only the command issuer can cancel.", ephemeral=True)
            await interaction.response.edit_message(content="❎ Cooldown reset cancelled.", view=None)

    await ctx.send(
        "⚠️ Are you sure you want to reset **all user cooldowns** (and clear today's Wordle state)?",
        view=ConfirmResetCD()
    )


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

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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
                content += f"\n🎉 You win! Gained 🥖 **{bet:,}**"
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

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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
    if ctx.channel.id != 1399446341012422797:
        return await ctx.send("❌ Trivia can only be played in <#1399446341012422797>.")

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
    await asyncio.sleep(10);
    msg = await ctx.send("⏳ 20s left to join!");
    messages_to_delete.append(msg)
    await asyncio.sleep(10);
    msg = await ctx.send("⏳ 10s left to join!");
    messages_to_delete.append(msg)
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
        correct = html.unescape(qobj["answer"])

        options = [html.unescape(opt) for opt in qobj.get("options", [])]
        random.shuffle(options)

        letters = ["A", "B", "C", "D"]
        mapping = dict(zip(letters, options))
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
                uid = interaction.user.id
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

        await asyncio.sleep(10);
        msg = await ctx.send("⏳ 20s left to answer!");
        messages_to_delete.append(msg)
        await asyncio.sleep(10);
        msg = await ctx.send("⏳ 10s left to answer!");
        messages_to_delete.append(msg)
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
    board = "\n".join(f"{i + 1}. <@{uid}> — **{pts}**" for i, (uid, pts) in enumerate(final))
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
            podium.append(f"{['🥇', '🥈', '🥉'][i]} +{prizes[i]} 🥖")
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


# @bot.command()
# async def pay(ctx, member: discord.Member, amount: int):
#    if member.bot or member == ctx.author:
#        return await ctx.send("❌ Invalid recipient.")

#    sender_data = await get_user(ctx.author.id)
#    receiver_data = await get_user(member.id)

#    if sender_data["wallet"] < amount or amount <= 0:
#        return await ctx.send("❌ Not enough 🥖 or invalid amount.")

#    sender_new_wallet = sender_data["wallet"] - amount
#    receiver_new_wallet = receiver_data["wallet"] + amount

#    await users.update_one(
#        {"_id": str(ctx.author.id)},
#        {"$set": {"wallet": sender_new_wallet}}
#    )
#    await users.update_one(
#        {"_id": str(member.id)},
#        {"$set": {"wallet": receiver_new_wallet}}
#    )

#    await ctx.send(f"✅ {ctx.author.mention} paid {member.mention} 🥖 {amount}.")


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
    top_users = users.find().sort("wallet", -1).limit(10)
    leaderboard = []
    rank = 1
    async for user_data in top_users:
        member = ctx.guild.get_member(int(user_data["_id"]))
        name = member.display_name if member else f"<@{user_data['_id']}>"
        balance = user_data.get("wallet", 0)
        leaderboard.append(f"**{rank}.** {name} - 🥖 {balance:,}")
        rank += 1

    embed = discord.Embed(title="🏆 Top 10 Richest Users", description="\n".join(leaderboard), color=0xFFD700)
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
    wallet = user_data["wallet"]
    bank = user_data["bank"]
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
    async def c1(self, interaction, button):
        await self.make_move(0, interaction)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary)
    async def c2(self, interaction, button):
        await self.make_move(1, interaction)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary)
    async def c3(self, interaction, button):
        await self.make_move(2, interaction)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary)
    async def c4(self, interaction, button):
        await self.make_move(3, interaction)

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary)
    async def c5(self, interaction, button):
        await self.make_move(4, interaction)

    @discord.ui.button(label="6", style=discord.ButtonStyle.secondary)
    async def c6(self, interaction, button):
        await self.make_move(5, interaction)

    @discord.ui.button(label="7", style=discord.ButtonStyle.secondary)
    async def c7(self, interaction, button):
        await self.make_move(6, interaction)


@bot.command(aliases=["c4"])
async def connect4(ctx, bet: int, opponent: discord.Member):
    if bet <= 0:
        return await ctx.send("❌ Bet must be a positive number.")

    if bet > 50000:
        return await ctx.send("❗ The maximum bet is **50,000 🥖**.")

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
                return await interaction.response.send_message("Only the challenged player can decline.",
                                                               ephemeral=True)
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


# ================================
# 🔥 RANDOM FORGE (FIRST-TO-CLAIM) + JACKPOT
# ================================


# ======= CONFIG =======
FORGE_CHANNEL_ID = 977201441146040362  # channel where forge spawns
FORGE_SPAWN_CHANCE = 0.015  # chance per eligible message
FORGE_MIN_INTERVAL_SEC = 5  # ≥ 5 sec between spawns
FORGE_EVENT_LIFETIME_SEC = 10  # claim window (seconds)
FORGE_CLAIM_COMMAND = "forge"  # first to say !forge
FORGE_IMAGE_URL = "https://cdn.discordapp.com/attachments/1010943160207294527/1410484765118697482/mystical-forge-burns-stockcake.jpg?ex=68b12fcd&is=68afde4d&hm=0019b71711d280034b60ea7fc74948448513b65d7e50aa7b179c855045e8cc05"  # 👈 replace with your image

# Jackpot behavior
FORGE_JACKPOT_KEY = "forge_jackpot"
FORGE_JACKPOT_CAP = 100_000  # hard cap
BACKFIRE_FEED_RATE = 0.20  # 20% of backfire loss feeds jackpot

FORGE_RARITIES = [
    (40, "Rusty Blade", "⚒️", +5_000, 0.00),  # Common
    (24, "Steel Sword", "🛠️", +15_000, 0.00),  # Uncommon
    (12, "Runed Greatsword", "🗡️", +30_000, 0.00),  # Rare
    (8, "Thunder Axe", "⚡", +50_000, 0.05),  # Epic (5% jackpot)
    (4, "Hammer of Eternity", "🏆", +100_000, 0.10),  # Legendary (10% jackpot)
    (2, "Zeus' Thunderbolt", "👑", +200_000, 0.25),  # Mythic (25% jackpot)
    (10, "Backfire", "💥", -20_000, 0.00),  # Negative outcome
]

# ======= GLOBALS =======
_forge_lock = asyncio.Lock()
_forge_state = {
    "active": False,
    "channel_id": None,
    "spawned_at": 0.0,
    "message_id": None,
    "claimed_by": None,
    "timeout_task": None,
}
_last_spawn_ts = 0.0


# ======= MONGO SHORTCUTS =======

async def _ensure_wallet(uid: str):
    doc = await users.find_one({"_id": uid})
    if not doc:
        await users.insert_one({"_id": uid, "wallet": 0, "bank": 0})


async def _inc_wallet(uid: str, delta: int) -> int:
    await _ensure_wallet(uid)
    doc = await users.find_one_and_update(
        {"_id": uid},
        {"$inc": {"wallet": int(delta)}},
        return_document=True
    )
    return int((doc or {}).get("wallet", 0))


async def _get_jackpot() -> int:
    doc = await bot_settings.find_one({"_id": FORGE_JACKPOT_KEY})
    return int(doc.get("value", 0)) if doc else 0


async def _set_jackpot(value: int):
    value = max(0, min(int(value), FORGE_JACKPOT_CAP))
    await bot_settings.update_one(
        {"_id": FORGE_JACKPOT_KEY},
        {"$set": {"value": value}},
        upsert=True
    )


async def _add_to_jackpot_capped(amount: int):
    """Add to jackpot but never exceed cap."""
    if amount <= 0:
        return
    current = await _get_jackpot()
    new_val = min(FORGE_JACKPOT_CAP, current + int(amount))
    await _set_jackpot(new_val)


# ======= UTILS =======

def _weighted_roll(rarities):
    total = sum(w for (w, *_rest) in rarities)
    x = random.uniform(0, total)
    upto = 0
    for w, name, emoji, delta, jp_chance in rarities:
        if upto + w >= x:
            return name, emoji, delta, jp_chance
        upto += w
    return rarities[-1][1], rarities[-1][2], rarities[-1][3], rarities[-1][4]  # fallback


def _spawn_embed(jackpot_now: int) -> discord.Embed:
    e = discord.Embed(
        title="⚒️ The Forge Glows With Power!",
        description="The forge is **active**. Be the first to type `!forge` to claim the flames!",
        color=discord.Color.orange()
    )
    if FORGE_IMAGE_URL:
        e.set_image(url=FORGE_IMAGE_URL)
    e.add_field(name="🔥 Forge Jackpot", value=f"{jackpot_now:,} 🥖 (cap {FORGE_JACKPOT_CAP:,})", inline=False)
    e.set_footer(text=f"Auto-despawns in {FORGE_EVENT_LIFETIME_SEC}s • First-come, first-forged")
    return e


def _result_embed(member: discord.abc.User, name: str, emoji: str, delta: int, new_wallet: int,
                  jackpot_before: int, jackpot_won: int) -> discord.Embed:
    gain_or_loss = "gained" if delta >= 0 else "lost"
    amount = f"{abs(delta):,} 🥖"
    lines = [
        f"{member.mention} forged **{name}** {emoji}",
        f"You **{gain_or_loss} {amount}**.",
        f"**Wallet:** {new_wallet:,} 🥖"
    ]
    if jackpot_won > 0:
        lines.append(f"🎉 **Jackpot!** You also won **{jackpot_won:,} 🥖** from the forge pool!")
    color = discord.Color.green() if (delta + jackpot_won) >= 0 else discord.Color.red()
    e = discord.Embed(title="⚒️ Forge Result", description="\n".join(lines), color=color)
    if jackpot_won == 0:
        # Show updated jackpot for visibility (after any feed)
        e.add_field(name="🔥 Forge Jackpot", value=f"{jackpot_before:,} 🥖 (cap {FORGE_JACKPOT_CAP:,})", inline=False)
    else:
        e.add_field(name="🔥 Forge Jackpot", value=f"0 🥖 (cap {FORGE_JACKPOT_CAP:,})", inline=False)
    return e


# ======= SPAWNER =======
@bot.listen("on_message")
async def _forge_spawner(msg: discord.Message):
    try:
        if not msg.guild or msg.author.bot:
            return
        if msg.channel.id != FORGE_CHANNEL_ID:
            return

        # Cooldown between spawns
        global _last_spawn_ts
        now = time.time()
        if now - _last_spawn_ts < FORGE_MIN_INTERVAL_SEC:
            return

        # Don’t spawn if one is active
        async with _forge_lock:
            if _forge_state["active"]:
                return

        # Chance roll
        if random.random() > FORGE_SPAWN_CHANCE:
            return

        # Spawn with current jackpot shown
        jackpot_now = await _get_jackpot()
        embed = _spawn_embed(jackpot_now)
        out = await msg.channel.send(embed=embed)

        async with _forge_lock:
            _forge_state.update({
                "active": True,
                "channel_id": msg.channel.id,
                "spawned_at": now,
                "message_id": out.id,
                "claimed_by": None,
                "timeout_task": asyncio.create_task(_expire_unclaimed(msg.channel, out.id)),
            })
            _last_spawn_ts = now

    except Exception as e:
        print(f"[FORGE] spawn error: {e}\n{traceback.format_exc()}")


async def _expire_unclaimed(channel: discord.TextChannel, message_id: int):
    try:
        await asyncio.sleep(FORGE_EVENT_LIFETIME_SEC)
        async with _forge_lock:
            if _forge_state["active"] and _forge_state["claimed_by"] is None:
                _forge_state["active"] = False
        try:
            msg = await channel.fetch_message(message_id)
            if msg:
                await msg.reply("⏳ The forge cools down—no one claimed it in time.")
        except Exception:
            pass
    except Exception as e:
        print(f"[FORGE] expire task error: {e}\n{traceback.format_exc()}")


# ======= CLAIM COMMAND: !forge =======
@bot.command(name=FORGE_CLAIM_COMMAND)
async def _forge_claim(ctx: commands.Context):
    try:
        # Must match active event + same channel
        async with _forge_lock:
            if not _forge_state["active"]:
                return await ctx.send("There’s no active forge right now. Wait for the flames to rise 🔥.")
            if _forge_state["channel_id"] != ctx.channel.id:
                return await ctx.send("The active forge is in another channel.")
            if _forge_state["claimed_by"] is not None:
                return await ctx.send("Someone already claimed this forge. Better luck next time!")
            _forge_state["claimed_by"] = ctx.author.id
            timeout_task = _forge_state.get("timeout_task")
            if timeout_task:
                timeout_task.cancel()
                _forge_state["timeout_task"] = None

        # Roll rarity & base bread change
        name, emoji, delta, jp_chance = _weighted_roll(FORGE_RARITIES)
        uid = str(ctx.author.id)

        jackpot_before = await _get_jackpot()
        jackpot_won = 0

        # Backfire feeds jackpot
        if name == "Backfire":
            feed = int(abs(delta) * BACKFIRE_FEED_RATE)
            if feed > 0:
                await _add_to_jackpot_capped(feed)

        # Apply bread change first
        new_wallet = await _inc_wallet(uid, delta)

        # If Epic+ and jackpot_chance > 0, roll jackpot
        if jp_chance > 0 and jackpot_before > 0:
            if random.random() < jp_chance:
                jackpot_won = jackpot_before
                await _inc_wallet(uid, jackpot_won)
                await _set_jackpot(0)

        # Post result
        final_jp_display = 0 if jackpot_won > 0 else await _get_jackpot()
        result = _result_embed(ctx.author, name, emoji, delta, new_wallet, final_jp_display, jackpot_won)
        await ctx.send(embed=result)

    except Exception as e:
        print(f"[FORGE] claim error: {e}\n{traceback.format_exc()}")
        try:
            await ctx.send("⚠️ Something went wrong processing the forge claim.")
        except Exception:
            pass
    finally:
        # End the event regardless of outcome
        async with _forge_lock:
            _forge_state["active"] = False
            _forge_state["claimed_by"] = None


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
        "weight": 30,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401441966813614141/dgifuzi-4e29b16f-aa74-4f1a-b597-f935e67e61a1.png?ex=68904a0a&is=688ef88a&hm=9da9b4a42e8d6884a8bed676aac30fb5a265ca35b1920938ad734e9d1c352ac3&"
    },
    {
        "key": "gold",
        "name": "Gold Chest",
        "color": 0xFFD700,
        "min": 1500,
        "max": 3000,
        "weight": 20,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1402382321704767549/360_F_1254659968_pCsO0jo2zcZonszsZ1nuNsEoR1BLoTph.jpg?ex=689658d0&is=68950750&hm=a9a052a5ec1f8f4314a8cc472fea034aaf8e3c288651c26469e6cbaa67d3d0be&"
    },
    {
        "key": "diamond",
        "name": "Diamond Chest",
        "color": 0x00E5FF,
        "min": 3000,
        "max": 6000,
        "weight": 15,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401442029652803696/1000146365-removebg-preview.png?ex=68904a19&is=688ef899&hm=26829d4a91f71bb4e180d14f411eedfe32c4b1efc6dc13993a1459b9c87539a0&"
    },
    {
        "key": "Sapphire",
        "name": "Sapphire Chest",
        "color": 0x89CFF0,
        "min": 10000,
        "max": 20000,
        "weight": 10,
        "cursed": False,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1402382286111899720/Screenshot_2025-08-05_at_4.03.40_PM.png?ex=689658c8&is=68950748&hm=890f2574c6885faddf06704a9cecaf9eb586c396144b7003cd02507ad23debd9&"
    },
    {
        "key": "cursed",
        "name": "Cursed Chest",
        "color": 0x8B0000,
        "min": -3000,
        "max": -500,
        "weight": 15,
        "cursed": True,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1401442089543270492/1000146368-removebg-preview.png?ex=68904a27&is=688ef8a7&hm=7b4d14b48237e60d3f027cd37e3b171b806300124e4e18c1bfa5baede9673f62&"
    },
    {
        "key": "cursed2",
        "name": "Cursed Dark Chest",
        "color": 0x06402B,
        "min": -5000,
        "max": -20000,
        "weight": 10,
        "cursed": True,
        "image": "https://cdn.discordapp.com/attachments/962847107318951976/1403155748204183672/87e6bd4f-70b9-465c-af8d-efd8109c24d1.png?ex=6896861f&is=6895349f&hm=3ead9adbc17dcb36e5ac7712d7eda0d06f55c3010f003442fc74bd232e9b61a5&"
    }
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


@bot.command(name="clearchest", aliases=["resetchest"])
@commands.has_permissions(administrator=True)
async def clear_chest(ctx):
    global active_chests
    count = len(active_chests)
    active_chests.clear()
    await ctx.send(f"🧹 Cleared {count} active chest(s). All stuck chests are now removed.")


# Register new commands

register_plinko_command(bot)


@bot.listen("on_command_completion")
async def _on_cmd_xp_award(ctx: commands.Context):
    # Only in servers, only for humans
    if not ctx.guild or ctx.author.bot:
        return
    # Only award XP in these channels
    if ctx.channel.id not in ALLOWED_XP_CHANNELS:
        return

    uid = str(ctx.author.id)
    before = await users.find_one({"_id": uid}) or {"_id": uid, "total_xp": 0, "xp_cooldowns": {}, "guild_xp": {}}

    # Short cooldown for command XP
    if _xp_cmd_cd_ready(before):
        gain = max(1, int(random.randint(*XP_PER_COMMAND_RANGE) * XP_MULTIPLIER))

        after_total = int(before.get("total_xp", 0)) + gain
        guild_xp = before.get("guild_xp") or {}
        gid = str(ctx.guild.id)
        guild_xp[gid] = int(guild_xp.get(gid, 0)) + gain

        update = {"total_xp": after_total, "guild_xp": guild_xp}
        _set_xp_cmd_cd(update)
        await users.update_one({"_id": uid}, {"$set": update}, upsert=True)

        # Re-fetch and payout level rewards if you crossed levels
        after = await users.find_one({"_id": uid}) or {"_id": uid, "total_xp": after_total}
        await award_level_prizes(ctx, before, after, announce=True)


# ===== Level math =====
try:
    xp_for_level
except NameError:
    def xp_for_level(level: int) -> int:
        return 100 * (level ** 2)  # 100, 400, 900, 1600, ...

try:
    level_from_total_xp
except NameError:
    def level_from_total_xp(total_xp: int) -> int:
        return int((total_xp / 100) ** 0.5)


# ===== Level prize curve (scales with level, caps to avoid breaking economy) =====
def level_prize_amount(level: int) -> int:
    """
    Scaled prize per level. Grows faster at higher levels, but capped.
    Tuned against your command rewards (5k–200k).
    """
    base = int(1500 * level + 200 * (level ** 2))  # quadratic-ish growth
    # Milestone bonus every 10 levels
    if level % 10 == 0:
        base += 50_000
    # Clamp so it’s meaningful but not nuts
    return max(5_000, min(base, 120_000))


async def award_level_prizes(ctx_or_channel, user_doc_before: dict, user_doc_after: dict, *, announce: bool = True):
    """
    Awards all missed level prizes between previous and current level.
    Uses users.level_claimed_upto to prevent double-awards.
    """
    uid = str(user_doc_after["_id"])
    before_xp = int(user_doc_before.get("total_xp", 0))
    after_xp = int(user_doc_after.get("total_xp", 0))

    before_lvl = level_from_total_xp(before_xp)
    after_lvl = level_from_total_xp(after_xp)
    if after_lvl <= before_lvl:
        return

    claimed_upto = int(user_doc_after.get("level_claimed_upto", 0))
    start_level = max(before_lvl + 1, claimed_upto + 1)
    if start_level > after_lvl:
        return

    total_award = 0
    details = []
    for L in range(start_level, after_lvl + 1):
        amt = level_prize_amount(L)
        total_award += amt
        details.append((L, amt))

    # Apply to wallet and persist claimed_upto
    await users.update_one(
        {"_id": uid},
        {"$inc": {"wallet": total_award}, "$set": {"level_claimed_upto": after_lvl}}
    )

    # Optional announcement
    if announce and total_award > 0:
        try:
            channel = getattr(ctx_or_channel, "channel", ctx_or_channel)
            # Compact summary (avoid spam if multiple levels at once)
            firstL, lastL = details[0][0], details[-1][0]
            await channel.send(
                f"🏆 **Level Up!** <@{uid}> reached **Lv {after_lvl}** "
                f"(+{len(details)} level{'s' if len(details) > 1 else ''}) "
                f"and earned **🥖({total_award:,})** in level rewards!"
            )
        except Exception:
            pass


# Lightweight anti-spam for command XP

def _xp_msg_cd_ready(doc: dict) -> bool:
    iso = (doc.get("xp_cooldowns") or {}).get("msg")
    if not iso:
        return True
    try:
        last = datetime.fromisoformat(iso)
        return (datetime.utcnow() - last).total_seconds() >= MSG_XP_COOLDOWN_SECONDS
    except Exception:
        return True


def _set_xp_msg_cd(update_doc: dict):
    xpcd = update_doc.get("xp_cooldowns") or {}
    xpcd["msg"] = datetime.utcnow().isoformat()
    update_doc["xp_cooldowns"] = xpcd


def _xp_cmd_cd_ready(doc: dict) -> bool:
    iso = (doc.get("xp_cooldowns") or {}).get("cmd")
    if not iso:
        return True
    try:
        last = datetime.fromisoformat(iso)
        return (datetime.utcnow() - last).total_seconds() >= CMD_XP_COOLDOWN_SECONDS
    except Exception:
        return True


def _set_xp_cmd_cd(update_doc: dict):
    xpcd = update_doc.get("xp_cooldowns") or {}
    xpcd["cmd"] = datetime.utcnow().isoformat()
    update_doc["xp_cooldowns"] = xpcd


# ============================
# Start the bot (on_ready event)
# ============================

@bot.event
async def on_message(message):
    if not message.guild or message.author.bot:
        return

    raw = message.content.strip()
    content = raw.lower()

    # --- Message XP (only in allowed channels, 5s CD)
    if message.channel.id in ALLOWED_XP_CHANNELS:
        uid = str(message.author.id)
        before = await users.find_one({"_id": uid}) or {"_id": uid, "total_xp": 0, "xp_cooldowns": {}, "guild_xp": {},
                                                        "messages_sent": 0}

        if _xp_msg_cd_ready(before):
            gain = max(1, int(random.randint(*XP_PER_MESSAGE_RANGE) * XP_MULTIPLIER))

            after_total = int(before.get("total_xp", 0)) + gain
            guild_xp = before.get("guild_xp") or {}
            gid = str(message.guild.id)
            guild_xp[gid] = int(guild_xp.get(gid, 0)) + gain
            new_msgs = int(before.get("messages_sent", 0)) + 1

            update = {"total_xp": after_total, "guild_xp": guild_xp, "messages_sent": new_msgs}
            _set_xp_msg_cd(update)
            await users.update_one({"_id": uid}, {"$set": update}, upsert=True)

            # (optional but recommended) level-up prizes on message XP too
            after = await users.find_one({"_id": uid}) or {"_id": uid, "total_xp": after_total}
            await award_level_prizes(message, before, after, announce=True)

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
                        await message.channel.send(
                            f"❌ **{message.author.display_name}** falsely called UNO and drew 1 penalty card.")
                        game.advance_turn()
                        await start_uno_game(bot, game)
                    return

    # ========================
    # Loot Chest System
    # ========================

    if message.channel.id != CHEST_CHANNEL_ID:
        return await bot.process_commands(message)

    global active_chests
    channel = message.channel

    if content == "!pick":
        if channel.id in active_chests:
            chest = active_chests[channel.id]
            if chest["claimed"]:
                if message.author.id == chest.get("claimed_by"):
                    return  # don't roast the winner again
                if chest["cursed"]:
                    await channel.send(
                        f"{message.author.mention} thank god you slow af, pay attention to what type of lootboxes are being spawned dickhead")
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
    await bot.change_presence(activity=random.choice(activities), status=discord.Status.away)


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
    bot.add_view(GuessButtonPlaceholder())
    bot.add_view(ModeSelect())
    print(f"✅ Registered commands: {[cmd.name for cmd in bot.commands]}")


@bot.event
async def on_command_error(ctx, error):
    original = getattr(error, "original", error)

    # 0) If the command has a local error handler, don't double-handle here
    if ctx.command and hasattr(ctx.command, 'on_error'):
        return

    # Build Usage line (reuses your style)
    usage = None
    if ctx.command:
        sig = ctx.command.signature
        usage = f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {sig}`" if sig else None

    # 1) Special handling for landmine / lm
    if ctx.command and ctx.command.name in ("landmine", "lm"):
        if isinstance(error, commands.CommandOnCooldown):
            secs = int(error.retry_after)
            # mirror to DB so your !cooldowns view stays accurate
            try:
                await users.update_one(
                    {"_id": str(ctx.author.id)},
                    {"$set": {"cooldowns.landmine": datetime.utcnow() + timedelta(seconds=secs)}},
                    upsert=True
                )
            except Exception:
                pass
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            pretty = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s")
            msg = f"⏳ You’re on Landmine cooldown. Try again in **{pretty}**."
            if usage: msg += f"\n{usage}"
            return await ctx.send(msg)

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            # refund decorator cooldown on invalid/missing bet
            try:
                ctx.command.reset_cooldown(ctx)
            except Exception:
                pass
            # Show your usage message
            return await ctx.send("Usage: `!landmine <bet>`")

    # 2) Generic handlers (your originals)
    if isinstance(error, commands.CommandNotFound):
        return  # ignore

    if isinstance(error, commands.MissingRequiredArgument):
        msg = f"❗ Missing argument: `{error.param.name}`."
        if usage: msg += f"\n{usage}"
        return await ctx.send(msg)

    if isinstance(error, commands.BadArgument):
        msg = f"❗ Bad argument: {original}"
        if usage: msg += f"\n{usage}"
        return await ctx.send(msg)

    if isinstance(error, commands.CommandOnCooldown):
        secs = int(error.retry_after)
        m, s = divmod(secs, 60)
        msg = f"⏳ This command is on cooldown. Try again in {m}m {s}s."
        if usage: msg += f"\n{usage}"
        return await ctx.send(msg)

    # 3) Fallback for any other exception
    msg = f"❌ `{ctx.command}` failed: {original}"
    if usage: msg += f"\n{usage}"
    await ctx.send(msg)
    print(f"[ERROR] in `{ctx.command}`: {original}")


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
