import logging
import sqlite3
import json
import time
import os
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from collections import defaultdict
import asyncio

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import TelegramError, Conflict, NetworkError, TimedOut
from telegram.request import HTTPXRequest

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs to reduce noise
logging.getLogger("httpx").setLevel(logging.WARNING)

# Bot Configuration - Use environment variables for security
BOT_TOKEN = os.getenv("BOT_TOKEN", "7958891298:AAFfZgHD-Yb0NJaBrEY1w4Vzi-Ce6r2A8Pc")
GROUP_ID = int(os.getenv("GROUP_ID", "-1002627868792"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7374497477,7896869974").split(",")]

# Global variables for admin replies
admin_reply_targets: Dict[int, int] = {}

@dataclass
class UserSession:
    user_id: int
    username: str
    first_name: str
    active_since: str
    message_ids: List[int]
    conversation_data: Dict
    last_activity: str

class RateLimiter:
    """Rate limiting to prevent spam"""
    def __init__(self, max_messages=10, time_window=60):
        self.max_messages = max_messages
        self.time_window = time_window
        self.user_messages = defaultdict(list)
    
    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        user_times = self.user_messages[user_id]
        
        # Remove old timestamps
        user_times[:] = [t for t in user_times if now - t < self.time_window]
        
        if len(user_times) < self.max_messages:
            user_times.append(now)
            return True
        return False

class UserManager:
    """Manages user sessions and database operations"""
    def __init__(self):
        self.active_users: Dict[int, UserSession] = {}
        self.admin_reply_timeouts: Dict[int, float] = {}
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database for persistent user data"""
        self.conn = sqlite3.connect('bot_users.db', check_same_thread=False)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                session_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS message_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                chat_id INTEGER,
                message_type TEXT DEFAULT 'text',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                target_user_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()
    
    def get_user_session(self, user_id: int, user_data: dict) -> UserSession:
        """Get or create user session"""
        if user_id not in self.active_users:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.active_users[user_id] = UserSession(
                user_id=user_id,
                username=user_data.get('username', ''),
                first_name=user_data.get('first_name', 'User'),
                active_since=current_time,
                message_ids=[],
                conversation_data={},
                last_activity=current_time
            )
            
            # Store in database
            self.conn.execute(
                "INSERT OR REPLACE INTO user_sessions (user_id, username, first_name, session_data, last_activity) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_data.get('username', ''), user_data.get('first_name', 'User'), 
                 json.dumps({}), current_time)
            )
            self.conn.commit()
        else:
            # Update last activity
            self.active_users[user_id].last_activity = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        return self.active_users[user_id]
    
    def store_message_id(self, user_id: int, message_id: int, chat_id: int, message_type: str = 'text'):
        """Store message ID for later deletion"""
        if user_id in self.active_users:
            self.active_users[user_id].message_ids.append(message_id)
        
        # Store in database for persistence
        self.conn.execute(
            "INSERT INTO message_history (user_id, message_id, chat_id, message_type) VALUES (?, ?, ?, ?)",
            (user_id, message_id, chat_id, message_type)
        )
        self.conn.commit()
    
    def log_admin_action(self, admin_id: int, action: str, target_user_id: int = None):
        """Log admin actions for audit trail"""
        self.conn.execute(
            "INSERT INTO admin_logs (admin_id, action, target_user_id) VALUES (?, ?, ?)",
            (admin_id, action, target_user_id)
        )
        self.conn.commit()
    
    def get_active_users_count(self) -> int:
        """Get count of active users"""
        return len(self.active_users)
    
    def set_admin_reply_timeout(self, admin_id: int):
        """Set timeout for admin reply mode"""
        self.admin_reply_timeouts[admin_id] = time.time()
    
    def check_reply_timeouts(self, timeout_minutes: int = 30):
        """Check and clear expired reply modes"""
        current_time = time.time()
        expired_admins = []
        
        for admin_id, start_time in self.admin_reply_timeouts.items():
            if current_time - start_time > (timeout_minutes * 60):
                expired_admins.append(admin_id)
        
        for admin_id in expired_admins:
            if admin_id in admin_reply_targets:
                del admin_reply_targets[admin_id]
            del self.admin_reply_timeouts[admin_id]
        
        return len(expired_admins)
    
    def cleanup_inactive_users(self, hours_threshold: int = 24):
        """Remove inactive users from memory (but keep in database)"""
        current_time = datetime.now()
        inactive_users = []
        
        for user_id, session in self.active_users.items():
            last_activity = datetime.strptime(session.last_activity, "%Y-%m-%d %H:%M:%S")
            if (current_time - last_activity).total_seconds() > (hours_threshold * 3600):
                inactive_users.append(user_id)
        
        for user_id in inactive_users:
            del self.active_users[user_id]
        
        logger.info(f"Cleaned up {len(inactive_users)} inactive users")
        return len(inactive_users)

# Initialize components
user_manager = UserManager()
rate_limiter = RateLimiter()

def is_admin(user_id: int) -> bool:
    """Check if user is an admin"""
    return user_id in ADMIN_IDS

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user = update.effective_user
    
    if update.effective_chat.id == GROUP_ID:
        # Admin start in group
        if is_admin(user.id):
            await update.message.reply_text(
                "🤖 **Bot is active and ready!**\n\n"
                f"👥 Currently {user_manager.get_active_users_count()} active users\n"
                f"👨‍💼 Active reply sessions: {len(admin_reply_targets)}\n"
                "Use /admin for admin commands.",
                parse_mode='Markdown'
            )
        return
    
    # User start in private chat
    welcome_message = (
        f"👋 **Hello {user.first_name}!**\n\n"
        "🔒 **Security Notice:**\n"
        "• For maximum privacy, consider using Secret Chat\n"
        "• Avoid sharing sensitive information in regular chats\n"
        "• Your messages are forwarded to our support team\n\n"
        "📝 Send me your message and we'll get back to you soon!\n\n"
        "💡 **Tip:** You can send text, photos, documents, and voice messages."
    )
    
    sent_msg = await update.message.reply_text(welcome_message, parse_mode='Markdown')
    user_manager.store_message_id(user.id, sent_msg.message_id, update.effective_chat.id)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin command"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use admin commands.")
        return
    
    active_count = user_manager.get_active_users_count()
    reply_sessions = len(admin_reply_targets)
    
    admin_panel = (
        "🛠️ **Admin Panel**\n\n"
        f"👥 Active Users: {active_count}\n"
        f"💬 Active Reply Sessions: {reply_sessions}\n"
        f"📊 Total Admins: {len(ADMIN_IDS)}\n"
        f"🕐 Current Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
        "**Available Commands:**\n"
        "/users - List active users\n"
        "/cleanup - Remove inactive users\n"
        "/stats - Show detailed statistics\n"
        "/broadcast <message> - Broadcast to all users\n"
        "/cancel - End current reply session\n"
        "/active_replies - Show active reply sessions"
    )
    
    await update.message.reply_text(admin_panel, parse_mode='Markdown')
    user_manager.log_admin_action(user.id, "accessed_admin_panel")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active users"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not user_manager.active_users:
        await update.message.reply_text("📝 No active users at the moment.")
        return
    
    users_list = "👥 **Active Users:**\n\n"
    for user_id, session in user_manager.active_users.items():
        reply_status = "🔄 In Reply" if user_id in admin_reply_targets.values() else "💤 Idle"
        users_list += (
            f"👤 {session.first_name} (@{session.username or 'no_username'})\n"
            f"🆔 ID: `{user_id}`\n"
            f"🕐 Since: {session.active_since}\n"
            f"📝 Messages: {len(session.message_ids)}\n"
            f"📊 Status: {reply_status}\n"
            "─────────────\n"
        )
    
    await update.message.reply_text(users_list, parse_mode='Markdown')
    user_manager.log_admin_action(user.id, "listed_users")

async def active_replies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active reply sessions"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not admin_reply_targets:
        await update.message.reply_text("💤 No active reply sessions.")
        return
    
    replies_list = "💬 **Active Reply Sessions:**\n\n"
    for admin_id, target_user_id in admin_reply_targets.items():
        try:
            admin_user = await context.bot.get_chat(admin_id)
            target_session = user_manager.active_users.get(target_user_id)
            target_name = target_session.first_name if target_session else "Unknown"
            
            replies_list += (
                f"👨‍💼 Admin: {admin_user.first_name}\n"
                f"👤 Replying to: {target_name} (`{target_user_id}`)\n"
                "─────────────\n"
            )
        except TelegramError:
            replies_list += (
                f"👨‍💼 Admin: Unknown (`{admin_id}`)\n"
                f"👤 Replying to: Unknown (`{target_user_id}`)\n"
                "─────────────\n"
            )
    
    await update.message.reply_text(replies_list, parse_mode='Markdown')

async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleanup inactive users"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    before_count = user_manager.get_active_users_count()
    cleaned_users = user_manager.cleanup_inactive_users()
    after_count = user_manager.get_active_users_count()
    
    # Also cleanup expired reply sessions
    expired_replies = user_manager.check_reply_timeouts(30)
    
    await update.message.reply_text(
        f"🧹 **Cleanup completed!**\n\n"
        f"👥 Removed {cleaned_users} inactive users\n"
        f"💬 Cleared {expired_replies} expired reply sessions\n"
        f"📊 Active users now: {after_count}",
        parse_mode='Markdown'
    )
    user_manager.log_admin_action(user.id, "cleanup_users")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages from users"""
    try:
        user = update.effective_user
        message = update.message
        
        # Rate limiting check
        if not rate_limiter.is_allowed(user.id):
            sent_msg = await message.reply_text(
                "⚠️ You're sending messages too quickly. Please wait a moment before trying again."
            )
            user_manager.store_message_id(user.id, sent_msg.message_id, update.effective_chat.id)
            return
        
        # Get user-specific session
        user_session = user_manager.get_user_session(user.id, {
            'username': user.username,
            'first_name': user.first_name
        })
        
        logger.info(f"Message from user {user.id} ({user.first_name}) - Active users: {len(user_manager.active_users)}")

        # Handle admin replies in group
        if update.effective_chat.id == GROUP_ID:
            if is_admin(user.id) and user.id in admin_reply_targets:
                await handle_admin_reply(update, context, user_session)
            return

        # Prevent admin replies in private
        if is_admin(user.id) and user.id in admin_reply_targets:
            sent_msg = await message.reply_text(
                "❌ **Please reply from the group chat, not in private.**\n\n"
                "Go to the group and send your reply there.\n"
                "Use /cancel to end reply mode.",
                parse_mode='Markdown'
            )
            user_manager.store_message_id(user.id, sent_msg.message_id, update.effective_chat.id)
            return

        # Store user conversation data
        user_session.conversation_data['last_message'] = {
            'text': message.text or message.caption or '[Media]',
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'message_id': message.message_id,
            'type': 'text' if message.text else 'media'
        }

        # Enhanced user info for admins
        reply_status = "🔄 Currently in conversation" if user.id in admin_reply_targets.values() else "💤 Awaiting reply"
        
        user_info = (
            f"👤 **User:** {user.first_name} (@{user.username or 'No username'})\n"
            f"🆔 **ID:** `{user.id}`\n"
            f"⏱️ **Time:** {datetime.now().strftime('%H:%M:%S')}\n"
            f"📊 **Active Users:** {len(user_manager.active_users)}\n"
            f"🕐 **Session Since:** {user_session.active_since}\n"
            f"💬 **Total Messages:** {len(user_session.message_ids)}\n"
            f"📋 **Status:** {reply_status}"
        )
        
        # Create reply keyboard for admins
        reply_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user.id}"),
                InlineKeyboardButton("🗑️ Delete Chat", callback_data=f"delete_{user.id}")
            ],
            [
                InlineKeyboardButton("📊 User Info", callback_data=f"info_{user.id}"),
                InlineKeyboardButton("🚫 Block User", callback_data=f"block_{user.id}")
            ]
        ])

        # Send to group with enhanced info
        try:
            if message.text:
                notification_text = f"📩 **New Text Message**\n\n{user_info}\n\n**Message:**\n{message.text}"
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=notification_text,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            elif message.photo:
                caption = f"📸 **New Photo**\n\n{user_info}"
                if message.caption:
                    caption += f"\n\n**Caption:**\n{message.caption}"
                
                await context.bot.send_photo(
                    chat_id=GROUP_ID,
                    photo=message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            elif message.document:
                caption = f"📎 **New Document**\n\n{user_info}"
                if message.caption:
                    caption += f"\n\n**Caption:**\n{message.caption}"
                
                await context.bot.send_document(
                    chat_id=GROUP_ID,
                    document=message.document.file_id,
                    caption=caption,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            elif message.voice:
                caption = f"🎤 **New Voice Message**\n\n{user_info}"
                await context.bot.send_voice(
                    chat_id=GROUP_ID,
                    voice=message.voice.file_id,
                    caption=caption,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            elif message.video:
                caption = f"🎥 **New Video**\n\n{user_info}"
                if message.caption:
                    caption += f"\n\n**Caption:**\n{message.caption}"
                
                await context.bot.send_video(
                    chat_id=GROUP_ID,
                    video=message.video.file_id,
                    caption=caption,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            elif message.audio:
                caption = f"🎵 **New Audio**\n\n{user_info}"
                if message.caption:
                    caption += f"\n\n**Caption:**\n{message.caption}"
                
                await context.bot.send_audio(
                    chat_id=GROUP_ID,
                    audio=message.audio.file_id,
                    caption=caption,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
            
            else:
                notification_text = f"📩 **New Message**\n\n{user_info}\n\n**Content:** [Unsupported message type]"
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=notification_text,
                    reply_markup=reply_keyboard,
                    parse_mode='Markdown'
                )
                
        except TelegramError as e:
            logger.error(f"Failed to send to group: {e}")

        # Send confirmation to user with security notice
        confirmation_message = (
            "✅ **Message Received!**\n\n"
            "Your message has been forwarded to our support team. "
            "We'll get back to you as soon as possible.\n\n"
            "🔒 **Security Tip:** For sensitive information, consider using Telegram's Secret Chat feature."
        )
        
        sent_msg = await message.reply_text(confirmation_message, parse_mode='Markdown')
        user_manager.store_message_id(user.id, sent_msg.message_id, update.effective_chat.id)

    except Exception as e:
        logger.error(f"Error in handle_user_message: {e}")
        try:
            await update.message.reply_text(
                "⚠️ An error occurred while processing your message. Please try again."
            )
        except:
            pass

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession) -> None:
    """Handle admin replies to users"""
    try:
        admin = update.effective_user
        target_user_id = admin_reply_targets.get(admin.id)
        
        if not target_user_id:
            return
        
        message = update.message
        
        # Send reply to user
        try:
            if message.text:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"💬 **Support Team:**\n\n{message.text}",
                    parse_mode='Markdown'
                )
            elif message.photo:
                await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=message.photo[-1].file_id,
                    caption=f"💬 **Support Team:**\n\n{message.caption or ''}",
                    parse_mode='Markdown'
                )
            elif message.document:
                await context.bot.send_document(
                    chat_id=target_user_id,
                    document=message.document.file_id,
                    caption=f"💬 **Support Team:**\n\n{message.caption or ''}",
                    parse_mode='Markdown'
                )
            elif message.voice:
                await context.bot.send_voice(
                    chat_id=target_user_id,
                    voice=message.voice.file_id,
                    caption=f"💬 **Support Team:**",
                    parse_mode='Markdown'
                )
            elif message.video:
                await context.bot.send_video(
                    chat_id=target_user_id,
                    video=message.video.file_id,
                    caption=f"💬 **Support Team:**\n\n{message.caption or ''}",
                    parse_mode='Markdown'
                )
            elif message.audio:
                await context.bot.send_audio(
                    chat_id=target_user_id,
                    audio=message.audio.file_id,
                    caption=f"💬 **Support Team:**\n\n{message.caption or ''}",
                    parse_mode='Markdown'
                )
            
            # Create inline keyboard for continued conversation
            continue_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Continue Reply", callback_data=f"continue_{target_user_id}"),
                    InlineKeyboardButton("🔚 End Reply", callback_data=f"end_reply_{target_user_id}")
                ],
                [
                    InlineKeyboardButton("🗑️ Delete Chat", callback_data=f"delete_{target_user_id}"),
                    InlineKeyboardButton("📊 User Info", callback_data=f"info_{target_user_id}")
                ]
            ])
            
            user_name = user_session.first_name if user_session else "Unknown User"
            
            # Confirm to admin with options
            await message.reply_text(
                f"✅ **Reply sent to {user_name}!**\n\n"
                f"🔄 **Reply mode is still active**\n"
                f"Send another message to continue the conversation, or use the buttons below:",
                reply_markup=continue_keyboard,
                parse_mode='Markdown'
            )
            
            # Log the action
            user_manager.log_admin_action(admin.id, "replied_to_user", target_user_id)
            
        except TelegramError as e:
            await message.reply_text(f"❌ Failed to send reply: {e}")
            # Clear reply target if message fails
            if admin.id in admin_reply_targets:
                del admin_reply_targets[admin.id]
        
    except Exception as e:
        logger.error(f"Error in handle_admin_reply: {e}")
        # Clear reply target on error
        if admin.id in admin_reply_targets:
            del admin_reply_targets[admin.id]

async def delete_user_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Delete entire conversation with a user"""
    try:
        admin = update.callback_query.from_user
        
        # Get stored message IDs from database
        cursor = user_manager.conn.execute(
            "SELECT message_id, chat_id FROM message_history WHERE user_id = ? ORDER BY timestamp DESC",
            (user_id,)
        )
        messages = cursor.fetchall()
        
        deleted_count = 0
        failed_count = 0
        
        for message_id, chat_id in messages:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted_count += 1
                await asyncio.sleep(0.1)  # Rate limiting
            except TelegramError as e:
                failed_count += 1
                logger.warning(f"Could not delete message {message_id}: {e}")
        
        # Clear from database
        user_manager.conn.execute(
            "DELETE FROM message_history WHERE user_id = ?", (user_id,)
        )
        user_manager.conn.commit()
        
        # Remove from active sessions
        user_name = "Unknown"
        if user_id in user_manager.active_users:
            user_name = user_manager.active_users[user_id].first_name
            del user_manager.active_users[user_id]
        
        # Clear admin reply target if exists
        admin_to_clear = None
        for admin_id, target_id in admin_reply_targets.items():
            if target_id == user_id:
                admin_to_clear = admin_id
                break
        
        if admin_to_clear:
            del admin_reply_targets[admin_to_clear]
            if admin_to_clear in user_manager.admin_reply_timeouts:
                del user_manager.admin_reply_timeouts[admin_to_clear]
        
        await update.callback_query.edit_message_text(
            f"🗑️ **Chat Deleted Successfully!**\n\n"
            f"👤 User: {user_name}\n"
            f"✅ Deleted: {deleted_count} bot messages\n"
            f"❌ Failed: {failed_count} messages\n\n"
            f"📝 Note: User messages cannot be deleted by the bot.\n"
            f"The user has been notified that the conversation has ended.",
            parse_mode='Markdown'
        )
        
        # Notify user that conversation has ended
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🔚 **Conversation Ended**\n\n"
                    "This conversation has been ended by our support team. "
                    "Thank you for contacting us!\n\n"
                    "If you need further assistance, feel free to send a new message."
                ),
                parse_mode='Markdown'
            )
        except TelegramError:
            pass  # User may have blocked the bot

        # Log the action
        user_manager.log_admin_action(admin.id, "deleted_user_chat", user_id)

    except Exception as e:
        logger.error(f"Error deleting chat for user {user_id}: {e}")
        await update.callback_query.answer("❌ Error deleting chat", show_alert=True)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks"""
    try:
        query = update.callback_query
        admin = query.from_user
        
        if not is_admin(admin.id):
            await query.answer("❌ You are not authorized to perform this action.")
            return
        
        await query.answer()

        if query.data.startswith("reply_"):
            user_id = int(query.data.split("_")[1])
            admin_reply_targets[admin.id] = user_id
            user_manager.set_admin_reply_timeout(admin.id)
            
            user_session = user_manager.active_users.get(user_id)
            user_info = f"Replying to: {user_session.first_name if user_session else 'Unknown'} (ID: {user_id})"
            
            # Create keyboard for reply management
            reply_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔚 End Reply Mode", callback_data=f"end_reply_{user_id}"),
                    InlineKeyboardButton("📊 User Info", callback_data=f"info_{user_id}")
                ]
            ])
            
            await query.edit_message_text(
                f"💬 **Reply Mode Activated**\n\n"
                f"👤 {user_info}\n\n"
                f"📝 **Send your messages** - Each message will be forwarded to the user.\n"
                f"🔄 **Reply mode stays active** until you end it.\n\n"
                f"Use the button below or send /cancel to end reply mode.",
                reply_markup=reply_keyboard,
                parse_mode='Markdown'
            )
            
        elif query.data.startswith("continue_"):
            user_id = int(query.data.split("_")[1])
            user_session = user_manager.active_users.get(user_id)
            user_name = user_session.first_name if user_session else "Unknown"
            
            # Create management keyboard
            manage_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔚 End Reply Mode", callback_data=f"end_reply_{user_id}"),
                    InlineKeyboardButton("📊 User Info", callback_data=f"info_{user_id}")
                ]
            ])
            
            await query.edit_message_text(
                f"🔄 **Continuing conversation with {user_name}**\n\n"
                f"Send your next message to continue the conversation.",
                reply_markup=manage_keyboard,
                parse_mode='Markdown'
            )
            
        elif query.data.startswith("end_reply_"):
            user_id = int(query.data.split("_")[2])
            
            # Clear the reply target
            if admin.id in admin_reply_targets:
                del admin_reply_targets[admin.id]
            if admin.id in user_manager.admin_reply_timeouts:
                del user_manager.admin_reply_timeouts[admin.id]
            
            user_session = user_manager.active_users.get(user_id)
            user_name = user_session.first_name if user_session else "Unknown"
            
            await query.edit_message_text(
                f"🔚 **Reply mode ended**\n\n"
                f"No longer replying to {user_name}.\n"
                f"Click 'Reply' on any user message to start a new conversation.",
                parse_mode='Markdown'
            )
            
            # Log the action
            user_manager.log_admin_action(admin.id, "ended_reply_mode", user_id)
            
        elif query.data.startswith("delete_"):
            user_id = int(query.data.split("_")[1])
            
            # Confirmation keyboard
            confirm_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{user_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete")
                ]
            ])
            
            user_session = user_manager.active_users.get(user_id)
            user_name = user_session.first_name if user_session else "Unknown"
            
            await query.edit_message_text(
                f"⚠️ **Confirm Chat Deletion**\n\n"
                f"👤 User: {user_name}\n"
                f"🆔 ID: {user_id}\n\n"
                f"This will delete all bot messages in the conversation with this user.\n"
                f"Are you sure?",
                reply_markup=confirm_keyboard,
                parse_mode='Markdown'
            )
            
        elif query.data.startswith("confirm_delete_"):
            user_id = int(query.data.split("_")[2])
            await delete_user_chat(update, context, user_id)
            
        elif query.data == "cancel_delete":
            await query.edit_message_text("❌ Chat deletion cancelled.")
            
        elif query.data.startswith("info_"):
            user_id = int(query.data.split("_")[1])
            user_session = user_manager.active_users.get(user_id)
            
            if user_session:
                # Check if user is in active reply
                in_reply = any(target_id == user_id for target_id in admin_reply_targets.values())
                reply_admin = None
                if in_reply:
                    for admin_id, target_id in admin_reply_targets.items():
                        if target_id == user_id:
                            try:
                                reply_admin_user = await context.bot.get_chat(admin_id)
                                reply_admin = reply_admin_user.first_name
                            except:
                                reply_admin = "Unknown"
                            break
                
                info_text = (
                    f"👤 **User Information**\n\n"
                    f"**Name:** {user_session.first_name}\n"
                    f"**Username:** @{user_session.username or 'None'}\n"
                    f"**ID:** `{user_session.user_id}`\n"
                    f"**Active Since:** {user_session.active_since}\n"
                    f"**Last Activity:** {user_session.last_activity}\n"
                    f"**Messages Sent:** {len(user_session.message_ids)}\n"
                    f"**Conversation Status:** {'🔄 In active reply' if in_reply else '💤 Waiting'}\n"
                )
                
                if in_reply and reply_admin:
                    info_text += f"**Replying Admin:** {reply_admin}\n"
                    
                info_text += f"**Session Data:** {len(user_session.conversation_data)} entries"
            else:
                info_text = f"❌ No session data found for user {user_id}"
            
            await query.edit_message_text(info_text, parse_mode='Markdown')
            
        elif query.data.startswith("block_"):
            user_id = int(query.data.split("_")[1])
            # This is a placeholder - implement your blocking logic here
            await query.edit_message_text(
                f"🚫 **User Blocking**\n\n"
                f"Blocking functionality not implemented yet.\n"
                f"User ID: {user_id}\n\n"
                f"You can manually delete the chat and ignore future messages.",
                parse_mode='Markdown'
            )

    except Exception as e:
        logger.error(f"Error in handle_callback: {e}")
        await query.answer("❌ An error occurred", show_alert=True)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current admin operation"""
    user = update.effective_user
    
    if not is_admin(user.id):
        return
    
    if user.id in admin_reply_targets:
        target_user_id = admin_reply_targets[user.id]
        user_session = user_manager.active_users.get(target_user_id)
        user_name = user_session.first_name if user_session else "Unknown"
        
        del admin_reply_targets[user.id]
        if user.id in user_manager.admin_reply_timeouts:
            del user_manager.admin_reply_timeouts[user.id]
        
        await update.message.reply_text(
            f"❌ **Reply mode cancelled**\n\n"
            f"No longer replying to {user_name}.\n"
            f"Use /admin to access the admin panel.",
            parse_mode='Markdown'
        )
        
        # Log the action
        user_manager.log_admin_action(user.id, "cancelled_reply_mode", target_user_id)
    else:
        await update.message.reply_text(
            "ℹ️ No active reply mode to cancel.\n"
            "Use /admin to access the admin panel."
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show detailed bot statistics"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    # Get database statistics
    cursor = user_manager.conn.execute("SELECT COUNT(*) FROM user_sessions")
    total_users = cursor.fetchone()[0]
    
    cursor = user_manager.conn.execute("SELECT COUNT(*) FROM message_history")
    total_messages = cursor.fetchone()[0]
    
    cursor = user_manager.conn.execute("SELECT COUNT(*) FROM admin_logs")
    total_admin_actions = cursor.fetchone()[0]
    
    stats_text = (
        f"📊 **Bot Statistics**\n\n"
        f"👥 **Active Users:** {user_manager.get_active_users_count()}\n"
        f"📝 **Total Users (All Time):** {total_users}\n"
        f"💬 **Total Messages:** {total_messages}\n"
        f"🛠️ **Admin Actions:** {total_admin_actions}\n"
        f"👨‍💼 **Active Reply Sessions:** {len(admin_reply_targets)}\n\n"
        f"🕐 **Current Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all active users"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📢 **Broadcast Usage:**\n\n"
            "/broadcast <message>\n\n"
            "This will send your message to all active users.",
            parse_mode='Markdown'
        )
        return
    
    message_text = " ".join(context.args)
    broadcast_message = f"📢 **Announcement:**\n\n{message_text}"
    
    success_count = 0
    failed_count = 0
    
    for user_id in user_manager.active_users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=broadcast_message,
                parse_mode='Markdown'
            )
            success_count += 1
            await asyncio.sleep(0.1)  # Rate limiting
        except TelegramError:
            failed_count += 1
    
    await update.message.reply_text(
        f"📢 **Broadcast Complete!**\n\n"
        f"✅ Sent: {success_count}\n"
        f"❌ Failed: {failed_count}",
        parse_mode='Markdown'
    )
    
    user_manager.log_admin_action(user.id, f"broadcast_message", None)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enhanced error handler with better conflict handling"""
    error = context.error
    
    # Handle specific error types
    if isinstance(error, Conflict):
        logger.warning("Conflict error detected: %s", error)
        # Don't restart on conflict - let it resolve naturally
        return
    elif isinstance(error, (NetworkError, TimedOut)):
        logger.warning("Network/Timeout error: %s", error)
        # These will be retried automatically by the application
        return
    else:
        logger.error('Update "%s" caused error "%s"', update, error)

async def set_bot_commands(application: Application) -> None:
    """Set bot commands for better UX"""
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("admin", "Admin panel (admins only)"),
        BotCommand("users", "List active users (admins only)"),
        BotCommand("stats", "Show bot statistics (admins only)"),
        BotCommand("cleanup", "Clean inactive users (admins only)"),
        BotCommand("broadcast", "Broadcast message (admins only)"),
        BotCommand("cancel", "Cancel current operation (admins only)"),
        BotCommand("active_replies", "Show active reply sessions (admins only)"),
    ]
    await application.bot.set_my_commands(commands)

async def periodic_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """Periodic cleanup task"""
    try:
        cleaned_users = user_manager.cleanup_inactive_users()
        expired_replies = user_manager.check_reply_timeouts(30)
        
        if cleaned_users > 0 or expired_replies > 0:
            logger.info(f"Periodic cleanup: {cleaned_users} users, {expired_replies} reply sessions")
    except Exception as e:
        logger.error(f"Error in periodic cleanup: {e}")

def main() -> None:
    """Start the bot with enhanced error handling"""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please set your BOT_TOKEN!")
        return
    
    if GROUP_ID == -1001234567890:
        logger.error("Please set your GROUP_ID!")
        return
    
    if not ADMIN_IDS or ADMIN_IDS == [123456789, 987654321]:
        logger.error("Please set your ADMIN_IDS!")
        return
    
    # Enhanced HTTP request configuration
    request = HTTPXRequest(
        http_version="1.1",
        read_timeout=60,  # Increased timeout
        write_timeout=60,
        connect_timeout=30,
        pool_timeout=10
    )
    
    # Create application with enhanced configuration
    application = Application.builder()\
        .token(BOT_TOKEN)\
        .request(request)\
        .get_updates_request(request)\
        .concurrent_updates(True)\
        .build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("users", list_users_command))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("active_replies", active_replies_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Schedule periodic tasks
    application.job_queue.run_once(
        lambda context: asyncio.create_task(set_bot_commands(application)), 
        when=1
    )
    
    # Cleanup inactive users every hour
    application.job_queue.run_repeating(periodic_cleanup, interval=3600)
    
    # Check for expired reply sessions every 30 minutes
    application.job_queue.run_repeating(
        lambda context: user_manager.check_reply_timeouts(30), 
        interval=1800
    )
    
    logger.info("Bot started successfully! Ready to handle messages.")
    
    # Enhanced polling configuration with automatic retry
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,  # Drop pending updates on restart
            close_loop=False
        )
    except Conflict as e:
        logger.error("Conflict error during startup: %s", e)
        logger.info("Another bot instance may be running. Please stop it first.")
    except Exception as e:
        logger.error("Unexpected error during startup: %s", e)

if __name__ == '__main__':
    main()
