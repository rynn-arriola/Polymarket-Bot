# ============================================================
# CREDENTIALS TEMPLATE — copy to credentials.py and fill in.
# credentials.py is gitignored; never commit real values.
# Every value can alternatively be supplied as an environment
# variable of the same name (env vars win over this file).
# ============================================================

# Polymarket US API (from polymarket.us/developer)
KEY_ID = "PASTE_YOUR_KEY_ID"
SECRET_KEY = "PASTE_YOUR_SECRET_KEY"

# PandaScore (Valorant data source; free token from pandascore.co)
# Blank = fall back to the vlr.gg mirror (no behavior change).
PANDASCORE_TOKEN = ""

# Discord webhooks (optional; blank = that report is disabled).
# Keep URLs private: anyone with one can post into that channel.
DISCORD_WEBHOOK_URL = ""
DISCORD_SETTLEMENT_WEBHOOK_URL = ""
DISCORD_CLV_WEBHOOK_URL = ""
DISCORD_ERRORS_WEBHOOK_URL = ""   # ops channel: WARNING+ logs pushed here
