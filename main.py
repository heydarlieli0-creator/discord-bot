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
from flask import Flask
import discord
from discord import app_commands
from groq import Groq

# Flask ile 7/24 aktif tutma
app = Flask('')

@app.route('/')
def home():
    return "Bot aktif ve çalışıyor!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# API Anahtarları
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DISCORD_TOKEN = os.environ.get("Discord_Token") or os.environ.get("DISCORD_TOKEN")
SEVIYE_KANAL_ID = os.environ.get("SEVIYE_KANAL_ID", "1533423499505307698")

if not DISCORD_TOKEN:
    print("❌ HATA: Discord Token bulunamadı!")
if not GROQ_API_KEY:
    print("⚠ UYARI: GROQ_API_KEY bulunamadı. /ask komutu çalışmaz.")

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

# ================= SEVİYE SİSTEMİ =================
SEVIYE_DOSYASI = "seviyeler.json"
BASLANGIC_SEVIYE_XP = 300
SEVIYE_XP_ARTISI = 300


# ================= SEVİYE SİSTEMİ (PostgreSQL) =================
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
            "DATABASE_URL tanımlı değil. PostgreSQL bağlantı adresini environment değişkenine ekle."
        )


def get_db_pool():
    """Thread-safe PostgreSQL connection pool'u lazy olarak oluşturur."""
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
    """Pool doluysa kısa süre bekler; anlık yoğunlukta XP kaybını önler."""
    if not _db_conn_semaphore.acquire(timeout=10):
        raise TimeoutError("PostgreSQL connection pool 10 saniye içinde boşalmadı")
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
    """Bağlantıyı pool'dan alır; commit/rollback ve iade işlemini garanti eder."""
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
    """Seviye tablosunu oluşturur ve eski user_id-only şemayı güvenle migrate eder."""
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
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

            # Eski sürümde guild_id yoktu ve PRIMARY KEY yalnız user_id idi.
            cur.execute("ALTER TABLE seviyeler ADD COLUMN IF NOT EXISTS guild_id TEXT")
            cur.execute("UPDATE seviyeler SET guild_id='0' WHERE guild_id IS NULL OR guild_id='' ")
            cur.execute("ALTER TABLE seviyeler ALTER COLUMN guild_id SET NOT NULL")

            # Eski PK yapısını yalnız gerekiyorsa composite PK'ye dönüştür.
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
                    # Constraint adı PostgreSQL tarafından üretildiği için identifier olarak quote edilir.
                    safe_pk_name = str(pk[0]).replace('"', '""')
                    cur.execute(f'ALTER TABLE seviyeler DROP CONSTRAINT "{safe_pk_name}"')
                cur.execute("ALTER TABLE seviyeler ADD PRIMARY KEY (guild_id, user_id)")

            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seviyeler_guild_rank "
                "ON seviyeler (guild_id, seviye DESC, xp DESC)"
            )

        _db_initialized = True
        print(f"✅ Database tablosu hazır • pool={DB_MIN_CONN}-{DB_MAX_CONN} • guild-scoped=true")
        return True

def _kullanici_satiri_kilitle(cur, guild_id, user_id):
    """Kullanıcı satırını transaction içinde FOR UPDATE ile kilitler."""
    gid = str(guild_id)
    uid = str(user_id)

    cur.execute(
        "SELECT * FROM seviyeler WHERE guild_id=%s AND user_id=%s FOR UPDATE",
        (gid, uid),
    )
    row = cur.fetchone()
    if row is not None:
        return dict(row)

    # Eski sürümden kalan global kaydı ilk gerçek sunucuda kaybetmeden devral.
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
        raise RuntimeError("Seviye kullanıcı satırı oluşturulamadı")
    return dict(row)


def kullanici_verisi_al(guild_id, user_id):
    """Kullanıcı verisini getirir. DB hatasında sahte 0 XP döndürmez."""
    try:
        if not _db_initialized:
            init_db()
        with db_cursor(dict_cursor=True) as (_, cur):
            return _kullanici_satiri_kilitle(cur, guild_id, user_id)
    except Exception as e:
        print(f"❌ kullanici_verisi_al hatası: {type(e).__name__}: {e}")
        raise


def seviye_verisi_kaydet(guild_id, user_id, veri):
    """Uyumluluk amaçlı güvenli UPSERT. Başarısız kayıt sessizce yutulmaz."""
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
        print(f"❌ seviye_verisi_kaydet hatası: {type(e).__name__}: {e}")
        raise


def seviye_xp_ekle(guild_id, user_id, xp_miktari, mesaj_artisi=0, son_daily=None):
    """XP artışını tek transaction içinde atomik olarak uygular."""
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
    """Daily cooldown kontrolünü ve XP kaydını aynı transaction içinde yapar."""
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
    """Yalnız ilgili Discord sunucusunun sıralamasını getirir."""
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
        print(f"❌ tum_seviye_verilerini_al hatası: {type(e).__name__}: {e}")
        raise

def toplam_xp_hesapla(veri):
    seviye = veri["seviye"]
    return 300 * (seviye - 1) * seviye // 2 + veri["xp"]


async def seviye_mesaj_kanali_al(varsayilan_kanal):
    if SEVIYE_KANAL_ID:
        try:
            kanal_id = int(SEVIYE_KANAL_ID)
        except ValueError:
            print(f"SEVIYE_KANAL_ID geçerli bir sayı değil: {SEVIYE_KANAL_ID}")
            return varsayilan_kanal

        kanal = client.get_channel(kanal_id)
        if kanal is not None:
            return kanal

        try:
            kanal = await client.fetch_channel(kanal_id)
            return kanal
        except discord.NotFound:
            print(f"SEVIYE_KANAL_ID ({SEVIYE_KANAL_ID}) ile eşleşen bir kanal yok.")
        except discord.Forbidden:
            print(f"SEVIYE_KANAL_ID ({SEVIYE_KANAL_ID}) kanalını görme izni yok!")
        except Exception as e:
            print(f"SEVIYE_KANAL_ID kanalı çekilemedi: {e}")

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
            print("Rol oluşturma izni yok! Bot'a 'Rolleri Yönet' izni ver.")
            return None
        except Exception as e:
            print(f"Rol oluşturma hatası: {e}")
            return None

    try:
        await member.add_roles(rol, reason="Seviye atladı")
    except discord.Forbidden:
        print("Rol verme izni yok! Bot rolünü rol hiyerarşisinde yukarı taşı.")
        return None
    except Exception as e:
        print(f"Rol verme hatası: {e}")
        return None

    onceki_seviye = yeni_seviye - 5
    if onceki_seviye > 0:
        onceki_rol = discord.utils.get(guild.roles, name=f"Level {onceki_seviye}")
        if onceki_rol and onceki_rol in member.roles:
            try:
                await member.remove_roles(onceki_rol, reason="Yeni seviye rolüyle değiştirildi")
            except Exception as e:
                print(f"Eski rol kaldırma hatası: {e}")

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

TRIVIA_SORULARI = "TRIVIA_PLACEHOLDER"


@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
    try:
        await asyncio.to_thread(init_db)
        def _db_startup_health():
            with db_cursor(dict_cursor=True) as (_, cur):
                cur.execute("SELECT COUNT(*) AS n FROM seviyeler")
                return int(cur.fetchone()['n'])
        level_rows = await asyncio.to_thread(_db_startup_health)
        print(
            f"✅ Seviye listener aktif • guild_messages={intents.guild_messages} "
            f"• message_content={intents.message_content} • dbRows={level_rows}"
        )
    except Exception as e:
        print(f"❌ Database başlatılamadı: {type(e).__name__}: {e}")
        print("⚠ Seviye sistemi DB düzelene kadar kayıt yapamaz; diğer bot özellikleri çalışmaya devam eder.")
    print("Bot hazır!")
    try:
        synced = await tree.sync()
        print(f"Komutlar sync edildi: {len(synced)} adet")
    except Exception as e:
        print(f"Sync hatası (önemsiz olabilir): {e}")


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
            f"💬 XP event alındı • guild={guild_id} • user={user_id} "
            f"• contentLen={len(message.content or '')}"
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
                    f"⏳ {message.author.mention}, günlük ödülünü zaten aldın! "
                    f"Tekrar almak için **{saat} saat {dakika} dakika** beklemen gerekiyor."
                )
                return

            await message.channel.send(
                f"🎁 {message.author.mention}, günlük ödülünü aldın: **+{kazanilan_xp} XP**!"
            )

            if int(veri["seviye"]) > onceki_seviye:
                kazanilan_rol = await seviye_rolu_ver(message.author, int(veri["seviye"]))
                embed = seviye_atlama_embed(message.author, int(veri["seviye"]), kazanilan_rol)
                hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
                await hedef_kanal.send(embed=embed)
        except Exception as e:
            print(f"❌ !köledailyxp DB/seviye hatası: {type(e).__name__}: {e}")
            await message.channel.send(
                "⚠ Seviye veritabanına şu anda ulaşılamıyor. XP kaybı olmaması için ödül uygulanmadı."
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
                f"✅ XP kaydedildi • guild={guild_id} • user={user_id} "
                f"• xp={veri['xp']} • level={veri['seviye']} • messages={veri['mesaj_sayisi']}"
            )

        if int(veri["seviye"]) > onceki_seviye:
            kazanilan_rol = await seviye_rolu_ver(message.author, int(veri["seviye"]))
            embed = seviye_atlama_embed(message.author, int(veri["seviye"]), kazanilan_rol)
            hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
            await hedef_kanal.send(embed=embed)
    except Exception as e:
        level_runtime_stats['errors'] += 1
        print(f"❌ Seviye sistemi DB hatası: {type(e).__name__}: {e}")


@tree.command(name="dbdurum", description="Seviye veritabanı ve mesaj dinleyicisinin durumunu gösterir.")
async def dbdurum(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if interaction.guild_id is None:
            await interaction.followup.send("Bu komut yalnızca sunucuda kullanılabilir.", ephemeral=True)
            return

        def _health():
            if not _db_initialized:
                init_db()
            with db_cursor(dict_cursor=True) as (_, cur):
                cur.execute("SELECT COUNT(*) AS n FROM seviyeler WHERE guild_id=%s", (str(interaction.guild_id),))
                return int(cur.fetchone()['n'])

        row_count = await asyncio.to_thread(_health)
        await interaction.followup.send(
            "🧪 **Seviye sistemi durumu**\n"
            f"• PostgreSQL: **bağlı**\n"
            f"• Bu sunucudaki kayıt: **{row_count}**\n"
            f"• Mesaj event'i: **{level_runtime_stats['events']}**\n"
            f"• Başarılı XP kaydı: **{level_runtime_stats['saved']}**\n"
            f"• XP kayıt hatası: **{level_runtime_stats['errors']}**\n"
            f"• Mesaj intent: **{intents.guild_messages}**\n"
            f"• Message Content: **{intents.message_content}**",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ PostgreSQL testi başarısız: `{type(e).__name__}: {e}`",
            ephemeral=True,
        )


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Komut hatası: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Bir hata oluştu, lütfen tekrar dene.", ephemeral=True)
        else:
            await interaction.response.send_message("Bir hata oluştu, lütfen tekrar dene.", ephemeral=True)
    except:
        pass


@tree.command(name="ask", description="Yapay zekaya soru sorarsın.")
@app_commands.describe(soru="Sorulacak soru")
async def ask(interaction: discord.Interaction, soru: str):
    await interaction.response.defer()
    try:
        if not groq_client:
            await interaction.followup.send("Groq API anahtarı bulunamadı!")
            return

        chat_completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                    "content": "Sen kibar, tarafsız, net ve profesyonel bir Discord asistanısın. Aşırı samimi hitaplar kullanma, küfür etme, doğrudan ve anlaşılır cevaplar ver."
                },
                {"role": "user", "content": soru},
            ],
        )
        response_text = chat_completion.choices[0].message.content
        if len(response_text) > 2000:
            response_text = response_text[:1993] + "\n..."
        await interaction.followup.send(response_text)
    except Exception as e:
        print(f"/ask hatası: {e}")
        await interaction.followup.send(f"Bir hata oluştu: {e}")


@tree.command(name="seviye", description="Seviyeni, XP'ni ve mesaj sayını gösterir.")
@app_commands.describe(kullanici="Seviyesini görmek istediğin kişi (boş bırakırsan kendini gösterir)")
async def seviye(interaction: discord.Interaction, kullanici: discord.Member = None):
    await interaction.response.defer()
    try:
        if interaction.guild_id is None:
            await interaction.followup.send("Bu komut yalnızca bir sunucuda kullanılabilir.")
            return
        hedef = kullanici or interaction.user
        veri = await asyncio.to_thread(kullanici_verisi_al, interaction.guild_id, hedef.id)
        metin = (
            f"📊 **{hedef.display_name}** için istatistikler\n\n"
            f"🏆 Seviye: **{veri['seviye']}**\n"
            f"✨ XP: **{veri['xp']} / {veri['sonraki_seviye_xp']}**\n"
            f"💬 Mesaj sayısı: **{veri['mesaj_sayisi']}**"
        )
        await interaction.followup.send(metin)
    except Exception as e:
        print(f"/seviye hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="sıralama", description="Seviye sıralama tablosunu gösterir (ilk 10 kişi).")
async def siralama(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        if interaction.guild_id is None:
            await interaction.followup.send("Bu komut yalnızca bir sunucuda kullanılabilir.")
            return
        seviye_verileri = await asyncio.to_thread(tum_seviye_verilerini_al, interaction.guild_id)
        if not seviye_verileri:
            await interaction.followup.send("Henüz kimse XP kazanmamış.")
            return 

        siralanmis = sorted(
            seviye_verileri.items(),
            key=lambda item: toplam_xp_hesapla(item[1]),
            reverse=True,
        )[:10]

        madalyalar = ["🥇", "🥈", "🥉"]
        satirlar = []
        for i, (uid, veri) in enumerate(siralanmis):
            sira_simge = madalyalar[i] if i < 3 else f"**{i + 1}.**"
            satirlar.append(
                f"{sira_simge} <@{uid}> — Seviye **{veri['seviye']}** "
                f"({veri['xp']}/{veri['sonraki_seviye_xp']} XP, {veri['mesaj_sayisi']} mesaj)"
            )

        metin = "🏆 **SIRALAMA TABLOSU** 🏆\n\n" + "\n".join(satirlar)
        await interaction.followup.send(metin)
    except Exception as e:
        print(f"/sıralama hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="tkm", description="Bot ile Taş, Kağıt, Makas oynarsın.")
@app_commands.choices(secim=[
    app_commands.Choice(name="Taş", value="taş"),
    app_commands.Choice(name="Kağıt", value="kağıt"),
    app_commands.Choice(name="Makas", value="makas")
])
async def tkm(interaction: discord.Interaction, secim: app_commands.Choice[str]):
    await interaction.response.defer()
    try:
        bot_secimi = random.choice(["taş", "kağıt", "makas"])
        kullanici_secimi = secim.value

        if kullanici_secimi == bot_secimi:
            sonuc = "🤝 **Berabere.**"
        elif ((kullanici_secimi == "taş" and bot_secimi == "makas") or
              (kullanici_secimi == "kağıt" and bot_secimi == "taş") or
              (kullanici_secimi == "makas" and bot_secimi == "kağıt")):
            sonuc = "🎉 **Tebrikler, kazandınız!**"
        else:
            sonuc = "😢 **Kaybettiniz.**"

        await interaction.followup.send(f"Seçiminiz: **{kullanici_secimi}**\nBotun seçimi: **{bot_secimi}**\n\n{sonuc}")
    except Exception as e:
        print(f"/tkm hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="tahmin", description="1-100 arası tutulan sayıyı tahmin etme oyunu.")
@app_commands.describe(sayi="1-100 arası bir sayı girin")
async def tahmin(interaction: discord.Interaction, sayi: int):
    await interaction.response.defer()
    try:
        user_id = interaction.user.id
        if user_id not in aktif_oyunlar:
            aktif_oyunlar[user_id] = random.randint(1, 100)

        gizli = aktif_oyunlar[user_id]
        if sayi == gizli:
            del aktif_oyunlar[user_id]
            await interaction.followup.send(f"🎉 **Tebrikler!** Doğru sayı **{gizli}** idi.")
        elif sayi < gizli:
            await interaction.followup.send("📈 Daha **büyük** bir sayı deneyin.")
        else:
            await interaction.followup.send("📉 Daha **küçük** bir sayı deneyin.")
    except Exception as e:
        print(f"/tahmin hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="slot", description="Slot makinesini çevirip şansınızı denersiniz.")
async def slot(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        semboller = ["🍒", "🍋", "🍊", "🔔", "⭐", "💎"]
        c1 = random.choice(semboller)
        c2 = random.choice(semboller)
        c3 = random.choice(semboller)

        sonuc_metni = f"🎰 **[ {c1} | {c2} | {c3} ]** 🎰\n\n"
        if c1 == c2 == c3:
            sonuc_metni += "🏆 **Büyük İkramiye! Üçlü eşleşti, kazandınız!**"
        elif c1 == c2 or c2 == c3 or c1 == c3:
            sonuc_metni += "✨ **İkili eşleşti! Fena değil.**"
        else:
            sonuc_metni += "❌ **Kaybettiniz, şansınızı tekrar deneyin.**"

        await interaction.followup.send(sonuc_metni)
    except Exception as e:
        print(f"/slot hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


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
                    f"🎉 **Tebrikler {interaction.user.name}, doğru cevap!** (`{self.dogru_cevap}`)",
                    ephemeral=False
                )
            else:
                await interaction.response.send_message(
                    f"❌ **Yanlış cevap!** Doğru cevap: **{self.dogru_cevap}** olmalıydı.",
                    ephemeral=True
                )

            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
        except Exception as e:
            print(f"View hatası: {e}")
            try:
                await interaction.response.send_message("Bir hata oluştu.", ephemeral=True)
            except:
                pass


@tree.command(name="bilgi-yarismasi", description="Butonlu genel kültür bilgi yarışması başlatır.")
async def bilgi_yarismasi(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        veri = random.choice(TRIVIA_SORULARI)
        dogru = veri["dogru"]
        secenekler = list(veri["secenekler"])
        random.shuffle(secenekler)

        view = BilgiYarismasiView(dogru, secenekler)

        metin = (
            f"🧠 **BİLGİ YARIŞMASI**\n\n"
            f"❓ **Soru:** {veri['soru']}\n\n"
            f"A) {secenekler[0]}\n"
            f"B) {secenekler[1]}\n"
            f"C) {secenekler[2]}\n"
            f"D) {secenekler[3]}\n\n"
            f"*Aşağıdaki butonlardan doğru şıkkı seç!*"
        )
        await interaction.followup.send(metin, view=view)
    except Exception as e:
        print(f"/bilgi-yarismasi hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="kasa-ac", description="Gizli bir kasa açarak içinden ne çıkacağını görürsün.")
async def kasa_ac(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        oduller = [
            "Boş çıktı! 🕸",
            "10 Altın kazandın! 🪙",
            "Efsanevi Kılıç çıktı! 🗡",
            "Lanetli Taş çıktı, puanın silindi! 💀",
            "100 Elmas kazandın! 💎",
            "Küçük bir iksir buldun! 🧪"
        ]
        cikan = random.choice(oduller)
        await interaction.followup.send(f"📦 **Kasa açılıyor...**\n\nİçinden çıkan: **{cikan}**")
    except Exception as e:
        print(f"/kasa-ac hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="zardüellosu", description="Bot ile zar düellosu yaparsınız (Büyük atan kazanır).")
async def zardüellosu(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        oyuncu_zar = random.randint(1, 6)
        bot_zar = random.randint(1, 6)

        metin = f"🎲 Senin attığın zar: **{oyuncu_zar}**\n🤖 Benim attığım zar: **{bot_zar}**\n\n"
        if oyuncu_zar > bot_zar:
            metin += "🎉 **Düelloyu kazandın!**"
        elif oyuncu_zar < bot_zar:
            metin += "😢 **Düelloyu kaybettin!**"
        else:
            metin += "🤝 **Zarlar eşit, berabere!**"

        await interaction.followup.send(metin)
    except Exception as e:
        print(f"/zardüellosu hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="yazitura", description="Klasik yazı tura atma oyunu.")
@app_commands.choices(secim=[
    app_commands.Choice(name="Yazı", value="yazı"),
    app_commands.Choice(name="Tura", value="tura")
])
async def yazitura(interaction: discord.Interaction, secim: app_commands.Choice[str]):
    await interaction.response.defer()
    try:
        sonuc = random.choice(["yazı", "tura"])
        kullanici_secimi = secim.value

        if kullanici_secimi == sonuc:
            durum = f"🪙 Para **{sonuc.upper()}** geldi! Kazandınız!"
        else:
            durum = f"🪙 Para **{sonuc.upper()}** geldi! Kaybettiniz."

        await interaction.followup.send(durum)
    except Exception as e:
        print(f"/yazitura hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.")


@tree.command(name="help", description="Botun tüm komutlarını ve ne işe yaradıklarını gösterir.")
async def help_komutu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        embed = discord.Embed(
            title="📖 Komut Listesi",
            description="Botun sahip olduğu tüm komutlar ve açıklamaları aşağıdadır.",
            color=0x5865F2
        )

        embed.add_field(
            name="🏅 Seviye Sistemi",
            value=(
                "**/seviye [kullanıcı]** — Seviyeni, XP'ni ve mesaj sayını gösterir.\n"
                "**/sıralama** — Sunucudaki ilk 10 kişinin seviye sıralamasını gösterir.\n"
                "**!köledailyxp** — Günlük XP ödülünü toplar (24 saatte bir).\n"
                "**/dbdurum** — Seviye veritabanının ve mesaj dinleyicisinin durumunu gösterir."
            ),
            inline=False
        )

        embed.add_field(
            name="🎮 Oyunlar",
            value=(
                "**/tkm** — Bot ile Taş, Kağıt, Makas oynarsın.\n"
                "**/tahmin [sayı]** — 1-100 arası tutulan sayıyı tahmin etme oyunu.\n"
                "**/slot** — Slot makinesini çevirip şansını denersin.\n"
                "**/bilgi-yarismasi** — Butonlu genel kültür bilgi yarışması başlatır.\n"
                "**/kasa-ac** — Gizli bir kasa açarak içinden ne çıkacağını görürsün.\n"
                "**/zardüellosu** — Bot ile zar düellosu yaparsın (büyük atan kazanır).\n"
                "**/yazitura** — Klasik yazı tura atma oyunu."
            ),
            inline=False
        )

        embed.add_field(
            name="🤖 Yapay Zeka",
            value="**/ask [soru]** — Yapay zekaya soru sorarsın.",
            inline=False
        )

        embed.set_footer(text="Sadece sen görebilirsin.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        print(f"/help hatası: {e}")
        await interaction.followup.send("Bir hata oluştu.", ephemeral=True)


keep_alive()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("HATA: Discord Token bulunamadı!")
    else:
        print("Bot başlatılıyor, 8 saniye bekleniyor (rate limit önlemi)...")
        time.sleep(8)  # 429 hatasını azaltmak için
        client.run(DISCORD_TOKEN)