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
    print("⚠️ UYARI: GROQ_API_KEY bulunamadı. /ask komutu çalışmaz.")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

aktif_oyunlar = {}

# ================= SEVİYE SİSTEMİ =================
SEVIYE_DOSYASI = "seviyeler.json"
BASLANGIC_SEVIYE_XP = 300
SEVIYE_XP_ARTISI = 300


def seviye_verisi_yukle():
    if os.path.exists(SEVIYE_DOSYASI):
        try:
            with open(SEVIYE_DOSYASI, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Seviye verisi okunamadı: {e}")
            return {}
    return {}


def seviye_verisi_kaydet():
    try:
        with open(SEVIYE_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(seviye_verileri, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Seviye verisi kaydedilemedi: {e}")


seviye_verileri = seviye_verisi_yukle()


def kullanici_verisi_al(user_id):
    uid = str(user_id)
    if uid not in seviye_verileri:
        seviye_verileri[uid] = {
            "xp": 0,
            "seviye": 1,
            "sonraki_seviye_xp": BASLANGIC_SEVIYE_XP,
            "mesaj_sayisi": 0,
            "son_daily": 0,
        }
    if "son_daily" not in seviye_verileri[uid]:
        seviye_verileri[uid]["son_daily"] = 0
    return seviye_verileri[uid]


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


TRIVIA_SORULARI = [
    # ==================== ANİME (280 soru) ====================
    {"soru": "Naruto'nun en iyi arkadaşı kimdir?", "dogru": "Sasuke Uchiha", "secenekler": ["Sakura Haruno", "Sasuke Uchiha", "Kakashi Hatake", "Shikamaru Nara"]},
    {"soru": "One Piece'te Luffy'nin meyvesi nedir?", "dogru": "Gomu Gomu no Mi", "secenekler": ["Mera Mera no Mi", "Gomu Gomu no Mi", "Hito Hito no Mi", "Yami Yami no Mi"]},
    {"soru": "Attack on Titan'da Eren'in Titan formu ne olarak bilinir?", "dogru": "Saldırı Titanı", "secenekler": ["Zırh Titanı", "Saldırı Titanı", "Dişi Titan", "Colossal Titan"]},
    {"soru": "Demon Slayer'da Tanjiro'nun kız kardeşinin adı nedir?", "dogru": "Nezuko", "secenekler": ["Shinobu", "Nezuko", "Mitsuri", "Kanao"]},
    {"soru": "Jujutsu Kaisen'de Gojo Satoru'nun en güçlü tekniği nedir?", "dogru": "Mugen", "secenekler": ["Domain Expansion", "Mugen", "Blue", "Red"]},
    {"soru": "Dragon Ball'da Goku'nun Saiyan adı nedir?", "dogru": "Kakarot", "secenekler": ["Vegeta", "Kakarot", "Bardock", "Raditz"]},
    {"soru": "Death Note'ta Light Yagami'nin kullandığı defterin adı nedir?", "dogru": "Death Note", "secenekler": ["Shinigami Note", "Death Note", "Ryuk Note", "Kira Note"]},
    {"soru": "My Hero Academia'da Deku'nun gerçek adı nedir?", "dogru": "Izuku Midoriya", "secenekler": ["Katsuki Bakugo", "Izuku Midoriya", "Shoto Todoroki", "All Might"]},
    {"soru": "Tokyo Ghoul'da Kaneki Ken'in maskesinin rengi nedir?", "dogru": "Beyaz", "secenekler": ["Siyah", "Kırmızı", "Beyaz", "Gri"]},
    {"soru": "Fullmetal Alchemist'te Edward Elric'in kardeşinin adı nedir?", "dogru": "Alphonse", "secenekler": ["Roy", "Alphonse", "Maes", "Hughes"]},
    {"soru": "Hunter x Hunter'da Gon'un en iyi arkadaşı kimdir?", "dogru": "Killua", "secenekler": ["Kurapika", "Killua", "Leorio", "Hisoka"]},
    {"soru": "Bleach'te Ichigo'nun Zanpakuto'sunun adı nedir?", "dogru": "Zangetsu", "secenekler": ["Senbonzakura", "Zangetsu", "Kyoka Suigetsu", "Tensa Zangetsu"]},
    {"soru": "Sword Art Online'da Kirito'nun gerçek adı nedir?", "dogru": "Kazuto Kirigaya", "secenekler": ["Asuna Yuuki", "Kazuto Kirigaya", "Eugeo", "Klein"]},
    {"soru": "One Punch Man'de Saitama'nın lakabı nedir?", "dogru": "Caped Baldy", "secenekler": ["Hero", "Caped Baldy", "Strongest", "One Punch"]},
    {"soru": "Naruto'da Dokuz Kuyruklu Tilki'nin adı nedir?", "dogru": "Kurama", "secenekler": ["Shukaku", "Kurama", "Gyuki", "Matatagi"]},
    {"soru": "Attack on Titan'da Duvarların isimleri nelerdir?", "dogru": "Maria, Rose, Sina", "secenekler": ["Maria, Rose, Sina", "Wall, Titan, Human", "North, South, East", "Eren, Mikasa, Armin"]},
    {"soru": "Demon Slayer'da Hashira'ların lideri kimdir?", "dogru": "Kagaya Ubuyashiki", "secenekler": ["Giyu Tomioka", "Kagaya Ubuyashiki", "Kyojuro Rengoku", "Tengen Uzui"]},
    {"soru": "Jujutsu Kaisen'de Yuji Itadori'nin içinde yaşayan lanet nedir?", "dogru": "Sukuna", "secenekler": ["Mahito", "Sukuna", "Jogo", "Hanami"]},
    {"soru": "Dragon Ball Z'de Frieza'nın en güçlü formu nedir?", "dogru": "Golden Frieza", "secenekler": ["Final Form", "Golden Frieza", "Mecha Frieza", "Black Frieza"]},
    {"soru": "My Hero Academia'da All Might'ın Quirk'i nedir?", "dogru": "One For All", "secenekler": ["All For One", "One For All", "Explosion", "Half-Cold Half-Hot"]},
    {"soru": "Tokyo Ghoul'da Ghoul'ların yediği şey nedir?", "dogru": "İnsan eti", "secenekler": ["Et", "İnsan eti", "Kan", "Kahve"]},
    {"soru": "Fullmetal Alchemist'te Alchemy'nin en temel kuralı nedir?", "dogru": "Eşit Değişim", "secenekler": ["Felsefe Taşı", "Eşit Değişim", "Transmutasyon", "Homunculus"]},
    {"soru": "Hunter x Hunter'da Nen'in 6 kategorisinden biri hangisidir?", "dogru": "Enhancer", "secenekler": ["Enhancer", "Nen Master", "Hunter", "Chimera"]},
    {"soru": "Bleach'te Soul Society'nin kralı kimdir?", "dogru": "Soul King", "secenekler": ["Yamamoto", "Soul King", "Aizen", "Urahara"]},
    {"soru": "Sword Art Online'da ilk oyunun adı nedir?", "dogru": "Sword Art Online", "secenekler": ["ALfheim Online", "Sword Art Online", "Gun Gale Online", "Underworld"]},
    {"soru": "One Punch Man'de Genos'un mesleği nedir?", "dogru": "Cyborg", "secenekler": ["Kahraman", "Cyborg", "Öğrenci", "Doktor"]},
    {"soru": "Naruto'da Sharingan'ı ilk kim bulmuştur?", "dogru": "Indra", "secenekler": ["Madara", "Indra", "Sasuke", "Itachi"]},
    {"soru": "Attack on Titan'da Mikasa'nın soyadı nedir?", "dogru": "Ackerman", "secenekler": ["Yeager", "Ackerman", "Arlert", "Smith"]},
    {"soru": "Demon Slayer'da Nezuko'nun kanı ne işe yarar?", "dogru": "Güneşte yanmasını engeller", "secenekler": ["Güç verir", "Güneşte yanmasını engeller", "İyileştirir", "Uyutur"]},
    {"soru": "Jujutsu Kaisen'de Domain Expansion'ın en güçlü kullanıcılarından biri kimdir?", "dogru": "Gojo Satoru", "secenekler": ["Geto", "Gojo Satoru", "Yuta", "Megumi"]},
    {"soru": "Dragon Ball'da Super Saiyan'ı ilk kim açmıştır?", "dogru": "Goku", "secenekler": ["Vegeta", "Goku", "Gohan", "Trunks"]},
    {"soru": "Death Note'ta Ryuk'un en sevdiği yiyecek nedir?", "dogru": "Elma", "secenekler": ["Çikolata", "Elma", "Et", "Kahve"]},
    {"soru": "My Hero Academia'da Bakugo'nun Quirk'i nedir?", "dogru": "Explosion", "secenekler": ["One For All", "Explosion", "Hardening", "Creation"]},
    {"soru": "Tokyo Ghoul'da Anteiku'nun sahibi kimdir?", "dogru": "Yoshimura", "secenekler": ["Kaneki", "Yoshimura", "Touka", "Nishiki"]},
    {"soru": "Fullmetal Alchemist'te Homunculus'ların lideri kimdir?", "dogru": "Father", "secenekler": ["Lust", "Father", "Envy", "Pride"]},
    {"soru": "Hunter x Hunter'da Phantom Troupe'un lideri kimdir?", "dogru": "Chrollo Lucilfer", "secenekler": ["Hisoka", "Chrollo Lucilfer", "Feitan", "Phinks"]},
    {"soru": "Bleach'te Aizen'in Zanpakuto'sunun adı nedir?", "dogru": "Kyoka Suigetsu", "secenekler": ["Zangetsu", "Kyoka Suigetsu", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Asuna'nun gerçek adı nedir?", "dogru": "Asuna Yuuki", "secenekler": ["Suguha", "Asuna Yuuki", "Sinon", "Leafa"]},
    {"soru": "One Punch Man'de En Güçlü Kahraman kimdir?", "dogru": "Saitama", "secenekler": ["Genos", "Saitama", "Bang", "King"]},
    {"soru": "Naruto'da Hokage'nin en yüksek rütbesi nedir?", "dogru": "Hokage", "secenekler": ["Jonin", "Hokage", "Anbu", "Kage"]},
    {"soru": "Attack on Titan'da Colossal Titan'ı kim kontrol eder?", "dogru": "Bertholdt", "secenekler": ["Reiner", "Bertholdt", "Annie", "Eren"]},
    {"soru": "Demon Slayer'da Rengoku'nun nefesi nedir?", "dogru": "Alev Nefesi", "secenekler": ["Su Nefesi", "Alev Nefesi", "Rüzgar Nefesi", "Yıldırım Nefesi"]},
    {"soru": "Jujutsu Kaisen'de Megumi'nin Shikigami'lerinden biri hangisidir?", "dogru": "Divine Dogs", "secenekler": ["Divine Dogs", "Mahoraga", "Nue", "Toad"]},
    {"soru": "Dragon Ball'da Namekian'ların lideri kimdir?", "dogru": "Guru", "secenekler": ["Piccolo", "Guru", "Nail", "Dende"]},
    {"soru": "Death Note'ta L'nin gerçek adı nedir?", "dogru": "L Lawliet", "secenekler": ["Near", "L Lawliet", "Mello", "Watari"]},
    {"soru": "My Hero Academia'da Todoroki'nin Quirk'i nedir?", "dogru": "Half-Cold Half-Hot", "secenekler": ["Explosion", "Half-Cold Half-Hot", "One For All", "Hardening"]},
    {"soru": "Tokyo Ghoul'da CCG'nin anlamı nedir?", "dogru": "Commission of Counter Ghoul", "secenekler": ["Central Ghoul Control", "Commission of Counter Ghoul", "Ghoul Hunter Agency", "Tokyo Defense"]},
    {"soru": "Fullmetal Alchemist'te Felsefe Taşı'nın gerçek adı nedir?", "dogru": "Philosopher's Stone", "secenekler": ["Red Stone", "Philosopher's Stone", "Homunculus Stone", "Truth Stone"]},
    {"soru": "Hunter x Hunter'da Greed Island'ın yaratıcısı kimdir?", "dogru": "Ging Freecss", "secenekler": ["Gon", "Ging Freecss", "Killua", "Biscuit"]},
    {"soru": "Bleach'te Bankai'yi ilk açan kimdir?", "dogru": "Ichigo", "secenekler": ["Byakuya", "Ichigo", "Kenpachi", "Toshiro"]},
    {"soru": "Sword Art Online'da Underworld'ün kraliçesi kimdir?", "dogru": "Administrator", "secenekler": ["Alice", "Administrator", "Eugeo", "Cardinal"]},
    {"soru": "One Punch Man'de S-Class'ın en güçlü üyesi kimdir?", "dogru": "Blast", "secenekler": ["Tatsumaki", "Blast", "King", "Metal Knight"]},
    {"soru": "Naruto'da Uchiha klanının en güçlü üyelerinden biri kimdir?", "dogru": "Madara Uchiha", "secenekler": ["Itachi", "Madara Uchiha", "Sasuke", "Obito"]},
    {"soru": "Attack on Titan'da Founding Titan'ın gücü nedir?", "dogru": "Titanları kontrol etmek", "secenekler": ["Sertleşme", "Titanları kontrol etmek", "Uçmak", "Görünmez olmak"]},
    {"soru": "Demon Slayer'da Muzan Kibutsuji'nin en güçlü formlarından biri hangisidir?", "dogru": "Upper Rank 1", "secenekler": ["Lower Rank", "Upper Rank 1", "Demon King", "Blood Demon"]},
    {"soru": "Jujutsu Kaisen'de Yuta Okkotsu'nun Rika'sı nedir?", "dogru": "Cursed Spirit", "secenekler": ["Shikigami", "Cursed Spirit", "Domain", "Technique"]},
    {"soru": "Dragon Ball'da Beerus'un melek yardımcısı kimdir?", "dogru": "Whis", "secenekler": ["Vados", "Whis", "Shin", "Zeno"]},
    {"soru": "Death Note'ta Kira'nın ikinci defteri kime aittir?", "dogru": "Misa Amane", "secenekler": ["Near", "Misa Amane", "Teru Mikami", "Rem"]},
    {"soru": "My Hero Academia'da Class 1-A'nın sınıf öğretmeni kimdir?", "dogru": "Aizawa", "secenekler": ["All Might", "Aizawa", "Present Mic", "Midnight"]},
    {"soru": "Tokyo Ghoul'da Kaneki'nin ilk Ghoul formunda saç rengi nedir?", "dogru": "Beyaz", "secenekler": ["Siyah", "Beyaz", "Kırmızı", "Gri"]},
    {"soru": "Fullmetal Alchemist'te State Alchemist'lerin unvanı nedir?", "dogru": "Dog of the Military", "secenekler": ["Hero", "Dog of the Military", "Alchemist", "Colonel"]},
    {"soru": "Hunter x Hunter'da Chimera Ant Kralı'nın adı nedir?", "dogru": "Meruem", "secenekler": ["Neferpitou", "Meruem", "Shaiapouf", "Youpi"]},
    {"soru": "Bleach'te Hueco Mundo'nun kralı kimdir?", "dogru": "Baraggan", "secenekler": ["Aizen", "Baraggan", "Stark", "Ulquiorra"]},
    {"soru": "Sword Art Online'da Kirito'nun en güçlü kılıcı nedir?", "dogru": "Night Sky Sword", "secenekler": ["Elucidator", "Night Sky Sword", "Dark Repulser", "Excalibur"]},
    {"soru": "One Punch Man'de Hero Association'ın başkanı kimdir?", "dogru": "Sitch", "secenekler": ["King", "Sitch", "Metal Knight", "Child Emperor"]},
    {"soru": "Naruto'da Sage Mode'u en iyi kullanan kimdir?", "dogru": "Naruto", "secenekler": ["Jiraiya", "Naruto", "Minato", "Kabuto"]},
    {"soru": "Attack on Titan'da Survey Corps'un komutanı kimdir?", "dogru": "Erwin Smith", "secenekler": ["Levi", "Erwin Smith", "Hange", "Miche"]},
    {"soru": "Demon Slayer'da Su Nefesi'nin en güçlü kullanıcısı kimdir?", "dogru": "Giyu Tomioka", "secenekler": ["Tanjiro", "Giyu Tomioka", "Sakonji", "Sabito"]},
    {"soru": "Jujutsu Kaisen'de Mahito'nun tekniği nedir?", "dogru": "Idle Transfiguration", "secenekler": ["Domain", "Idle Transfiguration", "Cleave", "Dismantle"]},
    {"soru": "Dragon Ball'da Super Saiyan Blue'yu ilk kim açmıştır?", "dogru": "Goku ve Vegeta", "secenekler": ["Goku", "Vegeta", "Goku ve Vegeta", "Gohan"]},
    {"soru": "Death Note'ta Near'ın takımı nedir?", "dogru": "SPK", "secenekler": ["Task Force", "SPK", "Wammy's House", "Kira Investigation"]},
    {"soru": "My Hero Academia'da Eri'nin Quirk'i nedir?", "dogru": "Rewind", "secenekler": ["Decay", "Rewind", "Overhaul", "One For All"]},
    {"soru": "Tokyo Ghoul'da One-Eyed Owl kimdir?", "dogru": "Yoshimura / Eto", "secenekler": ["Kaneki", "Yoshimura / Eto", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Truth'un göründüğü yer nedir?", "dogru": "Gate of Truth", "secenekler": ["Homunculus", "Gate of Truth", "Philosopher's Stone", "Ishval"]},
    {"soru": "Hunter x Hunter'da Dark Continent'in en tehlikeli yaratığı nedir?", "dogru": "Chimera Ant", "secenekler": ["Nen Beast", "Chimera Ant", "Calamity", "Ai"]},
    {"soru": "Bleach'te Final Getsuga Tensho'yu kim kullanmıştır?", "dogru": "Ichigo", "secenekler": ["Zangetsu", "Ichigo", "Aizen", "Yhwach"]},
    {"soru": "Sword Art Online'da Alicization'ın ana kahramanı kimdir?", "dogru": "Kirito", "secenekler": ["Eugeo", "Kirito", "Alice", "Administrator"]},
    {"soru": "One Punch Man'de Garou'nun lakabı nedir?", "dogru": "Human Monster", "secenekler": ["Hero Hunter", "Human Monster", "Strongest", "Martial Artist"]},
    {"soru": "Naruto'da Rinnegan'ı ilk kim açmıştır?", "dogru": "Hagoromo", "secenekler": ["Madara", "Hagoromo", "Sasuke", "Nagato"]},
    {"soru": "Attack on Titan'da War Hammer Titan'ın gücü nedir?", "dogru": "Sertleşme ve silah yaratma", "secenekler": ["Uçmak", "Sertleşme ve silah yaratma", "Kontrol", "Hız"]},
    {"soru": "Demon Slayer'da Upper Rank 3 kimdir?", "dogru": "Akaza", "secenekler": ["Kokushibo", "Akaza", "Doma", "Hantengu"]},
    {"soru": "Jujutsu Kaisen'de Gojo'nun Infinity'si ne işe yarar?", "dogru": "Dokunulmazlık sağlar", "secenekler": ["Saldırı", "Dokunulmazlık sağlar", "İyileşme", "Uçuş"]},
    {"soru": "Dragon Ball'da Universe 7'nin en güçlü savaşçısı kimdir?", "dogru": "Goku", "secenekler": ["Vegeta", "Goku", "Jiren", "Broly"]},
    {"soru": "Death Note'ta Shinigami'lerin kralı kimdir?", "dogru": "Shinigami King", "secenekler": ["Ryuk", "Shinigami King", "Rem", "Sidoh"]},
    {"soru": "My Hero Academia'da Shigaraki'nin Quirk'i nedir?", "dogru": "Decay", "secenekler": ["All For One", "Decay", "Warping", "Super Regeneration"]},
    {"soru": "Tokyo Ghoul'da Arima Kishou'nun lakabı nedir?", "dogru": "Reaper", "secenekler": ["White Reaper", "Reaper", "Owl", "One-Eyed"]},
    {"soru": "Fullmetal Alchemist'te Ishval Savaşı'nın sebebi nedir?", "dogru": "Din ve ırk ayrımı", "secenekler": ["Alchemy", "Din ve ırk ayrımı", "Homunculus", "Felsefe Taşı"]},
    {"soru": "Hunter x Hunter'da Nen'in en tehlikeli kullanım şekli nedir?", "dogru": "Post-Mortem Nen", "secenekler": ["Enhancement", "Post-Mortem Nen", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Quincy'lerin lideri kimdir?", "dogru": "Yhwach", "secenekler": ["Uryu", "Yhwach", "Haschwalth", "Bambietta"]},
    {"soru": "Sword Art Online'da Progressive'nin hikayesi nerededir?", "dogru": "Aincrad", "secenekler": ["Alfheim", "Aincrad", "Underworld", "Gun Gale"]},
    {"soru": "One Punch Man'de Tatsumaki'nin kardeşi kimdir?", "dogru": "Fubuki", "secenekler": ["Psykos", "Fubuki", "Blizzard", "Tornado"]},
    {"soru": "Naruto'da Bijuu'ların en güçlüsü hangisidir?", "dogru": "Kurama (Kyuubi)", "secenekler": ["Shukaku", "Kurama (Kyuubi)", "Gyuki", "Kokuo"]},
    {"soru": "Attack on Titan'da Paths'in sahibi kimdir?", "dogru": "Ymir Fritz", "secenekler": ["Eren", "Ymir Fritz", "Zeke", "Historia"]},
    {"soru": "Demon Slayer'da Hinokami Kagura'nın asıl adı nedir?", "dogru": "Güneş Nefesi", "secenekler": ["Alev Nefesi", "Güneş Nefesi", "Su Nefesi", "Rüzgar Nefesi"]},
    {"soru": "Jujutsu Kaisen'de Sukuna'nın Domain Expansion'ı nedir?", "dogru": "Malevolent Shrine", "secenekler": ["Unlimited Void", "Malevolent Shrine", "Self-Embodiment", "Horizon of the Captivating"]},
    {"soru": "Dragon Ball'da Ultra Instinct'i ilk kim açmıştır?", "dogru": "Goku", "secenekler": ["Vegeta", "Goku", "Whis", "Beerus"]},
    {"soru": "Death Note'ta Light'ın babasının adı nedir?", "dogru": "Soichiro Yagami", "secenekler": ["Watari", "Soichiro Yagami", "Matsuda", "Aizawa"]},
    {"soru": "My Hero Academia'da One For All'un önceki kullanıcılarından biri kimdir?", "dogru": "Nana Shimura", "secenekler": ["All For One", "Nana Shimura", "Gran Torino", "Endeavor"]},
    {"soru": "Tokyo Ghoul'da Quinx'lerin lideri kimdir?", "dogru": "Haise Sasaki", "secenekler": ["Urie", "Haise Sasaki", "Saiko", "Mutsuki"]},
    {"soru": "Fullmetal Alchemist'te Mustang'in lakabı nedir?", "dogru": "Flame Alchemist", "secenekler": ["Fullmetal", "Flame Alchemist", "Strong Arm", "Crystal"]},
    {"soru": "Hunter x Hunter'da Zoldyck ailesinin en güçlü üyesi kimdir?", "dogru": "Silva Zoldyck", "secenekler": ["Killua", "Silva Zoldyck", "Illumi", "Zeno"]},
    {"soru": "Bleach'te Soul Reaper'ların kılıcı nedir?", "dogru": "Zanpakuto", "secenekler": ["Katana", "Zanpakuto", "Sword", "Blade"]},
    {"soru": "Sword Art Online'da Kayaba Akihiko'nun oyundaki adı nedir?", "dogru": "Heathcliff", "secenekler": ["Kirito", "Heathcliff", "Oberon", "Sugou"]},
    {"soru": "One Punch Man'de Bang'in dojo'sunun adı nedir?", "dogru": "Bang Dojo", "secenekler": ["Hero Gym", "Bang Dojo", "Martial Arts", "Strongest"]},
    {"soru": "Naruto'da Chidori'yi kim bulmuştur?", "dogru": "Kakashi", "secenekler": ["Sasuke", "Kakashi", "Minato", "Itachi"]},
    {"soru": "Attack on Titan'da Armin'in Titan formu nedir?", "dogru": "Colossal Titan", "secenekler": ["Attack Titan", "Colossal Titan", "Armored Titan", "Female Titan"]},
    {"soru": "Demon Slayer'da Kochou Shinobu'nun nefesi nedir?", "dogru": "Böcek Nefesi", "secenekler": ["Çiçek Nefesi", "Böcek Nefesi", "Yılan Nefesi", "Aşk Nefesi"]},
    {"soru": "Jujutsu Kaisen'de Toji Fushiguro'nun lakabı nedir?", "dogru": "Sorcerer Killer", "secenekler": ["Heavenly Restriction", "Sorcerer Killer", "Invincible", "Assassin"]},
    {"soru": "Dragon Ball'da Frieza'nın babasının adı nedir?", "dogru": "King Cold", "secenekler": ["Cooler", "King Cold", "Chilled", "Frost"]},
    {"soru": "Death Note'ta Misa'nın Shinigami'si kimdir?", "dogru": "Rem", "secenekler": ["Ryuk", "Rem", "Sidoh", "Gelus"]},
    {"soru": "My Hero Academia'da Class 1-B'nin sınıf öğretmeni kimdir?", "dogru": "Vlad King", "secenekler": ["Aizawa", "Vlad King", "Cementoss", "Ectoplasm"]},
    {"soru": "Tokyo Ghoul'da Kaneki'nin en güçlü formu nedir?", "dogru": "Dragon", "secenekler": ["Centipede", "Dragon", "One-Eyed King", "Black Reaper"]},
    {"soru": "Fullmetal Alchemist'te Alphonse'un zırhının içinde ne vardır?", "dogru": "Ruh", "secenekler": ["Kan", "Ruh", "Felsefe Taşı", "Hiçbir şey"]},
    {"soru": "Hunter x Hunter'da Hisoka'nun Nen tipi nedir?", "dogru": "Transmutation", "secenekler": ["Enhancement", "Transmutation", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Espada'nın en güçlüsü kimdir?", "dogru": "Stark", "secenekler": ["Ulquiorra", "Stark", "Barragan", "Halibel"]},
    {"soru": "Sword Art Online'da Sinon'un gerçek adı nedir?", "dogru": "Shino Asada", "secenekler": ["Asuna", "Shino Asada", "Leafa", "Silica"]},
    {"soru": "One Punch Man'de King'in gerçek gücü nedir?", "dogru": "Şans ve korku", "secenekler": ["Süper güç", "Şans ve korku", "Teknik", "Hiçbir şey"]},
    {"soru": "Naruto'da Rasengan'ı kim bulmuştur?", "dogru": "Minato", "secenekler": ["Naruto", "Minato", "Jiraiya", "Kakashi"]},
    {"soru": "Attack on Titan'da Historia'nın Titan formu nedir?", "dogru": "Yok (Titan değil)", "secenekler": ["Female Titan", "Yok (Titan değil)", "Founding Titan", "Jaw Titan"]},
    {"soru": "Demon Slayer'da Tengen Uzui'nin nefesi nedir?", "dogru": "Ses Nefesi", "secenekler": ["Alev", "Ses Nefesi", "Rüzgar", "Taş"]},
    {"soru": "Jujutsu Kaisen'de Geto'nun Domain Expansion'ı nedir?", "dogru": "Womb Profusion", "secenekler": ["Unlimited Void", "Womb Profusion", "Malevolent Shrine", "Self-Embodiment"]},
    {"soru": "Dragon Ball'da Broly'nin ırkı nedir?", "dogru": "Saiyan", "secenekler": ["Namekian", "Saiyan", "Frieza Race", "Human"]},
    {"soru": "Death Note'ta Light'ın kız kardeşinin adı nedir?", "dogru": "Sayu", "secenekler": ["Misa", "Sayu", "Near", "Takada"]},
    {"soru": "My Hero Academia'da Hawks'ın Quirk'i nedir?", "dogru": "Fierce Wings", "secenekler": ["Hellflame", "Fierce Wings", "Explosion", "Hardening"]},
    {"soru": "Tokyo Ghoul'da Furuta'nın gerçek kimliği nedir?", "dogru": "Souta", "secenekler": ["Arima", "Souta", "Kanou", "Washuu"]},
    {"soru": "Fullmetal Alchemist'te Scar'ın kardeşinin araştırması nedir?", "dogru": "Felsefe Taşı", "secenekler": ["Alchemy", "Felsefe Taşı", "Homunculus", "Truth"]},
    {"soru": "Hunter x Hunter'da Gon'un babasının adı nedir?", "dogru": "Ging Freecss", "secenekler": ["Gon", "Ging Freecss", "Mito", "Kite"]},
    {"soru": "Bleach'te Ichigo'nun annesinin adı nedir?", "dogru": "Masaki", "secenekler": ["Rukia", "Masaki", "Orihime", "Yuzu"]},
    {"soru": "Sword Art Online'da Yuuki'nin hastalığı nedir?", "dogru": "AIDS", "secenekler": ["Kanser", "AIDS", "Lösemi", "Kalp hastalığı"]},
    {"soru": "One Punch Man'de Genos'un öğretmeni kimdir?", "dogru": "Saitama", "secenekler": ["Bang", "Saitama", "Dr. Kuseno", "Metal Knight"]},
    {"soru": "Naruto'da Shikamaru'nun Jutsu'su nedir?", "dogru": "Shadow Possession", "secenekler": ["Fireball", "Shadow Possession", "Chidori", "Rasengan"]},
    {"soru": "Attack on Titan'da Levi'nin lakabı nedir?", "dogru": "Humanity's Strongest", "secenekler": ["Captain", "Humanity's Strongest", "Ackerman", "Clean Freak"]},
    {"soru": "Demon Slayer'da Inosuke'nin maskesi nedir?", "dogru": "Yaban domuzu", "secenekler": ["Kurt", "Yaban domuzu", "Ayı", "Kaplan"]},
    {"soru": "Jujutsu Kaisen'de Nobara'nın silahı nedir?", "dogru": "Çekiç ve çivi", "secenekler": ["Kılıç", "Çekiç ve çivi", "Yay", "Mızrak"]},
    {"soru": "Dragon Ball'da Shenron'u kim çağırır?", "dogru": "Dragon Ball'larla", "secenekler": ["Dilek", "Dragon Ball'larla", "Namek", "Porunga"]},
    {"soru": "Death Note'ta L'nin en sevdiği tatlı nedir?", "dogru": "Şeker", "secenekler": ["Çikolata", "Şeker", "Dondurma", "Pasta"]},
    {"soru": "My Hero Academia'da Uraraka'nın Quirk'i nedir?", "dogru": "Zero Gravity", "secenekler": ["Explosion", "Zero Gravity", "Creation", "Hardening"]},
    {"soru": "Tokyo Ghoul'da Touka'nın kardeşi kimdir?", "dogru": "Ayato", "secenekler": ["Kaneki", "Ayato", "Nishiki", "Hinami"]},
    {"soru": "Fullmetal Alchemist'te Winry'nin mesleği nedir?", "dogru": "Otomail mühendisi", "secenekler": ["Alchemist", "Otomail mühendisi", "Doktor", "Asker"]},
    {"soru": "Hunter x Hunter'da Kurapika'nın klanı nedir?", "dogru": "Kurta", "secenekler": ["Zoldyck", "Kurta", "Phantom", "Hunter"]},
    {"soru": "Bleach'te Rukia'nın ağabeyi kimdir?", "dogru": "Byakuya", "secenekler": ["Renji", "Byakuya", "Kaien", "Ukitake"]},
    {"soru": "Sword Art Online'da Klein'ın loncasının adı nedir?", "dogru": "Fuurinkazan", "secenekler": ["Knights of the Blood", "Fuurinkazan", "Laughing Coffin", "ALS"]},
    {"soru": "One Punch Man'de Saitama'nın evi nerededir?", "dogru": "Z-City", "secenekler": ["A-City", "Z-City", "B-City", "Hero Association"]},
    {"soru": "Naruto'da Sakura'nın sensei'si kimdir?", "dogru": "Tsunade", "secenekler": ["Kakashi", "Tsunade", "Jiraiya", "Orochimaru"]},
    {"soru": "Attack on Titan'da Reiner'in Titan formu nedir?", "dogru": "Armored Titan", "secenekler": ["Colossal", "Armored Titan", "Female", "Jaw"]},
    {"soru": "Demon Slayer'da Zenitsu'nun nefesi nedir?", "dogru": "Yıldırım Nefesi", "secenekler": ["Su", "Yıldırım Nefesi", "Alev", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Maki'nin silahı nedir?", "dogru": "Naginata / Playful Cloud", "secenekler": ["Kılıç", "Naginata / Playful Cloud", "Yay", "Mızrak"]},
    {"soru": "Dragon Ball'da Vegeta'nın oğlunun adı nedir?", "dogru": "Trunks", "secenekler": ["Goten", "Trunks", "Gohan", "Bulla"]},
    {"soru": "Death Note'ta Near'ın gerçek adı nedir?", "dogru": "Nate River", "secenekler": ["L", "Nate River", "Mihael Keehl", "Mail Jeevas"]},
    {"soru": "My Hero Academia'da Iida'nın Quirk'i nedir?", "dogru": "Engine", "secenekler": ["Explosion", "Engine", "Hardening", "Creation"]},
    {"soru": "Tokyo Ghoul'da Hinami'nin annesinin adı nedir?", "dogru": "Ryouko", "secenekler": ["Touka", "Ryouko", "Eto", "Saeki"]},
    {"soru": "Fullmetal Alchemist'te Hughes'un karısının adı nedir?", "dogru": "Gracia", "secenekler": ["Riza", "Gracia", "Winry", "Maria"]},
    {"soru": "Hunter x Hunter'da Leorio'nun hayali nedir?", "dogru": "Doktor olmak", "secenekler": ["Hunter", "Doktor olmak", "Zengin", "Güçlü"]},
    {"soru": "Bleach'te Orihime'nin güçleri nedir?", "dogru": "Shun Shun Rikka", "secenekler": ["Zanpakuto", "Shun Shun Rikka", "Hollow", "Quincy"]},
    {"soru": "Sword Art Online'da Agil'in gerçek adı nedir?", "dogru": "Andrew Gilbert Mills", "secenekler": ["Klein", "Andrew Gilbert Mills", "Kirito", "Kayaba"]},
    {"soru": "One Punch Man'de Fubuki'nin grubunun adı nedir?", "dogru": "Blizzard Group", "secenekler": ["Tornado", "Blizzard Group", "Hero", "Psychic"]},
    {"soru": "Naruto'da Gaara'nın Bijuu'su nedir?", "dogru": "Shukaku", "secenekler": ["Kurama", "Shukaku", "Gyuki", "Matatagi"]},
    {"soru": "Attack on Titan'da Annie'nin Titan formu nedir?", "dogru": "Female Titan", "secenekler": ["Armored", "Female Titan", "Jaw", "Cart"]},
    {"soru": "Demon Slayer'da Muichiro'nun nefesi nedir?", "dogru": "Sis Nefesi", "secenekler": ["Su", "Sis Nefesi", "Alev", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Panda'nın gerçek doğası nedir?", "dogru": "Cursed Corpse", "secenekler": ["İnsan", "Cursed Corpse", "Shikigami", "Cursed Spirit"]},
    {"soru": "Dragon Ball'da Gohan'ın en güçlü formu nedir?", "dogru": "Beast", "secenekler": ["Ultimate", "Beast", "Super Saiyan 2", "Mystic"]},
    {"soru": "Death Note'ta Teru Mikami'nin mesleği nedir?", "dogru": "Savcı", "secenekler": ["Polis", "Savcı", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Mina'nın Quirk'i nedir?", "dogru": "Acid", "secenekler": ["Explosion", "Acid", "Hardening", "Creation"]},
    {"soru": "Tokyo Ghoul'da Nishiki'nin lakabı nedir?", "dogru": "Serpent", "secenekler": ["Rabbit", "Serpent", "Owl", "Centipede"]},
    {"soru": "Fullmetal Alchemist'te Ling Yao'nun amacı nedir?", "dogru": "Ölümsüzlük", "secenekler": ["Güç", "Ölümsüzlük", "İntikam", "Barış"]},
    {"soru": "Hunter x Hunter'da Biscuit'in gerçek görünüşü nedir?", "dogru": "Yaşlı kadın", "secenekler": ["Çocuk", "Yaşlı kadın", "Genç kız", "Erkek"]},
    {"soru": "Bleach'te Chad'ın gerçek adı nedir?", "dogru": "Yasutora Sado", "secenekler": ["Ichigo", "Yasutora Sado", "Uryu", "Renji"]},
    {"soru": "Sword Art Online'da Silica'nın evcil hayvanının adı nedir?", "dogru": "Pina", "secenekler": ["Yui", "Pina", "Alice", "Leafa"]},
    {"soru": "One Punch Man'de Metal Bat'in silahı nedir?", "dogru": "Beyzbol sopası", "secenekler": ["Kılıç", "Beyzbol sopası", "Çekiç", "Mızrak"]},
    {"soru": "Naruto'da Rock Lee'nin sensei'si kimdir?", "dogru": "Might Guy", "secenekler": ["Kakashi", "Might Guy", "Asuma", "Kurenai"]},
    {"soru": "Attack on Titan'da Zeke'nin Titan formu nedir?", "dogru": "Beast Titan", "secenekler": ["Attack", "Beast Titan", "Cart", "Jaw"]},
    {"soru": "Demon Slayer'da Mitsuri'nin nefesi nedir?", "dogru": "Aşk Nefesi", "secenekler": ["Alev", "Aşk Nefesi", "Su", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Toge'nin tekniği nedir?", "dogru": "Cursed Speech", "secenekler": ["Domain", "Cursed Speech", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Piccolo'nun babasının adı nedir?", "dogru": "King Piccolo", "secenekler": ["Kami", "King Piccolo", "Nail", "Guru"]},
    {"soru": "Death Note'ta Watari'nin gerçek adı nedir?", "dogru": "Quillsh Wammy", "secenekler": ["L", "Quillsh Wammy", "Near", "Roger"]},
    {"soru": "My Hero Academia'da Kirishima'nın Quirk'i nedir?", "dogru": "Hardening", "secenekler": ["Explosion", "Hardening", "Acid", "Creation"]},
    {"soru": "Tokyo Ghoul'da Uta'nın mesleği nedir?", "dogru": "Maske yapımcısı", "secenekler": ["Kahveci", "Maske yapımcısı", "Avcı", "Doktor"]},
    {"soru": "Fullmetal Alchemist'te May Chang'in alchemy stili nedir?", "dogru": "Alkahestry", "secenekler": ["Alchemy", "Alkahestry", "Transmutation", "Homunculus"]},
    {"soru": "Hunter x Hunter'da Illumi'nin kardeşi kimdir?", "dogru": "Killua", "secenekler": ["Alluka", "Killua", "Milluki", "Kalluto"]},
    {"soru": "Bleach'te Uryu'nun babasının adı nedir?", "dogru": "Ryuken", "secenekler": ["Soken", "Ryuken", "Yhwach", "Haschwalth"]},
    {"soru": "Sword Art Online'da Leafa'nın gerçek adı nedir?", "dogru": "Suguha Kirigaya", "secenekler": ["Asuna", "Suguha Kirigaya", "Sinon", "Yuuki"]},
    {"soru": "One Punch Man'de Child Emperor'un yaşı kaçtır?", "dogru": "10", "secenekler": ["12", "10", "8", "15"]},
    {"soru": "Naruto'da Hinata'nın klanı nedir?", "dogru": "Hyuga", "secenekler": ["Uchiha", "Hyuga", "Nara", "Akimichi"]},
    {"soru": "Attack on Titan'da Pieck'in Titan formu nedir?", "dogru": "Cart Titan", "secenekler": ["Jaw", "Cart Titan", "Beast", "War Hammer"]},
    {"soru": "Demon Slayer'da Gyomei'nin nefesi nedir?", "dogru": "Taş Nefesi", "secenekler": ["Alev", "Taş Nefesi", "Su", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Yuji'nin okulu neresidir?", "dogru": "Tokyo Jujutsu High", "secenekler": ["Kyoto", "Tokyo Jujutsu High", "Osaka", "Sendai"]},
    {"soru": "Dragon Ball'da Krillin'in karısının adı nedir?", "dogru": "Android 18", "secenekler": ["Bulma", "Android 18", "Chi-Chi", "Launch"]},
    {"soru": "Death Note'ta Mello'nun gerçek adı nedir?", "dogru": "Mihael Keehl", "secenekler": ["Near", "Mihael Keehl", "Nate", "Matt"]},
    {"soru": "My Hero Academia'da Tokoyami'nin Quirk'i nedir?", "dogru": "Dark Shadow", "secenekler": ["Explosion", "Dark Shadow", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Tsukiyama'nın lakabı nedir?", "dogru": "Gourmet", "secenekler": ["Rabbit", "Gourmet", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Izumi Curtis'in öğrencileri kimlerdir?", "dogru": "Edward ve Alphonse", "secenekler": ["Roy ve Riza", "Edward ve Alphonse", "Scar", "Ling"]},
    {"soru": "Hunter x Hunter'da Morel'in piposunun adı nedir?", "dogru": "Deep Purple", "secenekler": ["Smoke", "Deep Purple", "Nen", "Pipe"]},
    {"soru": "Bleach'te Kenpachi'nin Zanpakuto'sunun adı nedir?", "dogru": "Nozarashi", "secenekler": ["Zangetsu", "Nozarashi", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Cardinal System nedir?", "dogru": "Oyun yönetim sistemi", "secenekler": ["Karakter", "Oyun yönetim sistemi", "Kılıç", "Şehir"]},
    {"soru": "One Punch Man'de Drive Knight'ın gücü nedir?", "dogru": "Dönüşüm", "secenekler": ["Hız", "Dönüşüm", "Güç", "Zeka"]},
    {"soru": "Naruto'da Temari'nin kardeşi kimdir?", "dogru": "Gaara ve Kankuro", "secenekler": ["Naruto", "Gaara ve Kankuro", "Shikamaru", "Choji"]},
    {"soru": "Attack on Titan'da Porco'nun Titan formu nedir?", "dogru": "Jaw Titan", "secenekler": ["Cart", "Jaw Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Sanemi'nin nefesi nedir?", "dogru": "Rüzgar Nefesi", "secenekler": ["Alev", "Rüzgar Nefesi", "Su", "Taş"]},
    {"soru": "Jujutsu Kaisen'de Mai'nin silahı nedir?", "dogru": "Construction (kurşun)", "secenekler": ["Kılıç", "Construction (kurşun)", "Yay", "Mızrak"]},
    {"soru": "Dragon Ball'da Tien'in üçüncü gözü nedir?", "dogru": "Üçüncü göz", "secenekler": ["Güç", "Üçüncü göz", "Teknik", "Hiçbir şey"]},
    {"soru": "Death Note'ta Matt'in gerçek adı nedir?", "dogru": "Mail Jeevas", "secenekler": ["Near", "Mail Jeevas", "Mello", "L"]},
    {"soru": "My Hero Academia'da Jiro'nun Quirk'i nedir?", "dogru": "Earphone Jack", "secenekler": ["Explosion", "Earphone Jack", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Takizawa'nın lakabı nedir?", "dogru": "Owl", "secenekler": ["Rabbit", "Owl", "Serpent", "Gourmet"]},
    {"soru": "Fullmetal Alchemist'te Olivier Armstrong'un kardeşi kimdir?", "dogru": "Alex Louis Armstrong", "secenekler": ["Roy", "Alex Louis Armstrong", "Maes", "Havoc"]},
    {"soru": "Hunter x Hunter'da Knuckle'in tekniği nedir?", "dogru": "Hakoware (APR)", "secenekler": ["Enhancement", "Hakoware (APR)", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Toshiro'nun Zanpakuto'sunun adı nedir?", "dogru": "Hyorinmaru", "secenekler": ["Zangetsu", "Hyorinmaru", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Eugeo'nun kılıcı nedir?", "dogru": "Blue Rose Sword", "secenekler": ["Night Sky", "Blue Rose Sword", "Excalibur", "Dark Repulser"]},
    {"soru": "One Punch Man'de Zombi Man'in gücü nedir?", "dogru": "Ölümsüzlük / Yenilenme", "secenekler": ["Güç", "Ölümsüzlük / Yenilenme", "Hız", "Zeka"]},
    {"soru": "Naruto'da Ino'nun klanı nedir?", "dogru": "Yamanaka", "secenekler": ["Nara", "Yamanaka", "Akimichi", "Hyuga"]},
    {"soru": "Attack on Titan'da Lara Tybur'un Titan formu nedir?", "dogru": "War Hammer Titan", "secenekler": ["Founding", "War Hammer Titan", "Attack", "Beast"]},
    {"soru": "Demon Slayer'da Obanai'nin nefesi nedir?", "dogru": "Yılan Nefesi", "secenekler": ["Alev", "Yılan Nefesi", "Su", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Kokichi'nin robotunun adı nedir?", "dogru": "Mechamaru", "secenekler": ["Panda", "Mechamaru", "Ultimate", "Cursed"]},
    {"soru": "Dragon Ball'da Yamcha'nın tekniği nedir?", "dogru": "Wolf Fang Fist", "secenekler": ["Kamehameha", "Wolf Fang Fist", "Special Beam", "Final Flash"]},
    {"soru": "Death Note'ta Takada'nın mesleği nedir?", "dogru": "Sunucu", "secenekler": ["Polis", "Sunucu", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Sero'nun Quirk'i nedir?", "dogru": "Tape", "secenekler": ["Explosion", "Tape", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Naki'nin grubunun adı nedir?", "dogru": "White Suits", "secenekler": ["Aogiri", "White Suits", "CCG", "Anteiku"]},
    {"soru": "Fullmetal Alchemist'te Selim Bradley'nin gerçek kimliği nedir?", "dogru": "Pride", "secenekler": ["Wrath", "Pride", "Envy", "Gluttony"]},
    {"soru": "Hunter x Hunter'da Shoot'un silahı nedir?", "dogru": "Hotel Rafflesia", "secenekler": ["Nen", "Hotel Rafflesia", "Pipe", "Cards"]},
    {"soru": "Bleach'te Yamamoto'nun Zanpakuto'sunun adı nedir?", "dogru": "Ryujin Jakka", "secenekler": ["Zangetsu", "Ryujin Jakka", "Senbonzakura", "Hyorinmaru"]},
    {"soru": "Sword Art Online'da Alice'in soyadı nedir?", "dogru": "Zuberg", "secenekler": ["Synthesis", "Zuberg", "Schuberg", "Integrity"]},
    {"soru": "One Punch Man'de Pig God'un gücü nedir?", "dogru": "Yemek / Yutmak", "secenekler": ["Güç", "Yemek / Yutmak", "Hız", "Zeka"]},
    {"soru": "Naruto'da Choji'nin klanı nedir?", "dogru": "Akimichi", "secenekler": ["Nara", "Akimichi", "Yamanaka", "Hyuga"]},
    {"soru": "Attack on Titan'da Galliard'ın Titan formu nedir?", "dogru": "Jaw Titan", "secenekler": ["Cart", "Jaw Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Kanao'nun nefesi nedir?", "dogru": "Çiçek Nefesi", "secenekler": ["Böcek", "Çiçek Nefesi", "Su", "Alev"]},
    {"soru": "Jujutsu Kaisen'de Utahime'nin okulu neresidir?", "dogru": "Kyoto Jujutsu High", "secenekler": ["Tokyo", "Kyoto Jujutsu High", "Osaka", "Sendai"]},
    {"soru": "Dragon Ball'da Chiaotzu'nun gücü nedir?", "dogru": "Telekinezi", "secenekler": ["Güç", "Telekinezi", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Ide'nin mesleği nedir?", "dogru": "Polis", "secenekler": ["Savcı", "Polis", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Kaminari'nin Quirk'i nedir?", "dogru": "Electrification", "secenekler": ["Explosion", "Electrification", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Miza'nın lakabı nedir?", "dogru": "Jade", "secenekler": ["Rabbit", "Jade", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Buccaneer'in lakabı nedir?", "dogru": "The Barracuda", "secenekler": ["Strong Arm", "The Barracuda", "Flame", "Fullmetal"]},
    {"soru": "Hunter x Hunter'da Palm'ın tekniği nedir?", "dogru": "Wink Blue", "secenekler": ["Enhancement", "Wink Blue", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Unohana'nın Zanpakuto'sunun adı nedir?", "dogru": "Minazuki", "secenekler": ["Zangetsu", "Minazuki", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Cardinal'in kardeşi kimdir?", "dogru": "Administrator", "secenekler": ["Alice", "Administrator", "Eugeo", "Quinella"]},
    {"soru": "One Punch Man'de Watchdog Man'in bölgesi neresidir?", "dogru": "Q-City", "secenekler": ["Z-City", "Q-City", "A-City", "B-City"]},
    {"soru": "Naruto'da Shino'nun klanı nedir?", "dogru": "Aburame", "secenekler": ["Hyuga", "Aburame", "Inuzuka", "Nara"]},
    {"soru": "Attack on Titan'da Marcel'in Titan formu nedir?", "dogru": "Jaw Titan", "secenekler": ["Cart", "Jaw Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Genya'nın nefesi nedir?", "dogru": "Yok (nefes kullanmaz)", "secenekler": ["Alev", "Yok (nefes kullanmaz)", "Su", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Miwa'nın silahı nedir?", "dogru": "Simple Domain + kılıç", "secenekler": ["Naginata", "Simple Domain + kılıç", "Yay", "Mızrak"]},
    {"soru": "Dragon Ball'da Launch'ın iki kişiliği nedir?", "dogru": "Mavi ve sarı saç", "secenekler": ["Güçlü ve zayıf", "Mavi ve sarı saç", "İyi ve kötü", "Hızlı ve yavaş"]},
    {"soru": "Death Note'ta Aizawa'nın mesleği nedir?", "dogru": "Polis", "secenekler": ["Savcı", "Polis", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Ojiro'nun Quirk'i nedir?", "dogru": "Tail", "secenekler": ["Explosion", "Tail", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Shuu'nun ailesinin adı nedir?", "dogru": "Tsukiyama", "secenekler": ["Kaneki", "Tsukiyama", "Kirishima", "Fueguchi"]},
    {"soru": "Fullmetal Alchemist'te Denny Brosh'un partneri kimdir?", "dogru": "Maria Ross", "secenekler": ["Riza", "Maria Ross", "Winry", "Gracia"]},
    {"soru": "Hunter x Hunter'da Ikalgo'nun ırkı nedir?", "dogru": "Chimera Ant", "secenekler": ["İnsan", "Chimera Ant", "Nen Beast", "Zoldyck"]},
    {"soru": "Bleach'te Byakuya'nın Zanpakuto'sunun adı nedir?", "dogru": "Senbonzakura", "secenekler": ["Zangetsu", "Senbonzakura", "Hyorinmaru", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Quinella'nın unvanı nedir?", "dogru": "Administrator", "secenekler": ["Integrity Knight", "Administrator", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Flashy Flash'in gücü nedir?", "dogru": "Hız", "secenekler": ["Güç", "Hız", "Zeka", "Dayanıklılık"]},
    {"soru": "Naruto'da Kiba'nın köpeğinin adı nedir?", "dogru": "Akamaru", "secenekler": ["Pakkun", "Akamaru", "Bull", "Guruko"]},
    {"soru": "Attack on Titan'da Ymir'in Titan formu nedir?", "dogru": "Jaw Titan", "secenekler": ["Cart", "Jaw Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Gyokko'nun Upper Rank'i nedir?", "dogru": "Upper Rank 5", "secenekler": ["3", "Upper Rank 5", "4", "6"]},
    {"soru": "Jujutsu Kaisen'de Todo'nun tekniği nedir?", "dogru": "Boogie Woogie", "secenekler": ["Domain", "Boogie Woogie", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Master Roshi'nin lakabı nedir?", "dogru": "Turtle Hermit", "secenekler": ["Martial Artist", "Turtle Hermit", "Kame", "Strongest"]},
    {"soru": "Death Note'ta Ukita'nın mesleği nedir?", "dogru": "Polis", "secenekler": ["Savcı", "Polis", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Shoji'nun Quirk'i nedir?", "dogru": "Dupli-Arms", "secenekler": ["Explosion", "Dupli-Arms", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Roma'nın lakabı nedir?", "dogru": "The Monster", "secenekler": ["Rabbit", "The Monster", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Heymans Breda'nın mesleği nedir?", "dogru": "Asker", "secenekler": ["Alchemist", "Asker", "Doktor", "Polis"]},
    {"soru": "Hunter x Hunter'da Meleoron'un tekniği nedir?", "dogru": "God's Accomplice", "secenekler": ["Enhancement", "God's Accomplice", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Gin'in Zanpakuto'sunun adı nedir?", "dogru": "Shinsou", "secenekler": ["Zangetsu", "Shinsou", "Kyoka Suigetsu", "Senbonzakura"]},
    {"soru": "Sword Art Online'da Bercouli'nin unvanı nedir?", "dogru": "Integrity Knight Commander", "secenekler": ["Administrator", "Integrity Knight Commander", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Superalloy Darkshine'ın gücü nedir?", "dogru": "Dayanıklılık / Kas", "secenekler": ["Hız", "Dayanıklılık / Kas", "Zeka", "Teknik"]},
    {"soru": "Naruto'da Neji'nin klanı nedir?", "dogru": "Hyuga", "secenekler": ["Uchiha", "Hyuga", "Aburame", "Inuzuka"]},
    {"soru": "Attack on Titan'da Grisha'nın Titan formu nedir?", "dogru": "Attack Titan", "secenekler": ["Founding", "Attack Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Daki'nin Upper Rank'i nedir?", "dogru": "Upper Rank 6", "secenekler": ["5", "Upper Rank 6", "4", "3"]},
    {"soru": "Jujutsu Kaisen'de Ino'nun tekniği nedir?", "dogru": "Auspicious Beasts Summon", "secenekler": ["Domain", "Auspicious Beasts Summon", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Frieza'nın babasının adı nedir?", "dogru": "King Cold", "secenekler": ["Cooler", "King Cold", "Chilled", "Frost"]},
    {"soru": "Death Note'ta Matsuda'nın mesleği nedir?", "dogru": "Polis", "secenekler": ["Savcı", "Polis", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Sato'nun Quirk'i nedir?", "dogru": "Sugar Rush", "secenekler": ["Explosion", "Sugar Rush", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Donato'nun lakabı nedir?", "dogru": "The Priest", "secenekler": ["Rabbit", "The Priest", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Vato Falman'ın mesleği nedir?", "dogru": "Asker", "secenekler": ["Alchemist", "Asker", "Doktor", "Polis"]},
    {"soru": "Hunter x Hunter'da Pouf'un krala olan bağlılığı nedir?", "dogru": "Aşırı sadakat", "secenekler": ["Nefret", "Aşırı sadakat", "Korku", "İntikam"]},
    {"soru": "Bleach'te Tousen'in Zanpakuto'sunun adı nedir?", "dogru": "Suzumushi", "secenekler": ["Zangetsu", "Suzumushi", "Kyoka Suigetsu", "Senbonzakura"]},
    {"soru": "Sword Art Online'da Fanatio'nun unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Atomic Samurai'nin gücü nedir?", "dogru": "Kılıç ustalığı", "secenekler": ["Hız", "Kılıç ustalığı", "Zeka", "Dayanıklılık"]},
    {"soru": "Naruto'da Tenten'in uzmanlığı nedir?", "dogru": "Silah kullanma", "secenekler": ["Taijutsu", "Silah kullanma", "Genjutsu", "Ninjutsu"]},
    {"soru": "Attack on Titan'da Frieda'nın Titan formu nedir?", "dogru": "Founding Titan", "secenekler": ["Attack", "Founding Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Gyutaro'nun Upper Rank'i nedir?", "dogru": "Upper Rank 6", "secenekler": ["5", "Upper Rank 6", "4", "3"]},
    {"soru": "Jujutsu Kaisen'de Nishimiya'nın tekniği nedir?", "dogru": "Tool Manipulation", "secenekler": ["Domain", "Tool Manipulation", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Dende'nin gücü nedir?", "dogru": "İyileştirme", "secenekler": ["Saldırı", "İyileştirme", "Hız", "Güç"]},
    {"soru": "Death Note'ta Ide'nin partneri kimdir?", "dogru": "Aizawa", "secenekler": ["Matsuda", "Aizawa", "Mogi", "Ukita"]},
    {"soru": "My Hero Academia'da Aoyama'nın Quirk'i nedir?", "dogru": "Navel Laser", "secenekler": ["Explosion", "Navel Laser", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kanae'nin gerçek kimliği nedir?", "dogru": "Karren von Rosewald", "secenekler": ["Touka", "Karren von Rosewald", "Hinami", "Eto"]},
    {"soru": "Fullmetal Alchemist'te Jean Havoc'un mesleği nedir?", "dogru": "Asker", "secenekler": ["Alchemist", "Asker", "Doktor", "Polis"]},
    {"soru": "Hunter x Hunter'da Youpi'nin gücü nedir?", "dogru": "Dönüşüm / Güç", "secenekler": ["Hız", "Dönüşüm / Güç", "Zeka", "Nen"]},
    {"soru": "Bleach'te Komamura'nun Zanpakuto'sunun adı nedir?", "dogru": "Tenken", "secenekler": ["Zangetsu", "Tenken", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Deusolbert'in unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Puri-Puri Prisoner'ın gücü nedir?", "dogru": "Kas gücü", "secenekler": ["Hız", "Kas gücü", "Zeka", "Teknik"]},
    {"soru": "Naruto'da Lee'nin en güçlü tekniği nedir?", "dogru": "Eight Gates", "secenekler": ["Rasengan", "Eight Gates", "Chidori", "Shadow"]},
    {"soru": "Attack on Titan'da Uri'nin Titan formu nedir?", "dogru": "Founding Titan", "secenekler": ["Attack", "Founding Titan", "Beast", "Armored"]},
    {"soru": "Demon Slayer'da Hantengu'nun Upper Rank'i nedir?", "dogru": "Upper Rank 4", "secenekler": ["5", "Upper Rank 4", "6", "3"]},
    {"soru": "Jujutsu Kaisen'de Kamo'nun tekniği nedir?", "dogru": "Blood Manipulation", "secenekler": ["Domain", "Blood Manipulation", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Kami'nin asıl adı nedir?", "dogru": "Namekian", "secenekler": ["Piccolo", "Namekian", "Guru", "Nail"]},
    {"soru": "Death Note'ta Mogi'nin mesleği nedir?", "dogru": "Polis", "secenekler": ["Savcı", "Polis", "Doktor", "Öğretmen"]},
    {"soru": "My Hero Academia'da Hagakure'nin Quirk'i nedir?", "dogru": "Invisibility", "secenekler": ["Explosion", "Invisibility", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Tatara'nın grubu nedir?", "dogru": "Aogiri Tree", "secenekler": ["CCG", "Aogiri Tree", "Anteiku", "White Suits"]},
    {"soru": "Fullmetal Alchemist'te Kain Fuery'nin mesleği nedir?", "dogru": "Asker", "secenekler": ["Alchemist", "Asker", "Doktor", "Polis"]},
    {"soru": "Hunter x Hunter'da Pitou'nun tekniği nedir?", "dogru": "Terpsichora / Doctor Blythe", "secenekler": ["Enhancement", "Terpsichora / Doctor Blythe", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Soi Fon'un Zanpakuto'sunun adı nedir?", "dogru": "Suzumebachi", "secenekler": ["Zangetsu", "Suzumebachi", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Sheyta'nın unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Tanktop Master'ın gücü nedir?", "dogru": "Kas gücü", "secenekler": ["Hız", "Kas gücü", "Zeka", "Teknik"]},
    {"soru": "Naruto'da Asuma'nın en sevdiği şeyi nedir?", "dogru": "Sigara ve shogi", "secenekler": ["Savaş", "Sigara ve shogi", "Yemek", "Uyku"]},
    {"soru": "Attack on Titan'da Helos'un efsanesi nedir?", "dogru": "Titan avcısı", "secenekler": ["Kral", "Titan avcısı", "Asker", "Doktor"]},
    {"soru": "Demon Slayer'da Kaigaku'nun nefesi nedir?", "dogru": "Yıldırım Nefesi", "secenekler": ["Alev", "Yıldırım Nefesi", "Su", "Rüzgar"]},
    {"soru": "Jujutsu Kaisen'de Noritoshi'nin klanı nedir?", "dogru": "Kamo", "secenekler": ["Zenin", "Kamo", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Oolong'un gücü nedir?", "dogru": "Şekil değiştirme", "secenekler": ["Güç", "Şekil değiştirme", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Aizawa'nın takımı nedir?", "dogru": "Task Force", "secenekler": ["SPK", "Task Force", "Wammy", "Kira"]},
    {"soru": "My Hero Academia'da Koji'nin Quirk'i nedir?", "dogru": "Anivoice", "secenekler": ["Explosion", "Anivoice", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Eto'nun kitabı nedir?", "dogru": "The Black Goat's Egg", "secenekler": ["Tokyo Ghoul", "The Black Goat's Egg", "Anteiku", "CCG"]},
    {"soru": "Fullmetal Alchemist'te Martel'in gücü nedir?", "dogru": "Esneklik", "secenekler": ["Güç", "Esneklik", "Hız", "Zeka"]},
    {"soru": "Hunter x Hunter'da Meruem'in kraliçe annesi kimdir?", "dogru": "Chimera Ant Queen", "secenekler": ["Pouf", "Chimera Ant Queen", "Pitou", "Youpi"]},
    {"soru": "Bleach'te Kensei'nin Zanpakuto'sunun adı nedir?", "dogru": "Tachikaze", "secenekler": ["Zangetsu", "Tachikaze", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Sortiliena'nın unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Metal Knight'ın gerçek adı nedir?", "dogru": "Bofoi", "secenekler": ["King", "Bofoi", "Child Emperor", "Drive Knight"]},
    {"soru": "Naruto'da Kurenai'nin uzmanlığı nedir?", "dogru": "Genjutsu", "secenekler": ["Taijutsu", "Genjutsu", "Ninjutsu", "Kenjutsu"]},
    {"soru": "Attack on Titan'da Willy Tybur'un rolü nedir?", "dogru": "Marley'nin sözcüsü", "secenekler": ["Asker", "Marley'nin sözcüsü", "Titan", "Kral"]},
    {"soru": "Demon Slayer'da Enmu'nun Lower Rank'i nedir?", "dogru": "Lower Rank 1", "secenekler": ["2", "Lower Rank 1", "3", "4"]},
    {"soru": "Jujutsu Kaisen'de Mai'nin ikiz kardeşi kimdir?", "dogru": "Maki", "secenekler": ["Nobara", "Maki", "Miwa", "Nishimiya"]},
    {"soru": "Dragon Ball'da Puar'ın gücü nedir?", "dogru": "Şekil değiştirme", "secenekler": ["Güç", "Şekil değiştirme", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Mogi'nin takımı nedir?", "dogru": "Task Force", "secenekler": ["SPK", "Task Force", "Wammy", "Kira"]},
    {"soru": "My Hero Academia'da Mashirao'nun Quirk'i nedir?", "dogru": "Tail", "secenekler": ["Explosion", "Tail", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Seidou'nun lakabı nedir?", "dogru": "Owl", "secenekler": ["Rabbit", "Owl", "Serpent", "Gourmet"]},
    {"soru": "Fullmetal Alchemist'te Dolcetto'nun gücü nedir?", "dogru": "Köpek formu", "secenekler": ["Güç", "Köpek formu", "Hız", "Zeka"]},
    {"soru": "Hunter x Hunter'da Komugi'nin oyunu nedir?", "dogru": "Gungi", "secenekler": ["Shogi", "Gungi", "Go", "Chess"]},
    {"soru": "Bleach'te Love'un Zanpakuto'sunun adı nedir?", "dogru": "Tengumaru", "secenekler": ["Zangetsu", "Tengumaru", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Ronie'nin unvanı nedir?", "dogru": "Integrity Knight adayı", "secenekler": ["Administrator", "Integrity Knight adayı", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Drive Knight'ın partneri kimdir?", "dogru": "Genos (geçici)", "secenekler": ["Saitama", "Genos (geçici)", "Bang", "King"]},
    {"soru": "Naruto'da Anko'nun sensei'si kimdir?", "dogru": "Orochimaru", "secenekler": ["Kakashi", "Orochimaru", "Jiraiya", "Tsunade"]},
    {"soru": "Attack on Titan'da Magath'in rütbesi nedir?", "dogru": "Mareşal", "secenekler": ["Komutan", "Mareşal", "Kaptan", "Teğmen"]},
    {"soru": "Demon Slayer'da Rui'nin Lower Rank'i nedir?", "dogru": "Lower Rank 5", "secenekler": ["1", "Lower Rank 5", "3", "4"]},
    {"soru": "Jujutsu Kaisen'de Kasumi'nin silahı nedir?", "dogru": "Kılıç", "secenekler": ["Naginata", "Kılıç", "Yay", "Mızrak"]},
    {"soru": "Dragon Ball'da Yajirobe'nin silahı nedir?", "dogru": "Katana", "secenekler": ["Kılıç", "Katana", "Mızrak", "Balta"]},
    {"soru": "Death Note'ta Ukita'nın takımı nedir?", "dogru": "Task Force", "secenekler": ["SPK", "Task Force", "Wammy", "Kira"]},
    {"soru": "My Hero Academia'da Toru'nun Quirk'i nedir?", "dogru": "Invisibility", "secenekler": ["Explosion", "Invisibility", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Matsumae'nin grubu nedir?", "dogru": "Tsukiyama", "secenekler": ["Aogiri", "Tsukiyama", "CCG", "Anteiku"]},
    {"soru": "Fullmetal Alchemist'te Roa'nın gücü nedir?", "dogru": "Boğa formu", "secenekler": ["Güç", "Boğa formu", "Hız", "Zeka"]},
    {"soru": "Hunter x Hunter'da Welfin'in gücü nedir?", "dogru": "Missile Man", "secenekler": ["Enhancement", "Missile Man", "Emission", "Manipulation"]},
    {"soru": "Bleach'te Hiyori'nin Zanpakuto'sunun adı nedir?", "dogru": "Kubikiri Orochi", "secenekler": ["Zangetsu", "Kubikiri Orochi", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Tiese'nin unvanı nedir?", "dogru": "Integrity Knight adayı", "secenekler": ["Administrator", "Integrity Knight adayı", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Watchdog Man'in gücü nedir?", "dogru": "Bilinmiyor (çok güçlü)", "secenekler": ["Hız", "Bilinmiyor (çok güçlü)", "Zeka", "Kas"]},
    {"soru": "Naruto'da Ibiki'nin uzmanlığı nedir?", "dogru": "İşkence / Sorgulama", "secenekler": ["Taijutsu", "İşkence / Sorgulama", "Genjutsu", "Ninjutsu"]},
    {"soru": "Attack on Titan'da Theo Magath'in ölümü nasıl olmuştur?", "dogru": "Savaşta", "secenekler": ["Hastalık", "Savaşta", "İntihar", "Kaza"]},
    {"soru": "Demon Slayer'da Susamaru'nun Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Momo'nun tekniği nedir?", "dogru": "Tool Manipulation", "secenekler": ["Domain", "Tool Manipulation", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Baba'nın gücü nedir?", "dogru": "Falcılık", "secenekler": ["Güç", "Falcılık", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Ide'nin rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Mezo'nun Quirk'i nedir?", "dogru": "Dupli-Arms", "secenekler": ["Explosion", "Dupli-Arms", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Hairu'nun lakabı nedir?", "dogru": "The Garden", "secenekler": ["Rabbit", "The Garden", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Martel'in grubu nedir?", "dogru": "Greed'in ekibi", "secenekler": ["Homunculus", "Greed'in ekibi", "Asker", "Alchemist"]},
    {"soru": "Hunter x Hunter'da Ikalgo'nun arkadaşı kimdir?", "dogru": "Killua", "secenekler": ["Gon", "Killua", "Kurapika", "Leorio"]},
    {"soru": "Bleach'te Mashiro'nun Zanpakuto'sunun adı nedir?", "dogru": "Yok (Hollow maske)", "secenekler": ["Zangetsu", "Yok (Hollow maske)", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Eldrie'nin unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Genos'un yaratıcısı kimdir?", "dogru": "Dr. Kuseno", "secenekler": ["Saitama", "Dr. Kuseno", "Metal Knight", "Child Emperor"]},
    {"soru": "Naruto'da Iruka'nın mesleği nedir?", "dogru": "Öğretmen", "secenekler": ["Hokage", "Öğretmen", "Anbu", "Jonin"]},
    {"soru": "Attack on Titan'da Nile Dok'un rütbesi nedir?", "dogru": "Komutan", "secenekler": ["Kaptan", "Komutan", "Teğmen", "Mareşal"]},
    {"soru": "Demon Slayer'da Yahaba'nın Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Arata'nın tekniği nedir?", "dogru": "Yok (destek)", "secenekler": ["Domain", "Yok (destek)", "Shikigami", "Idle"]},
    {"soru": "Dragon Ball'da Fortuneteller Baba'nın kardeşi kimdir?", "dogru": "Master Roshi", "secenekler": ["Kami", "Master Roshi", "Korin", "Popo"]},
    {"soru": "Death Note'ta Aizawa'nın rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Fumikage'nin Quirk'i nedir?", "dogru": "Dark Shadow", "secenekler": ["Explosion", "Dark Shadow", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Ui'nin lakabı nedir?", "dogru": "The T-Human", "secenekler": ["Rabbit", "The T-Human", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Roa'nın grubu nedir?", "dogru": "Greed'in ekibi", "secenekler": ["Homunculus", "Greed'in ekibi", "Asker", "Alchemist"]},
    {"soru": "Hunter x Hunter'da Meleoron'un arkadaşı kimdir?", "dogru": "Knuckle", "secenekler": ["Gon", "Knuckle", "Killua", "Morel"]},
    {"soru": "Bleach'te Lisa'nın Zanpakuto'sunun adı nedir?", "dogru": "Haguro Tonbo", "secenekler": ["Zangetsu", "Haguro Tonbo", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Fizel'in unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Child Emperor'un silahı nedir?", "dogru": "Okame-chan", "secenekler": ["Kılıç", "Okame-chan", "Silah", "Robot"]},
    {"soru": "Naruto'da Konohamaru'nun dedesi kimdir?", "dogru": "Üçüncü Hokage", "secenekler": ["Dördüncü", "Üçüncü Hokage", "Beşinci", "Altıncı"]},
    {"soru": "Attack on Titan'da Keith Shadis'in rütbesi nedir?", "dogru": "Eğitim birliği komutanı", "secenekler": ["Survey Corps", "Eğitim birliği komutanı", "Garnizon", "Askeri Polis"]},
    {"soru": "Demon Slayer'da Kyogai'nin Lower Rank'i nedir?", "dogru": "Lower Rank 6", "secenekler": ["1", "Lower Rank 6", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Mechamaru'nun okul arkadaşı kimdir?", "dogru": "Miwa", "secenekler": ["Todo", "Miwa", "Maki", "Panda"]},
    {"soru": "Dragon Ball'da Uranai Baba'nın hizmetkarı kimdir?", "dogru": "Mumiyola", "secenekler": ["Yajirobe", "Mumiyola", "Oolong", "Puar"]},
    {"soru": "Death Note'ta Mogi'nin rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Denki'nin Quirk'i nedir?", "dogru": "Electrification", "secenekler": ["Explosion", "Electrification", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Hirako'nun lakabı nedir?", "dogru": "The Reaper'in ortağı", "secenekler": ["Rabbit", "The Reaper'in ortağı", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Dolcetto'nun grubu nedir?", "dogru": "Greed'in ekibi", "secenekler": ["Homunculus", "Greed'in ekibi", "Asker", "Alchemist"]},
    {"soru": "Hunter x Hunter'da Welfin'in krala olan bağlılığı nedir?", "dogru": "Korku ve sadakat", "secenekler": ["Nefret", "Korku ve sadakat", "İntikam", "Hiçbir şey"]},
    {"soru": "Bleach'te Hachigen'in Zanpakuto'sunun adı nedir?", "dogru": "Yok (Kido uzmanı)", "secenekler": ["Zangetsu", "Yok (Kido uzmanı)", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Linel'in unvanı nedir?", "dogru": "Integrity Knight", "secenekler": ["Administrator", "Integrity Knight", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Zombi Man'in gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Moegi'nin takımı nedir?", "dogru": "Konohamaru'nun takımı", "secenekler": ["7. Takım", "Konohamaru'nun takımı", "10. Takım", "8. Takım"]},
    {"soru": "Attack on Titan'da Dot Pixis'in rütbesi nedir?", "dogru": "Garnizon komutanı", "secenekler": ["Survey Corps", "Garnizon komutanı", "Askeri Polis", "Eğitim"]},
    {"soru": "Demon Slayer'da Spider Family'nin lideri kimdir?", "dogru": "Rui", "secenekler": ["Muzan", "Rui", "Akaza", "Doma"]},
    {"soru": "Jujutsu Kaisen'de Panda'nın yaratıcısı kimdir?", "dogru": "Masamichi Yaga", "secenekler": ["Gojo", "Masamichi Yaga", "Geto", "Principal"]},
    {"soru": "Dragon Ball'da Upa'nın babasının adı nedir?", "dogru": "Bora", "secenekler": ["Korin", "Bora", "Roshi", "Kami"]},
    {"soru": "Death Note'ta Ukita'nın rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Kyoka'nın Quirk'i nedir?", "dogru": "Earphone Jack", "secenekler": ["Explosion", "Earphone Jack", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Arima'nın öğrencisi kimdir?", "dogru": "Haise Sasaki", "secenekler": ["Kaneki", "Haise Sasaki", "Furuta", "Ui"]},
    {"soru": "Fullmetal Alchemist'te Bido'nun gücü nedir?", "dogru": "Tırmanma", "secenekler": ["Güç", "Tırmanma", "Hız", "Zeka"]},
    {"soru": "Hunter x Hunter'da Palm'ın krala olan bağlılığı nedir?", "dogru": "Değişken", "secenekler": ["Sadakat", "Değişken", "Nefret", "Korku"]},
    {"soru": "Bleach'te Shinji'nin Zanpakuto'sunun adı nedir?", "dogru": "Sakanade", "secenekler": ["Zangetsu", "Sakanade", "Senbonzakura", "Ryujin Jakka"]},
    {"soru": "Sword Art Online'da Medina'nın unvanı nedir?", "dogru": "Integrity Knight adayı", "secenekler": ["Administrator", "Integrity Knight adayı", "Pontifex", "Cardinal"]},
    {"soru": "One Punch Man'de Pig God'un gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Udon'un takımı nedir?", "dogru": "Konohamaru'nun takımı", "secenekler": ["7. Takım", "Konohamaru'nun takımı", "10. Takım", "8. Takım"]},
    {"soru": "Attack on Titan'da Darius Zackly'nin rütbesi nedir?", "dogru": "Başkomutan", "secenekler": ["Komutan", "Başkomutan", "Mareşal", "Kaptan"]},
    {"soru": "Demon Slayer'da Hand Demon'un Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın mesleği nedir?", "dogru": "Müdür", "secenekler": ["Öğretmen", "Müdür", "Büyücü", "Doktor"]},
    {"soru": "Dragon Ball'da Giran'ın gücü nedir?", "dogru": "Yapışkan mukus", "secenekler": ["Güç", "Yapışkan mukus", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Matsuda'nın rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Hanta'nın Quirk'i nedir?", "dogru": "Tape", "secenekler": ["Explosion", "Tape", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Take'nin lakabı nedir?", "dogru": "The Reaper'in ortağı", "secenekler": ["Rabbit", "The Reaper'in ortağı", "Owl", "Serpent"]},
    {"soru": "Fullmetal Alchemist'te Ultimo'nun gücü nedir?", "dogru": "Patlama", "secenekler": ["Güç", "Patlama", "Hız", "Zeka"]},
    {"soru": "Hunter x Hunter'da Ikalgo'nun krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Hiyori'nin lakabı nedir?", "dogru": "The Angry", "secenekler": ["Captain", "The Angry", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Ronie'nin partneri kimdir?", "dogru": "Tiese", "secenekler": ["Alice", "Tiese", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Superalloy Darkshine'ın gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Hanabi'nin klanı nedir?", "dogru": "Hyuga", "secenekler": ["Uchiha", "Hyuga", "Aburame", "Inuzuka"]},
    {"soru": "Attack on Titan'da Nile'nin rütbesi nedir?", "dogru": "Askeri Polis komutanı", "secenekler": ["Survey Corps", "Askeri Polis komutanı", "Garnizon", "Eğitim"]},
    {"soru": "Demon Slayer'da Temple Demon'un Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Principal'in adı nedir?", "dogru": "Masamichi Yaga", "secenekler": ["Gojo", "Masamichi Yaga", "Geto", "Utahime"]},
    {"soru": "Dragon Ball'da Bacterian'ın gücü nedir?", "dogru": "Kötü koku", "secenekler": ["Güç", "Kötü koku", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Ide'nin rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Rikido'nun Quirk'i nedir?", "dogru": "Sugar Rush", "secenekler": ["Explosion", "Sugar Rush", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın ikizi kimdir?", "dogru": "Nashiro", "secenekler": ["Touka", "Nashiro", "Hinami", "Eto"]},
    {"soru": "Fullmetal Alchemist'te Martel'in ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Meleoron'un krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Hachigen'in lakabı nedir?", "dogru": "The Kido Master", "secenekler": ["Captain", "The Kido Master", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Tiese'nin partneri kimdir?", "dogru": "Ronie", "secenekler": ["Alice", "Ronie", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Atomic Samurai'nin öğrencileri kimlerdir?", "dogru": "Iaian, Okamaitachi, Bushidrill", "secenekler": ["Genos", "Iaian, Okamaitachi, Bushidrill", "Bang", "King"]},
    {"soru": "Naruto'da Himawari'nin klanı nedir?", "dogru": "Hyuga / Uzumaki", "secenekler": ["Uchiha", "Hyuga / Uzumaki", "Aburame", "Inuzuka"]},
    {"soru": "Attack on Titan'da Keith'in rütbesi nedir?", "dogru": "Eğitim birliği komutanı", "secenekler": ["Survey Corps", "Eğitim birliği komutanı", "Garnizon", "Askeri Polis"]},
    {"soru": "Demon Slayer'da Swamp Demon'un Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın yaratığı nedir?", "dogru": "Cursed Corpse (Panda)", "secenekler": ["Shikigami", "Cursed Corpse (Panda)", "Domain", "Technique"]},
    {"soru": "Dragon Ball'da Nam'un gücü nedir?", "dogru": "Kuyu suyu için savaş", "secenekler": ["Güç", "Kuyu suyu için savaş", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Aizawa'nın rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Yuga'nın Quirk'i nedir?", "dogru": "Navel Laser", "secenekler": ["Explosion", "Navel Laser", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Nashiro'nun ikizi kimdir?", "dogru": "Kurona", "secenekler": ["Touka", "Kurona", "Hinami", "Eto"]},
    {"soru": "Fullmetal Alchemist'te Roa'nın ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Welfin'in krala olan bağlılığı nedir?", "dogru": "Korku", "secenekler": ["Sadakat", "Korku", "İntikam", "Hiçbir şey"]},
    {"soru": "Bleach'te Lisa'nın lakabı nedir?", "dogru": "The Bookworm", "secenekler": ["Captain", "The Bookworm", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Medina'nın partneri kimdir?", "dogru": "Yok", "secenekler": ["Alice", "Yok", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Puri-Puri Prisoner'ın gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Boruto'nun klanı nedir?", "dogru": "Uzumaki / Hyuga", "secenekler": ["Uchiha", "Uzumaki / Hyuga", "Aburame", "Inuzuka"]},
    {"soru": "Attack on Titan'da Pixis'in rütbesi nedir?", "dogru": "Garnizon komutanı", "secenekler": ["Survey Corps", "Garnizon komutanı", "Askeri Polis", "Eğitim"]},
    {"soru": "Demon Slayer'da Tongue Demon'un Lower Rank'i nedir?", "dogru": "Yok (Lower Rank değil)", "secenekler": ["1", "Yok (Lower Rank değil)", "3", "5"]},
    {"soru": "Jujutsu Kaisen'de Principal'in yaratığı nedir?", "dogru": "Cursed Corpse", "secenekler": ["Shikigami", "Cursed Corpse", "Domain", "Technique"]},
    {"soru": "Dragon Ball'da Ranfan'ın gücü nedir?", "dogru": "Güzellik ile dikkat dağıtma", "secenekler": ["Güç", "Güzellik ile dikkat dağıtma", "Hız", "Zeka"]},
    {"soru": "Death Note'ta Mogi'nin rolü nedir?", "dogru": "Görev gücü üyesi", "secenekler": ["Lider", "Görev gücü üyesi", "Savcı", "Doktor"]},
    {"soru": "My Hero Academia'da Koji'nin Quirk'i nedir?", "dogru": "Anivoice", "secenekler": ["Explosion", "Anivoice", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın grubu nedir?", "dogru": "Aogiri / CCG", "secenekler": ["Anteiku", "Aogiri / CCG", "Tsukiyama", "White Suits"]},
    {"soru": "Fullmetal Alchemist'te Dolcetto'nun ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Palm'ın krala olan bağlılığı nedir?", "dogru": "Değişken", "secenekler": ["Sadakat", "Değişken", "Nefret", "Korku"]},
    {"soru": "Bleach'te Hiyori'nin lakabı nedir?", "dogru": "The Angry", "secenekler": ["Captain", "The Angry", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Fizel'in partneri kimdir?", "dogru": "Linel", "secenekler": ["Alice", "Linel", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Tanktop Master'ın gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Sarada'nın klanı nedir?", "dogru": "Uchiha", "secenekler": ["Uzumaki", "Uchiha", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Zackly'nin rütbesi nedir?", "dogru": "Başkomutan", "secenekler": ["Komutan", "Başkomutan", "Mareşal", "Kaptan"]},
    {"soru": "Demon Slayer'da Hand Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın öğrencisi kimdir?", "dogru": "Gojo, Geto, Shoko", "secenekler": ["Yuji", "Gojo, Geto, Shoko", "Megumi", "Nobara"]},
    {"soru": "Dragon Ball'da Giran'ın turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Ukita'nın ölümü nasıl olmuştur?", "dogru": "Kira tarafından", "secenekler": ["Hastalık", "Kira tarafından", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Mezo'nun Quirk'i nedir?", "dogru": "Dupli-Arms", "secenekler": ["Explosion", "Dupli-Arms", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Nashiro'nun grubu nedir?", "dogru": "Aogiri / CCG", "secenekler": ["Anteiku", "Aogiri / CCG", "Tsukiyama", "White Suits"]},
    {"soru": "Fullmetal Alchemist'te Ultimo'nun ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Ikalgo'nun krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Hachigen'in lakabı nedir?", "dogru": "The Kido Master", "secenekler": ["Captain", "The Kido Master", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Linel'in partneri kimdir?", "dogru": "Fizel", "secenekler": ["Alice", "Fizel", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Flashy Flash'in gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Mitsuki'nin yaratıcısı kimdir?", "dogru": "Orochimaru", "secenekler": ["Kabuto", "Orochimaru", "Sasuke", "Naruto"]},
    {"soru": "Attack on Titan'da Darius'un rütbesi nedir?", "dogru": "Başkomutan", "secenekler": ["Komutan", "Başkomutan", "Mareşal", "Kaptan"]},
    {"soru": "Demon Slayer'da Temple Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Principal'in öğrencileri kimlerdir?", "dogru": "Gojo, Geto, Shoko", "secenekler": ["Yuji", "Gojo, Geto, Shoko", "Megumi", "Nobara"]},
    {"soru": "Dragon Ball'da Nam'un turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Ide'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Toru'nun Quirk'i nedir?", "dogru": "Invisibility", "secenekler": ["Explosion", "Invisibility", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Bido'nun ölümü nasıl olmuştur?", "dogru": "Greed tarafından", "secenekler": ["Wrath", "Greed tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Meleoron'un krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Lisa'nın lakabı nedir?", "dogru": "The Bookworm", "secenekler": ["Captain", "The Bookworm", "Visored", "Hollow"]},
    {"soru": "Sword Art Online'da Medina'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Sweet Mask'in gerçek kimliği nedir?", "dogru": "Amai Mask", "secenekler": ["King", "Amai Mask", "Saitama", "Genos"]},
    {"soru": "Naruto'da Kawaki'nin yaratıcısı kimdir?", "dogru": "Jigen / Isshiki", "secenekler": ["Orochimaru", "Jigen / Isshiki", "Sasuke", "Naruto"]},
    {"soru": "Attack on Titan'da Nile'nin ölümü nasıl olmuştur?", "dogru": "Rumbling'de", "secenekler": ["Hastalık", "Rumbling'de", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Swamp Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Ranfan'ın turnuvadaki rakipleri kimlerdir?", "dogru": "Nam", "secenekler": ["Goku", "Nam", "Krillin", "Yamcha"]},
    {"soru": "Death Note'ta Aizawa'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Denki'nin Quirk'i nedir?", "dogru": "Electrification", "secenekler": ["Explosion", "Electrification", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Nashiro'nun ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Martel'in ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Welfin'in krala olan bağlılığı nedir?", "dogru": "Korku", "secenekler": ["Sadakat", "Korku", "İntikam", "Hiçbir şey"]},
    {"soru": "Bleach'te Hiyori'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Aizen", "Yok (sağ kaldı)", "Hollow", "Visored"]},
    {"soru": "Sword Art Online'da Fizel'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Metal Knight'ın gerçek adı nedir?", "dogru": "Bofoi", "secenekler": ["King", "Bofoi", "Child Emperor", "Drive Knight"]},
    {"soru": "Naruto'da Code'un klanı nedir?", "dogru": "Kara / Cyborg", "secenekler": ["Uchiha", "Kara / Cyborg", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Pixis'in ölümü nasıl olmuştur?", "dogru": "Rumbling'de", "secenekler": ["Hastalık", "Rumbling'de", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Tongue Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Principal'in ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Giran'ın turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Mogi'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Kyoka'nın Quirk'i nedir?", "dogru": "Earphone Jack", "secenekler": ["Explosion", "Earphone Jack", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Roa'nın ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Palm'ın krala olan bağlılığı nedir?", "dogru": "Değişken", "secenekler": ["Sadakat", "Değişken", "Nefret", "Korku"]},
    {"soru": "Bleach'te Hachigen'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Aizen", "Yok (sağ kaldı)", "Hollow", "Visored"]},
    {"soru": "Sword Art Online'da Linel'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Child Emperor'un gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Ada'nin klanı nedir?", "dogru": "Kara / Cyborg", "secenekler": ["Uchiha", "Kara / Cyborg", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Zackly'nin ölümü nasıl olmuştur?", "dogru": "Bombalı saldırı", "secenekler": ["Rumbling", "Bombalı saldırı", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Hand Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Nam'un turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Ide'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Hanta'nın Quirk'i nedir?", "dogru": "Tape", "secenekler": ["Explosion", "Tape", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Nashiro'nun ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Dolcetto'nun ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Ikalgo'nun krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Lisa'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Aizen", "Yok (sağ kaldı)", "Hollow", "Visored"]},
    {"soru": "Sword Art Online'da Medina'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Zombi Man'in gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Daemon'un klanı nedir?", "dogru": "Kara / Cyborg", "secenekler": ["Uchiha", "Kara / Cyborg", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Keith'in ölümü nasıl olmuştur?", "dogru": "Rumbling'de", "secenekler": ["Hastalık", "Rumbling'de", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Temple Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Principal'in ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Ranfan'ın turnuvadaki rakipleri kimlerdir?", "dogru": "Nam", "secenekler": ["Goku", "Nam", "Krillin", "Yamcha"]},
    {"soru": "Death Note'ta Aizawa'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Rikido'nun Quirk'i nedir?", "dogru": "Sugar Rush", "secenekler": ["Explosion", "Sugar Rush", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Ultimo'nun ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Meleoron'un krala olan bağlılığı nedir?", "dogru": "Yok (düşman)", "secenekler": ["Sadakat", "Yok (düşman)", "Korku", "İntikam"]},
    {"soru": "Bleach'te Hiyori'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Aizen", "Yok (sağ kaldı)", "Hollow", "Visored"]},
    {"soru": "Sword Art Online'da Fizel'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Pig God'un gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Eida'nin klanı nedir?", "dogru": "Kara / Cyborg", "secenekler": ["Uchiha", "Kara / Cyborg", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Pixis'in ölümü nasıl olmuştur?", "dogru": "Rumbling'de", "secenekler": ["Hastalık", "Rumbling'de", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Swamp Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Yaga'nın ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Giran'ın turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Mogi'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Yuga'nın Quirk'i nedir?", "dogru": "Navel Laser", "secenekler": ["Explosion", "Navel Laser", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Nashiro'nun ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Bido'nun ölümü nasıl olmuştur?", "dogru": "Greed tarafından", "secenekler": ["Wrath", "Greed tarafından", "Savaş", "Kaza"]},
    {"soru": "Hunter x Hunter'da Welfin'in krala olan bağlılığı nedir?", "dogru": "Korku", "secenekler": ["Sadakat", "Korku", "İntikam", "Hiçbir şey"]},
    {"soru": "Bleach'te Hachigen'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Aizen", "Yok (sağ kaldı)", "Hollow", "Visored"]},
    {"soru": "Sword Art Online'da Linel'in ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Administrator", "Yok (sağ kaldı)", "Eugeo", "Kirito"]},
    {"soru": "One Punch Man'de Superalloy Darkshine'ın gerçek adı nedir?", "dogru": "Bilinmiyor", "secenekler": ["King", "Bilinmiyor", "Genos", "Saitama"]},
    {"soru": "Naruto'da Bug'un klanı nedir?", "dogru": "Kara / Cyborg", "secenekler": ["Uchiha", "Kara / Cyborg", "Hyuga", "Aburame"]},
    {"soru": "Attack on Titan'da Zackly'nin ölümü nasıl olmuştur?", "dogru": "Bombalı saldırı", "secenekler": ["Rumbling", "Bombalı saldırı", "Savaş", "Kaza"]},
    {"soru": "Demon Slayer'da Tongue Demon'un ölümü nasıl olmuştur?", "dogru": "Tanjiro tarafından", "secenekler": ["Giyu", "Tanjiro tarafından", "Rengoku", "Muzan"]},
    {"soru": "Jujutsu Kaisen'de Principal'in ölümü nasıl olmuştur?", "dogru": "Yüksek Konsey tarafından", "secenekler": ["Sukuna", "Yüksek Konsey tarafından", "Gojo", "Geto"]},
    {"soru": "Dragon Ball'da Nam'un turnuvadaki rakipleri kimlerdir?", "dogru": "Goku", "secenekler": ["Krillin", "Goku", "Yamcha", "Tien"]},
    {"soru": "Death Note'ta Ide'nin ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kira", "Yok (sağ kaldı)", "Savaş", "Kaza"]},
    {"soru": "My Hero Academia'da Koji'nin Quirk'i nedir?", "dogru": "Anivoice", "secenekler": ["Explosion", "Anivoice", "Hardening", "Acid"]},
    {"soru": "Tokyo Ghoul'da Kurona'nın ölümü nasıl olmuştur?", "dogru": "Yok (sağ kaldı)", "secenekler": ["Kaneki", "Yok (sağ kaldı)", "Arima", "Furuta"]},
    {"soru": "Fullmetal Alchemist'te Martel'in ölümü nasıl olmuştur?", "dogru": "Wrath tarafından", "secenekler": ["Hastalık", "Wrath tarafından", "Savaş", "Kaza"]}
]


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
    print("Bot hazır!")


@client.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    icerik = message.content.strip()

    if icerik.lower() == "!köledailyxp":
        try:
            veri = kullanici_verisi_al(message.author.id)
            simdi = time.time()
            son_daily = veri.get("son_daily", 0)
            bekleme_suresi = 24 * 60 * 60
            fark = simdi - son_daily

            if fark < bekleme_suresi:
                kalan = int(bekleme_suresi - fark)
                saat = kalan // 3600
                dakika = (kalan % 3600) // 60
                await message.channel.send(
                    f"⏳ {message.author.mention}, günlük ödülünü zaten aldın! "
                    f"Tekrar almak için **{saat} saat {dakika} dakika** beklemen gerekiyor."
                )
                return

            kazanilan_xp = random.randint(350, 750)
            veri["xp"] += kazanilan_xp
            veri["son_daily"] = simdi

            seviye_atladi = False
            while veri["xp"] >= veri["sonraki_seviye_xp"]:
                veri["xp"] -= veri["sonraki_seviye_xp"]
                veri["seviye"] += 1
                veri["sonraki_seviye_xp"] += SEVIYE_XP_ARTISI
                seviye_atladi = True

            seviye_verisi_kaydet()

            await message.channel.send(
                f"🎁 {message.author.mention}, günlük ödülünü aldın: **+{kazanilan_xp} XP**!"
            )
            if seviye_atladi:
                kazanilan_rol = await seviye_rolu_ver(message.author, veri["seviye"])
                embed = seviye_atlama_embed(message.author, veri["seviye"], kazanilan_rol)
                hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
                await hedef_kanal.send(embed=embed)
        except Exception as e:
            print(f"!köledailyxp hatası: {e}")
        return

    if icerik.startswith("!"):
        return

    try:
        veri = kullanici_verisi_al(message.author.id)
        veri["xp"] += 5
        veri["mesaj_sayisi"] += 1

        seviye_atladi = False
        while veri["xp"] >= veri["sonraki_seviye_xp"]:
            veri["xp"] -= veri["sonraki_seviye_xp"]
            veri["seviye"] += 1
            veri["sonraki_seviye_xp"] += SEVIYE_XP_ARTISI
            seviye_atladi = True

        seviye_verisi_kaydet()

        if seviye_atladi:
            kazanilan_rol = await seviye_rolu_ver(message.author, veri["seviye"])
            embed = seviye_atlama_embed(message.author, veri["seviye"], kazanilan_rol)
            hedef_kanal = await seviye_mesaj_kanali_al(message.channel)
            await hedef_kanal.send(embed=embed)
    except Exception as e:
        print(f"Seviye sistemi hatası: {e}")


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
        hedef = kullanici or interaction.user
        veri = kullanici_verisi_al(hedef.id)
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
            "Boş çıktı! 🕸️",
            "10 Altın kazandın! 🪙",
            "Efsanevi Kılıç çıktı! 🗡️",
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


keep_alive()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("HATA: Discord Token bulunamadı!")
    else:
        client.run(DISCORD_TOKEN)