import os
import logging
import re
import tweepy
import asyncio
import aiohttp
from io import BytesIO
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv
from tweepy import Client  # Use synchronous Tweepy Client
from telegram import Update, InputMediaPhoto, InputMediaVideo, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import json

load_dotenv()

# ✅ Configure Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ✅ Global Variables
_twitter_api_client = None
tweet_queue = asyncio.Queue(maxsize=5)
twitter_username = os.getenv("TWITTER_USERNAME_TO_MONITOR")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
MAX_RETRIES = 3
BASE_DELAY = 15  # Base delay in seconds
POLL_INTERVAL = 1200  # Check every 20 minutes
USER_ID_CACHE_FILE = "user_id_cache.json"
FORWARDED_TWEETS = []
MAX_FORWARDED_TWEETS_TO_TRACK = 5
WATCH_MODE_ENABLED = False

# ✅ Twitter API Client
def get_twitter_api():
    global _twitter_api_client
    if _twitter_api_client:
        return _twitter_api_client

    try:
        client = Client(
            bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
            consumer_key=os.getenv("TWITTER_CONSUMER_KEY"),
            consumer_secret=os.getenv("TWITTER_CONSUMER_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
            wait_on_rate_limit=True
        )
        logger.info("✅ Twitter API Connected")
        _twitter_api_client = client
        return _twitter_api_client

    except tweepy.TweepyException as e:
        logger.error(f"❌ Twitter API Error: {e}")
        return None

# ✅ Fetch Latest Tweet (Ignoring Replies & Retweets)
def get_latest_tweet():
    """Fetch the latest tweet from the monitored account, ignoring replies and retweets."""
    api = get_twitter_api()
    if not api:
        logger.error("❌ Twitter API could not be initialized.")
        return None

    try:
        logger.info(f"🔍 Fetching latest tweets from @{twitter_username}...")
        tweets = api.get_users_tweets(
            id=twitter_username,
            tweet_fields=["created_at", "text", "referenced_tweets"],
            expansions=["attachments.media_keys"],
            media_fields=["url", "type", "variants"],
            max_results=5  # Fetch last 5 tweets
        )

        if tweets and tweets.data:
            for tweet in tweets.data:
                if "referenced_tweets" not in tweet:  # Ignore replies & retweets
                    return tweet
        logger.info("⚠️ No valid new tweets found.")
        return None

    except tweepy.TweepyException as e:
        logger.error(f"❌ Error fetching tweets: {e}")
        return None

# ✅ Process Tweet URL
async def process_tweet_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes a tweet URL, adding it to the queue if valid."""
    match = re.search(r"https://(?:www\.)?(?:twitter|x)\.com/\w+/status/(\d+)", update.message.text)
    if match:
        tweet_id = match.group(1)
        logger.info(f"Detected Tweet ID: {tweet_id}")
        await tweet_queue.put((update, tweet_id, context))
        await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Tweet added to queue!")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Please provide a valid Twitter URL.")

# ✅ Download Media
async def download_media(url: str) -> Optional[tuple[BytesIO, str]]:
    """Downloads media from a URL and returns it as a BytesIO object."""
    logger.info(f"📥 Downloading media from: {url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    file_extension = url.split(".")[-1].split("?")[0]
                    return BytesIO(content), f"media.{file_extension}"
                logger.error(f"⚠️ Failed to download media. HTTP Status: {response.status}")
                return None, None
    except Exception as e:
        logger.error(f"❌ Error downloading media: {e}")
        return None, None

# ✅ Send Tweet to Telegram
async def send_tweet_to_telegram(tweet, context: ContextTypes.DEFAULT_TYPE):
    """Formats and sends the tweet with media as a single post to Telegram."""
    tweet_text = tweet.text
    tweet_id = tweet.id
    tweet_url = f"https://twitter.com/{twitter_username}/status/{tweet_id}"
    formatted_text = f"{tweet_text}\n\n<a href='{tweet_url}'>View on X</a>"
    media_list = tweet.get("includes", {}).get("media", [])
    media_group = []

    if not media_list:  # If no media, send text only
        await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=formatted_text, parse_mode=ParseMode.HTML)
        return

    for i, media in enumerate(media_list):
        media_url = media.get("url")
        media_type = media.get("type")
        caption_text = formatted_text if i == 0 else None
        if media_url and media_type:
            media_content, filename = await download_media(media_url)
            if media_content:
                media_content.seek(0)
                if media_type == "photo":
                    media_group.append(InputMediaPhoto(media_content, caption=caption_text, parse_mode=ParseMode.HTML))
                elif media_type in ["video", "animated_gif"]:
                    media_group.append(InputMediaVideo(media_content, caption=caption_text, parse_mode=ParseMode.HTML))

    if media_group:
        await context.bot.send_media_group(chat_id=TELEGRAM_CHANNEL_ID, media=media_group)

# ✅ Start Bot Function
async def start_bot():
    logger.info("🚀 Bot started.")
    application = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_tweet_url))

    async with application:
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()
