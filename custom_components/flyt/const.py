"""Constants for the Flyt bombrikke integration."""

DOMAIN = "flyt"

# Azure AD B2C (Flyt MinSide)
B2C_HOST = "https://fertsp.b2clogin.com"
B2C_TENANT = "fertsp.onmicrosoft.com"
B2C_POLICY = "B2C_1_sign-up-sign-in-self-service-portal-prod"
CLIENT_ID = "342902ff-8a3a-437c-96c1-7e91c92095ca"
API_SCOPE = "https://fertsp.onmicrosoft.com/710d947a-f384-4f83-8cae-cf001214ae1c/Read"
SCOPES = f"openid offline_access {API_SCOPE}"
REDIRECT_URI = "https://minside-flyt.flytpass.no/authcallback"
ORIGIN = "https://minside-flyt.flytpass.no"

API_BASE = "https://func-flyt-tsp-self-gateway-prod.azurewebsites.net/api"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL_MIN = 30
MIN_SCAN_INTERVAL_MIN = 10

# How far back to fetch passings on every poll (covers "this month" + last passing)
LOOKBACK_DAYS = 45

CONF_SHOW_ALL_PASSINGS = "show_all_passings"

GEO_SOURCE = "flyt"

EVENT_NEW_PASSING = "flyt_new_passing"
TIMEZONE = "Europe/Oslo"
