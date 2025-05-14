# config.py
#
# =====================================================================
# INSTÄLLNINGAR FÖR FACEBOOK REACH REPORT
# =====================================================================

# =====================================================================
# 1. TIDSPERIOD - UTGÅNGSPUNKT FÖR DATAINSAMLING
# =====================================================================
#
# Format: YYYY-MM (År-Månad)  
# Exempel: "2025-01" för januari 2025
#
INITIAL_START_YEAR_MONTH = "2025-01"  # <- ÄNDRA DETTA DATUM till önskat startår och -månad
#
# Skriptet kommer att samla in data för alla kompletta månader från detta datum 
# fram till föregående månad (dvs. den senast avslutade hela månaden)
# =====================================================================


# =====================================================================
# 2. TOKEN-INFORMATION - UPPDATERA VARJE GÅNG DU SKAFFAR NY TOKEN
# =====================================================================
#
# Din långlivade användartoken från Meta (giltig i 60 dagar)
# Kopiera in din token från Facebook Business Manager här:
ACCESS_TOKEN = "TOKEN HERE"

# Datum då du senast skapade tokenen (YYYY-MM-DD)
# Uppdatera varje gång du skapar en ny token
TOKEN_LAST_UPDATED = "2025-05-12"  # <- ÄNDRA DETTA DATUM när du skapar ny token
TOKEN_VALID_DAYS = 60              # Meta-token är normalt giltig i 60 dagar
# =====================================================================


# =====================================================================
# 3. API OCH PRESTANDA-INSTÄLLNINGAR
# =====================================================================
API_VERSION = "v19.0"           # Facebook Graph API-version
CACHE_FILE = "page_names.json"  # Cache för sidnamn
BATCH_SIZE = 10                 # Antal sidor att bearbeta samtidigt
MAX_RETRIES = 3                 # Antal försök innan vi ger upp
RETRY_DELAY = 5                 # Sekunder att vänta mellan försök
MAX_REQUESTS_PER_HOUR = 200     # Ungefärlig gräns från Facebook
MONTH_PAUSE_SECONDS = 60        # Sekunder att vänta mellan månader
# =====================================================================
