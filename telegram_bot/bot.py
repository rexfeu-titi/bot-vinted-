import asyncio
import base64
import io
import json
import logging
import os
import statistics
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vinted-bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED = int(os.getenv("TELEGRAM_USER_ID", "0") or 0)
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
CHECK_EVERY = max(15, int(os.getenv("CHECK_EVERY_SECONDS", "30")))  # jamais sous 15 s
DATA = Path("alerts.json")

ETATS = ["Neuf avec étiquette", "Neuf sans étiquette", "Très bon état", "Bon état", "Satisfaisant"]

SYSTEM = f"""Tu es un expert de la revente de vêtements et accessoires sur Vinted France.
Tu analyses les photos d'un article et tu prépares l'annonce.

Règles strictes :
- Marque et taille : ne les renseigne que si tu les vois (étiquette, logo, col, semelle).
  Sinon mets null et ajoute le champ à "a_verifier". N'invente JAMAIS.
- Type d'article et couleur : déduis-les des photos. Matière seulement si lisible, sinon null.
- "etat" doit être exactement l'une de ces valeurs : {ETATS}.
- Titre : max 60 caractères (marque, type, taille, couleur, atout).
- Description : en français, simple et sympa, honnête, avec les défauts visibles,
  sans info inventée. Mentionne l'envoi rapide et la possibilité de faire un lot.
- Prix : suggestion réaliste en euros pour de l'occasion.

Réponds UNIQUEMENT avec un objet JSON :
{{"titre": "", "marque": null, "type_article": "", "taille": null, "couleur": "",
"matiere": null, "etat": "", "description": "", "hashtags": [],
"prix_suggere": 0, "a_verifier": [], "conseil": ""}}"""


# ------------------------- Claude -------------------------
def to_block(raw: bytes) -> dict:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((1568, 1568))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def analyze(photos: list, notes: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    content = [to_block(p) for p in photos]
    content.append({"type": "text", "text": f"Infos connues du vendeur : {notes or 'aucune'}"})
    r = client.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in r.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


# ------------------------- Vinted -------------------------
VINTED = "https://www.vinted.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "application/json, text/plain, */*",
}


SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def refresh_cookies():
    SESSION.cookies.clear()
    SESSION.get(VINTED, timeout=15)


def vinted_search(query: str, price_max: int) -> list:
    if not SESSION.cookies:
        refresh_cookies()
    params = {"search_text": query, "order": "newest_first", "per_page": 48}
    if price_max:
        params["price_to"] = price_max
    url = f"{VINTED}/api/v2/catalog/items"
    r = SESSION.get(url, params=params, timeout=15)
    if r.status_code in (401, 403):  # session expirée : on la renouvelle une fois
        refresh_cookies()
        r = SESSION.get(url, params=params, timeout=15)
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        price = it.get("price")
        if isinstance(price, dict):
            price = price.get("amount")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        url = it.get("url") or f"/items/{it.get('id')}"
        out.append({
            "id": it.get("id"), "titre": it.get("title"), "prix": price,
            "marque": it.get("brand_title"), "taille": it.get("size_title"),
            "etat": it.get("status"),
            "url": VINTED + url if url.startswith("/") else url,
            "photo": (it.get("photo") or {}).get("url"),
        })
    return out


# ------------------------- Stockage des alertes -------------------------
def load() -> dict:
    if DATA.exists():
        return json.loads(DATA.read_text())
    return {"next_id": 1, "alerts": [], "blocked": False}


def save(d: dict):
    DATA.write_text(json.dumps(d, ensure_ascii=False))


# ------------------------- Sécurité -------------------------
def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED and update.effective_user.id == ALLOWED:
            return await func(update, context)
        await update.message.reply_text("⛔ Accès refusé. Ce bot est privé.")
    return wrapper


# ------------------------- Commandes -------------------------
HELP = (
    "🛍️ Bot Vinted\n\n"
    "📸 Créer une annonce :\n"
    "  1. Envoie tes photos (étiquette et défauts inclus)\n"
    "  2. Tape /annonce (tu peux ajouter des infos : /annonce porté 2 fois)\n"
    "  /reset pour vider les photos\n\n"
    "🔥 Alertes bons plans :\n"
    "  /alerte veste carhartt L | 40 | 30\n"
    "     (recherche | prix max € | % moins cher que la médiane)\n"
    "  /alertes pour voir la liste\n"
    "  /stop 1 pour supprimer l'alerte n°1"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ALLOWED and uid == ALLOWED:
        await update.message.reply_text(HELP)
    elif not ALLOWED:
        await update.message.reply_text(
            f"Ton ID Telegram est : {uid}\n"
            "Mets-le dans le fichier .env (TELEGRAM_USER_ID), puis relance le bot."
        )
    else:
        await update.message.reply_text("⛔ Accès refusé. Ce bot est privé.")


@authorized
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])
    if len(photos) >= 8:
        await update.message.reply_text("8 photos maximum. Tape /annonce.")
        return
    f = await update.message.photo[-1].get_file()
    photos.append(bytes(await f.download_as_bytearray()))
    if update.message.caption:
        context.user_data["notes"] = update.message.caption
    await update.message.reply_text(
        f"📸 Photo {len(photos)}/8 reçue. Envoie-en d'autres (étiquette, défauts) "
        "ou tape /annonce."
    )


@authorized
async def annonce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text("Envoie d'abord au moins une photo.")
        return
    notes = " ".join(context.args) or context.user_data.get("notes", "")
    await update.message.reply_text("⏳ Analyse en cours...")
    try:
        r = await asyncio.to_thread(analyze, photos, notes)
    except Exception as e:
        log.exception("analyse")
        await update.message.reply_text(f"Erreur pendant l'analyse : {e}")
        return
    context.user_data.clear()

    fiche = (
        "📋 Fiche détectée\n"
        f"Marque : {r.get('marque') or '❓'}\n"
        f"Type : {r.get('type_article') or '❓'}\n"
        f"Taille : {r.get('taille') or '❓'}\n"
        f"Couleur : {r.get('couleur') or '-'}\n"
        f"Matière : {r.get('matiere') or '-'}\n"
        f"État : {r.get('etat') or '-'}\n"
        f"Prix suggéré : {r.get('prix_suggere', '?')} €"
    )
    if r.get("a_verifier"):
        fiche += "\n\n⚠️ À vérifier : " + ", ".join(r["a_verifier"])
    if r.get("conseil"):
        fiche += f"\n💡 {r['conseil']}"
    await update.message.reply_text(fiche)
    await update.message.reply_text(r.get("titre", ""))
    await update.message.reply_text(
        r.get("description", "") + "\n\n" + " ".join(r.get("hashtags", []))
    )


@authorized
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🗑️ Photos supprimées.")


@authorized
async def alerte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    if not parts[0]:
        await update.message.reply_text("Exemple : /alerte veste carhartt L | 40 | 30")
        return
    try:
        pmax = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        seuil = int(parts[2]) if len(parts) > 2 and parts[2] else 30
    except ValueError:
        await update.message.reply_text("Le prix max et le pourcentage doivent être des nombres.")
        return
    d = load()
    a = {"id": d["next_id"], "query": parts[0], "pmax": pmax, "seuil": seuil, "seen": [], "init": False}
    d["next_id"] += 1
    d["alerts"].append(a)
    save(d)
    await update.message.reply_text(
        f"✅ Alerte #{a['id']} créée : « {a['query']} »"
        + (f", max {pmax} €" if pmax else "")
        + f", au moins {seuil}% moins cher que la médiane.\n"
        "Les annonces déjà en ligne sont ignorées, je te préviens des nouvelles."
    )


@authorized
async def alertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load()
    if not d["alerts"]:
        await update.message.reply_text("Aucune alerte. Crée-en une avec /alerte.")
        return
    lines = [
        f"#{a['id']} : {a['query']}" + (f" (max {a['pmax']} €)" if a["pmax"] else "") + f" | {a['seuil']}%"
        for a in d["alerts"]
    ]
    await update.message.reply_text("\n".join(lines))


@authorized
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Exemple : /stop 1")
        return
    n = int(context.args[0])
    d = load()
    before = len(d["alerts"])
    d["alerts"] = [a for a in d["alerts"] if a["id"] != n]
    save(d)
    await update.message.reply_text("🗑️ Alerte supprimée." if len(d["alerts"]) < before else "Alerte introuvable.")


# ------------------------- Surveillance -------------------------
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    d = load()
    for a in d["alerts"]:
        try:
            items = await asyncio.to_thread(vinted_search, a["query"], a["pmax"])
            d["blocked"] = False
        except requests.HTTPError as e:
            log.warning("Vinted a répondu %s", e.response.status_code)
            if not d.get("blocked"):
                d["blocked"] = True
                await context.bot.send_message(
                    ALLOWED,
                    f"⚠️ Vinted refuse les requêtes (code {e.response.status_code}). "
                    "Je réessaie automatiquement. Si ça dure, ton adresse IP est "
                    "sûrement bloquée (essaie depuis une connexion personnelle).",
                )
            break
        except Exception:
            log.exception("recherche")
            continue

        ids = [i["id"] for i in items]
        if not a["init"]:  # première passe : on mémorise sans notifier
            a["seen"], a["init"] = ids, True
            continue
        prices = [i["prix"] for i in items]
        if len(prices) < 4:
            continue
        med = statistics.median(prices)
        seen = set(a["seen"])
        deals = [
            i for i in items
            if i["id"] not in seen and i["prix"] <= med * (1 - a["seuil"] / 100)
        ]
        a["seen"] = (list(seen | set(ids)))[-500:]

        for i in sorted(deals, key=lambda x: x["prix"])[:5]:
            ecart = round((med - i["prix"]) / med * 100)
            caption = (
                f"🔥 Bon plan (alerte #{a['id']})\n{i['titre']}\n"
                f"💶 {i['prix']:.0f} € (−{ecart}% vs médiane {med:.0f} €)\n"
                f"🏷 {i['marque'] or '?'} · {i['taille'] or '?'} · {i['etat'] or '?'}"
            )
            # Boutons-liens : ils n'agissent que si tu appuies dessus
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Acheter", url=i["url"]),
                InlineKeyboardButton("💬 Contacter", url=i["url"]),
            ]])
            try:
                if i["photo"]:
                    await context.bot.send_photo(
                        ALLOWED, i["photo"], caption=caption, reply_markup=keyboard
                    )
                else:
                    await context.bot.send_message(ALLOWED, caption, reply_markup=keyboard)
            except Exception:
                await context.bot.send_message(ALLOWED, caption, reply_markup=keyboard)
            await asyncio.sleep(1.1)  # limite d'envoi de Telegram
        await asyncio.sleep(1)  # pause entre deux alertes
    save(d)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("annonce", annonce))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("alerte", alerte))
    app.add_handler(CommandHandler("alertes", alertes))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    if ALLOWED:
        app.job_queue.run_repeating(check_alerts, interval=CHECK_EVERY, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
