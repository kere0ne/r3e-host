"""r3e (larp) - Discord bot by kers0nes: login, forwardsources, clone, clear,
purge, ban, unban, lock, unlock. All commands owner-only."""

import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
FORWARDS_PATH = BASE_DIR / "forwards.json"
LOGIN_PATH = BASE_DIR / "login.json"

MASTER_OWNER_IDS = {1207803375807373415}
OWNER_NOTIFY_ID = 1207803375807373415  # DM target when the bot joins a server
BOT_NAME = "r3e"
API = "https://discord.com/api/v10"

TXT_EXT = (".txt",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def load_env_file():
    """Parse .env ourselves; panel never exports it into the process."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def save_env_value(key, value):
    """Persist one KEY=value into .env, replacing an existing line."""
    lines = []
    if ENV_PATH.exists():
        lines = [l for l in ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("=")]
    lines = [l for l in lines if l.split("=", 1)[0].strip() != key]
    lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


load_env_file()
BOT_TOKEN = os.environ.get("TOKEN") or os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
USER_TOKEN = os.environ.get("USER_TOKEN") or os.environ.get("DISCORD_USER_TOKEN") or ""

if not BOT_TOKEN:
    print("FATAL: no TOKEN in environment/.env", flush=True)
    sys.exit(1)

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="\u0000", intents=intents, help_command=None)


# ---------- login session (persists across restarts and servers) ----------

def load_login_session():
    """Restore the user-token session from login.json (survives restarts,
    new servers, and the panel rewriting .env)."""
    global USER_TOKEN
    if not USER_TOKEN and LOGIN_PATH.exists():
        try:
            data = json.loads(LOGIN_PATH.read_text(encoding="utf-8"))
            tok = (data.get("user_token") or "").strip()
            if tok:
                USER_TOKEN = tok
                save_env_value("USER_TOKEN", USER_TOKEN)
                print(f"Restored user-token session from login.json (user id {data.get('user_id', '?')})", flush=True)
        except Exception as e:
            print(f"login.json unread: {e}", flush=True)


def save_login_session(token, user):
    try:
        LOGIN_PATH.write_text(json.dumps({
            "user_token": token,
            "user_id": user.get("id"),
            "username": user.get("global_name") or user.get("username"),
            "saved_at": int(discord.utils.utcnow().timestamp()),
        }, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"login.json write failed: {e}", flush=True)


# ---------- forwards mapping ----------

def load_forwards():
    if FORWARDS_PATH.exists():
        try:
            data = json.loads(FORWARDS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_forwards(data):
    FORWARDS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


FORWARDS = load_forwards()


def is_master(user_id: int) -> bool:
    return user_id in MASTER_OWNER_IDS


def clean_id(raw: str) -> str:
    return re.sub(r"[@#!&<>]", "", (raw or "").strip())


def owner_only(interaction: discord.Interaction) -> bool:
    if not is_master(interaction.user.id):
        return False
    return True


# ---------- user-token REST helpers ----------

async def user_token_headers():
    return {"Authorization": USER_TOKEN}


async def validate_user_token(token: str):
    clean = re.sub(r"^(Bot|Bearer)\s+", "", token.strip(), flags=re.I)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(f"{API}/users/@me",
                                   headers={"Authorization": clean}) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:
        return None


async def rest_get_json(url: str):
    if not USER_TOKEN:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url, headers=await user_token_headers()) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:
        return None


async def find_source_channel(channel_id: str):
    """Bot-attached channels first; user-token REST for external servers."""
    cid = clean_id(channel_id)
    ch = bot.get_channel(int(cid)) if cid.isdigit() else None
    if ch is not None:
        return ("bot", ch)
    data = await rest_get_json(f"{API}/channels/{cid}")
    if data:
        return ("user", data)
    return (None, None)


# ---------- attachment / file helpers ----------

def ext_attachment_name(att: dict) -> str:
    return att.get("filename") or att.get("name") or "file"


def all_files_from_attachments(attachments):
    """EVERY attachment, any file type, deduped by url."""
    out, seen = [], set()
    for att in attachments or []:
        name = ext_attachment_name(att)
        url = att.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((name, url))
    return out


def embed_image_urls(embeds):
    urls = []
    for emb in embeds or []:
        if isinstance(emb, dict):
            img = (emb.get("image") or {}).get("url")
            thumb = (emb.get("thumbnail") or {}).get("url")
        else:
            img = getattr(getattr(emb, "image", None), "url", None)
            thumb = getattr(getattr(emb, "thumbnail", None), "url", None)
        if img:
            urls.append(("image.png", img))
        elif thumb:
            urls.append(("image.png", thumb))
    return urls


def snapshot_files(snapshots):
    """Discord-forwarded messages: pull files + content out of message_snapshots."""
    files, content = [], ""
    for snap in snapshots or []:
        sm = snap.get("message") if isinstance(snap, dict) else getattr(snap, "message", None)
        if sm is None:
            continue
        if isinstance(sm, dict):
            content = content or (sm.get("content") or "")
            files += all_files_from_attachments(sm.get("attachments"))
            files += embed_image_urls(sm.get("embeds"))
        else:
            content = content or (getattr(sm, "content", "") or "")
            files += [(a.filename, a.url) for a in (getattr(sm, "attachments", None) or [])]
    return content, files


def message_parts(m):
    """Normalize a bot-object or REST-dict message to (content, [(name, url), ...])."""
    if isinstance(m, dict):
        content = m.get("content") or ""
        files = all_files_from_attachments(m.get("attachments"))
        snap_content, snap_files = snapshot_files(m.get("message_snapshots"))
        content = content or snap_content
        files += snap_files
        files += embed_image_urls(m.get("embeds"))
        return content, dedupe_files(files)
    content = m.content or ""
    files = [(a.filename, a.url) for a in (m.attachments or [])]
    snap_content, snap_files = snapshot_files(getattr(m, "message_snapshots", None))
    content = content or snap_content
    files += snap_files
    files += embed_image_urls([e.to_dict() for e in (m.embeds or [])])
    return content, dedupe_files(files)


def dedupe_files(files):
    out, seen = [], set()
    for name, url in files:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((name, url))
    return out


async def fetch_file_bytes(url: str):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception:
        return None


DL_SEMAPHORE = asyncio.Semaphore(8)


async def forward_message_parts(m, target_channel):
    """Forward one message's files (any type, forwarded messages included) to
    target, downloads in parallel. Returns file count."""
    content, files_src = message_parts(m)
    if not files_src:
        return 0

    async def dl(name, url):
        async with DL_SEMAPHORE:
            data = await fetch_file_bytes(url)
            return discord.File(io.BytesIO(data), filename=name) if data else None

    files = [f for f in await asyncio.gather(*(dl(n, u) for n, u in files_src[:10])) if f]
    if not files:
        return 0

    kwargs = {}
    if content and len(content) <= 1900:
        kwargs["content"] = content
    try:
        for i in range(0, len(files), 10):
            await target_channel.send(files=files[i:i + 10], **kwargs)
            kwargs = {}  # caption only on the first chunk
    except Exception as e:
        print(f"send failed: {e}", flush=True)
        return 0
    return len(files)


async def fetch_batch(source_kind, source, before_id=None, limit=100):
    if source_kind == "bot":
        msgs = [m async for m in source.history(limit=limit,
                before=discord.Object(id=int(before_id)) if before_id else None)]
        return msgs
    url = f"{API}/channels/{source['id']}/messages?limit={limit}"
    if before_id:
        url += f"&before={before_id}"
    data = await rest_get_json(url)
    return data or []


async def copy_all_messages(source_kind, source, target_channel, status_msg=None):
    try:
        probe = await fetch_batch(source_kind, source, limit=1)
        if not probe:
            return False, ("Cannot access source channel. Ensure either:\n"
                           "1) The bot is invited to that server, OR\n"
                           "2) A user token is logged in via /login.")

        src_id = clean_id(source["id"]) if source_kind == "user" else str(source.id)
        await target_channel.send(f"**Starting FULL COPY of sources from <#{src_id}>**")

        total_copied = 0
        total_failed = 0
        total_messages = 0
        batch_count = 0
        before_id = None
        while True:
            batch = await fetch_batch(source_kind, source, before_id=before_id)
            if not batch:
                break
            batch_count += 1
            total_messages += len(batch)
            before_id = min(int(m["id"] if isinstance(m, dict) else m.id) for m in batch)

            for m in batch:
                try:
                    n = await forward_message_parts(m, target_channel)
                    total_copied += 1 if n else 0
                except Exception:
                    total_failed += 1
                await asyncio.sleep(0.1)

            if batch_count % 20 == 0 and status_msg is not None:
                try:
                    await status_msg.edit(
                        content=f"Copy in progress... scanned **{total_messages}** messages, forwarded **{total_copied}** file batch(es).")
                except Exception:
                    status_msg = None

            if len(batch) < 100:
                break

        guild_id = str(target_channel.guild.id)
        FORWARDS.setdefault(guild_id, {}).setdefault(str(target_channel.id), [])
        if src_id not in FORWARDS[guild_id][str(target_channel.id)]:
            FORWARDS[guild_id][str(target_channel.id)].append(src_id)
            save_forwards(FORWARDS)

        embed = discord.Embed(
            title="FULL COPY COMPLETE",
            description=f"Copied everything from <#{src_id}> to <#{target_channel.id}>",
            color=0x00FF00,
        )
        embed.add_field(name="Scanned Messages", value=str(total_messages), inline=True)
        embed.add_field(name="Forwarded", value=str(total_copied), inline=True)
        if total_failed:
            embed.add_field(name="Failed", value=str(total_failed), inline=True)
        embed.add_field(name="Auto-Forward", value="On: future files auto-forward", inline=False)
        await target_channel.send(embed=embed)
        return True, {"copied": total_copied, "failed": total_failed, "scanned": total_messages}
    except Exception as e:
        return False, str(e)


# ---------- events ----------

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.dnd,
                              activity=discord.Game(name="made by kers0nes"))
    print(f"Logged in as {bot.user} (id {bot.user.id}) - status: dnd, 'made by kers0nes'", flush=True)
    if USER_TOKEN:
        user = await validate_user_token(USER_TOKEN)
        if user:
            print(f"User-token session active: {user.get('global_name') or user.get('username')}", flush=True)
        else:
            print("Saved user token is expired/invalid. Run /login with a fresh token; "
                  "forwarding still works in servers the bot is in.", flush=True)


@bot.event
async def on_guild_join(guild):
    try:
        owner = bot.get_user(OWNER_NOTIFY_ID) or await bot.fetch_user(OWNER_NOTIFY_ID)
        if owner:
            await owner.send(
                f"{BOT_NAME} was just added to **{guild.name}** (`{guild.id}`)\n"
                f"Members: {guild.member_count}\nOwner: {guild.owner}")
    except Exception as e:
        print(f"guild-join DM failed: {e}", flush=True)


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    guild_id = str(message.guild.id) if message.guild else None
    if not guild_id:
        return
    mapping = FORWARDS.get(guild_id, {})
    for dest_id, sources in mapping.items():
        if str(message.channel.id) not in sources:
            continue
        dest = bot.get_channel(int(dest_id))
        if dest is None or dest.id == message.channel.id:
            continue
        try:
            await forward_message_parts(message, dest)
        except Exception as e:
            print(f"auto-forward error: {e}", flush=True)


@bot.event
async def setup_hook():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application commands", flush=True)
    except Exception as e:
        print(f"Command sync failed: {e}", flush=True)


@bot.tree.error
async def on_app_command_error(interaction, error):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"Command error: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Command error: {error}", ephemeral=True)
    except Exception:
        print(f"command error: {error}", flush=True)


def deny_owner(interaction) -> bool:
    """Returns True when denied (not the bot owner)."""
    if not owner_only(interaction):
        return True
    return False


# ---------- /login ----------

@bot.tree.command(name="login", description="Login with a user token (owner only, session persists)")
@app_commands.describe(token="Discord user token (enables forwarding from external servers)")
@app_commands.default_permissions(administrator=True)
async def login_slash(interaction: discord.Interaction, token: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return

    async def private_reply(text: str):
        try:
            await interaction.user.send(text)
            await interaction.response.send_message("Check your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(text, ephemeral=True)

    if token and token.strip():
        user = await validate_user_token(token)
        if user:
            global USER_TOKEN
            USER_TOKEN = re.sub(r"^(Bot|Bearer)\s+", "", token.strip(), flags=re.I)
            save_env_value("USER_TOKEN", USER_TOKEN)
            save_login_session(USER_TOKEN, user)
            name = user.get("global_name") or user.get("username")
            await private_reply(f"Logged in as **{name}**\n"
                                f"Session is saved permanently: it keeps working in every server this bot joins, "
                                f"and survives restarts. Forward from any channel with /forwardsources.")
        else:
            await interaction.response.send_message("Login failed. Please check your credentials.", ephemeral=True)
        return

    if USER_TOKEN:
        user = await validate_user_token(USER_TOKEN)
        if user:
            name = user.get("global_name") or user.get("username")
            await private_reply(f"Logged in as **{name}** (saved session, works in all servers).")
            return
        await private_reply("Your saved token no longer works. Run `/login` again with a fresh token. "
                            "Meanwhile, forwarding still works in servers the bot is in.")
        return

    name = interaction.user.global_name or interaction.user.name
    await private_reply(f"No user token saved. You are covered in servers the bot is in, **{name}**; "
                        f"run `/login token:<your token>` to forward from external servers too.")


# ---------- /forwardsources ----------

@bot.tree.command(name="forwardsources", description="Fast-forward ALL files from a source channel (any type), then auto-forward")
@app_commands.describe(
    channel_id="Source channel ID (where files are copied from)",
    destination_channel_id="Destination channel ID or mention (optional, defaults to current channel)",
)
@app_commands.default_permissions(administrator=True)
async def forwardsources_slash(interaction: discord.Interaction, channel_id: str, destination_channel_id: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    kind, source = await find_source_channel(channel_id)
    if source is None:
        await interaction.followup.send("Source channel not found. Make sure the bot is in that server or run /login with a user token first.")
        return

    dest_channel = interaction.channel
    if destination_channel_id and destination_channel_id.strip():
        did = clean_id(destination_channel_id)
        found = bot.get_channel(int(did)) if did.isdigit() else None
        if found is None or not found.is_text_based():
            await interaction.followup.send(f"Destination channel (`{did}`) not found or is not a text channel accessible by the bot.")
            return
        dest_channel = found

    src_id = str(source["id"]) if kind == "user" else str(source.id)
    if src_id == str(dest_channel.id):
        await interaction.followup.send("Source and destination channels cannot be the same!")
        return

    perms = dest_channel.permissions_for(dest_channel.guild.me)
    if not (perms.send_messages and perms.attach_files and perms.read_message_history):
        await interaction.followup.send(f"I don't have permission to send messages/files in <#{dest_channel.id}>.")
        return

    status = await interaction.followup.send(f"Starting FAST COPY from <#{src_id}> to <#{dest_channel.id}> (all file types, forwarded messages included)...")
    ok, result = await copy_all_messages(kind, source, dest_channel, status_msg=status)
    if not ok:
        await interaction.channel.send(f"Failed: {result}")


# ---------- /clone ----------

async def _clone_progress(status_msg, text):
    """Edit the clone status message so progress is visible; never fatal."""
    try:
        await status_msg.edit(content=text)
    except Exception:
        pass


async def build_source_plan(src_id: str):
    """Source server data via the bot when it is there, else the user token."""
    g = bot.get_guild(int(src_id))
    if g is not None:
        cats = sorted(g.categories, key=lambda c: (c.position if c.position is not None else 0, c.id))
        cat_index = {c.id: i for i, c in enumerate(cats)}
        plan = {
            "mode": "bot",
            "name": g.name,
            "icon_url": str(g.icon.url) if g.icon else None,
            "banner_url": str(g.banner.url) if g.banner else None,
            "roles": [{"name": r.name, "colour": r.colour, "permissions": r.permissions,
                       "hoist": r.hoist, "mentionable": r.mentionable, "position": r.position}
                      for r in sorted((r for r in g.roles if not r.is_default() and not r.managed),
                                      key=lambda r: (r.position, r.id))],
            "categories": [{"name": c.name, "position": c.position if c.position is not None else 0}
                           for c in cats],
            "channels": [], "emojis": [],
            "afk_channel_id": g.afk_channel.id if g.afk_channel else None,
            "afk_timeout": g.afk_timeout,
            "system_channel_id": g.system_channel.id if g.system_channel else None,
        }
        for ch in sorted((c for c in g.channels if not isinstance(c, discord.CategoryChannel)),
                         key=lambda c: (c.position if c.position is not None else 0, c.id)):
            ctype = ("text" if isinstance(ch, discord.TextChannel)
                     else "voice" if isinstance(ch, discord.VoiceChannel)
                     else "forum" if isinstance(ch, discord.ForumChannel) else None)
            if not ctype:
                continue
            plan["channels"].append({
                "type": ctype, "name": ch.name,
                "position": ch.position if ch.position is not None else 0,
                "parent_index": cat_index.get(ch.category_id),
                "src_id": ch.id,
                "topic": getattr(ch, "topic", None), "nsfw": getattr(ch, "nsfw", False),
                "slowmode": getattr(ch, "slowmode_delay", 0),
                "user_limit": getattr(ch, "user_limit", None),
            })
        plan["emojis"] = [{"name": e.name, "url": str(e.url)} for e in g.emojis]
        return plan

    if not USER_TOKEN:
        return None
    base = await rest_get_json(f"{API}/guilds/{src_id}")
    if not base:
        return None
    chans = await rest_get_json(f"{API}/guilds/{src_id}/channels") or []
    roles = await rest_get_json(f"{API}/guilds/{src_id}/roles") or []
    emojis = await rest_get_json(f"{API}/guilds/{src_id}/emojis") or []
    gid = base.get("id")

    def icon_url(icon):
        if not icon:
            return None
        ext = "gif" if icon.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/icons/{gid}/{icon}.{ext}?size=1024"

    def banner_url(b):
        if not b:
            return None
        return f"https://cdn.discordapp.com/banners/{gid}/{b}.png?size=1024"

    cats = sorted((c for c in chans if c.get("type") == 4),
                  key=lambda c: (c.get("position", 0), c["id"]))
    cat_index = {c["id"]: i for i, c in enumerate(cats)}
    plan = {
        "mode": "user",
        "name": base.get("name"),
        "icon_url": icon_url(base.get("icon")),
        "banner_url": banner_url(base.get("banner")),
        "roles": [{"name": r["name"],
                   "colour": discord.Colour(int(r.get("color") or 0)),
                   "permissions": discord.Permissions(int(r.get("permissions") or 0)),
                   "hoist": bool(r.get("hoist")), "mentionable": bool(r.get("mentionable")),
                   "position": r.get("position", 0)}
                  for r in sorted((r for r in roles if not r.get("managed") and r["name"] != "@everyone"),
                                  key=lambda r: (r.get("position", 0), r["id"]))],
        "categories": [{"name": c["name"], "position": c.get("position", 0)} for c in cats],
        "channels": [], "emojis": [],
        "afk_channel_id": base.get("afk_channel_id"),
        "afk_timeout": base.get("afk_timeout", 300),
        "system_channel_id": base.get("system_channel_id"),
    }
    for c in sorted((c for c in chans if c.get("type") != 4),
                    key=lambda c: (c.get("position", 0), c["id"])):
        ctype = {0: "text", 2: "voice", 15: "forum"}.get(c.get("type"))
        if not ctype:
            continue
        plan["channels"].append({
            "type": ctype, "name": c["name"], "position": c.get("position", 0),
            "parent_index": cat_index.get(c.get("parent_id")),
            "src_id": c["id"],
            "topic": c.get("topic"), "nsfw": bool(c.get("nsfw")),
            "slowmode": c.get("rate_limit_per_user") or 0,
            "user_limit": c.get("user_limit"),
        })
    for e in emojis:
        ext = "gif" if e.get("animated") else "png"
        plan["emojis"].append({"name": e["name"],
                               "url": f"https://cdn.discordapp.com/emojis/{e['id']}.{ext}"})
    return plan




@bot.tree.command(name="clone", description="Clone a server: icon, banner, roles, categories, channels, emojis, all in the original order")
@app_commands.describe(
    source_server_id="Source server ID (server to copy FROM)",
    destination_server_id="Destination server ID (defaults to current server)",
    clear_destination="Delete all destination channels and roles before cloning (default: true)",
    clone_roles="Clone roles with colors, permissions and order (default: true)",
    clone_emojis="Clone custom emojis as well (default: true)",
)
@app_commands.default_permissions(administrator=True)
async def clone_slash(interaction: discord.Interaction, source_server_id: str,
                      destination_server_id: str = None,
                      clear_destination: bool = True,
                      clone_roles: bool = True,
                      clone_emojis: bool = True):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    src_id = clean_id(source_server_id)
    dest_id = clean_id(destination_server_id) if destination_server_id else (str(interaction.guild.id) if interaction.guild else None)
    if not src_id or not src_id.isdigit():
        await interaction.followup.send("Provide a valid source server ID.")
        return
    if not dest_id or not dest_id.isdigit():
        await interaction.followup.send("Please specify a destination server ID or run this command inside a Discord server.")
        return

    plan = await build_source_plan(src_id)
    if plan is None:
        if USER_TOKEN and await validate_user_token(USER_TOKEN) is None:
            await interaction.followup.send(
                "Source server not found. The bot isn't in that server and your saved user token no longer works. "
                "Run /login with a fresh token from an account that is in the source server, then retry.")
            return
        await interaction.followup.send(
            "Source server not found. The bot isn't in that server. Run /login with a user token "
            "from an account that is in the source server, then retry.")
        return

    dest_guild = bot.get_guild(int(dest_id))
    if dest_guild is None:
        await interaction.followup.send("Destination server not found. Make sure the bot is in that server.")
        return

    src_name = plan["name"]
    status = await interaction.followup.send(
        f"Initiating FULL server clone...\nSource: `{src_name}` (via {plan['mode']} token)\nDestination: `{dest_guild.name}`\n"
        f"Icon + banner + roles + categories + channels + emojis, original order preserved.\n"
        f"Wipe destination: {'channels + roles' if clear_destination else 'No'}")

    dest_channels_before = len(dest_guild.channels)
    roles_cloned = 0
    roles_skipped = 0
    categories_cloned = 0
    channels_cloned = 0
    emojis_cloned = 0
    extras_done = []

    wiped_channels = 0
    try:
        if clear_destination and dest_guild.me.guild_permissions.manage_channels:
            for ch in list(dest_guild.channels):
                try:
                    await ch.delete(reason="Clone: clear destination")
                    wiped_channels += 1
                    if wiped_channels % 5 == 0:
                        await _clone_progress(status, f"Clearing destination... {wiped_channels} channels deleted")
                    await asyncio.sleep(0.7)
                except Exception:
                    pass
            await _clone_progress(status, f"Destination cleared: {wiped_channels} channels")

        roles_deleted = 0
        if clear_destination and dest_guild.me.guild_permissions.manage_roles:
            for r in list(dest_guild.roles):
                if r.is_default() or r.managed or r >= dest_guild.me.top_role:
                    continue
                try:
                    await r.delete(reason="Clone: clear destination")
                    roles_deleted += 1
                    if roles_deleted % 5 == 0:
                        await _clone_progress(status, f"Clearing destination... {roles_deleted} roles deleted")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass

        icon_bytes = None
        if plan["icon_url"]:
            icon_bytes = await fetch_file_bytes(plan["icon_url"])
        banner_bytes = None
        if plan["banner_url"]:
            banner_bytes = await fetch_file_bytes(plan["banner_url"])
        try:
            edit_kwargs = {"reason": "Clone: server identity"}
            if icon_bytes:
                edit_kwargs["icon"] = icon_bytes
            if banner_bytes:
                edit_kwargs["banner"] = banner_bytes
            if "icon" in edit_kwargs or "banner" in edit_kwargs:
                await dest_guild.edit(**edit_kwargs)
                if icon_bytes:
                    extras_done.append("icon")
                if banner_bytes:
                    extras_done.append("banner")
        except Exception:
            pass

        role_news = []
        if clone_roles and dest_guild.me.guild_permissions.manage_roles:
            for r in plan["roles"]:
                try:
                    new_role = await dest_guild.create_role(
                        name=r["name"],
                        colour=r["colour"],
                        permissions=r["permissions"],
                        hoist=r["hoist"],
                        mentionable=r["mentionable"],
                        reason="Clone")
                    role_news.append((r["position"], new_role))
                    roles_cloned += 1
                    await asyncio.sleep(0.6)
                except Exception:
                    roles_skipped += 1
            await _clone_progress(status, f"Cloning roles... {roles_cloned} created")
            for src_pos, new_role in sorted(role_news, key=lambda p: p[0]):
                try:
                    await new_role.edit(position=src_pos, reason="Clone: order")
                except Exception:
                    pass

        cat_news = []
        for c in plan["categories"]:
            try:
                nc = await dest_guild.create_category(c["name"], position=c["position"], reason="Clone")
                cat_news.append(nc)
                categories_cloned += 1
                await asyncio.sleep(0.7)
            except Exception:
                cat_news.append(None)

        await _clone_progress(status, f"Cloning categories... {categories_cloned} created")
        ch_news = []
        for ch in plan["channels"]:
            parent = None
            if ch["parent_index"] is not None and ch["parent_index"] < len(cat_news):
                parent = cat_news[ch["parent_index"]]
            try:
                new_ch = None
                if ch["type"] == "text":
                    new_ch = await dest_guild.create_text_channel(
                        ch["name"], category=parent, topic=ch["topic"], nsfw=ch["nsfw"],
                        slowmode_delay=ch["slowmode"], position=ch["position"], reason="Clone")
                elif ch["type"] == "voice":
                    new_ch = await dest_guild.create_voice_channel(
                        ch["name"], category=parent, user_limit=ch["user_limit"] or 0,
                        position=ch["position"], reason="Clone")
                elif ch["type"] == "forum":
                    new_ch = await dest_guild.create_forum(
                        ch["name"], category=parent, topic=ch["topic"], nsfw=ch["nsfw"],
                        position=ch["position"], reason="Clone")
                ch_news.append(new_ch)
                if new_ch is not None:
                    channels_cloned += 1
                    if channels_cloned % 5 == 0:
                        await _clone_progress(status, f"Cloning channels... {channels_cloned}/{len(plan['channels'])}")
                await asyncio.sleep(0.7)
            except Exception:
                ch_news.append(None)
                continue

        await _clone_progress(status, f"Channels done: {channels_cloned}. Copying emojis..." if clone_emojis else f"Channels done: {channels_cloned}")
        if clone_emojis and dest_guild.me.guild_permissions.manage_emojis:
            for e in plan["emojis"]:
                try:
                    data = await fetch_file_bytes(e["url"])
                    if not data:
                        continue
                    await dest_guild.create_custom_emoji(name=e["name"], image=data, reason="Clone")
                    emojis_cloned += 1
                    await asyncio.sleep(0.7)
                except Exception:
                    continue

        try:
            edit_kwargs = {"reason": "Clone: server settings"}
            afk_idx = next((i for i, ch in enumerate(plan["channels"])
                            if ch.get("src_id") is not None and str(ch["src_id"]) == str(plan["afk_channel_id"])), None)
            sys_idx = next((i for i, ch in enumerate(plan["channels"])
                            if ch.get("src_id") is not None and str(ch["src_id"]) == str(plan["system_channel_id"])), None)
            if afk_idx is not None and afk_idx < len(ch_news) and ch_news[afk_idx] is not None:
                edit_kwargs["afk_channel"] = ch_news[afk_idx]
                edit_kwargs["afk_timeout"] = plan["afk_timeout"] or 300
            if sys_idx is not None and sys_idx < len(ch_news) and ch_news[sys_idx] is not None:
                edit_kwargs["system_channel"] = ch_news[sys_idx]
            if len(edit_kwargs) > 1:
                await dest_guild.edit(**edit_kwargs)
                extras_done.append("afk/system channel")
        except Exception:
            pass

        extras_done = [e for e in extras_done if e]
        embed = discord.Embed(
            title="Server Successfully Cloned",
            description=f"Replicated **{src_name}** to **{dest_guild.name}** in the original order",
            color=0x00E5FF,
        )
        embed.add_field(name="Roles", value=f"{roles_cloned}" + (f" ({roles_skipped} skipped)" if roles_skipped else ""), inline=True)
        embed.add_field(name="Categories", value=str(categories_cloned), inline=True)
        embed.add_field(name="Channels", value=str(channels_cloned), inline=True)
        embed.add_field(name="Custom Emojis", value=str(emojis_cloned), inline=True)
        if clear_destination:
            embed.add_field(name="Destination Wiped", value=f"{dest_channels_before} channels, {roles_deleted} roles", inline=True)
        if extras_done:
            embed.add_field(name="Also Cloned", value=", ".join(extras_done), inline=False)
        embed.set_footer(text="r3e server cloner - full clone, original order")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Server clone failed: {e}")


async def _resolve_target_channel(interaction, channel_id):
    if channel_id and channel_id.strip():
        did = clean_id(channel_id)
        found = bot.get_channel(int(did)) if did.isdigit() else None
        if found is None or not found.is_text_based():
            await interaction.followup.send(f"Channel (`{did}`) not found or is not a text channel I can see.")
            return None
        return found
    return interaction.channel


async def _check_mod_perms(interaction, channel) -> bool:
    me = channel.guild.me
    perms = channel.permissions_for(me)
    if not (perms.read_message_history and perms.manage_messages):
        await interaction.followup.send(f"I need Read Message History and Manage Messages in <#{channel.id}>.")
        return False
    return True


async def _clear_channel(channel, include_old: bool):
    """Wipe a channel. include_old also deletes messages past the 14-day
    bulk-delete window one by one. Returns (deleted, skipped)."""
    now = discord.utils.utcnow()
    deleted = 0
    skipped = 0
    while True:
        msgs = [m async for m in channel.history(limit=100)]
        if not msgs:
            break
        recent = [m for m in msgs if m.pinned is False and (now - m.created_at).total_seconds() < 14 * 24 * 3600]
        old = [m for m in msgs if m not in recent]

        if recent:
            try:
                await channel.delete_messages(recent)
                deleted += len(recent)
            except Exception:
                for m in recent:
                    try:
                        await m.delete()
                        deleted += 1
                    except Exception:
                        skipped += 1
                    await asyncio.sleep(0.6)

        if include_old:
            for m in old:
                try:
                    await m.delete()
                    deleted += 1
                except Exception:
                    skipped += 1
                await asyncio.sleep(0.6)
        else:
            skipped += len(old)

        if len(msgs) < 100:
            break
    return deleted, skipped


@bot.tree.command(name="clear", description="Bulk delete messages in a channel, skipping pinned and messages older than 14 days")
@app_commands.describe(channel_id="Channel ID to clear (defaults to the current channel)")
@app_commands.default_permissions(administrator=True)
async def clear_slash(interaction: discord.Interaction, channel_id: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    channel = await _resolve_target_channel(interaction, channel_id)
    if channel is None:
        return
    if not await _check_mod_perms(interaction, channel):
        return
    deleted, skipped = await _clear_channel(channel, include_old=False)
    await interaction.followup.send(
        f"Cleared **{deleted}** message(s) in <#{channel.id}>."
        + (f"\n**{skipped}** older than 14 days were left (use /purge all for those)." if skipped else ""))


@bot.tree.command(name="purge", description="Purge ALL messages in a channel, including old ones")
@app_commands.describe(
    all="Set true to confirm (purges everything, even messages older than 14 days)",
    channel_id="Channel to purge (optional, defaults to current channel)",
)
@app_commands.default_permissions(administrator=True)
async def purge_slash(interaction: discord.Interaction, all: bool, channel_id: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    if not all:
        await interaction.response.send_message("This wipes every message in the channel. Confirm by setting `all` to true.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    channel = await _resolve_target_channel(interaction, channel_id)
    if channel is None:
        return
    if not await _check_mod_perms(interaction, channel):
        return
    deleted, skipped = await _clear_channel(channel, include_old=True)
    await interaction.followup.send(
        f"Purged **{deleted}** message(s) in <#{channel.id}>, old ones included."
        + (f"\n**{skipped}** could not be deleted (missing perms or already gone)." if skipped else ""))


# ---------- /ban, /unban, /lock, /unlock ----------

@bot.tree.command(name="ban", description="Ban a user from the server (owner only)")
@app_commands.describe(
    member="User to ban",
    reason="Reason (optional)",
    delete_days="Also delete their messages from the last N days (0-7, default 0)",
)
@app_commands.default_permissions(administrator=True)
async def ban_slash(interaction: discord.Interaction, member: discord.User,
                    reason: str = None, delete_days: app_commands.Range[int, 0, 7] = 0):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Use /ban inside a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    me = interaction.guild.me
    if not me.guild_permissions.ban_members:
        await interaction.followup.send("I need the **Ban Members** permission in this server.")
        return
    try:
        await interaction.guild.ban(
            member,
            delete_message_seconds=delete_days * 86400,
            reason=f"By {interaction.user}: {reason or 'no reason given'}")
    except discord.Forbidden:
        await interaction.followup.send("Ban failed: my role is too low or I lack permission for that user.")
        return
    except Exception as e:
        await interaction.followup.send(f"Ban failed: {e}")
        return
    msg = f"Banned **{member}** (`{member.id}`)"
    if delete_days:
        msg += f" and deleted their messages from the last {delete_days} day(s)"
    await interaction.followup.send(msg + ("." if not reason else f".\nReason: {reason}"))


@bot.tree.command(name="unban", description="Unban a user by ID (owner only)")
@app_commands.describe(user_id="ID of the banned user", reason="Reason (optional)")
@app_commands.default_permissions(administrator=True)
async def unban_slash(interaction: discord.Interaction, user_id: str, reason: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Use /unban inside a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    uid = clean_id(user_id)
    if not uid.isdigit():
        await interaction.followup.send("Provide a valid user ID to unban.")
        return
    if not interaction.guild.me.guild_permissions.ban_members:
        await interaction.followup.send("I need the **Ban Members** permission in this server.")
        return
    try:
        user = await interaction.guild.unban(discord.Object(id=int(uid)),
                                             reason=f"By {interaction.user}: {reason or 'no reason given'}")
    except discord.NotFound:
        await interaction.followup.send(f"No ban found for user ID `{uid}` in this server.")
        return
    except Exception as e:
        await interaction.followup.send(f"Unban failed: {e}")
        return
    await interaction.followup.send(f"Unbanned **{user}** (`{uid}`).")


@bot.tree.command(name="lock", description="Lock a channel so members can't send messages (owner only)")
@app_commands.describe(channel_id="Channel to lock (optional, defaults to current channel)")
@app_commands.default_permissions(administrator=True)
async def lock_slash(interaction: discord.Interaction, channel_id: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    channel = await _resolve_target_channel(interaction, channel_id)
    if channel is None:
        return
    me = channel.guild.me
    if not channel.permissions_for(me).manage_permissions:
        await interaction.followup.send(f"I need Manage Permissions in <#{channel.id}>.")
        return
    overwrite = channel.overwrites_for(channel.guild.default_role)
    overwrite.send_messages = False
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite,
                                  reason=f"Locked by {interaction.user}")
    await interaction.followup.send(f"Locked <#{channel.id}>. Members can't send messages.")


@bot.tree.command(name="unlock", description="Unlock a locked channel (owner only)")
@app_commands.describe(channel_id="Channel to unlock (optional, defaults to current channel)")
@app_commands.default_permissions(administrator=True)
async def unlock_slash(interaction: discord.Interaction, channel_id: str = None):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    channel = await _resolve_target_channel(interaction, channel_id)
    if channel is None:
        return
    me = channel.guild.me
    if not channel.permissions_for(me).manage_permissions:
        await interaction.followup.send(f"I need Manage Permissions in <#{channel.id}>.")
        return
    overwrite = channel.overwrites_for(channel.guild.default_role)
    overwrite.send_messages = True
    await channel.set_permissions(channel.guild.default_role, overwrite=overwrite,
                                  reason=f"Unlocked by {interaction.user}")
    await interaction.followup.send(f"Unlocked <#{channel.id}>. Members can send messages again.")


@bot.tree.command(name="lockall", description="Lockdown: members can't send messages in any channel (owner only)")
@app_commands.default_permissions(administrator=True)
async def lockall_slash(interaction: discord.Interaction):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Run this inside a server.")
        return
    me = guild.me
    if not me.guild_permissions.manage_permissions:
        await interaction.followup.send("I need Manage Permissions in this server.")
        return
    locked = 0
    failed = 0
    for ch in list(guild.channels):
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
            continue
        try:
            overwrite = ch.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await ch.set_permissions(guild.default_role, overwrite=overwrite,
                                     reason=f"Lockdown by {interaction.user}")
            locked += 1
            if locked % 5 == 0:
                await interaction.edit_original_response(content=f"Locking channels... {locked} done")
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await interaction.followup.send(
        f"Lockdown complete: {locked} channels locked." + (f" ({failed} skipped)" if failed else "") +
        " Use /unlockall to reverse it.")


@bot.tree.command(name="unlockall", description="Reverse a lockdown: members can send messages everywhere (owner only)")
@app_commands.default_permissions(administrator=True)
async def unlockall_slash(interaction: discord.Interaction):
    if deny_owner(interaction):
        await interaction.response.send_message("Permission denied.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Run this inside a server.")
        return
    me = guild.me
    if not me.guild_permissions.manage_permissions:
        await interaction.followup.send("I need Manage Permissions in this server.")
        return
    unlocked = 0
    failed = 0
    for ch in list(guild.channels):
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
            continue
        try:
            overwrite = ch.overwrites_for(guild.default_role)
            overwrite.send_messages = True
            await ch.set_permissions(guild.default_role, overwrite=overwrite,
                                     reason=f"Lockdown lifted by {interaction.user}")
            unlocked += 1
            if unlocked % 5 == 0:
                await interaction.edit_original_response(content=f"Unlocking channels... {unlocked} done")
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await interaction.followup.send(
        f"Lockdown lifted: {unlocked} channels unlocked." + (f" ({failed} skipped)" if failed else ""))


load_login_session()

if __name__ == "__main__":
    bot.run(BOT_TOKEN, log_handler=None)
