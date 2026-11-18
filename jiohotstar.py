import logging
import os
import time
import asyncio
import re
import yt_dlp
import shutil
import html
import subprocess
from datetime import datetime
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

logger = logging.getLogger(__name__)

# ജിയോ ഹോസ്റ്റാർ പ്രീമിയം ആക്സസ് ഉള്ള യൂസർമാരുടെ IDs ഉം ആക്ടീവ് ടാസ്കുകളും
JIO_PREMIUM_USERS = set()
ACTIVE_JIO_TASKS = set()

USER_URLS = {}
USER_THUMBNAILS = {}
CANCEL_REQUESTS = set()

def get_video_duration(file_path):
    try:
        parser = createParser(file_path)
        if parser:
            metadata = extractMetadata(parser)
            if metadata and metadata.has("duration"):
                return int(metadata.get("duration").seconds)
    except Exception:
        pass
    return 0

def download_youtube_thumbnail(thumbnail_url, user_id):
    os.makedirs("thumbnails", exist_ok=True)
    thumb_path = f"thumbnails/yt_{user_id}.jpg"
    try:
        import requests
        response = requests.get(thumbnail_url, timeout=10)
        if response.status_code == 200:
            with open(thumb_path, "wb") as f:
                f.write(response.content)
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                converted_thumb = f"thumbnails/thumb_{user_id}.jpg"
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", thumb_path, "-vf",
                     "scale=320:320:force_original_aspect_ratio=decrease",
                     "-q:v", "5", converted_thumb],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                if result.returncode == 0 and os.path.exists(converted_thumb):
                    return converted_thumb
                elif os.path.exists(thumb_path):
                    return thumb_path
    except Exception as e:
        logger.error(f"Error downloading thumbnail: {e}")
    return None

def generate_thumbnail(video_path, user_id):
    os.makedirs("thumbnails", exist_ok=True)
    thumb_path = f"thumbnails/auto_{user_id}.jpg"
    try:
        cmd = [
            "ffmpeg", "-ss", "00:00:05", "-i", video_path,
            "-vframes", "1", "-q:v", "2", thumb_path, "-y"
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
    return None

def build_hotstar_output_name(original_filename, custom_name, quality, audio_tag):
    """Keep the extractor's original base name unless -n supplies a replacement.

    The quality/source/audio suffix is always retained, e.g.
    Ee Puzhayum Kadannu.1440p.HS.WEB-DL.MAL.mp4
    """
    directory = os.path.dirname(original_filename) or "downloads"
    extension = os.path.splitext(original_filename)[1] or ".mp4"

    if custom_name:
        base = os.path.basename(custom_name.strip())
        # Avoid adding a second extension when the user includes one in -n.
        if os.path.splitext(base)[1].lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            base = os.path.splitext(base)[0]
        # Preserve spaces; remove only characters unsafe in filenames.
        base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip().strip(".")
    else:
        base = os.path.splitext(os.path.basename(original_filename))[0]

    if not base:
        base = "OTT_Video"

    suffix = f".{quality}p.HS.WEB-DL.{audio_tag}"
    # Prevent duplicate suffixes if a source filename already contains it.
    if base.lower().endswith(suffix.lower()):
        final_base = base
    else:
        final_base = base + suffix
    return os.path.join(directory, final_base + extension)


def get_progress_bar(percentage):
    try:
        pct = float(percentage.replace('%', '').strip())
    except:
        pct = 0.0
    completed = int(pct // 10)
    remaining = 10 - completed
    return "█" * completed + "░" * remaining

def get_progress_hook(client, message, user_id, title="Downloading"):
    last_update_time = 0
    def progress_hook(d):
        nonlocal last_update_time
        if user_id in CANCEL_REQUESTS:
            raise Exception("Task cancelled by user.")
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time > 3:
                percentage = d.get('_percent_str', '0%').strip()
                speed = d.get('_speed_str', 'N/A').strip()
                eta = d.get('_eta_str', 'N/A').strip()
                bar = get_progress_bar(percentage)
                cancel_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✖️ Cancel Task", callback_data=f"cancel_dl_{user_id}")]
                ])
                try:
                    asyncio.run_coroutine_threadsafe(
                        message.edit_text(
                            f"📥 {title}...\n\n"
                            f"[{bar}] {percentage}\n"
                            f"🚀 Speed: {speed}\n"
                            f"⏱️ ETA: {eta}",
                            reply_markup=cancel_keyboard
                        ),
                        client.loop
                    )
                except Exception:
                    pass
                last_update_time = current_time
    return progress_hook

def register_jiohotstar_handlers(app: Client, premium_check=None, admin_ids=None):
    """Register JioHotstar handlers with the bot's shared subscription system."""
    premium_check = premium_check or (lambda _user_id: False)
    admin_ids = set(admin_ids or [])

    def is_admin(user_id):
        return user_id in admin_ids

    def has_jio_access(user_id):
        # Jio-only grants are retained for admin-managed access; normal plans
        # are checked through the main bot's shared premium system.
        try:
            return is_admin(user_id) or user_id in JIO_PREMIUM_USERS or bool(premium_check(user_id))
        except Exception:
            logger.exception("Could not verify premium status for user %s", user_id)
            return False

    def task_limit_reached(user_id):
        return not is_admin(user_id) and user_id in ACTIVE_JIO_TASKS

    def reserve_task(user_id):
        # Admin/setup accounts are unlimited; regular premium accounts get one
        # active JioHotstar task at a time.
        if not is_admin(user_id):
            ACTIVE_JIO_TASKS.add(user_id)


    @app.on_message(filters.command("addjiopremium") & filters.private)
    async def add_jio_premium(client, message):
        if not is_admin(message.from_user.id):
            await message.reply_text("⛔ Only admins can grant JioHotstar access.")
            return
        args = message.command[1:]
        if not args:
            await message.reply_text("❌ Usage: /addjiopremium <user_id>")
            return
        
        try:
            target_id = int(args[0])
            JIO_PREMIUM_USERS.add(target_id)
            await message.reply_text(f"✅ Successfully added user <code>{target_id}</code> to JioHotstar Premium access.", parse_mode="html")
        except ValueError:
            await message.reply_text("❌ Invalid User ID format.")

    @app.on_callback_query(filters.regex("^cancel_dl_"))
    async def cancel_task_callback(client, callback_query: CallbackQuery):
        user_id = callback_query.from_user.id
        target_user_id = int(callback_query.data.split("_")[2])
        if user_id == target_user_id:
            # Keep the task slot reserved until the download's finally block
            # has actually stopped and released it.
            CANCEL_REQUESTS.add(target_user_id)
            await callback_query.answer("⚠️ Task cancellation requested...", show_alert=True)
        else:
            await callback_query.answer("❌ You are not authorized to cancel this task!", show_alert=True)

    @app.on_message(
        filters.private
        & filters.regex(r"https?://[^\s]*(?:hotstar\.com|jiocinema\.com|jiohotstar\.com)")
        & ~filters.command(["start", "help", "platforms", "rip", "status", "cancel", "addjiopremium"])
    )
    async def handle_user_inputs(client, message):
        user_id = message.from_user.id
        text = message.text.strip() if message.text else ""
        url_match = re.search(r'https?://[^\s]+', text)
        extracted_url = url_match.group(0) if url_match else text

        # ജിയോ ഹോസ്റ്റാർ ലിങ്ക് ആണോ എന്ന് പരിശോധിക്കുന്നു
        if any(p in extracted_url for p in ["hotstar.com", "jiocinema.com", "jiohotstar.com"]):
            
            # Use the same /plan, /redeem and /addpremium subscription state
            # as the rest of the bot. Free users are blocked before info fetch.
            if not has_jio_access(user_id):
                await message.reply_text(
                    "🔒 You haven't subscribed yet.\n\n"
                    "Check /plan and contact the owner to get a subscription."
                )
                return

            # Regular premium users: one active JioHotstar task at a time.
            # Admin/setup accounts are exempt.
            if task_limit_reached(user_id):
                await message.reply_text("⚠️ Task Limit Reached! You can download only one JioHotstar video at a time. Please wait for your current task to finish.")
                return

            # Parse -n only when it is a standalone option after the URL.
            # Do not split the URL itself: URL paths/query strings may contain similar text.
            name_match = re.search(r"(?:^|\s)-n(?:\s+|=)(.+?)\s*$", text, flags=re.IGNORECASE)
            url = url_match.group(0).rstrip(".,)") if url_match else text.strip()
            custom_name = name_match.group(1).strip() if name_match else None
            if custom_name:
                # Remove the -n option from the URL text only; preserve the exact supplied name.
                custom_name = re.sub(r"\s+", " ", custom_name).strip()

            status_msg = await message.reply_text("⏳ Fetching video info, thumbnail & auto-detecting audio languages...")

            cookie_file_path = os.getenv("JIOHOTSTAR_COOKIES")
            if not cookie_file_path or not os.path.isfile(cookie_file_path):
                cookie_file_path = "cookies.txt" if os.path.isfile("cookies.txt") else None

            try:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'noplaylist': True,
                    'cookiefile': cookie_file_path,
                    'nocheckcertificate': True
                }

                def extract_info_fast():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(url, download=False)

                info = await asyncio.to_thread(extract_info_fast)
                title = info.get('title', 'OTT Video')
                thumbnail_url = info.get('thumbnail')
                if not thumbnail_url and 'thumbnails' in info:
                    thumbnails = info.get('thumbnails', [])
                    if thumbnails:
                        thumbnail_url = thumbnails[-1].get('url')

                formats = info.get('formats', [])
                extracted_langs = set()
                for f in formats:
                    lang = f.get('language')
                    if lang and lang.strip():
                        extracted_langs.add(lang.lower())

                if not extracted_langs:
                    detected_langs = ["mal", "tam", "hin", "eng", "tel", "kan"]
                else:
                    detected_langs = list(extracted_langs)

                USER_URLS[user_id] = {
                    "url": url, 
                    "custom_name": custom_name,
                    "languages": detected_langs,
                    "selected_langs": [],
                    "info": info
                }

                yt_thumb_path = None
                if thumbnail_url:
                    yt_thumb_path = await asyncio.to_thread(download_youtube_thumbnail, thumbnail_url, user_id)
                if yt_thumb_path:
                    USER_THUMBNAILS[f"yt_{user_id}"] = yt_thumb_path

                keyboard_buttons = [
                    [
                        InlineKeyboardButton("📹 240p", callback_data="dl|video|240"),
                        InlineKeyboardButton("📹 360p", callback_data="dl|video|360")
                    ],
                    [
                        InlineKeyboardButton("📹 480p", callback_data="dl|video|480"),
                        InlineKeyboardButton("📹 576p", callback_data="dl|video|576")
                    ],
                    [
                        InlineKeyboardButton("📹 720p", callback_data="dl|video|720"),
                        InlineKeyboardButton("📹 1080p", callback_data="dl|video|1080")
                    ],
                    [
                        InlineKeyboardButton("📹 1440p (2K)", callback_data="dl|video|1440"),
                        InlineKeyboardButton("📹 2160p (4K)", callback_data="dl|video|2160")
                    ],
                    [InlineKeyboardButton("🎵 Extract MP3 Audio", callback_data="dl|audio|mp3|all")]
                ]

                caption_text = f"🎬 <b>{title}</b>\n\n✅ <b>Auto-detected Audio Languages:</b> {', '.join([l.upper() for l in detected_langs])}\n\n👇 <b>Select your preferred quality:</b>"

                if thumbnail_url:
                    try:
                        await message.reply_photo(photo=thumbnail_url, caption=caption_text, reply_markup=InlineKeyboardMarkup(keyboard_buttons))
                        await status_msg.delete()
                        return
                    except Exception:
                        pass

                await status_msg.edit_text(caption_text, reply_markup=InlineKeyboardMarkup(keyboard_buttons))

            except Exception as e:
                logging.exception("Info extraction error: %s", e)
                await status_msg.edit_text(f"❌ Could not fetch video info.\n\nReason: <code>{html.escape(str(e)[:500])}</code>", parse_mode="html")
        else:
            return

    @app.on_callback_query(filters.regex(r"^back_to_quality$"))
    async def back_to_quality(client, callback_query: CallbackQuery):
        user_id = callback_query.from_user.id
        user_data = USER_URLS.get(user_id)
        if not user_data:
            await callback_query.answer("Session expired. Send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📹 240p", callback_data="dl|video|240"), InlineKeyboardButton("📹 360p", callback_data="dl|video|360")],
            [InlineKeyboardButton("📹 480p", callback_data="dl|video|480"), InlineKeyboardButton("📹 576p", callback_data="dl|video|576")],
            [InlineKeyboardButton("📹 720p", callback_data="dl|video|720"), InlineKeyboardButton("📹 1080p", callback_data="dl|video|1080")],
            [InlineKeyboardButton("📹 1440p (2K)", callback_data="dl|video|1440"), InlineKeyboardButton("📹 2160p (4K)", callback_data="dl|video|2160")],
            [InlineKeyboardButton("🎵 Extract MP3 Audio", callback_data="dl|audio|mp3|all")]
        ])
        await callback_query.message.edit_reply_markup(reply_markup=keyboard)
        await callback_query.answer()

    @app.on_callback_query(filters.regex(r"^dl\|video\|"))
    async def select_audio_languages(client, callback_query: CallbackQuery):
        if not has_jio_access(callback_query.from_user.id):
            await callback_query.answer("🔒 You haven't subscribed yet. Check /plan and contact the owner.", show_alert=True)
            return
        data_parts = callback_query.data.split("|")
        quality = data_parts[2]
        
        user_id = callback_query.from_user.id
        user_data = USER_URLS.get(user_id)

        if not user_data:
            await callback_query.message.edit_text("❌ Link expired. Please send the link again.")
            return

        user_data["quality"] = quality
        detected_langs = user_data.get("languages", ["mal", "tam", "hin", "eng", "tel", "kan"])
        selected_langs = user_data.get("selected_langs", [])

        audio_buttons = []
        for lang in detected_langs:
            lang_name = lang.upper()
            is_selected = lang in selected_langs
            checkbox = "✅ " if is_selected else "☑️ "
            audio_buttons.append([
                InlineKeyboardButton(f"{checkbox}{lang_name} Audio", callback_data=f"toggle_lang|{quality}|{lang}")
            ])
        
        audio_buttons.append([
            InlineKeyboardButton("🚀 Start Download with Selected", callback_data=f"dl_final_multi|{quality}")
        ])
        audio_buttons.append([
            InlineKeyboardButton("🌐 Download All Audio (Multi-Audio)", callback_data=f"dl_final|{quality}|all")
        ])
        audio_buttons.append([
            InlineKeyboardButton("🔙 Back to Quality", callback_data="back_to_quality")
        ])

        await callback_query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(audio_buttons)
        )
        await callback_query.answer("Select audio language(s):")

    @app.on_callback_query(filters.regex(r"^toggle_lang\|"))
    async def toggle_language_selection(client, callback_query: CallbackQuery):
        data_parts = callback_query.data.split("|")
        quality = data_parts[1]
        lang = data_parts[2]

        user_id = callback_query.from_user.id
        user_data = USER_URLS.get(user_id)
        if not user_data:
            await callback_query.answer("❌ Session expired!", show_alert=True)
            return

        if "selected_langs" not in user_data:
            user_data["selected_langs"] = []

        if lang in user_data["selected_langs"]:
            user_data["selected_langs"].remove(lang)
            await callback_query.answer(f"Removed {lang.upper()} audio")
        else:
            user_data["selected_langs"].append(lang)
            await callback_query.answer(f"Selected {lang.upper()} audio ✅")

        detected_langs = user_data.get("languages", [])
        selected_langs = user_data["selected_langs"]

        audio_buttons = []
        for l in detected_langs:
            lang_name = l.upper()
            is_selected = l in selected_langs
            checkbox = "✅ " if is_selected else "☑️ "
            audio_buttons.append([
                InlineKeyboardButton(f"{checkbox}{lang_name} Audio", callback_data=f"toggle_lang|{quality}|{l}")
            ])
        
        audio_buttons.append([
            InlineKeyboardButton("🚀 Start Download with Selected", callback_data=f"dl_final_multi|{quality}")
        ])
        audio_buttons.append([
            InlineKeyboardButton("🌐 Download All Audio (Multi-Audio)", callback_data=f"dl_final|{quality}|all")
        ])
        audio_buttons.append([
            InlineKeyboardButton("🔙 Back to Quality", callback_data="back_to_quality")
        ])

        await callback_query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(audio_buttons)
        )

    @app.on_callback_query(filters.regex(r"^dl\|audio\|mp3\|"))
    async def download_mp3_audio(client, callback_query: CallbackQuery):
        user_id = callback_query.from_user.id
        if not has_jio_access(user_id):
            await callback_query.answer("🔒 You haven't subscribed yet. Check /plan and contact the owner.", show_alert=True)
            return
        user_data = USER_URLS.get(user_id)
        if not user_data:
            await callback_query.answer("Link expired. Send it again.", show_alert=True)
            return
        if task_limit_reached(user_id):
            await callback_query.answer("⚠️ Task Limit Reached! Finish your current JioHotstar download before starting another.", show_alert=True)
            return
        reserve_task(user_id)
        url = user_data["url"]
        status_msg = await callback_query.message.reply_text("⏳ Preparing MP3 audio download...")
        filename = None
        try:
            os.makedirs("downloads", exist_ok=True)
            opts = {
                "format": "bestaudio/best",
                "outtmpl": "downloads/%(title)s.%(ext)s",
                "noplaylist": True,
                "progress_hooks": [get_progress_hook(client, status_msg, user_id, "Extracting MP3")],
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            }
            cookie_file = os.getenv("JIOHOTSTAR_COOKIES")
            if cookie_file and os.path.isfile(cookie_file):
                opts["cookiefile"] = cookie_file
            elif os.path.isfile("cookies.txt"):
                opts["cookiefile"] = "cookies.txt"
            def do_download():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info)
            filename = await asyncio.to_thread(do_download)
            mp3_path = os.path.splitext(filename)[0] + ".mp3"
            if not os.path.isfile(mp3_path):
                raise RuntimeError("MP3 output was not created. This stream may not provide a downloadable audio format.")
            await status_msg.edit_text("📤 Uploading MP3 to Telegram...")
            await client.send_audio(chat_id=callback_query.message.chat.id, audio=mp3_path, title=os.path.basename(mp3_path))
            await status_msg.delete()
            filename = mp3_path
        except Exception as exc:
            await status_msg.edit_text(f"❌ MP3 download failed: {html.escape(str(exc)[:700])}", parse_mode="html")
        finally:
            ACTIVE_JIO_TASKS.discard(user_id)
            if filename and os.path.isfile(filename):
                try: os.remove(filename)
                except OSError: pass
            CANCEL_REQUESTS.discard(user_id)

    @app.on_callback_query(filters.regex(r"^dl_final_multi\|"))
    async def download_multi_selected_media(client, callback_query: CallbackQuery):
        if not has_jio_access(callback_query.from_user.id):
            await callback_query.answer("🔒 You haven't subscribed yet. Check /plan and contact the owner.", show_alert=True)
            return
        data_parts = callback_query.data.split("|")
        quality = data_parts[1]
        
        user_id = callback_query.from_user.id
        
        if task_limit_reached(user_id):
            await callback_query.answer("⚠️ Task Limit Reached! Finish your current JioHotstar download before starting another.", show_alert=True)
            return
        reserve_task(user_id)

        user_data = USER_URLS.get(user_id)
        if not user_data:
            ACTIVE_JIO_TASKS.discard(user_id)
            await callback_query.message.edit_text("❌ Link expired. Please send the link again.")
            return

        selected_langs = user_data.get("selected_langs", [])
        if not selected_langs:
            ACTIVE_JIO_TASKS.discard(user_id)
            await callback_query.answer("⚠️ Please select at least one language using checkboxes first!", show_alert=True)
            return

        url = user_data["url"]
        custom_name = user_data["custom_name"]
        info = user_data.get("info", {})

        try:
            await callback_query.message.delete()
        except Exception:
            pass

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✖️ Cancel Task", callback_data=f"cancel_dl_{user_id}")]
        ])

        langs_str = ", ".join([l.upper() for l in selected_langs])
        status_msg = await client.send_message(
            chat_id=callback_query.message.chat.id,
            text=f"⏳ Initializing download for {quality} (Selected Audios: {langs_str})... Please wait.",
            reply_markup=cancel_keyboard
        )

        audio_selector = "+".join([f"bestaudio[language={l}]" for l in selected_langs])
        format_str = f'bestvideo[height<={quality}]+({audio_selector})/best[height<={quality}]/best'

        ydl_opts = {
            'format': format_str,
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'progress_hooks': [get_progress_hook(client, status_msg, user_id, f"Downloading {quality} Video")],
        }

        cookie_file_path = os.getenv("JIOHOTSTAR_COOKIES")
        if cookie_file_path and os.path.isfile(cookie_file_path):
            ydl_opts['cookiefile'] = cookie_file_path
        elif os.path.isfile("cookies.txt"):
            ydl_opts['cookiefile'] = "cookies.txt"

        thumb_path = None
        filename = None
        try:
            os.makedirs("downloads", exist_ok=True)
            def run_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    if user_id in CANCEL_REQUESTS:
                        raise Exception("Task cancelled by user.")
                    inf = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(inf)

            filename = await asyncio.to_thread(run_download)

            if user_id in CANCEL_REQUESTS:
                raise Exception("Task cancelled by user.")

            if filename and os.path.exists(filename):
                audio_tag = langs_str.replace(", ", ".")
                new_filename = build_hotstar_output_name(filename, custom_name, quality, audio_tag)
                if os.path.abspath(filename) != os.path.abspath(new_filename):
                    if os.path.exists(new_filename):
                        os.remove(new_filename)
                    os.rename(filename, new_filename)
                filename = new_filename

            await status_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_keyboard)

            if user_id in USER_THUMBNAILS and os.path.exists(USER_THUMBNAILS[user_id]):
                thumb_path = USER_THUMBNAILS[user_id]
            elif f"yt_{user_id}" in USER_THUMBNAILS and os.path.exists(USER_THUMBNAILS[f"yt_{user_id}"]):
                thumb_path = USER_THUMBNAILS[f"yt_{user_id}"]
                
            if not thumb_path or not os.path.exists(thumb_path):
                thumb_path = generate_thumbnail(filename, user_id)

            file_name = os.path.basename(filename)
            duration = get_video_duration(filename)

            await client.send_video(
                chat_id=callback_query.message.chat.id,
                video=filename,
                file_name=file_name,
                duration=duration,
                thumb=thumb_path,
                caption=f"📹 <b>{file_name}</b>"
            )

            await status_msg.delete()
            
        except Exception as e:
            if user_id in CANCEL_REQUESTS or "cancelled" in str(e).lower():
                await status_msg.edit_text("❌ Task Cancelled successfully!")
            else:
                await status_msg.edit_text(f"❌ Download failed: {str(e)}")
        finally:
            ACTIVE_JIO_TASKS.discard(user_id)
            if filename and os.path.exists(filename):
                try:
                    os.remove(filename)
                except:
                    pass
            if thumb_path and "thumbnails/auto_" in thumb_path and os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                except:
                    pass
            if f"yt_{user_id}" in USER_THUMBNAILS:
                try:
                    os.remove(USER_THUMBNAILS[f"yt_{user_id}"])
                    del USER_THUMBNAILS[f"yt_{user_id}"]
                except:
                    pass
            CANCEL_REQUESTS.discard(user_id)

    @app.on_callback_query(filters.regex(r"^dl_final\|"))
    async def download_final_media(client, callback_query: CallbackQuery):
        if not has_jio_access(callback_query.from_user.id):
            await callback_query.answer("🔒 You haven't subscribed yet. Check /plan and contact the owner.", show_alert=True)
            return
        data_parts = callback_query.data.split("|")
        quality = data_parts[1]
        selected_audio = data_parts[2]
        
        user_id = callback_query.from_user.id
        
        if task_limit_reached(user_id):
            await callback_query.answer("⚠️ Task Limit Reached! Finish your current JioHotstar download before starting another.", show_alert=True)
            return
        reserve_task(user_id)

        user_data = USER_URLS.get(user_id)
        if not user_data:
            ACTIVE_JIO_TASKS.discard(user_id)
            await callback_query.message.edit_text("❌ Link expired. Please send the link again.")
            return

        url = user_data["url"]
        custom_name = user_data["custom_name"]
        info = user_data.get("info", {})

        try:
            await callback_query.message.delete()
        except Exception:
            pass

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✖️ Cancel Task", callback_data=f"cancel_dl_{user_id}")]
        ])

        status_msg = await client.send_message(
            chat_id=callback_query.message.chat.id,
            text=f"⏳ Initializing download for {quality} (Audio: {selected_audio.upper()})... Please wait.",
            reply_markup=cancel_keyboard
        )

        if selected_audio == "all":
            format_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        else:
            format_str = f'bestvideo[height<={quality}]+bestaudio[language={selected_audio}]/best[height<={quality}]/best'

        ydl_opts = {
            'format': format_str,
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'progress_hooks': [get_progress_hook(client, status_msg, user_id, f"Downloading {quality} Video")],
        }

        cookie_file_path = os.getenv("JIOHOTSTAR_COOKIES")
        if cookie_file_path and os.path.isfile(cookie_file_path):
            ydl_opts['cookiefile'] = cookie_file_path
        elif os.path.isfile("cookies.txt"):
            ydl_opts['cookiefile'] = "cookies.txt"

        thumb_path = None
        filename = None
        try:
            os.makedirs("downloads", exist_ok=True)
            def run_download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    if user_id in CANCEL_REQUESTS:
                        raise Exception("Task cancelled by user.")
                    inf = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(inf)

            filename = await asyncio.to_thread(run_download)

            if user_id in CANCEL_REQUESTS:
                raise Exception("Task cancelled by user.")

            if filename and os.path.exists(filename):
                audio_str = "Multi-Audio" if selected_audio == "all" else selected_audio.upper()
                new_filename = build_hotstar_output_name(filename, custom_name, quality, audio_str)
                if os.path.abspath(filename) != os.path.abspath(new_filename):
                    if os.path.exists(new_filename):
                        os.remove(new_filename)
                    os.rename(filename, new_filename)
                filename = new_filename

            await status_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_keyboard)

            if user_id in USER_THUMBNAILS and os.path.exists(USER_THUMBNAILS[user_id]):
                thumb_path = USER_THUMBNAILS[user_id]
            elif f"yt_{user_id}" in USER_THUMBNAILS and os.path.exists(USER_THUMBNAILS[f"yt_{user_id}"]):
                thumb_path = USER_THUMBNAILS[f"yt_{user_id}"]
                
            if not thumb_path or not os.path.exists(thumb_path):
                thumb_path = generate_thumbnail(filename, user_id)

            file_name = os.path.basename(filename)
            duration = get_video_duration(filename)

            await client.send_video(
                chat_id=callback_query.message.chat.id,
                video=filename,
                file_name=file_name,
                duration=duration,
                thumb=thumb_path,
                caption=f"📹 <b>{file_name}</b>"
            )

            await status_msg.delete()
            
        except Exception as e:
            if user_id in CANCEL_REQUESTS or "cancelled" in str(e).lower():
                await status_msg.edit_text("❌ Task Cancelled successfully!")
            else:
                await status_msg.edit_text(f"❌ Download failed: {str(e)}")
        finally:
            ACTIVE_JIO_TASKS.discard(user_id)
            if filename and os.path.exists(filename):
                try:
                    os.remove(filename)
                except:
                    pass
            if thumb_path and "thumbnails/auto_" in thumb_path and os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                except:
                    pass
            if f"yt_{user_id}" in USER_THUMBNAILS:
                try:
                    os.remove(USER_THUMBNAILS[f"yt_{user_id}"])
                    del USER_THUMBNAILS[f"yt_{user_id}"]
                except:
                    pass
            CANCEL_REQUESTS.discard(user_id)
