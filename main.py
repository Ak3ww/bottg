import os
import logging
import re
import tweepy
import asyncio
import aiohttp
from io import BytesIO
from typing import Optional
from datetime import datetime, timedelta
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

# Twitter Regex
TWITTER_URL_REGEX = re.compile(r"https://(?:www\.)?(?:twitter|x)\.com/\w+/status/(\d+)")

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
MAX_FORWARDED_TWEETS_TO_TRACK = 5  # Number of forwarded tweets to track
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
            wait_on_rate_limit=True  # Automatically handle rate limits
        )
        logger.info("✅ Twitter API Connected")
        _twitter_api_client = client
        return _twitter_api_client

    except tweepy.TweepyException as e:
        logger.error(f"❌ Twitter API Error: {e}")
        return None

# ✅ Load Twitter User ID
async def initialize_twitter_user_id():
    """Loads user ID from a file or fetches it if not found."""
    global twitter_user_id
    api = await get_twitter_api()
    if not api:
        logger.error("❌ ERROR: Twitter API could not be initialized.")
        return False

    if os.path.exists(USER_ID_CACHE_FILE):
        try:
            with open(USER_ID_CACHE_FILE, 'r') as f:
                data = json.load(f)
                twitter_user_id = data['user_id']
            logger.info(f"✅ Loaded User ID from file: {twitter_user_id}")
            return True
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Error loading user ID from cache: {e}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"🔍 Fetching user ID for @{twitter_username} (Attempt {attempt}/{MAX_RETRIES})")
            user = await api.get_user(username=twitter_username)
            twitter_user_id = str(user.data.id)
            data = {'user_id': twitter_user_id, 'timestamp': datetime.now().isoformat()}
            with open(USER_ID_CACHE_FILE, "w") as f:
                json.dump(data, f)
            logger.info(f"✅ User ID saved: {twitter_user_id}")
            return True
        except tweepy.TweepyException as e:
            logger.error(f"❌ ERROR fetching user ID: {e}")
            await asyncio.sleep(BASE_DELAY * (2 ** attempt))

    logger.error(f"🚨 Could not fetch user ID for @{twitter_username} after multiple attempts.")
    return False

# ✅ Start Command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_message = """
    📌 Welcome! This bot forwards Tweets from X to a Telegram channel.

    ➡️ Simply send me a valid tweet URL, and I will forward the text with media to the channel.

    ⚠️ Rate limits from the Twitter API might cause delays or re-queueing of your Tweet.
    """
    await context.bot.send_message(chat_id=update.effective_chat.id, text=start_message)

# ✅ Toggle Watch Mode
async def watch_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles watch mode and starts/stops the auto-fetching process."""
    global WATCH_MODE_ENABLED
    WATCH_MODE_ENABLED = not WATCH_MODE_ENABLED
    status = "enabled" if WATCH_MODE_ENABLED else "disabled"

    logger.info(f"🔄 Watch mode is now {status}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔄 Watch mode is now {status}.")
    
    if WATCH_MODE_ENABLED:
        asyncio.create_task(fetch_and_forward_tweets(context))

# ✅ Auto-fetch tweets from a user
async def fetch_and_forward_tweets(context: ContextTypes.DEFAULT_TYPE):
    """Automatically fetches new tweets from @avocadoguild and forwards them to Telegram."""
    global WATCH_MODE_ENABLED, twitter_user_id
    api = await get_twitter_api()
    if not api:
        logger.error("❌ Twitter API could not be initialized.")
        return

    while WATCH_MODE_ENABLED:
        try:
            logger.info(f"🔍 Fetching latest tweets from @{twitter_username}...")

            response = await api.get_users_tweets(
                id=twitter_user_id,
                tweet_fields=["created_at", "text", "entities"],
                expansions=["attachments.media_keys"],
                media_fields=["url", "type", "variants"],
                max_results=5  # Fetch last 5 tweets
            )

            if response and response.data:
                latest_tweet = response.data[0]
                tweet_id = latest_tweet.id

                if not any(d['tweet_id'] == tweet_id for d in FORWARDED_TWEETS):  # Avoid duplicates
                    logger.info(f"✅ New tweet found: {tweet_id}")
                    await send_tweet_to_telegram(latest_tweet, context)

                    # ✅ Store tweet in history
                    FORWARDED_TWEETS.insert(0, {"tweet_id": tweet_id})
                    if len(FORWARDED_TWEETS) > MAX_FORWARDED_TWEETS_TO_TRACK:
                        FORWARDED_TWEETS.pop()
                else:
                    logger.info(f"⏩ Skipping duplicate tweet {tweet_id}")

            else:
                logger.info(f"⚠️ No new tweets found for @{twitter_username}.")

        except tweepy.TweepyException as e:
            logger.error(f"❌ Error fetching tweets: {e}")

        await asyncio.sleep(POLL_INTERVAL)  # Sleep for 20 minutes before checking again

# ✅ Main Function
async def start_bot():
    logger.info("🚀 Bot started.")
    application = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("fwd_list", fwd_list_command))
    application.add_handler(CommandHandler("watchmode", watch_mode_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_tweet_url))

    if await initialize_twitter_user_id():
        asyncio.create_task(fetch_and_forward_tweets(application.bot))

    bot_commands = [
        BotCommand("start", "Manually forward a tweet"),
        BotCommand("fwd_list", "List latest forwarded tweets"),
        BotCommand("watchmode", "Toggle watch mode"),
    ]
    await application.bot.set_my_commands(bot_commands)

    async with application:
        await application.start()
        await application.updater.start_polling()
        asyncio.create_task(process_tweet_queue())
        await asyncio.Event().wait()

