import logging
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler
)
from telegram.error import Conflict, TelegramError

# --- Configuration ---
BOT_TOKEN = "8053841837:AAH5lDzkh4Y7SCptEFP-k1YsovxCJsDr74c"
OWNER_ID = 6814396510
ADMIN_IDS = [7110958447] #Chethiya Dialog
ADMIN_IDS = [7379431450] #Chethiya mobitel

GROUP_ID = -1002686597510  # <-- Your group ID is hardcoded here

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Store conversations
conversations = {}

# --- Helper Functions ---
def is_admin(user_id):
    return user_id == OWNER_ID or user_id in ADMIN_IDS

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        "Send me your message and our team will reply soon."
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        message = update.message

        # Ignore messages sent in the group itself
        if update.effective_chat.id == GROUP_ID:
            # If admin is replying in the group, handle reply
            if is_admin(user.id) and "replying_to" in context.chat_data:
                user_id = context.chat_data["replying_to"]
                reply_text = message.text or (message.caption if message.caption else None)
                sent = False

                # Send text reply
                if reply_text:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"💌 Reply from admin ({user.full_name}):\n\n{reply_text}"
                    )
                    sent = True

                # Send photo if present
                if message.photo:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=message.photo[-1].file_id,
                        caption=f"📷 Photo from admin ({user.full_name})"
                    )
                    sent = True

                if sent:
                    await message.reply_text("✅ Reply sent to user.")
                    await context.bot.send_message(
                        chat_id=GROUP_ID,
                        text=f"📤 Admin {user.full_name} replied to user {user_id}."
                    )
                else:
                    await message.reply_text("⚠️ Please send a text or photo as a reply.")

                del context.chat_data["replying_to"]
            return

        # If admin is replying in private chat (not supported)
        if is_admin(user.id) and "replying_to" in context.user_data:
            await message.reply_text("❌ Please reply from the group, not in private chat.")
            return

        # Regular user message - store the conversation
        conversations[user.id] = {
            "last_message": message,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        user_info = f"👤 User: {user.full_name}\n🆔 ID: {user.id}\n⏱️ Time: {datetime.datetime.now().strftime('%H:%M:%S')}"
        reply_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user.id}")]])

        # Forward to the group
        try:
            if message.text:
                notification_text = f"📩 New message\n\n{user_info}\n\n{message.text}"
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=notification_text,
                    reply_markup=reply_keyboard
                )
            elif message.photo:
                caption = f"📷 New photo\n\n{user_info}"
                if message.caption:
                    caption += f"\n\n{message.caption}"
                await context.bot.send_photo(
                    chat_id=GROUP_ID,
                    photo=message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=reply_keyboard
                )
            else:
                notification_text = f"📦 New message (unsupported format)\n\n{user_info}"
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=notification_text,
                    reply_markup=reply_keyboard
                )
        except TelegramError as e:
            logger.error(f"Failed to send to group: {e}")
            for admin_id in [OWNER_ID] + ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"⚠️ Failed to send to group. Message from {user.full_name}:\n\n{message.text or '[Media]'}"
                    )
                except:
                    pass

        await message.reply_text("✅ Thank you! Your message has been received. We'll reply soon.")

    except Exception as e:
        logger.error(f"Error in handle_user_message: {e}")
        await update.message.reply_text("⚠️ An error occurred. Please try again.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        admin = query.from_user
        if not is_admin(admin.id):
            await query.answer("You are not authorized to perform this action.")
            return
        await query.answer()

        if query.data.startswith("reply_"):
            user_id = int(query.data.split("_")[1])
            context.chat_data["replying_to"] = user_id

            user_info = ""
            if user_id in conversations:
                last_message_time = conversations[user_id].get("timestamp", "unknown time")
                user_info = f"\n\nReplying to user ID: {user_id}\nLast message at: {last_message_time}"

            # Check if original message is text or photo with caption
            if query.message.text:
                # Original message is text
                base_text = query.message.text
                await query.edit_message_text(
                    base_text + f"{user_info}\n\n💬 Please type your reply (text or photo) in the group."
                )
            elif query.message.photo:
                # Original message is a photo (possibly with caption)
                base_caption = query.message.caption or ""
                await query.edit_message_caption(
                    caption=base_caption + f"{user_info}\n\n💬 Please type your reply (text or photo) in the group."
                )
            else:
                # Neither text nor photo found, fallback
                await query.answer("Cannot edit this message type.", show_alert=True)

    except Exception as e:
        logger.error(f"Error in handle_callback: {e}")




async def handle_admin_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        admin = update.effective_user
        if not is_admin(admin.id):
            await update.message.reply_text("❌ Unauthorized access.")
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /reply <user_id> <message>")
            return
        user_id = int(context.args[0])
        reply_text = " ".join(context.args[1:])
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"💌 Reply from admin ({admin.full_name}):\n\n{reply_text}"
            )
            await update.message.reply_text("✅ Reply sent successfully.")
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"📤 Admin {admin.full_name} replied to user {user_id} via command."
            )
        except TelegramError as e:
            logger.error(f"Failed to send reply to user {user_id}: {e}")
            await update.message.reply_text("⚠️ Failed to send reply. Please try again.")
    except Exception as e:
        logger.error(f"Error in handle_admin_reply_command: {e}")
        await update.message.reply_text("⚠️ Failed to send reply. Please try again.")

# --- Error Handling ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error: {context.error}")
    if update and hasattr(update, 'message') and update.message:
        try:
            await update.message.reply_text("⚠️ An error occurred. Please try again later.")
        except:
            pass

# --- Main Application ---
def main() -> None:
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("reply", handle_admin_reply_command))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_user_message))
        application.add_error_handler(error_handler)
        logger.info("Starting bot...")
        application.run_polling(drop_pending_updates=True)
    except Conflict:
        logger.error("Another bot instance is already running. Exiting.")
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")

if __name__ == '__main__':
    main()
