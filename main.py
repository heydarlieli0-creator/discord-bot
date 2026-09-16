import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
import asyncio
import atexit
import os
import random
import threading
import json
import time
import io
import urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from flask import Flask
import discord
from discord import app_commands
from groq import Groq

# Flask ile 7/24 aktif tutma
app = Flask('')

@app.route('/')
def home():
   return "Bot aktif ve çal???yor!"

def run_web():
   app.run(host='0.0.0.0', port=8080)

def keep_alive():
   t = threading.Thread(target=run_web)
   t.start()

# API Anahtarlar?
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DISCORD_TOKEN = os.environ.get("Discord_Token") or os.environ.get("DISCORD_TOKEN")
SEVIYE_KANAL_ID = os.environ.get("SEVIYE_KANAL_ID", "1533423499505307698")

if not DISCORD_TOKEN:
   print("? HATA: Discord Token bulunamad?!")
if not GROQ_API_KEY:
   print("? UYARI: GROQ_API_KEY bulunamad?. /ask komutu çal??maz.")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guild_messages = True
intents.dm_messages = True
intents.voice_states = True
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

aktif_oyunlar = {}

LEVEL_DEBUG = os.environ.get('LEVEL_DEBUG', 'false').strip().lower() in {'1','true','yes','on'}
level_runtime_stats = {'events': 0, 'saved': 0, 'errors': 0, 'last_user': None, 'last_guild': None}

# ================= SEV?YE S?STEM? =================
SEVIYE_DOSYASI = "seviyeler.json"
BASLANGIC_SEVIYE_XP = 300
SEVIYE_XP_ARTISI = 300


# ================= SEV?YE S?STEM? (PostgreSQL) =================
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")
DB_MIN_CONN = max(1, int(os.environ.get("DB_MIN_CONN", "1")))
DB_MAX_CONN = max(DB_MIN_CONN, int(os.environ.get("DB_MAX_CONN", "8")))

_db_pool = None
_db_pool_lock = threading.Lock()
_db_init_lock = threading.Lock()
_db_conn_semaphore = threading.BoundedSemaphore(DB_MAX_CONN)
_db_initialized = False


def _database_url_kontrol():
   if not DATABASE_URL:
       raise RuntimeError(
           "DATABASE_URL tan?ml? de?il. PostgreSQL ba?lant? adresini environment de?i?kenine ekle."
       )


def get_db_pool():
   """Thread-safe PostgreSQL connection pool'u lazy olarak olu?turur."""
   global _db_pool
   _database_url_kontrol()

   if _db_pool is None:
       with _db_pool_lock:
           if _db_pool is None:
               _db_pool = ThreadedConnectionPool(
                   DB_MIN_CONN,
                   DB_MAX_CONN,
                   dsn=DATABASE_URL,
                   sslmode=DB_SSLMODE,
                   connect_timeout=10,
                   application_name="discord-seviye-botu",
               )
   return _db_pool


def get_db_connection():
   """Pool doluysa k?sa süre bekler; anl?k yo?unlukta XP kayb?n? önler."""
   if not _db_conn_semaphore.acquire(timeout=10):
       raise TimeoutError("PostgreSQL connection pool 10 saniye içinde bo?almad?")
   try:
       return get_db_pool().getconn()
   except Exception:
       _db_conn_semaphore.release()
       raise


def release_db_connection(conn, close=False):
   try:
       if conn is not None and _db_pool is not None:
           _db_pool.putconn(conn, close=close or bool(conn.closed))
   finally:
       _db_conn_semaphore.release()


@contextmanager
def db_cursor(dict_cursor=False):
   """Ba?lant?y? pool'dan al?r; commit/rollback ve iade i?lemini garanti eder."""
   conn = None
   cur = None
   broken = False
   try:
       conn = get_db_connection()
       cur = conn.cursor(cursor_factory=RealDictCursor if dict_cursor else None)
       yield conn, cur
       conn.commit()
   except Exception as e:
       broken = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))
       if conn is not None:
           try:
               conn.rollback()
           except Exception:
               broken = True
       raise
   finally:
       if cur is not None:
           try:
               cur.close()
           except Exception:
               pass
       if conn is not None:
           release_db_connection(conn, close=broken)


def close_db_pool():
   global _db_pool
   with _db_pool_lock:
       if _db_pool is not None:
           try:
               _db_pool.closeall()
           finally:
               _db_pool = None


atexit.register(close_db_pool)


def init_db():
   """Seviye tablosunu olu?turur ve eski user_id-only ?emay? güvenle migrate eder."""
   global _db_initialized
   if _db_initialized:
       return True

   with _db_init_lock:
       if _db_initialized:
           return True

       with db_cursor() as (_, cur):
           cur.execute("SELECT 1")
           cur.execute("""
               CREATE TABLE IF NOT EXISTS seviyeler (
                   guild_id TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   xp INTEGER NOT NULL DEFAULT 0,
                   seviye INTEGER NOT NULL DEFAULT 1,
                   sonraki_seviye_xp INTEGER NOT NULL DEFAULT 300,
                   mesaj_sayisi INTEGER NOT NULL DEFAULT 0,
                   son_daily DOUBLE PRECISION NOT NULL DEFAULT 0,
                   ses_suresi BIGINT NOT NULL DEFAULT 0,
                   ses_baslangic DOUBLE PRECISION NOT NULL DEFAULT 0,
                   PRIMARY KEY (guild_id, user_id)
               )
           """)

           # Eski sürümde guild_id yoktu ve PRIMARY KEY yaln?z user_id idi.
           cur.execute("ALTER TABLE seviyeler ADD COLUMN IF NOT EXISTS guild_id TEXT")
           cur.execute("UPDATE seviyeler SET guild_id='0' WHERE guild_id IS NULL OR guild_id='' ")
           cur.execute("ALTER TABLE seviyeler ALTER COLUMN guild_id SET NOT NULL")

           # Eski PK yap?s?n? yaln?z gerekiyorsa composite PK'ye dönü?tür.
           cur.execute("""
               SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
               FROM pg_constraint c
               JOIN pg_class t ON t.oid = c.conrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
               WHERE t.relname = 'seviyeler'
                 AND n.nspname = current_schema()
                 AND c.contype = 'p'
               LIMIT 1
           """)
           pk = cur.fetchone()
           definition = pk[1] if pk else None
           if definition != 'PRIMARY KEY (guild_id, user_id)':
               if pk:
                   # Constraint ad? PostgreSQL taraf?ndan üretildi?i için identifier olarak quote edilir.
                   safe_pk_name = str(pk[0]).replace('"', '""')
                   cur.execute(f'ALTER TABLE seviyeler DROP CONSTRAINT "{safe_pk_name}"')
               cur.execute("ALTER TABLE seviyeler ADD PRIMARY KEY (guild_id, user_id)")

           cur.execute(
               "CREATE INDEX IF NOT EXISTS idx_seviyeler_guild_rank "
               "ON seviyeler (guild_id, seviye DESC, xp DESC)"
           )

           # Ekonomi kolonlar?
           cur.execute("""
               ALTER TABLE seviyeler 
               ADD COLUMN IF NOT EXISTS para BIGINT NOT NULL DEFAULT 0,
               ADD COLUMN IF NOT EXISTS son_daily_para DOUBLE PRECISION NOT NULL DEFAULT 0,
               ADD COLUMN IF NOT EXISTS son_work DOUBLE PRECISION NOT NULL DEFAULT 0
           """)

           # Ses istatistikleri
           cur.execute("""
               ALTER TABLE seviyeler
               ADD COLUMN IF NOT EXISTS ses_suresi BIGINT NOT NULL DEFAULT 0,
               ADD COLUMN IF NOT EXISTS ses_baslangic DOUBLE PRECISION NOT NULL DEFAULT 0
           """)

           # Envanter kolonu
           cur.execute("""
               ALTER TABLE seviyeler 
               ADD COLUMN IF NOT EXISTS envanter JSONB NOT NULL DEFAULT '{}'::jsonb
           """)

           # Futbol karti temasi
           cur.execute("""
               ALTER TABLE seviyeler
               ADD COLUMN IF NOT EXISTS kart_tema_url TEXT NOT NULL DEFAULT ''
           """)

           cur.execute("""
               ALTER TABLE seviyeler
               ADD COLUMN IF NOT EXISTS kart_tema_data BYTEA
           """)

       _db_initialized = True
       print(f"? Database tablosu haz?r - pool={DB_MIN_CONN}-{DB_MAX_CONN} - guild-scoped=true")
       return True

def _kullanici_satiri_kilitle(cur, guild_id, user_id):
   """Kullan?c? sat?r?n? transaction içinde FOR UPDATE ile kilitler."""
   gid = str(guild_id)
   uid = str(user_id)

   cur.execute(
       "SELECT * FROM seviyeler WHERE guild_id=%s AND user_id=%s FOR UPDATE",
       (gid, uid),
   )
   row = cur.fetchone()
   if row is not None:
       return dict(row)

   # Eski sürümden kalan global kayd? ilk gerçek sunucuda kaybetmeden devral.
   cur.execute(
       "SELECT * FROM seviyeler WHERE guild_id='0' AND user_id=%s FOR UPDATE",
       (uid,),
   )
   legacy = cur.fetchone()
   if legacy is not None:
       cur.execute(
           "UPDATE seviyeler SET guild_id=%s WHERE guild_id='0' AND user_id=%s RETURNING *",
           (gid, uid),
       )
       return dict(cur.fetchone())

   cur.execute("""
       INSERT INTO seviyeler (
           guild_id, user_id, xp, seviye, sonraki_seviye_xp, mesaj_sayisi, son_daily
       ) VALUES (%s, %s, 0, 1, %s, 0, 0)
       ON CONFLICT (guild_id, user_id) DO NOTHING
   """, (gid, uid, BASLANGIC_SEVIYE_XP))
   cur.execute(
       "SELECT * FROM seviyeler WHERE guild_id=%s AND user_id=%s FOR UPDATE",
       (gid, uid),
   )
   row = cur.fetchone()
   if row is None:
       raise RuntimeError("Seviye kullan?c? sat?r? olu?turulamad?")
   return dict(row)


def kullanici_verisi_al(guild_id, user_id):
   """Kullan?c? verisini getirir. DB hatas?nda sahte 0 XP döndürmez."""
   try:
       if not _db_initialized:
           init_db()
       with db_cursor(dict_cursor=True) as (_, cur):
           return _kullanici_satiri_kilitle(cur, guild_id, user_id)
   except Exception as e:
       print(f"? kullanici_verisi_al hatas?: {type(e).__name__}: {e}")
       raise


def seviye_verisi_kaydet(guild_id, user_id, veri):
   """Uyumluluk amaçl? güvenli UPSERT. Ba?ar?s?z kay?t sessizce yutulmaz."""
   if not _db_initialized:
       init_db()
   gid = str(guild_id)
   uid = str(user_id)
   try:
       with db_cursor() as (_, cur):
           cur.execute("""
               INSERT INTO seviyeler (
                   guild_id, user_id, xp, seviye, sonraki_seviye_xp, mesaj_sayisi, son_daily
               ) VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (guild_id, user_id) DO UPDATE SET
                   xp = EXCLUDED.xp,
                   seviye = EXCLUDED.seviye,
                   sonraki_seviye_xp = EXCLUDED.sonraki_seviye_xp,
                   mesaj_sayisi = EXCLUDED.mesaj_sayisi,
                   son_daily = EXCLUDED.son_daily
           """, (
               gid,
               uid,
               int(veri["xp"]),
               int(veri["seviye"]),
               int(veri["sonraki_seviye_xp"]),
               int(veri["mesaj_sayisi"]),
               float(veri.get("son_daily", 0)),
           ))
       return True
   except Exception as e:
       print(f"? seviye_verisi_kaydet hatas?: {type(e).__name__}: {e}")
       raise


def seviye_xp_ekle(guild_id, user_id, xp_miktari, mesaj_artisi=0, son_daily=None):
   """XP art???n? tek transaction içinde atomik olarak uygular."""
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       onceki_seviye = int(veri["seviye"])

       veri["xp"] = int(veri["xp"]) + int(xp_miktari)
       veri["mesaj_sayisi"] = int(veri["mesaj_sayisi"]) + int(mesaj_artisi)
       if son_daily is not None:
           veri["son_daily"] = float(son_daily)

       while veri["xp"] >= veri["sonraki_seviye_xp"]:
           veri["xp"] -= veri["sonraki_seviye_xp"]
           veri["seviye"] += 1
           veri["sonraki_seviye_xp"] += SEVIYE_XP_ARTISI

       cur.execute("""
           UPDATE seviyeler
           SET xp=%s, seviye=%s, sonraki_seviye_xp=%s, mesaj_sayisi=%s, son_daily=%s
           WHERE guild_id=%s AND user_id=%s
           RETURNING *
       """, (
           int(veri["xp"]),
           int(veri["seviye"]),
           int(veri["sonraki_seviye_xp"]),
           int(veri["mesaj_sayisi"]),
           float(veri.get("son_daily", 0)),
           str(guild_id),
           str(user_id),
       ))
       guncel = dict(cur.fetchone())
       return guncel, onceki_seviye


def gunluk_xp_al(guild_id, user_id):
   """Daily cooldown kontrolünü ve XP kayd?n? ayn? transaction içinde yapar."""
   if not _db_initialized:
       init_db()
   simdi = time.time()
   bekleme_suresi = 24 * 60 * 60

   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       son_daily = float(veri.get("son_daily", 0) or 0)
       fark = simdi - son_daily

       if fark < bekleme_suresi:
           return False, veri, int(bekleme_suresi - fark), 0, int(veri["seviye"])

       kazanilan_xp = random.randint(350, 750)
       onceki_seviye = int(veri["seviye"])
       veri["xp"] = int(veri["xp"]) + kazanilan_xp
       veri["son_daily"] = simdi

       while veri["xp"] >= veri["sonraki_seviye_xp"]:
           veri["xp"] -= veri["sonraki_seviye_xp"]
           veri["seviye"] += 1
           veri["sonraki_seviye_xp"] += SEVIYE_XP_ARTISI

       cur.execute("""
           UPDATE seviyeler
           SET xp=%s, seviye=%s, sonraki_seviye_xp=%s, mesaj_sayisi=%s, son_daily=%s
           WHERE guild_id=%s AND user_id=%s
           RETURNING *
       """, (
           int(veri["xp"]),
           int(veri["seviye"]),
           int(veri["sonraki_seviye_xp"]),
           int(veri["mesaj_sayisi"]),
           float(veri["son_daily"]),
           str(guild_id),
           str(user_id),
       ))
       guncel = dict(cur.fetchone())
       return True, guncel, 0, kazanilan_xp, onceki_seviye


def tum_seviye_verilerini_al(guild_id):
   """Yaln?z ilgili Discord sunucusunun s?ralamas?n? getirir."""
   try:
       if not _db_initialized:
           init_db()
       with db_cursor(dict_cursor=True) as (_, cur):
           cur.execute(
               "SELECT * FROM seviyeler WHERE guild_id=%s ORDER BY seviye DESC, xp DESC",
               (str(guild_id),),
           )
           rows = cur.fetchall()
           return {row["user_id"]: dict(row) for row in rows}
   except Exception as e:
       print(f"? tum_seviye_verilerini_al hatas?: {type(e).__name__}: {e}")
       raise

def toplam_xp_hesapla(veri):
   seviye = veri["seviye"]
   return 300 * (seviye - 1) * seviye // 2 + veri["xp"]



# ================= SES ISTATISTIKLERI =================

def ses_oturumu_baslat(guild_id, user_id):
   """Kullanici ses kanalina girdiginde oturumu baslatir."""
   if not _db_initialized:
       init_db()
   simdi = time.time()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       if float(veri.get("ses_baslangic", 0) or 0) <= 0:
           cur.execute(
               "UPDATE seviyeler SET ses_baslangic=%s "
               "WHERE guild_id=%s AND user_id=%s",
               (simdi, str(guild_id), str(user_id)),
           )


def ses_oturumu_bitir(guild_id, user_id):
   """Kullanici ses kanalindan ciktiginda aktif sureyi toplam sureye ekler."""
   if not _db_initialized:
       init_db()
   simdi = time.time()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       baslangic = float(veri.get("ses_baslangic", 0) or 0)
       toplam = int(veri.get("ses_suresi", 0) or 0)

       if baslangic > 0:
           toplam += max(0, int(simdi - baslangic))

       cur.execute(
           "UPDATE seviyeler SET ses_suresi=%s, ses_baslangic=0 "
           "WHERE guild_id=%s AND user_id=%s",
           (toplam, str(guild_id), str(user_id)),
       )
       return toplam


def ses_istatistigi_al(guild_id, user_id):
   """Toplam ses suresini, aktif oturum dahil, saniye olarak dondurur."""
   if not _db_initialized:
       init_db()
   simdi = time.time()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       toplam = int(veri.get("ses_suresi", 0) or 0)
       baslangic = float(veri.get("ses_baslangic", 0) or 0)

       if baslangic > 0:
           toplam += max(0, int(simdi - baslangic))

       return toplam, baslangic > 0


def saniyeyi_formatla(saniye):
   saniye = max(0, int(saniye))
   gun, kalan = divmod(saniye, 86400)
   saat, kalan = divmod(kalan, 3600)
   dakika, saniye = divmod(kalan, 60)

   parcalar = []
   if gun:
       parcalar.append(f"{gun} gun")
   if saat:
       parcalar.append(f"{saat} saat")
   if dakika:
       parcalar.append(f"{dakika} dakika")
   if saniye or not parcalar:
       parcalar.append(f"{saniye} saniye")
   return " ".join(parcalar)


def tum_ses_istatistiklerini_al(guild_id):
   """Sunucudaki ses istatistiklerini getirir."""
   if not _db_initialized:
       init_db()
   simdi = time.time()
   with db_cursor(dict_cursor=True) as (_, cur):
       cur.execute(
           "SELECT user_id, ses_suresi, ses_baslangic "
           "FROM seviyeler WHERE guild_id=%s",
           (str(guild_id),),
       )
       sonuc = {}
       for row in cur.fetchall():
           toplam = int(row["ses_suresi"] or 0)
           baslangic = float(row["ses_baslangic"] or 0)
           if baslangic > 0:
               toplam += max(0, int(simdi - baslangic))
           sonuc[str(row["user_id"])] = toplam
       return sonuc


# ================= FUTBOL KARTI =================

KART_GENISLIK = 700
KART_YUKSEKLIK = 980


def kart_tema_verisi_al(guild_id, user_id):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       return veri.get("kart_tema_data")


def kart_tema_kaydet(guild_id, user_id, tema_bytes):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       _kullanici_satiri_kilitle(cur, guild_id, user_id)
       cur.execute(
           "UPDATE seviyeler SET kart_tema_data=%s, kart_tema_url='' WHERE guild_id=%s AND user_id=%s",
           (psycopg2.Binary(tema_bytes) if tema_bytes else None, str(guild_id), str(user_id))
       )


def futbol_reyting_hesapla(veri, ses_suresi):
   """Mesaj, ses ve seviye istatistiklerinden 40-99 arasi genel reyting."""
   seviye = int(veri.get("seviye", 1) or 1)
   mesaj = int(veri.get("mesaj_sayisi", 0) or 0)
   ses_dakika = int(ses_suresi) // 60
   reyting = 40
   reyting += min(20, seviye * 2)
   reyting += min(20, mesaj // 50)
   reyting += min(19, ses_dakika // 120)
   return max(40, min(99, reyting))


def _kart_font(size, bold=False):
   adaylar = [
       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
       "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
   ]
   for yol in adaylar:
       if os.path.exists(yol):
           return ImageFont.truetype(yol, size)
   return ImageFont.load_default()


def _kart_resim_indir(url):
   if not url:
       return None
   try:
       req = urllib.request.Request(url, headers={"User-Agent": "DiscordFootballCard/1.0"})
       with urllib.request.urlopen(req, timeout=12) as response:
           return Image.open(io.BytesIO(response.read())).convert("RGBA")
   except Exception as e:
       print(f"Kart resmi indirilemedi: {type(e).__name__}: {e}")
       return None


def _kart_resim_bytes_ac(data):
   if not data:
       return None
   try:
       return Image.open(io.BytesIO(bytes(data))).convert("RGBA")
   except Exception as e:
       print(f"Kart tema resmi acilamadi: {type(e).__name__}: {e}")
       return None


def _kart_arkaplan_hazirla(tema):
   if tema is None:
       tema = Image.new("RGBA", (KART_GENISLIK, KART_YUKSEKLIK), (22, 24, 32, 255))
   tema = tema.convert("RGB")
   tema = ImageOps.fit(tema, (KART_GENISLIK, KART_YUKSEKLIK), method=Image.Resampling.LANCZOS)
   tema = tema.filter(ImageFilter.GaussianBlur(0.8)).convert("RGBA")
   tema.alpha_composite(Image.new("RGBA", tema.size, (8, 10, 15, 115)))
   return tema


def _yuvarlak_avatar(base, avatar, xy, size):
   if avatar is None:
       return
   avatar = ImageOps.fit(avatar.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
   maske = Image.new("L", (size, size), 0)
   ImageDraw.Draw(maske).ellipse((0, 0, size, size), fill=255)
   base.paste(avatar, xy, maske)
   draw = ImageDraw.Draw(base)
   draw.ellipse((xy[0]-5, xy[1]-5, xy[0]+size+5, xy[1]+size+5), outline=(255,255,255,220), width=5)


def futbol_karti_olustur(member, veri, ses_suresi, tema_resmi=None):
   base = _kart_arkaplan_hazirla(tema_resmi)
   draw = ImageDraw.Draw(base)
   draw.rounded_rectangle((18,18,KART_GENISLIK-18,KART_YUKSEKLIK-18), radius=42, outline=(255,255,255,205), width=5, fill=(12,15,22,75))
   draw.rounded_rectangle((34,34,KART_GENISLIK-34,KART_YUKSEKLIK-34), radius=32, outline=(255,196,70,170), width=2)

   reyting = futbol_reyting_hesapla(veri, ses_suresi)
   mesaj = int(veri.get("mesaj_sayisi", 0) or 0)
   seviye = int(veri.get("seviye", 1) or 1)
   xp = int(veri.get("xp", 0) or 0)

   avatar = _kart_resim_indir(member.display_avatar.with_size(256).url)
   _yuvarlak_avatar(base, avatar, (62,95), 190)

   draw.text((520,75), str(reyting), font=_kart_font(92,True), fill=(255,215,92), anchor="mm")
   draw.text((520,150), "OVR", font=_kart_font(28,True), fill=(255,255,255), anchor="mm")
   draw.text((62,315), member.display_name[:20], font=_kart_font(40,True), fill=(255,255,255))
   draw.text((62,362), "COMMUNITY PLAYER", font=_kart_font(20,True), fill=(255,215,92))

   kutular = [("LEVEL",str(seviye)),("MESSAGES",f"{mesaj:,}"),("VOICE",saniyeyi_formatla(ses_suresi)),("XP",f"{xp:,}")]
   y=450
   for baslik,deger in kutular:
       draw.rounded_rectangle((62,y,KART_GENISLIK-62,y+90), radius=20, fill=(0,0,0,115), outline=(255,255,255,70), width=2)
       draw.text((85,y+45), baslik, font=_kart_font(22,True), fill=(210,215,225), anchor="lm")
       draw.text((KART_GENISLIK-85,y+45), deger, font=_kart_font(27 if baslik=="VOICE" else 34,True), fill=(255,255,255), anchor="rm")
       y += 105

   draw.line((62,885,KART_GENISLIK-62,885), fill=(255,215,92,150), width=2)
   draw.text((62,915), "SWENAX FC", font=_kart_font(24,True), fill=(255,215,92))
   draw.text((KART_GENISLIK-62,915), "PLAYER CARD", font=_kart_font(22,True), fill=(220,225,235), anchor="ra")

   output=io.BytesIO()
   base.convert("RGB").save(output, format="PNG", optimize=True)
   output.seek(0)
   return output


# ================= EKONOM? S?STEM? =================

WORK_JOBS = [
   {"id": "kurye", "name": "Kurye", "min": 180, "max": 420, "emoji": "?"},
   {"id": "garson", "name": "Garson", "min": 150, "max": 350, "emoji": "??"},
   {"id": "yazilimci", "name": "Yaz?l?mc?", "min": 250, "max": 600, "emoji": "?"},
   {"id": "temizlikci", "name": "Temizlikçi", "min": 120, "max": 280, "emoji": "?"},
   {"id": "streamer", "name": "Streamer", "min": 100, "max": 700, "emoji": "?"},
   {"id": "taksici", "name": "Taksici", "min": 200, "max": 450, "emoji": "?"},
   {"id": "asci", "name": "A?ç?", "min": 170, "max": 380, "emoji": "???"},
   {"id": "bekci", "name": "Bekçi", "min": 140, "max": 300, "emoji": "??"},
]

def ekonomi_verisi_al(guild_id, user_id):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       if "para" not in veri:
           veri["para"] = 0
       if "son_daily_para" not in veri:
           veri["son_daily_para"] = 0
       if "son_work" not in veri:
           veri["son_work"] = 0
       return veri


def para_ekle(guild_id, user_id, miktar):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       yeni_para = max(0, int(veri.get("para", 0)) + int(miktar))
       cur.execute(
           "UPDATE seviyeler SET para=%s WHERE guild_id=%s AND user_id=%s RETURNING para",
           (yeni_para, str(guild_id), str(user_id))
       )
       return int(cur.fetchone()["para"])


def daily_para_al(guild_id, user_id):
   if not _db_initialized:
       init_db()
   simdi = time.time()
   bekleme = 24 * 60 * 60

   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       son = float(veri.get("son_daily_para", 0) or 0)
       fark = simdi - son

       if fark < bekleme:
           return False, int(bekleme - fark), 0, int(veri.get("para", 0))

       kazanilan = random.randint(400, 900)
       yeni_para = int(veri.get("para", 0)) + kazanilan

       cur.execute("""
           UPDATE seviyeler 
           SET para=%s, son_daily_para=%s 
           WHERE guild_id=%s AND user_id=%s
           RETURNING para
       """, (yeni_para, simdi, str(guild_id), str(user_id)))
       
       return True, 0, kazanilan, int(cur.fetchone()["para"])


def work_yap(guild_id, user_id, job_id):
   if not _db_initialized:
       init_db()
   simdi = time.time()
   bekleme = 8 * 60  # 8 dakika

   job = next((j for j in WORK_JOBS if j["id"] == job_id), None)
   if not job:
       return False, "Geçersiz meslek.", 0, 0

   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       son = float(veri.get("son_work", 0) or 0)
       fark = simdi - son

       if fark < bekleme:
           return False, int(bekleme - fark), 0, int(veri.get("para", 0))

       kazanilan = random.randint(job["min"], job["max"])
       yeni_para = int(veri.get("para", 0)) + kazanilan

       cur.execute("""
           UPDATE seviyeler 
           SET para=%s, son_work=%s 
           WHERE guild_id=%s AND user_id=%s
           RETURNING para
       """, (yeni_para, simdi, str(guild_id), str(user_id)))

       return True, job, kazanilan, int(cur.fetchone()["para"])


# ================= MARKET + ENVANTER =================

MARKET_ITEMS = {
   "xp_boost": {
       "name": "XP Boost (1 Saat)",
       "price": 2500,
       "emoji": "?",
       "description": "1 saat boyunca %50 daha fazla XP kazan?rs?n.",
       "type": "boost"
   },
   "para_boost": {
       "name": "Para Boost (1 Saat)",
       "price": 3000,
       "emoji": "?",
       "description": "1 saat boyunca work'ten %50 daha fazla para kazan?rs?n.",
       "type": "boost"
   },
   "sansli_kutu": {
       "name": "?ansl? Kutu",
       "price": 1500,
       "emoji": "?",
       "description": "Aç?nca rastgele para veya item ç?kar.",
       "type": "consumable"
   },
   "koruma": {
       "name": "Soygun Korumas?",
       "price": 2000,
       "emoji": "??",
       "description": "1 soyguna kar?? koruma sa?lar.",
       "type": "consumable"
   },
   "vip_rol": {
       "name": "VIP Rolü (30 Gün)",
       "price": 15000,
       "emoji": "?",
       "description": "30 gün boyunca özel VIP rolü al?rs?n.",
       "type": "role"
   }
}


def envanter_al(guild_id, user_id):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       envanter = veri.get("envanter") or {}
       if isinstance(envanter, str):
           envanter = json.loads(envanter)
       return envanter


def item_ekle(guild_id, user_id, item_id, miktar=1):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       envanter = veri.get("envanter") or {}
       if isinstance(envanter, str):
           envanter = json.loads(envanter)

       envanter[item_id] = envanter.get(item_id, 0) + miktar

       cur.execute(
           "UPDATE seviyeler SET envanter=%s WHERE guild_id=%s AND user_id=%s",
           (json.dumps(envanter), str(guild_id), str(user_id))
       )
       return envanter


def item_sil(guild_id, user_id, item_id, miktar=1):
   if not _db_initialized:
       init_db()
   with db_cursor(dict_cursor=True) as (_, cur):
       veri = _kullanici_satiri_kilitle(cur, guild_id, user_id)
       envanter = veri.get("envanter") or {}
       if isinstance(envanter, str):
           envanter = json.loads(envanter)

       mevcut = envanter.get(item_id, 0)
       if mevcut < miktar:
           return False, envanter

       envanter[item_id] = mevcut - miktar
       if envanter[item_id] <= 0:
           del envanter[item_id]

       cur.execute(
           "UPDATE seviyeler SET envanter=%s WHERE guild_id=%s AND user_id=%s",
           (json.dumps(envanter), str(guild_id), str(user_id))
       )
       return True, envanter


async def seviye_mesaj_kanali_al(varsayilan_kanal):
   if SEVIYE_KANAL_ID:
       try:
           kanal_id = int(SEVIYE_KANAL_ID)
       except ValueError:
           print(f"SEVIYE_KANAL_ID geçerli bir say? de?il: {SEVIYE_KANAL_ID}")
           return varsayilan_kanal

       kanal = client.get_channel(kanal_id)
       if kanal is not None:
           return kanal

       try:
           kanal = await client.fetch_channel(kanal_id)
           return kanal
       except discord.NotFound:
           print(f"SEVIYE_KANAL_ID ({SEVIYE_KANAL_ID}) ile e?le?en bir kanal yok.")
       except discord.Forbidden:
           print(f"SEVIYE_KANAL_ID ({SEVIYE_KANAL_ID}) kanal?n? görme izni yok!")
       except Exception as e:
           print(f"SEVIYE_KANAL_ID kanal? çekilemedi: {e}")

   return varsayilan_kanal


async def seviye_rolu_ver(member, yeni_seviye):
   if yeni_seviye % 5 != 0:
       return None

   guild = member.guild
   rol_adi = f"Level {yeni_seviye}"
   rol = discord.utils.get(guild.roles, name=rol_adi)

   if rol is None:
       try:
           rol = await guild.create_role(name=rol_adi, reason="Seviye ödülü rolü")
       except discord.Forbidden:
           print("Rol olu?turma izni yok! Bot'a 'Rolleri Yönet' izni ver.")
           return None
       except Exception as e:
           print(f"Rol olu?turma hatas?: {e}")
           return None

   try:
       await member.add_roles(rol, reason="Seviye atlad?")
   except discord.Forbidden:
       print("Rol verme izni yok! Bot rolünü rol hiyerar?isinde yukar? ta??.")
       return None
   except Exception as e:
       print(f"Rol verme hatas?: {e}")
       return None

   onceki_seviye = yeni_seviye - 5
   if onceki_seviye > 0:
       onceki_rol = discord.utils.get(guild.roles, name=f"Level {onceki_seviye}")
       if onceki_rol and onceki_rol in member.roles:
           try:
               await member.remove_roles(onceki_rol, reason="Yeni seviye rolüyle de?i?tirildi")
           except Exception as e:
               print(f"Eski rol kald?rma hatas?: {e}")

   return rol


def seviye_atlama_embed(member, yeni_seviye, kazanilan_rol=None):
   sonraki_rol_seviye = ((yeni_seviye // 5) + 1) * 5

   if kazanilan_rol:
       rol_satiri = f"You just advanced to **level {yeni_seviye}** and earned {kazanilan_rol.mention} role!"
   else:
       rol_satiri = f"You just advanced to **level {yeni_seviye}**!"

   embed = discord.Embed(
       title=f"{member.display_name} level up!",
       description=(
           f"{rol_satiri}\n"
           f"You'll earn a role when you reach **level {sonraki_rol_seviye}**."
       ),
       color=0x57F287
   )
   embed.set_thumbnail(url=member.display_avatar.url)
   embed.set_footer(text="Seviye Sistemi", icon_url=member.display_avatar.url)
   return embed

TRIVIA_SORULARI = [
   # ==================== AN?ME (280 soru) ====================
   # Buraya senin orijinal TRIVIA_SORULARI listeni koy
]


@client.event
async def on_ready():
   print(f"? Logged in as {client.user} (ID: {client.user.id})")
   try:
       await asyncio.to_thread(init_db)
       def _db_startup_health():
           with db_cursor(dict_cursor=True) as (_, cur):
               cur.execute("SELECT COUNT(*) AS n FROM seviyeler")
               return int(cur.fetchone()['n'])
       level_rows = await asyncio.to_thread(_db_startup_health)
       print(
           f"? Seviye listener aktif - guild_messages={intents.guild_messages} "
           f"- message_content={intents.message_content} - dbRows={level_rows}"
       )
   except Exception as e:
       print(f"? Database ba?lat?lamad?: {type(e).__name__}: {e}")
       print("? Seviye sistemi DB düzelene kadar kay?t yapamaz; di?er bot özellikleri çal??maya devam eder.")
   # Bot yeniden basladiginda o anda seste olan uyeler icin yeni oturum ac.
   try:
       for guild in client.guilds:
           for kanal in guild.voice_channels:
               for member in kanal.members:
                   if not member.bot:
                       await asyncio.to_thread(
                           ses_oturumu_baslat, guild.id, member.id
                       )
   except Exception as e:
       print(f"Ses oturumlari baslatilamadi: {type(e).__name__}: {e}")

   print("Bot haz?r!")
   try:
       synced = await tree.sync()
       print(f"Komutlar sync edildi: {len(synced)} adet")
   except Exception as e:
       print(f"Sync hatas? (önemsiz olabilir): {e}")


@client.event
async def on_message(message):
   if message.author.bot or message.guild is None:
       return

   level_runtime_stats['events'] += 1
   level_runtime_stats['last_user'] = str(message.author.id)
   level_runtime_stats['last_guild'] = str(message.guild.id)

   icerik = message.content.strip()
   guild_id = message.guild.id
   user_id = message.author.id
   if LEVEL_DEBUG:
       print(
           f"? XP event al?nd? - guild={guild_id} - user={user_id} "
           f"- contentLen={len(message.content or '')}"
       )

   if icerik.lower() == "!köledailyxp":
       try:
           uygun, veri, kalan, kazanilan_xp, onceki_seviye = await asyncio.to_thread(
               gunluk_xp_al, guild_id, user_id
           )

           if not uygun:
               saat = kalan // 3600
               dakika = (kalan % 3600) // 60
               await message.channel.send(
                   f"? {message.author.mention}, günlük ödülünü zaten ald?n! "
                   f"Tekrar almak için **{saat} saat {dakika} dakika** beklemen gerekiyor."
               )
               return

           await message.channel.send(
               f"? {message.author.mention}, günlük ödülünü ald?n: **+{kazanilan_xp} XP**!"
           )

           if int(veri["seviye"]) > onceki_seviye:
               kazanilan_rol = await seviye_rolu_ver(message.author, int(veri["seviye"]))
               embed = seviye_atlama_embed(message.author, int(veri["seviye"]), kazanilan_rol)
               hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
               await hedef_kanal.send(embed=embed)
       except Exception as e:
           print(f"? !köledailyxp DB/seviye hatas?: {type(e).__name__}: {e}")
           await message.channel.send(
               "? Seviye veritaban?na ?u anda ula??lam?yor. XP kayb? olmamas? için ödül uygulanmad?."
           )
       return

   if icerik.startswith("!"):
       return

   try:
       veri, onceki_seviye = await asyncio.to_thread(
           seviye_xp_ekle, guild_id, user_id, 5, 1
       )
       level_runtime_stats['saved'] += 1
       if LEVEL_DEBUG:
           print(
               f"? XP kaydedildi - guild={guild_id} - user={user_id} "
               f"- xp={veri['xp']} - level={veri['seviye']} - messages={veri['mesaj_sayisi']}"
           )

       if int(veri["seviye"]) > onceki_seviye:
           kazanilan_rol = await seviye_rolu_ver(message.author, int(veri["seviye"]))
           embed = seviye_atlama_embed(message.author, int(veri["seviye"]), kazanilan_rol)
           hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
           await hedef_kanal.send(embed=embed)
   except Exception as e:
       level_runtime_stats['errors'] += 1
       print(f"? Seviye sistemi DB hatas?: {type(e).__name__}: {e}")



@client.event
async def on_voice_state_update(member, before, after):
   """Ses kanali giris/cikislarini takip eder."""
   if member.bot or member.guild is None:
       return

   try:
       # Kanal degismediyse (mute/deaf vb.) sureyi degistirme.
       if before.channel is None and after.channel is not None:
           await asyncio.to_thread(
               ses_oturumu_baslat, member.guild.id, member.id
           )
       elif before.channel is not None and after.channel is None:
           await asyncio.to_thread(
               ses_oturumu_bitir, member.guild.id, member.id
           )
       elif before.channel is not None and after.channel is not None:
           # Bir ses kanalindan digerine geciste oturum devam eder.
           pass
   except Exception as e:
       print(f"Ses istatistigi hatasi: {type(e).__name__}: {e}")


@tree.command(name="dbdurum", description="Seviye veritaban? ve mesaj dinleyicisinin durumunu gösterir.")
async def dbdurum(interaction: discord.Interaction):
   await interaction.response.defer(ephemeral=True)
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yaln?zca sunucuda kullan?labilir.", ephemeral=True)
           return

       def _health():
           if not _db_initialized:
               init_db()
           with db_cursor(dict_cursor=True) as (_, cur):
               cur.execute("SELECT COUNT(*) AS n FROM seviyeler WHERE guild_id=%s", (str(interaction.guild_id),))
               return int(cur.fetchone()['n'])

       row_count = await asyncio.to_thread(_health)
       await interaction.followup.send(
           "? **Seviye sistemi durumu**\n"
           f"- PostgreSQL: **ba?l?**\n"
           f"- Bu sunucudaki kay?t: **{row_count}**\n"
           f"- Mesaj event'i: **{level_runtime_stats['events']}**\n"
           f"- Ba?ar?l? XP kayd?: **{level_runtime_stats['saved']}**\n"
           f"- XP kay?t hatas?: **{level_runtime_stats['errors']}**\n"
           f"- Mesaj intent: **{intents.guild_messages}**\n"
           f"- Message Content: **{intents.message_content}**",
           ephemeral=True,
       )
   except Exception as e:
       await interaction.followup.send(
           f"? PostgreSQL testi ba?ar?s?z: `{type(e).__name__}: {e}`",
           ephemeral=True,
       )


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
   print(f"Komut hatas?: {error}")
   try:
       if interaction.response.is_done():
           await interaction.followup.send("Bir hata olu?tu, lütfen tekrar dene.", ephemeral=True)
       else:
           await interaction.response.send_message("Bir hata olu?tu, lütfen tekrar dene.", ephemeral=True)
   except:
       pass


@tree.command(name="ask", description="Yapay zekaya soru sorars?n.")
@app_commands.describe(soru="Sorulacak soru")
async def ask(interaction: discord.Interaction, soru: str):
   await interaction.response.defer()
   try:
       if not groq_client:
           await interaction.followup.send("Groq API anahtar? bulunamad?!")
           return

       chat_completion = groq_client.chat.completions.create(
           model="openai/gpt-oss-20b",
           messages=[
               {
                   "role": "system",
                   "content": "Sen kibar, tarafs?z, net ve profesyonel bir Discord asistan?s?n. A??r? samimi hitaplar kullanma, küfür etme, do?rudan ve anla??l?r cevaplar ver."
               },
               {"role": "user", "content": soru},
           ],
       )
       response_text = chat_completion.choices[0].message.content
       if len(response_text) > 2000:
           response_text = response_text[:1993] + "\n..."
       await interaction.followup.send(response_text)
   except Exception as e:
       print(f"/ask hatas?: {e}")
       await interaction.followup.send(f"Bir hata olu?tu: {e}")


@tree.command(name="seviye", description="Seviyeni, XP'ni ve mesaj say?n? gösterir.")
@app_commands.describe(kullanici="Seviyesini görmek istedi?in ki?i (bo? b?rak?rsan kendini gösterir)")
async def seviye(interaction: discord.Interaction, kullanici: discord.Member = None):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yaln?zca bir sunucuda kullan?labilir.")
           return
       hedef = kullanici or interaction.user
       veri = await asyncio.to_thread(kullanici_verisi_al, interaction.guild_id, hedef.id)
       metin = (
           f"? **{hedef.display_name}** için istatistikler\n\n"
           f"? Seviye: **{veri['seviye']}**\n"
           f"? XP: **{veri['xp']} / {veri['sonraki_seviye_xp']}**\n"
           f"? Mesaj say?s?: **{veri['mesaj_sayisi']}**"
       )
       await interaction.followup.send(metin)
   except Exception as e:
       print(f"/seviye hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")



@tree.command(name="kart", description="Futbol istatistik kartini gosterir.")
@app_commands.describe(kullanici="Kartini gormek istedigin kisi")
async def kart(interaction: discord.Interaction, kullanici: discord.Member = None):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yalnizca bir sunucuda kullanilabilir.")
           return
       hedef=kullanici or interaction.user
       veri=await asyncio.to_thread(kullanici_verisi_al, interaction.guild_id, hedef.id)
       ses_suresi,_=await asyncio.to_thread(ses_istatistigi_al, interaction.guild_id, hedef.id)
       tema_data=await asyncio.to_thread(kart_tema_verisi_al, interaction.guild_id, hedef.id)
       tema=await asyncio.to_thread(_kart_resim_bytes_ac, tema_data) if tema_data else None
       kart_resmi=await asyncio.to_thread(futbol_karti_olustur, hedef, veri, ses_suresi, tema)
       await interaction.followup.send(content=f"⚽ **{hedef.display_name}** futbol karti", file=discord.File(kart_resmi, filename="futbol-karti.png"))
   except Exception as e:
       print(f"/kart hatasi: {type(e).__name__}: {e}")
       await interaction.followup.send("Futbol karti olusturulurken bir hata olustu.")


@tree.command(name="karttema", description="Futbol kartinda kullanilacak tema fotografini ayarlar.")
@app_commands.describe(foto="Kartinin arka planinda kullanilacak fotograf")
async def karttema(interaction: discord.Interaction, foto: discord.Attachment):
   await interaction.response.defer(ephemeral=True)
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yalnizca bir sunucuda kullanilabilir.", ephemeral=True)
           return
       izinli={"image/png","image/jpeg","image/webp","image/gif"}
       if foto.content_type not in izinli:
           await interaction.followup.send("Lutfen PNG, JPG, WEBP veya GIF bir fotograf yukle.", ephemeral=True)
           return
       if foto.size > 8*1024*1024:
           await interaction.followup.send("Fotograf 8 MB'dan kucuk olmali.", ephemeral=True)
           return
       tema_bytes = await foto.read()
       if not tema_bytes:
           await interaction.followup.send("Fotograf okunamadi. Lutfen tekrar yukle.", ephemeral=True)
           return
       await asyncio.to_thread(kart_tema_kaydet, interaction.guild_id, interaction.user.id, tema_bytes)
       await interaction.followup.send("✅ Kart temanin kalici olarak kaydedildi. Artik **/kart** yazdiginda bu fotograf arka plan olacak.", ephemeral=True)
   except Exception as e:
       print(f"/karttema hatasi: {type(e).__name__}: {e}")
       await interaction.followup.send("Kart temasi kaydedilirken bir hata olustu.", ephemeral=True)


@tree.command(name="karttemasil", description="Kaydettigin futbol karti temasini kaldirir.")
async def karttemasil(interaction: discord.Interaction):
   await interaction.response.defer(ephemeral=True)
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yalnizca bir sunucuda kullanilabilir.", ephemeral=True)
           return
       await asyncio.to_thread(kart_tema_kaydet, interaction.guild_id, interaction.user.id, None)
       await interaction.followup.send("✅ Kart temasi kaldirildi. Varsayilan tema kullanilacak.", ephemeral=True)
   except Exception as e:
       print(f"/karttemasil hatasi: {type(e).__name__}: {e}")
       await interaction.followup.send("Kart temasi kaldirilirken bir hata olustu.", ephemeral=True)


@tree.command(name="ses", description="Ses kanalinda ne kadar kaldigini gosterir.")
@app_commands.describe(kullanici="Ses suresini gormek istedigin kisi")
async def ses(interaction: discord.Interaction, kullanici: discord.Member = None):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send(
               "Bu komut yalnizca bir sunucuda kullanilabilir."
           )
           return

       hedef = kullanici or interaction.user
       toplam_sure, aktif = await asyncio.to_thread(
           ses_istatistigi_al, interaction.guild_id, hedef.id
       )

       durum = "🟢 Su anda ses kanalinda." if aktif else "⚪ Su anda seste degil."
       metin = (
           f"🔊 **{hedef.display_name} ses istatistikleri**\n\n"
           f"⏱️ Toplam ses suresi: **{saniyeyi_formatla(toplam_sure)}**\n"
           f"{durum}"
       )
       await interaction.followup.send(metin)
   except Exception as e:
       print(f"/ses hatasi: {e}")
       await interaction.followup.send("Ses istatistigi alinirken bir hata olustu.")


@tree.command(name="sesiralama", description="Sunucudaki ses suresi siralamasini gosterir.")
async def sesiralama(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send(
               "Bu komut yalnizca bir sunucuda kullanilabilir."
           )
           return

       ses_verileri = await asyncio.to_thread(
           tum_ses_istatistiklerini_al, interaction.guild_id
       )
       siralanmis = sorted(
           ses_verileri.items(),
           key=lambda item: item[1],
           reverse=True,
       )[:10]

       if not siralanmis:
           await interaction.followup.send("Henuz ses istatistigi bulunmuyor.")
           return

       madalyalar = ["🥇", "🥈", "🥉"]
       satirlar = []
       for i, (uid, toplam_sure) in enumerate(siralanmis):
           sira_simge = madalyalar[i] if i < 3 else f"**{i + 1}.**"
           satirlar.append(
               f"{sira_simge} <@{uid}> - **{saniyeyi_formatla(toplam_sure)}**"
           )

       metin = "🔊 **SES SIRALAMASI** 🔊\n\n" + "\n".join(satirlar)
       await interaction.followup.send(metin)
   except Exception as e:
       print(f"/sesiralama hatasi: {e}")
       await interaction.followup.send("Ses siralamasi alinirken bir hata olustu.")


@tree.command(name="s?ralama", description="Seviye s?ralama tablosunu gösterir (ilk 10 ki?i).")
async def siralama(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut yaln?zca bir sunucuda kullan?labilir.")
           return
       seviye_verileri = await asyncio.to_thread(tum_seviye_verilerini_al, interaction.guild_id)
       if not seviye_verileri:
           await interaction.followup.send("Henüz kimse XP kazanmam??.")
           return 

       siralanmis = sorted(
           seviye_verileri.items(),
           key=lambda item: toplam_xp_hesapla(item[1]),
           reverse=True,
       )[:10]

       madalyalar = ["?", "?", "?"]
       satirlar = []
       for i, (uid, veri) in enumerate(siralanmis):
           sira_simge = madalyalar[i] if i < 3 else f"**{i + 1}.**"
           satirlar.append(
               f"{sira_simge} <@{uid}> - Seviye **{veri['seviye']}** "
               f"({veri['xp']}/{veri['sonraki_seviye_xp']} XP, {veri['mesaj_sayisi']} mesaj)"
           )

       metin = "? **SIRALAMA TABLOSU** ?\n\n" + "\n".join(satirlar)
       await interaction.followup.send(metin)
   except Exception as e:
       print(f"/s?ralama hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="tkm", description="Bot ile Ta?, Ka??t, Makas oynars?n.")
@app_commands.choices(secim=[
   app_commands.Choice(name="Ta?", value="ta?"),
   app_commands.Choice(name="Ka??t", value="ka??t"),
   app_commands.Choice(name="Makas", value="makas")
])
async def tkm(interaction: discord.Interaction, secim: app_commands.Choice[str]):
   await interaction.response.defer()
   try:
       bot_secimi = random.choice(["ta?", "ka??t", "makas"])
       kullanici_secimi = secim.value

       if kullanici_secimi == bot_secimi:
           sonuc = "? **Berabere.**"
       elif ((kullanici_secimi == "ta?" and bot_secimi == "makas") or
             (kullanici_secimi == "ka??t" and bot_secimi == "ta?") or
             (kullanici_secimi == "makas" and bot_secimi == "ka??t")):
           sonuc = "? **Tebrikler, kazand?n?z!**"
       else:
           sonuc = "? **Kaybettiniz.**"

       await interaction.followup.send(f"Seçiminiz: **{kullanici_secimi}**\nBotun seçimi: **{bot_secimi}**\n\n{sonuc}")
   except Exception as e:
       print(f"/tkm hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="tahmin", description="1-100 aras? tutulan say?y? tahmin etme oyunu.")
@app_commands.describe(sayi="1-100 aras? bir say? girin")
async def tahmin(interaction: discord.Interaction, sayi: int):
   await interaction.response.defer()
   try:
       user_id = interaction.user.id
       if user_id not in aktif_oyunlar:
           aktif_oyunlar[user_id] = random.randint(1, 100)

       gizli = aktif_oyunlar[user_id]
       if sayi == gizli:
           del aktif_oyunlar[user_id]
           await interaction.followup.send(f"? **Tebrikler!** Do?ru say? **{gizli}** idi.")
       elif sayi < gizli:
           await interaction.followup.send("? Daha **büyük** bir say? deneyin.")
       else:
           await interaction.followup.send("? Daha **küçük** bir say? deneyin.")
   except Exception as e:
       print(f"/tahmin hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="slot", description="Slot makinesini çevirip ?ans?n?z? denersiniz.")
async def slot(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       semboller = ["?", "?", "?", "?", "?", "?"]
       c1 = random.choice(semboller)
       c2 = random.choice(semboller)
       c3 = random.choice(semboller)

       sonuc_metni = f"? **[ {c1} | {c2} | {c3} ]** ?\n\n"
       if c1 == c2 == c3:
           sonuc_metni += "? **Büyük ?kramiye! Üçlü e?le?ti, kazand?n?z!**"
       elif c1 == c2 or c2 == c3 or c1 == c3:
           sonuc_metni += "? **?kili e?le?ti! Fena de?il.**"
       else:
           sonuc_metni += "? **Kaybettiniz, ?ans?n?z? tekrar deneyin.**"

       await interaction.followup.send(sonuc_metni)
   except Exception as e:
       print(f"/slot hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


class BilgiYarismasiView(discord.ui.View):
   def __init__(self, dogru_cevap, secenekler):
       super().__init__(timeout=30)
       self.dogru_cevap = dogru_cevap
       self.secenekler_listesi = secenekler

   @discord.ui.button(label="A", style=discord.ButtonStyle.blurple)
   async def secenek_a(self, interaction: discord.Interaction, button: discord.ui.Button):
       await self.kontrol(interaction, button.label)

   @discord.ui.button(label="B", style=discord.ButtonStyle.blurple)
   async def secenek_b(self, interaction: discord.Interaction, button: discord.ui.Button):
       await self.kontrol(interaction, button.label)

   @discord.ui.button(label="C", style=discord.ButtonStyle.blurple)
   async def secenek_c(self, interaction: discord.Interaction, button: discord.ui.Button):
       await self.kontrol(interaction, button.label)

   @discord.ui.button(label="D", style=discord.ButtonStyle.blurple)
   async def secenek_d(self, interaction: discord.Interaction, button: discord.ui.Button):
       await self.kontrol(interaction, button.label)

   async def kontrol(self, interaction: discord.Interaction, secilen_harf):
       try:
           harf_index = {"A": 0, "B": 1, "C": 2, "D": 3}
           secilen_metin = self.secenekler_listesi[harf_index[secilen_harf]]

           if secilen_metin == self.dogru_cevap:
               await interaction.response.send_message(
                   f"? **Tebrikler {interaction.user.name}, do?ru cevap!** (`{self.dogru_cevap}`)",
                   ephemeral=False
               )
           else:
               await interaction.response.send_message(
                   f"? **Yanl?? cevap!** Do?ru cevap: **{self.dogru_cevap}** olmal?yd?.",
                   ephemeral=True
               )

           for child in self.children:
               child.disabled = True
           await interaction.message.edit(view=self)
       except Exception as e:
           print(f"View hatas?: {e}")
           try:
               await interaction.response.send_message("Bir hata olu?tu.", ephemeral=True)
           except:
               pass


@tree.command(name="bilgi-yarismasi", description="Butonlu genel kültür bilgi yar??mas? ba?lat?r.")
async def bilgi_yarismasi(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       veri = random.choice(TRIVIA_SORULARI)
       dogru = veri["dogru"]
       secenekler = list(veri["secenekler"])
       random.shuffle(secenekler)

       view = BilgiYarismasiView(dogru, secenekler)

       metin = (
           f"? **B?LG? YARI?MASI**\n\n"
           f"? **Soru:** {veri['soru']}\n\n"
           f"A) {secenekler[0]}\n"
           f"B) {secenekler[1]}\n"
           f"C) {secenekler[2]}\n"
           f"D) {secenekler[3]}\n\n"
           f"*A?a??daki butonlardan do?ru ??kk? seç!*"
       )
       await interaction.followup.send(metin, view=view)
   except Exception as e:
       print(f"/bilgi-yarismasi hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="kasa-ac", description="Gizli bir kasa açarak içinden ne ç?kaca??n? görürsün.")
async def kasa_ac(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       oduller = [
           "Bo? ç?kt?! ?",
           "10 Alt?n kazand?n! ?",
           "Efsanevi K?l?ç ç?kt?! ?",
           "Lanetli Ta? ç?kt?, puan?n silindi! ?",
           "100 Elmas kazand?n! ?",
           "Küçük bir iksir buldun! ?"
       ]
       cikan = random.choice(oduller)
       await interaction.followup.send(f"? **Kasa aç?l?yor...**\n\n?çinden ç?kan: **{cikan}**")
   except Exception as e:
       print(f"/kasa-ac hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="zardüellosu", description="Bot ile zar düellosu yapars?n?z (Büyük atan kazan?r).")
async def zardüellosu(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       oyuncu_zar = random.randint(1, 6)
       bot_zar = random.randint(1, 6)

       metin = f"? Senin att???n zar: **{oyuncu_zar}**\n? Benim att???m zar: **{bot_zar}**\n\n"
       if oyuncu_zar > bot_zar:
           metin += "? **Düelloyu kazand?n!**"
       elif oyuncu_zar < bot_zar:
           metin += "? **Düelloyu kaybettin!**"
       else:
           metin += "? **Zarlar e?it, berabere!**"

       await interaction.followup.send(metin)
   except Exception as e:
       print(f"/zardüellosu hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="yazitura", description="Klasik yaz? tura atma oyunu.")
@app_commands.choices(secim=[
   app_commands.Choice(name="Yaz?", value="yaz?"),
   app_commands.Choice(name="Tura", value="tura")
])
async def yazitura(interaction: discord.Interaction, secim: app_commands.Choice[str]):
   await interaction.response.defer()
   try:
       sonuc = random.choice(["yaz?", "tura"])
       kullanici_secimi = secim.value

       if kullanici_secimi == sonuc:
           durum = f"? Para **{sonuc.upper()}** geldi! Kazand?n?z!"
       else:
           durum = f"? Para **{sonuc.upper()}** geldi! Kaybettiniz."

       await interaction.followup.send(durum)
   except Exception as e:
       print(f"/yazitura hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


# ================= EKONOM? KOMUTLARI =================

class WorkView(discord.ui.View):
   def __init__(self, user_id: int):
       super().__init__(timeout=60)
       self.user_id = user_id

       options = [
           discord.SelectOption(
               label=job["name"],
               value=job["id"],
               emoji=job["emoji"],
               description=f"{job['min']} - {job['max']} ?"
           ) for job in WORK_JOBS
       ]
       self.select = discord.ui.Select(placeholder="Meslek seç...", options=options)
       self.select.callback = self.job_secildi
       self.add_item(self.select)

   async def job_secildi(self, interaction: discord.Interaction):
       if interaction.user.id != self.user_id:
           await interaction.response.send_message("Bu menü sana ait de?il!", ephemeral=True)
           return

       job_id = self.select.values[0]
       await interaction.response.defer()

       try:
           basarili, data, kazanilan, yeni_para = await asyncio.to_thread(
               work_yap, interaction.guild_id, interaction.user.id, job_id
           )

           if not basarili:
               if isinstance(data, int):
                   dakika = data // 60
                   saniye = data % 60
                   await interaction.followup.send(
                       f"? Daha **{dakika} dakika {saniye} saniye** beklemelisin.",
                       ephemeral=True
                   )
               else:
                   await interaction.followup.send(f"? {data}", ephemeral=True)
               return

           job = data
           embed = discord.Embed(
               title=f"{job['emoji']} {job['name']} olarak çal??t?n!",
               description=f"**+{kazanilan} ?** kazand?n.\nYeni bakiyen: **{yeni_para} ?**",
               color=0x57F287
           )
           await interaction.followup.send(embed=embed)

           for item in self.children:
               item.disabled = True
           await interaction.message.edit(view=self)

       except Exception as e:
           print(f"/work hatas?: {e}")
           await interaction.followup.send("Bir hata olu?tu.", ephemeral=True)


@tree.command(name="bal", description="Paran? veya ba?kas?n?n paras?n? gösterir.")
@app_commands.describe(kullanici="Bakmak istedi?in ki?i (bo? b?rak?rsan kendini gösterir)")
async def bal(interaction: discord.Interaction, kullanici: discord.Member = None):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       hedef = kullanici or interaction.user
       veri = await asyncio.to_thread(ekonomi_verisi_al, interaction.guild_id, hedef.id)
       para = int(veri.get("para", 0))

       embed = discord.Embed(
           title=f"? {hedef.display_name} bakiyesi",
           description=f"**{para} ?**",
           color=0xFEE75C
       )
       await interaction.followup.send(embed=embed)
   except Exception as e:
       print(f"/bal hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="daily", description="Günlük para ödülünü al?rs?n.")
async def daily(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       basarili, kalan, kazanilan, yeni_para = await asyncio.to_thread(
           daily_para_al, interaction.guild_id, interaction.user.id
       )

       if not basarili:
           saat = kalan // 3600
           dakika = (kalan % 3600) // 60
           await interaction.followup.send(
               f"? Günlük ödülünü zaten ald?n!\nTekrar almak için **{saat} saat {dakika} dakika** beklemelisin."
           )
           return

       embed = discord.Embed(
           title="? Günlük Ödül",
           description=f"**+{kazanilan} ?** kazand?n!\nYeni bakiyen: **{yeni_para} ?**",
           color=0x57F287
       )
       await interaction.followup.send(embed=embed)
   except Exception as e:
       print(f"/daily hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="work", description="Meslek seçip çal??arak para kazan?rs?n.")
async def work(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       veri = await asyncio.to_thread(ekonomi_verisi_al, interaction.guild_id, interaction.user.id)
       son = float(veri.get("son_work", 0) or 0)
       kalan = (8 * 60) - (time.time() - son)

       if kalan > 0:
           dakika = int(kalan) // 60
           saniye = int(kalan) % 60
           await interaction.followup.send(
               f"? Daha **{dakika} dakika {saniye} saniye** beklemelisin.",
               ephemeral=True
           )
           return

       view = WorkView(interaction.user.id)
       embed = discord.Embed(
           title="? Meslek Seç",
           description="A?a??dan çal??mak istedi?in mesle?i seç:",
           color=0x5865F2
       )
       await interaction.followup.send(embed=embed, view=view)
   except Exception as e:
       print(f"/work hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="pay", description="Ba?ka birine para gönderirsin.")
@app_commands.describe(kullanici="Para gönderece?in ki?i", miktar="Göndermek istedi?in miktar")
async def pay(interaction: discord.Interaction, kullanici: discord.Member, miktar: int):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       if kullanici.id == interaction.user.id:
           await interaction.followup.send("Kendine para gönderemezsin.")
           return
       if miktar <= 0:
           await interaction.followup.send("Miktar 0'dan büyük olmal?.")
           return

       gonderen = await asyncio.to_thread(ekonomi_verisi_al, interaction.guild_id, interaction.user.id)
       if int(gonderen.get("para", 0)) < miktar:
           await interaction.followup.send("Yeterli paran yok.")
           return

       await asyncio.to_thread(para_ekle, interaction.guild_id, interaction.user.id, -miktar)
       await asyncio.to_thread(para_ekle, interaction.guild_id, kullanici.id, miktar)

       embed = discord.Embed(
           title="? Para Gönderildi",
           description=f"{interaction.user.mention} ? {kullanici.mention}\n**{miktar} ?** gönderildi.",
           color=0x57F287
       )
       await interaction.followup.send(embed=embed)
   except Exception as e:
       print(f"/pay hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="zenginler", description="Sunucudaki en zengin 10 ki?iyi gösterir.")
async def zenginler(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       def _get_top():
           if not _db_initialized:
               init_db()
           with db_cursor(dict_cursor=True) as (_, cur):
               cur.execute("""
                   SELECT user_id, para FROM seviyeler 
                   WHERE guild_id=%s 
                   ORDER BY para DESC 
                   LIMIT 10
               """, (str(interaction.guild_id),))
               return cur.fetchall()

       rows = await asyncio.to_thread(_get_top)
       if not rows:
           await interaction.followup.send("Henüz kimse para kazanmam??.")
           return

       madalyalar = ["?", "?", "?"]
       satirlar = []
       for i, row in enumerate(rows):
           sira = madalyalar[i] if i < 3 else f"**{i+1}.**"
           satirlar.append(f"{sira} <@{row['user_id']}> - **{row['para']} ?**")

       embed = discord.Embed(
           title="? En Zenginler",
           description="\n".join(satirlar),
           color=0xFEE75C
       )
       await interaction.followup.send(embed=embed)
   except Exception as e:
       print(f"/zenginler hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


# ================= MARKET KOMUTLARI =================

class MarketView(discord.ui.View):
   def __init__(self, user_id: int):
       super().__init__(timeout=60)
       self.user_id = user_id

       options = []
       for item_id, item in MARKET_ITEMS.items():
           options.append(
               discord.SelectOption(
                   label=f"{item['name']} - {item['price']} ?",
                   value=item_id,
                   emoji=item["emoji"],
                   description=item["description"][:50]
               )
           )

       self.select = discord.ui.Select(placeholder="Sat?n almak istedi?in ürünü seç...", options=options)
       self.select.callback = self.urun_secildi
       self.add_item(self.select)

   async def urun_secildi(self, interaction: discord.Interaction):
       if interaction.user.id != self.user_id:
           await interaction.response.send_message("Bu menü sana ait de?il!", ephemeral=True)
           return

       item_id = self.select.values[0]
       item = MARKET_ITEMS[item_id]

       await interaction.response.defer()

       try:
           veri = await asyncio.to_thread(ekonomi_verisi_al, interaction.guild_id, interaction.user.id)
           para = int(veri.get("para", 0))

           if para < item["price"]:
               await interaction.followup.send(f"? Yeterli paran yok! Gerekli: **{item['price']} ?**", ephemeral=True)
               return

           await asyncio.to_thread(para_ekle, interaction.guild_id, interaction.user.id, -item["price"])
           await asyncio.to_thread(item_ekle, interaction.guild_id, interaction.user.id, item_id)

           embed = discord.Embed(
               title="? Sat?n Alma Ba?ar?l?",
               description=f"{item['emoji']} **{item['name']}** envanterine eklendi!\nÖdenen: **{item['price']} ?**",
               color=0x57F287
           )
           await interaction.followup.send(embed=embed)

       except Exception as e:
           print(f"/market hatas?: {e}")
           await interaction.followup.send("Bir hata olu?tu.", ephemeral=True)


@tree.command(name="market", description="Marketten ürün sat?n al?rs?n.")
async def market(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       embed = discord.Embed(
           title="? Market",
           description="A?a??dan sat?n almak istedi?in ürünü seç:",
           color=0x5865F2
       )

       for item_id, item in MARKET_ITEMS.items():
           embed.add_field(
               name=f"{item['emoji']} {item['name']}",
               value=f"**{item['price']} ?**\n{item['description']}",
               inline=False
           )

       view = MarketView(interaction.user.id)
       await interaction.followup.send(embed=embed, view=view)

   except Exception as e:
       print(f"/market hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="envanter", description="Envanterini gösterir.")
async def envanter(interaction: discord.Interaction):
   await interaction.response.defer()
   try:
       if interaction.guild_id is None:
           await interaction.followup.send("Bu komut sadece sunucuda kullan?labilir.")
           return

       envanter_data = await asyncio.to_thread(envanter_al, interaction.guild_id, interaction.user.id)

       if not envanter_data:
           await interaction.followup.send("Envanterin bo?.")
           return

       embed = discord.Embed(
           title=f"? {interaction.user.display_name} Envanteri",
           color=0xFEE75C
       )

       for item_id, miktar in envanter_data.items():
           item = MARKET_ITEMS.get(item_id)
           if item:
               embed.add_field(
                   name=f"{item['emoji']} {item['name']}",
                   value=f"Adet: **{miktar}**",
                   inline=True
               )
           else:
               embed.add_field(name=item_id, value=f"Adet: **{miktar}**", inline=True)

       await interaction.followup.send(embed=embed)

   except Exception as e:
       print(f"/envanter hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.")


@tree.command(name="help", description="Botun tüm komutlar?n? ve ne i?e yarad?klar?n? gösterir.")
async def help_komutu(interaction: discord.Interaction):
   await interaction.response.defer(ephemeral=True)
   try:
       embed = discord.Embed(
           title="? Komut Listesi",
           description="Botun sahip oldu?u tüm komutlar ve aç?klamalar? a?a??dad?r.",
           color=0x5865F2
       )

       embed.add_field(
           name="? Seviye Sistemi",
           value=(
               "**/seviye [kullan?c?]** - Seviyeni, XP'ni ve mesaj say?n? gösterir.\n"
               "**/s?ralama** - Sunucudaki ilk 10 ki?inin seviye s?ralamas?n? gösterir.\n"
               "**/ses [kullan?c?]** - Toplam ses süresini gösterir.\n"
               "**/sesiralama** - Sunucudaki ses süresi sıralamasını gösterir.\n"
               "**/kart [kullan?c?]** - Futbol kartini, reytingini ve istatistiklerini gosterir.\n"
               "**/karttema [foto]** - Kartinin arka plan fotografini ayarlar.\n"
               "**/karttemasil** - Kayitli kart temasini kaldirir.\n"
               "**!köledailyxp** - Günlük XP ödülünü toplar (24 saatte bir).\n"
               "**/dbdurum** - Seviye veritaban?n?n ve mesaj dinleyicisinin durumunu gösterir."
           ),
           inline=False
       )

       embed.add_field(
           name="? Ekonomi",
           value=(
               "**/bal [kullan?c?]** - Paran? gösterir.\n"
               "**/daily** - Günlük para ödülünü al?rs?n.\n"
               "**/work** - Meslek seçip çal??arak para kazan?rs?n.\n"
               "**/pay** - Ba?kas?na para gönderirsin.\n"
               "**/zenginler** - En zengin 10 ki?iyi gösterir.\n"
               "**/market** - Marketten ürün sat?n al?rs?n.\n"
               "**/envanter** - Envanterini gösterir."
           ),
           inline=False
       )

       embed.add_field(
           name="? Oyunlar",
           value=(
               "**/tkm** - Bot ile Ta?, Ka??t, Makas oynars?n.\n"
               "**/tahmin [say?]** - 1-100 aras? tutulan say?y? tahmin etme oyunu.\n"
               "**/slot** - Slot makinesini çevirip ?ans?n? denersin.\n"
               "**/bilgi-yarismasi** - Butonlu genel kültür bilgi yar??mas? ba?lat?r.\n"
               "**/kasa-ac** - Gizli bir kasa açarak içinden ne ç?kaca??n? görürsün.\n"
               "**/zardüellosu** - Bot ile zar düellosu yapars?n (büyük atan kazan?r).\n"
               "**/yazitura** - Klasik yaz? tura atma oyunu."
           ),
           inline=False
       )

       embed.add_field(
           name="? Yapay Zeka",
           value="**/ask [soru]** - Yapay zekaya soru sorars?n.",
           inline=False
       )

       embed.set_footer(text="Sadece sen görebilirsin.")
       await interaction.followup.send(embed=embed, ephemeral=True)
   except Exception as e:
       print(f"/help hatas?: {e}")
       await interaction.followup.send("Bir hata olu?tu.", ephemeral=True)


keep_alive()

if __name__ == "__main__":
   if not DISCORD_TOKEN:
       print("HATA: Discord Token bulunamad?!")
   else:
       print("Bot ba?lat?l?yor, 8 saniye bekleniyor (rate limit önlemi)...")
       time.sleep(8)
       client.run(DISCORD_TOKEN)