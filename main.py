import os
import logging
import json
import re
import tweepy
import asyncio
from dotenv import load_dotenv
from tweepy.asynchronous import AsyncClient
from telegram import Update, InputMediaPhoto
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ✅ Load environment variables
load_dotenv()

# ✅ Configure Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ✅ Twitter Regex
TWITTER_URL_REGEX = re.compile(r"https://(?:www\.)?(?:twitter|x)\.com/\w+/status/(\d+)")

# ✅ Global Variables
_twitter_api_client = None
twitter_username = os.getenv("TWITTER_USERNAME_TO_MONITOR")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
POLL_INTERVAL = 1200  # 20 minutes
WATCH_MODE_ENABLED = False
twitter_user_id = None
watchmode_chat_id = None

# ✅ Read Twitter User ID from JSON File
def get_twitter_user_id():
    try:
        with open("user_id.json", "r") as file:
            data = json.load(file)
            return data.get("user_id")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"❌ ERROR: Failed to read user_id.json: {e}")
        return None

# ✅ Twitter API Client
async def get_twitter_api():
    global _twitter_api_client
    if _twitter_api_client:
        return _twitter_api_client

    try:
        client = AsyncClient(
            bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
            consumer_key=os.getenv("TWITTER_CONSUMER_KEY"),
            consumer_secret=os.getenv("TWITTER_CONSUMER_SECRET"),
            access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
            access_token_secret=os.getenv("TWITTER_ACCESS_TOKEN_SECRET"),
        )
        logger.info("✅ Twitter API Connected")
        _twitter_api_client = client
        return _twitter_api_client
    except tweepy.TweepyException as e:
        logger.error(f"❌ Twitter API Error: {e}")
        return None

# ✅ Initialize Twitter User ID
async def initialize_twitter():
    global twitter_user_id
    api = await get_twitter_api()
    if not api:
        logger.error("❌ ERROR: Twitter API could not be initialized.")
        return False

    twitter_user_id = get_twitter_user_id()
    if not twitter_user_id:
        logger.error("❌ ERROR: Twitter User ID could not be loaded from user_id.json.")
        return False

    logger.info(f"✅ Twitter User ID Loaded: {twitter_user_id}")
    return True

# ✅ Start Command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_message = "📌 Send me a valid tweet URL, and I will forward it to the channel."
    await context.bot.send_message(chat_id=update.effective_chat.id, text=start_message)
    logger.info(f"📢 [LOG] /start command used by {update.effective_user.id}")

# ✅ Extract Media from Tweet
def extract_media(tweet):
    """Extracts media URLs from a tweet."""
    media_urls = []
    
    if not tweet.includes or "media" not in tweet.includes:
        return media_urls  # No media found

    for media in tweet.includes["media"]:
        if media["type"] == "photo":
            media_urls.append(media["url"])
    
    return media_urls

# ✅ Process Tweet URL
async def process_tweet_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tweet URLs sent by users"""
    chat_id = update.effective_chat.id
    message = update.message.text
    match = TWITTER_URL_REGEX.search(message)

    if match:
        tweet_id = match.group(1)
        logger.info(f"🔄 [LOG] Detected Tweet ID: {tweet_id}")
        await context.bot.send_message(chat_id=chat_id, text=f"🔄 [LOG] Processing Tweet ID: {tweet_id}...")

        api = await get_twitter_api()
        try:
            tweet = await api.get_tweet(
                tweet_id, 
                expansions=["attachments.media_keys"], 
                media_fields=["url", "type"]
            )
        except tweepy.errors.TooManyRequests:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ [LOG] Twitter API rate limit hit! Try again later.")
            return

        if tweet and tweet.data:
            await send_tweet_to_telegram(tweet, context)
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ [LOG] Tweet not found!")
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ [LOG] Invalid Twitter URL!")

# ✅ Send Tweet to Telegram
async def send_tweet_to_telegram(tweet, context: ContextTypes.DEFAULT_TYPE):
    """Uploads media and forwards tweet to Telegram."""
    tweet_text = tweet.data.get("text", "")
    tweet_id = tweet.data.get("id", "")
    tweet_url = f"https://twitter.com/{twitter_username}/status/{tweet_id}"
    formatted_text = f"{tweet_text}\n\n<a href='{tweet_url}'>View on X</a>"

    media_urls = extract_media(tweet)

    if not media_urls:
        await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=formatted_text, parse_mode="HTML")
    else:
        await context.bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=media_urls[0], caption=formatted_text, parse_mode="HTML")

# ✅ Watch Mode: Fetch and Forward Tweets
async def fetch_and_forward_tweets(context: ContextTypes.DEFAULT_TYPE):
    """Automatically fetches new tweets and forwards them"""
    global WATCH_MODE_ENABLED
    api = await get_twitter_api()
    if not api:
        return

    while WATCH_MODE_ENABLED:
        try:
            logger.info(f"🔍 Checking for new tweets from @{twitter_username}...")
            response = await api.get_users_tweets(
                id=twitter_user_id,
                tweet_fields=["created_at", "text"],
                expansions=["attachments.media_keys"],
                media_fields=["url", "type"],
                max_results=5
            )

            if response and response.data:
                for tweet in response.data:
                    if "referenced_tweets" in tweet.data:
                        logger.info(f"❌ Skipping Tweet ID {tweet.data['id']} (It's a reply)")
                        continue  # Skip replies

                    logger.info(f"✅ Forwarding Tweet ID {tweet.data['id']} to Telegram...")
                    await send_tweet_to_telegram(tweet, context)
                    break  # Stop after first valid tweet

        except tweepy.errors.TooManyRequests:
            logger.warning("⚠️ Rate limit hit, retrying in 20 minutes...")
            await asyncio.sleep(POLL_INTERVAL)

        await asyncio.sleep(POLL_INTERVAL)

# ✅ Watch Mode Command
async def watch_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle watch mode on/off and start the task if enabled"""
    global WATCH_MODE_ENABLED, watchmode_chat_id
    WATCH_MODE_ENABLED = not WATCH_MODE_ENABLED
    watchmode_chat_id = update.effective_chat.id

    status = "enabled ✅" if WATCH_MODE_ENABLED else "disabled ❌"
    logger.info(f"🔄 Watch mode is now {status}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔄 Watch mode is now {status}.")

    if WATCH_MODE_ENABLED:
        asyncio.create_task(fetch_and_forward_tweets(context))

# ✅ Main Function
async def main():
    await initialize_twitter()
    application = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("watchmode", watch_mode_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_tweet_url))
    async with application:
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
