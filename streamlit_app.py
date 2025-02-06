import streamlit as st
import os
import asyncio
from main import start_bot  # Import your Telegram bot function

st.title("🚀 Telegram Bot Control Panel")

if st.button("Start Bot"):
    st.write("Bot is starting... ✅")
    asyncio.run(start_bot())  # Start bot manually

if st.button("Stop Bot"):
    st.write("❌ Bot stopping is not supported on Streamlit.")

st.write("ℹ️ This Streamlit app only starts the bot. It may stop if the app shuts down.")
