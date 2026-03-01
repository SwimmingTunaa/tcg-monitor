#!/usr/bin/env python3
"""
TCG Monitor — Discord Admin Bot
================================
Slash command bot for inspecting and managing the monitor from Discord.

All commands are restricted to ADMIN_CHANNEL_ID.

Commands:
  /ping                        Health check + DB stats
  /db canonical [set_key]      Print canonical products (all or filtered by set)
  /db status [retailer]        Print product_status rows with match info
  /db unmatched                Print rows with no canonical match
  /seed [set_key] [dry_run]    Run the canonical seeder
  /discover [dry_run]          Run EB Games discovery
  /match [dry_run]             Bulk re-match unmatched products

Setup:
  1. Create bot at https://discord.com/developers/applications
     - Scopes: bot + applications.commands
     - Permissions: Send Messages, Read Message History, Use Slash Commands
  2. Add to .env:
       DISCORD_BOT_TOKEN=...
       ADMIN_CHANNEL_ID=123456789
  3. pip install "discord.py>=2.0"
  4. python bot.py
"""

import os
import sys
import asyncio
import logging
import subprocess
from io import StringIO
from datetime import datetime
from typing import Optional

# ── Load .env ──────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# ── Discord ────────────────────────────────────────────────────────
try:
    import discord
    from discord import app_commands
except ImportError:
    print("❌  pip install 'discord.py>=2.0'")
    sys.exit(1)

# ── Project imports ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from utils.database import Database
from canonical.matcher import run_bulk_match

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tcg-bot")

# ── Config ─────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ADMIN_CHANNEL_ID = int(os.environ.get("ADMIN_CHANNEL_ID", "0"))
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

SET_DISPLAY = {
    "perfect-order":        "Mega Evolution — Perfect Order",
    "ascended-heroes":      "Mega Evolution — Ascended Heroes",
    "phantasmal-flames":    "Mega Evolution — Phantasmal Flames",
    "mega-evolutions":      "Mega Evolution",
    "journey-together":     "Scarlet & Violet — Journey Together",
    "destined-rivals":      "Scarlet & Violet — Destined Rivals",
    "prismatic-evolutions": "Scarlet & Violet — Prismatic Evolutions",
    "surging-sparks":       "Scarlet & Violet — Surging Sparks",
}

TYPE_EMOJI = {
    "booster-box":        "📦",
    "booster-bundle":     "🎁",
    "booster-pack":       "🃏",
    "elite-trainer-box":  "⭐",
    "pokemon-center-etb": "🌟",
    "collection-box":     "🗃️",
    "premium-collection": "💎",
    "tin":                "🥫",
    "three-pack-blister": "📋",
    "blister":            "📋",
    "build-and-battle":   "⚔️",
    "starter-deck":       "🎴",
}


# ══════════════════════════════════════════════════════════════════
# Bot client
# ══════════════════════════════════════════════════════════════════

class AdminBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = Database()

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        await self.tree.sync(guild=guild)
        logger.info(f"Slash commands synced to guild {GUILD_ID}")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} — admin channel: {ADMIN_CHANNEL_ID}")


client = AdminBot()
MY_GUILD = discord.Object(id=GUILD_ID)


# ── Guard helper ───────────────────────────────────────────────────

async def guard(interaction: discord.Interaction) -> bool:
    """Return False and reply if not in admin channel."""
    if ADMIN_CHANNEL_ID and interaction.channel_id != ADMIN_CHANNEL_ID:
        await interaction.response.send_message(
            f"⛔ Use <#{ADMIN_CHANNEL_ID}> for admin commands.",
            ephemeral=True,
        )
        return False
    return True


# ── Text chunking ──────────────────────────────────────────────────

def chunks(text: str, size: int = 1900) -> list[str]:
    """Split text into Discord-safe chunks, breaking on newlines."""
    parts = []
    while text:
        if len(text) <= size:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut == -1:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def send_code(interaction: discord.Interaction, text: str):
    """Send text as code blocks, paginating if needed."""
    for i, part in enumerate(chunks(text)):
        block = f"```\n{part}\n```"
        if i == 0:
            await interaction.followup.send(block)
        else:
            await interaction.channel.send(block)


# ── Subprocess runner ──────────────────────────────────────────────

async def run_script(args: list[str]) -> str:
    """Run a project script in a thread, return stdout+stderr."""
    def _run():
        result = subprocess.run(
            [sys.executable] + args,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        return result.stdout + result.stderr
    return await asyncio.get_event_loop().run_in_executor(None, _run)


def tail_lines(output: str, n: int = 35, keywords=None) -> str:
    """Return last n lines, or lines containing keywords."""
    lines = output.splitlines()
    if keywords:
        lines = [l for l in lines if any(k in l for k in keywords)]
    return "\n".join(lines[-n:])


# ══════════════════════════════════════════════════════════════════
# /ping
# ══════════════════════════════════════════════════════════════════

@client.tree.command(name="ping", description="Health check — shows DB stats", guilds=[MY_GUILD])
async def ping(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    conn = client.db._get_conn()
    try:
        canonical_total  = conn.execute("SELECT COUNT(*) FROM canonical_products WHERE active=1").fetchone()[0]
        status_total     = conn.execute("SELECT COUNT(*) FROM product_status").fetchone()[0]
        matched          = conn.execute("SELECT COUNT(*) FROM product_status WHERE match_status='matched'").fetchone()[0]
        needs_review     = conn.execute("SELECT COUNT(*) FROM product_status WHERE match_status='review'").fetchone()[0]
        unmatched        = conn.execute("SELECT COUNT(*) FROM product_status WHERE match_status='unmatched'").fetchone()[0]
        in_stock         = conn.execute("SELECT COUNT(*) FROM product_status WHERE in_stock=1").fetchone()[0]
        set_count        = conn.execute("SELECT COUNT(DISTINCT set_key) FROM canonical_products WHERE active=1").fetchone()[0]
    finally:
        conn.close()

    e = discord.Embed(title="🏓 TCG Monitor — Online", color=0x00CC44,
                      timestamp=datetime.utcnow())
    e.add_field(name="📚 Canonical Products", value=str(canonical_total), inline=True)
    e.add_field(name="🎴 Sets Tracked",       value=str(set_count),       inline=True)
    e.add_field(name="🗃️ Monitored URLs",     value=str(status_total),    inline=True)
    e.add_field(name="✅ Matched",            value=str(matched),          inline=True)
    e.add_field(name="⚠️ Needs Review",       value=str(needs_review),     inline=True)
    e.add_field(name="❌ Unmatched",          value=str(unmatched),         inline=True)
    e.add_field(name="🟢 In Stock Now",       value=str(in_stock),          inline=True)
    e.set_footer(text="Run /db canonical · /db status · /db unmatched for details")
    await interaction.followup.send(embed=e)


# ══════════════════════════════════════════════════════════════════
# /db group
# ══════════════════════════════════════════════════════════════════

db_group = app_commands.Group(name="db", description="Inspect the TCG monitor database", guild_ids=[GUILD_ID])


@db_group.command(name="canonical", description="List canonical products")
@app_commands.describe(set_key="Filter by set key, e.g. perfect-order (leave blank for all)")
async def db_canonical(interaction: discord.Interaction, set_key: Optional[str] = None):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    products = client.db.get_all_canonical(active_only=False)
    if set_key:
        products = [p for p in products if p["set_key"] == set_key]

    if not products:
        msg = f"No canonical products for `{set_key}`." if set_key else "Canonical table is empty — run `/seed`."
        await interaction.followup.send(f"❌ {msg}")
        return

    # Group by set
    by_set: dict[str, list] = {}
    for p in products:
        by_set.setdefault(p["set_key"], []).append(p)

    lines = [f"📚 Canonical Products — {len(products)} total\n"]
    for sk, items in by_set.items():
        label = SET_DISPLAY.get(sk, sk)
        lines.append(f"\n── {label} ({len(items)}) ──")
        for p in items:
            emoji  = TYPE_EMOJI.get(p["type"], "•")
            msrp   = f"  AU${p['msrp']:.2f}" if p.get("msrp") else ""
            active = "" if p.get("active", 1) else "  [inactive]"
            lines.append(f"  {emoji} {p['name']}{msrp}{active}")
            lines.append(f"     id: {p['id']}  |  type: {p['type']}")

    await send_code(interaction, "\n".join(lines))


@db_group.command(name="status", description="Show monitored product URLs and their match status")
@app_commands.describe(retailer="Filter by retailer, e.g. ebgames_au (leave blank for all)")
async def db_status(interaction: discord.Interaction, retailer: Optional[str] = None):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    conn = client.db._get_conn()
    try:
        q = """
            SELECT ps.name, ps.retailer, ps.in_stock, ps.price_str,
                   ps.match_status, ps.canonical_id,
                   cp.name AS canonical_name,
                   ps.last_checked
            FROM product_status ps
            LEFT JOIN canonical_products cp ON ps.canonical_id = cp.id
        """
        params: list = []
        if retailer:
            q += " WHERE ps.retailer = ?"
            params.append(retailer)
        q += " ORDER BY ps.retailer, ps.match_status, ps.name"
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    if not rows:
        await interaction.followup.send("❌ No rows in product_status — run `/discover` first.")
        return

    match_icon = {"matched": "✅", "review": "⚠️", "unmatched": "❌"}

    lines = [f"🗃️ Product Status — {len(rows)} rows\n"]
    current_r = None
    for r in rows:
        if r["retailer"] != current_r:
            current_r = r["retailer"]
            lines.append(f"\n── {current_r} ──")
        stock  = "🟢" if r["in_stock"] else "🔴"
        match  = match_icon.get(r["match_status"] or "unmatched", "❓")
        price  = f"  {r['price_str']}" if r["price_str"] else ""
        lines.append(f"  {stock} {match}  {r['name']}{price}")
        if r["canonical_name"]:
            lines.append(f"         ↳ {r['canonical_name']}")

    await send_code(interaction, "\n".join(lines))


@db_group.command(name="unmatched", description="Show products with no canonical match")
async def db_unmatched(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    rows = client.db.get_unmatched()
    if not rows:
        await interaction.followup.send("✅ All products have a canonical match!")
        return

    review   = [r for r in rows if r["match_status"] == "review"]
    no_match = [r for r in rows if r["match_status"] == "unmatched"]

    lines = [f"🔍 Unmatched / Needs Review — {len(rows)} total\n"]

    if review:
        lines.append(f"⚠️  NEEDS REVIEW ({len(review)}) — close match, verify manually:")
        for r in review:
            lines.append(f"  • {r['name']}")
            lines.append(f"    {r['url']}")

    if no_match:
        lines.append(f"\n❌ NO MATCH ({len(no_match)}) — nothing in canonical DB:")
        for r in no_match:
            lines.append(f"  • {r['name']}")
            lines.append(f"    {r['url']}")

    lines.append("\nTip: run `/seed` to populate canonical DB, then `/match` to re-attempt.")

    await send_code(interaction, "\n".join(lines))


client.tree.add_command(db_group)


# ══════════════════════════════════════════════════════════════════
# /seed
# ══════════════════════════════════════════════════════════════════

@client.tree.command(name="seed", description="Seed canonical products from PokéBeach + Claude", guilds=[MY_GUILD])
@app_commands.describe(
    set_key  = "Set to seed, e.g. perfect-order (leave blank for all sets)",
    dry_run  = "Print what would be added without writing to DB",
)
async def seed(
    interaction: discord.Interaction,
    set_key: Optional[str] = None,
    dry_run: bool = False,
):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    target = f"`{set_key}`" if set_key else "all sets"
    mode   = " [DRY RUN]" if dry_run else ""
    await interaction.followup.send(f"🌱 Seeding {target}{mode}… (~30–60s)")

    cmd = ["canonical/seed_pokemon.py"]
    if set_key:
        cmd += ["--set", set_key]
    if dry_run:
        cmd += ["--dry-run"]

    output = await run_script(cmd)

    # Filter to the useful log lines
    summary = tail_lines(output, n=40, keywords=[
        "✅", "Added", "Updated", "Done", "ERROR", "WARNING",
        "products", "Claude", "PokéBeach", "generated", "extracted",
    ])

    color = 0x00CC44 if "Done" in output and "ERROR" not in output else 0xFF6600
    e = discord.Embed(
        title=f"🌱 Seed — {target}{mode}",
        description=f"```\n{summary[:3900]}\n```",
        color=color,
        timestamp=datetime.utcnow(),
    )
    await interaction.channel.send(embed=e)


# ══════════════════════════════════════════════════════════════════
# /discover
# ══════════════════════════════════════════════════════════════════

@client.tree.command(name="discover", description="Run EB Games product discovery", guilds=[MY_GUILD])
@app_commands.describe(
    tcg     = "TCG to search: pokemon, one-piece, mtg (leave blank for all)",
    dry_run = "Print results without saving to DB",
)
async def discover(
    interaction: discord.Interaction,
    tcg: Optional[str] = None,
    dry_run: bool = False,
):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    target = tcg or "all TCGs"
    mode   = " [DRY RUN]" if dry_run else ""
    await interaction.followup.send(f"🔍 Discovering EB Games products for {target}{mode}… (~60s)")

    cmd = ["discovery/ebgames_discovery.py"]
    if tcg:
        cmd += ["--tcg", tcg]
    if dry_run:
        cmd += ["--dry-run"]

    output = await run_script(cmd)

    summary = tail_lines(output, n=40, keywords=[
        "✅", "Added", "found", "complete", "ERROR", "⚠️",
        "products", "skipped", "matched", "discovered",
    ])

    color = 0x3498DB if dry_run else (0x00CC44 if "ERROR" not in output else 0xFF4444)
    e = discord.Embed(
        title=f"🔍 Discovery — {target}{mode}",
        description=f"```\n{summary[:3900]}\n```",
        color=color,
        timestamp=datetime.utcnow(),
    )
    await interaction.channel.send(embed=e)


# ══════════════════════════════════════════════════════════════════
# /match
# ══════════════════════════════════════════════════════════════════

@client.tree.command(name="match", description="Bulk re-match unmatched products against canonical DB", guilds=[MY_GUILD])
@app_commands.describe(
    retailer = "Restrict to retailer key, e.g. ebgames_au (leave blank for all)",
    dry_run  = "Print results without writing to DB",
)
async def match(
    interaction: discord.Interaction,
    retailer: Optional[str] = None,
    dry_run: bool = False,
):
    if not await guard(interaction):
        return
    await interaction.response.defer()

    mode = " [DRY RUN]" if dry_run else ""
    await interaction.followup.send(f"🔗 Running bulk matcher{mode}…")

    # Capture run_bulk_match output (it logs via logging + print in dry_run mode)
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    def _match():
        return run_bulk_match(client.db, retailer=retailer, dry_run=dry_run)

    stats = await asyncio.get_event_loop().run_in_executor(None, _match)
    root_logger.removeHandler(handler)

    stats = stats or {}
    matched   = stats.get("matched",   0)
    review    = stats.get("review",    0)
    unmatched = stats.get("unmatched", 0)
    total     = matched + review + unmatched

    color = 0x00CC44 if unmatched == 0 else (0xFF6600 if unmatched <= 3 else 0xFF4444)

    e = discord.Embed(
        title=f"🔗 Bulk Match{mode}",
        color=color,
        timestamp=datetime.utcnow(),
    )
    e.add_field(name="✅ Matched",      value=str(matched),   inline=True)
    e.add_field(name="⚠️ Needs Review", value=str(review),    inline=True)
    e.add_field(name="❌ Unmatched",    value=str(unmatched), inline=True)
    e.add_field(name="📊 Total",        value=str(total),     inline=True)

    log_excerpt = log_stream.getvalue()
    if log_excerpt.strip():
        e.add_field(
            name="Log",
            value=f"```\n{log_excerpt[-800:]}\n```",
            inline=False,
        )

    if unmatched > 0:
        e.set_footer(text="Run /db unmatched to see what needs attention")

    await interaction.channel.send(embed=e)


# ══════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        print("❌  DISCORD_BOT_TOKEN not set in .env")
        print("    1. discord.com/developers/applications → New Application → Bot → Reset Token")
        print("    2. Add DISCORD_BOT_TOKEN=<token> to .env")
        sys.exit(1)

    if not ADMIN_CHANNEL_ID:
        print("⚠️   ADMIN_CHANNEL_ID not set — commands will work in any channel")

    logger.info(f"Starting bot  |  admin channel: {ADMIN_CHANNEL_ID or 'any'}")
    client.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
