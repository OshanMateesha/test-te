import os
import json
from telethon.sync import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import RequestWebViewRequest
from telethon.tl.types import DataJSON

# my.telegram.org -> API development tools වලින් ගන්න
API_ID = int(os.environ.get("TG_API_ID", "31855342"))
API_HASH = os.environ.get("TG_API_HASH", "720b7e861db769d41bebd92b6b9b283c")
THEME = {
    "bg_color": "#212121", "text_color": "#ffffff", "hint_color": "#aaaaaa",
    "link_color": "#8774e1", "button_color": "#8774e1", "button_text_color": "#ffffff",
    "secondary_bg_color": "#181818", "header_bg_color": "#212121",
    "accent_text_color": "#8774e1", "section_bg_color": "#212121",
    "section_header_text_color": "#8774e1", "subtitle_text_color": "#aaaaaa",
    "destructive_text_color": "#ff595a",
}
SESSION = "tg_session"  # මේ file එක කාටවත් දෙන්න එපා (ඔයාගේ account එකට access දෙනවා)


def get_webapp_url(bot_username: str) -> str:
    """Web app URL එක #tgWebAppData (initData) එක්කම දෙනවා."""
    bot_username = bot_username.lstrip("@")
    with TelegramClient(SESSION, API_ID, API_HASH) as client:
        bot = client.get_input_entity(bot_username)

        # Bot ගේ menu button (Open button එක) එකේ base URL එක
        full = client(GetFullUserRequest(bot))
        info = full.full_user.bot_info
        menu = getattr(info, "menu_button", None) if info else None
        base_url = getattr(menu, "url", None)

        # Web view request එකෙන් initData එක්ක URL එක ගන්නවා
        try:
            result = client(RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform="web",
                from_bot_menu=True,
                url=base_url,
                theme_params=DataJSON(data=json.dumps(THEME)),
            ))
            return result.url  # https://site/#tgWebAppData=...&tgWebAppVersion=...
        except Exception as e:
            print("WebView request fail උනා:", e)
            if base_url:
                return base_url  # initData නැතුව (site එක ඉල්ලනවා නම් වැඩ කරන්නේ නෑ)
            raise


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "terabox_downloader_new_bot"
    print(get_webapp_url(name))