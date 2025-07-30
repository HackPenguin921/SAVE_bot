import discord
from discord.ext import commands, tasks
import aiohttp
import json
import os
from datetime import datetime
from geopy.geocoders import Nominatim
import feedparser

load_dotenv()
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

CHANNEL_FILE = "guild_channels.json"
REGION_FILE = "user_region.json"
LAST_QUAKE_FILE = "last_quake_id.json"
LAST_JALERT_FILE = "last_jalert_id.json"

def load_json(filename):
    if not os.path.exists(filename):
        with open(filename, "w") as f:
            json.dump({}, f)
    with open(filename, "r") as f:
        return json.load(f)

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)

guild_channels = load_json(CHANNEL_FILE)
user_region = load_json(REGION_FILE)
last_quake = load_json(LAST_QUAKE_FILE)
last_jalert = load_json(LAST_JALERT_FILE)

# 地名→緯度経度
def geocode_location(location_name):
    geolocator = Nominatim(user_agent="quake_bot")
    location = geolocator.geocode(location_name)
    if location:
        return location.latitude, location.longitude
    return None

@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx):
    guild_channels[str(ctx.guild.id)] = ctx.channel.id
    save_json(CHANNEL_FILE, guild_channels)
    await ctx.send(f"このチャンネルを通知チャンネルに設定しました。")

@bot.command()
async def showchannel(ctx):
    channel_id = guild_channels.get(str(ctx.guild.id))
    if channel_id:
        channel = bot.get_channel(channel_id)
        await ctx.send(f"通知チャンネルは {channel.mention} に設定されています。")
    else:
        await ctx.send("通知チャンネルはまだ設定されていません。")

@bot.command()
async def setregion(ctx, *, location):
    latlon = geocode_location(location)
    if latlon:
        user_region[str(ctx.author.id)] = {
            "location": location,
            "lat": latlon[0],
            "lon": latlon[1]
        }
        save_json(REGION_FILE, user_region)
        await ctx.send(f"{ctx.author.mention} 地域を「{location}」に設定しました。")
    else:
        await ctx.send("地域の位置情報を取得できませんでした。地名を確認してください。")

@bot.command()
async def showregion(ctx):
    region = user_region.get(str(ctx.author.id))
    if region:
        await ctx.send(f"{ctx.author.mention} の設定地域は {region['location']} です。")
    else:
        await ctx.send("あなたの地域はまだ設定されていません。")

# 距離計算（ハーバーサイン式）
from math import radians, sin, cos, sqrt, atan2

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2*atan2(sqrt(a), sqrt(1-a))
    return R*c

# 震度簡易推定
def estimate_shindo(magnitude, distance_km):
    if distance_km == 0:
        distance_km = 1  # ゼロ除算防止
    intensity = 1.5 * magnitude - 3.0 * (distance_km / 100)
    return max(0, round(intensity))

# EEWチェック（P2PQuake v2 JMA Quake API）
@tasks.loop(seconds=30)
async def check_quake():
    url = "https://api.p2pquake.net/v2/jma/quake"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if not data:
                return
            quake = data[0]
            quake_id = quake.get("id")
            if quake_id == last_quake.get("id"):
                return  # 新規情報なし

            last_quake["id"] = quake_id
            save_json(LAST_QUAKE_FILE, last_quake)

            epicenter = quake["earthquake"]["hypocenter"]["name"]
            mag = quake["earthquake"]["magnitude"]
            origin_time = quake["earthquake"]["origin_time"]

            lat = quake["earthquake"]["hypocenter"]["latitude"]
            lon = quake["earthquake"]["hypocenter"]["longitude"]

            for guild_id, channel_id in guild_channels.items():
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                # 各ギルドのユーザーの震度推定をまとめる（簡易版）
                mentions = []
                for user_id, region in user_region.items():
                    d = haversine(lat, lon, region["lat"], region["lon"])
                    shindo = estimate_shindo(mag, d)
                    if shindo >= 3:
                        mentions.append(f"<@{user_id}> (震度{shindo})")

                msg = (
                    f"📢 **緊急地震速報**\n"
                    f"震源地: {epicenter}\n"
                    f"マグニチュード: {mag}\n"
                    f"発生時刻: {origin_time}\n"
                )
                if mentions:
                    msg += "揺れる可能性のあるユーザー:\n" + "\n".join(mentions)

                await channel.send(msg)

# 津波警報チェック（P2PQuake 津波コード561）
@tasks.loop(seconds=60)
async def check_tsunami():
    url = "https://api.p2pquake.net/v2/history?codes=561&limit=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if not data:
                return
            tsunami = data[0].get("tsunami", {})
            cancelled = tsunami.get("cancelled", True)
            if cancelled:
                return

            for guild_id, channel_id in guild_channels.items():
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                warnings = tsunami.get("warnings", [])
                msg_lines = ["🌊 **津波警報情報**"]
                for warn in warnings:
                    area = warn["area"]["name"]
                    grade = warn["grade"]
                    immediate = "即時" if warn["immediate"] else "通常"
                    msg_lines.append(f"{area}：{grade}（{immediate}）")
                await channel.send("\n".join(msg_lines))

# J-ALERTチェック（NHK RSS監視）
last_entry_id = None

@tasks.loop(seconds=120)
async def check_jalert():
    global last_entry_id
    rss_url = "https://www3.nhk.or.jp/rss/jalert.xml"
    feed = feedparser.parse(rss_url)
    if not feed.entries:
        return
    entry = feed.entries[0]
    if entry.id == last_entry_id:
        return
    last_entry_id = entry.id

    msg = f"🚨 **J-ALERT速報**\n{entry.title}\n{entry.summary}"
    for guild_id, channel_id in guild_channels.items():
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(msg)

@bot.event
async def on_ready():
    print(f"ログインしました: {bot.user}")
    check_quake.start()
    check_tsunami.start()
    check_jalert.start()

bot.run(os.getenv("DISCORD_BOT_TOKEN"))
