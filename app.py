import base64
import io
import json
import os
import statistics

import anthropic
import requests
import streamlit as st
from PIL import Image

st.set_page_config(page_title="Vinted Bot", page_icon="🛍️", layout="wide")

MODELS = {
    "Précis (Sonnet)": "claude-sonnet-5-5",
    "Économique (Haiku)": "claude-haiku-5-5",
}
STYLES = ["Simple et sympa", "Professionnel", "Court et efficace", "Détaillé"]
ETATS = [
    "Neuf avec étiquette",
    "Neuf sans étiquette",
    "Très bon état",
    "Bon état",
    "Satisfaisant",
]


# ------------------------- Utilitaires -------------------------
def secret(name, default=""):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)


def check_password():
    pwd = secret("APP_PASSWORD")
    if not pwd or st.session_state.get("auth"):
        return True
    entered = st.text_input("Mot de passe", type="password")
    if entered:
        if entered == pwd:
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    return False


def parse_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def prepare_image(f):
    img = Image.open(f).convert("RGB")
    img.thumbnail((1568, 1568))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def ask_claude(system, content, max_tokens=1500):
    client = anthropic.Anthropic(api_key=API_KEY)
    r = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in r.content if b.type == "text")


# ------------------------- Analyse des photos -------------------------
ANALYSE_SYSTEM = f"""Tu es un expert de la revente de vêtements et accessoires sur Vinted France.
Tu analyses les photos d'un article et tu prépares l'annonce.

Règles strictes :
- Marque et taille : ne les renseigne que si tu les vois (étiquette, logo, col, semelle,
  inscription). Sinon mets null et ajoute le champ à "a_verifier". N'invente JAMAIS.
- Type d'article, couleur, matière : déduis-les des photos (matière seulement si visible
  sur l'étiquette, sinon null).
- "etat" doit être exactement l'une de ces valeurs : {ETATS}. Base-toi sur ce que tu vois
  et signale les défauts visibles dans la description.
- Titre : max 60 caractères (marque, type, taille, couleur, atout).
- Description : en français, honnête, sans info inventée.
- Prix : suggestion réaliste en euros pour de l'occasion.

Réponds UNIQUEMENT avec un objet JSON :
{{"titre": "", "marque": null, "type_article": "", "taille": null, "couleur": "",
"matiere": null, "etat": "", "description": "", "categorie": "", "hashtags": [],
"prix_suggere": 0, "a_verifier": [], "conseil": ""}}"""


def analyze_photos(files, style, notes):
    content = [prepare_image(f) for f in files]
    content.append(
        {
            "type": "text",
            "text": f"Style de description : {style}\n"
            f"Infos connues du vendeur : {notes or 'aucune'}",
        }
    )
    return parse_json(ask_claude(ANALYSE_SYSTEM, content))


def rewrite_description(fields, style):
    prompt = (
        "Réécris le titre et la description de cette annonce Vinted en tenant compte "
        f"de ces infos (corrigées par le vendeur), style : {style}.\n"
        f"{json.dumps(fields, ensure_ascii=False)}\n"
        'Réponds en JSON : {"titre": "", "description": "", "hashtags": []}'
    )
    return parse_json(ask_claude("Tu rédiges des annonces Vinted en français.", prompt))


# ------------------------- Recherche Vinted -------------------------
VINTED = "https://www.vinted.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "application/json, text/plain, */*",
}


def vinted_search(query, price_min, price_max, per_page):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(VINTED, timeout=15)  # récupère les cookies de session
    params = {"search_text": query, "order": "newest_first", "per_page": per_page}
    if price_min:
        params["price_from"] = price_min
    if price_max:
        params["price_to"] = price_max
    r = s.get(f"{VINTED}/api/v2/catalog/items", params=params, timeout=15)
    r.raise_for_status()
    return [normalize(i) for i in r.json().get("items", [])]


def normalize(it):
    price = it.get("price")
    if isinstance(price, dict):
        price = price.get("amount")
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = None
    url = it.get("url") or f"/items/{it.get('id')}"
    if url.startswith("/"):
        url = VINTED + url
    return {
        "id": it.get("id"),
        "titre": it.get("title"),
        "prix": price,
        "marque": it.get("brand_title"),
        "taille": it.get("size_title"),
        "etat": it.get("status"),
        "url": url,
        "photo": (it.get("photo") or {}).get("url"),
    }


def score_deals(items):
    prices = [i["prix"] for i in items if i["prix"]]
    if len(prices) < 4:
        return items, None
    med = statistics.median(prices)
    for i in items:
        i["ecart"] = round((med - i["prix"]) / med * 100) if i["prix"] else 0
    return items, med


def ai_pick_deals(items, query):
    lines = [
        {k: i[k] for k in ("id", "titre", "prix", "marque", "taille", "etat")}
        for i in items[:15]
    ]
    prompt = (
        f"Recherche : {query}\nAnnonces Vinted (prix en €) :\n"
        f"{json.dumps(lines, ensure_ascii=False)}\n\n"
        "Choisis jusqu'à 5 vrais bons plans (prix bas par rapport à la valeur réelle "
        "de l'article d'occasion). Méfie-toi des prix trop beaux pour être vrais "
        "(contrefaçons, arnaques). Réponds en JSON : "
        '{"picks": [{"id": 0, "valeur_estimee": 0, "raison": "", "risque": ""}]}'
    )
    return parse_json(
        ask_claude("Tu es un expert de la revente d'occasion.", prompt)
    ).get("picks", [])


# ------------------------- Interface -------------------------
if not check_password():
    st.stop()

API_KEY = secret("ANTHROPIC_API_KEY")
with st.sidebar:
    st.header("Configuration")
    if not API_KEY:
        API_KEY = st.text_input("Clé API Anthropic", type="password")
    model_label = st.selectbox("Modèle", list(MODELS))
    style = st.selectbox("Style de description", STYLES)
MODEL = MODELS[model_label]

st.title("🛍️ Vinted Bot")
tab_create, tab_deals = st.tabs(["📸 Créer une annonce", "🔥 Bons plans"])

# ---------- Onglet 1 : création ----------
with tab_create:
    files = st.file_uploader(
        "Photos de l'article (étiquette et défauts inclus si possible)",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )
    if files:
        files = files[:8]
        st.image(files, width=110)
    notes = st.text_input(
        "Infos que tu connais déjà (optionnel)", placeholder="ex : porté 2 fois, taille M"
    )

    if st.button("✨ Analyser les photos", type="primary"):
        if not API_KEY:
            st.error("Ajoute ta clé API (Secrets ou barre latérale).")
        elif not files:
            st.error("Ajoute au moins une photo.")
        else:
            with st.spinner("Analyse en cours..."):
                try:
                    st.session_state.listing = analyze_photos(files, style, notes)
                    st.session_state.lid = st.session_state.get("lid", 0) + 1
                except (json.JSONDecodeError, ValueError):
                    st.error("Réponse illisible, réessaie.")
                except anthropic.APIError as e:
                    st.error(f"Erreur API : {e}")

    L = st.session_state.get("listing")
    if L:
        n = st.session_state.lid
        if L.get("a_verifier"):
            st.warning("À vérifier : " + ", ".join(L["a_verifier"]))
        c1, c2, c3 = st.columns(3)
        marque = c1.text_input("Marque", L.get("marque") or "", key=f"m{n}")
        type_a = c2.text_input("Type", L.get("type_article") or "", key=f"t{n}")
        taille = c3.text_input("Taille", L.get("taille") or "", key=f"s{n}")
        c4, c5, c6 = st.columns(3)
        couleur = c4.text_input("Couleur", L.get("couleur") or "", key=f"c{n}")
        matiere = c5.text_input("Matière", L.get("matiere") or "", key=f"mat{n}")
        etat_idx = ETATS.index(L["etat"]) if L.get("etat") in ETATS else 2
        etat = c6.selectbox("État", ETATS, index=etat_idx, key=f"e{n}")

        titre = st.text_input("Titre", L.get("titre", ""), key=f"ti{n}")
        st.caption(f"{len(titre)}/60 caractères")
        desc = st.text_area(
            "Description",
            L.get("description", "") + "\n\n" + " ".join(L.get("hashtags", [])),
            height=260,
            key=f"d{n}",
        )
        st.metric("Prix suggéré", f"{L.get('prix_suggere', '?')} €")
        if L.get("conseil"):
            st.info(L["conseil"])

        if st.button("🔄 Réécrire avec mes corrections"):
            fields = dict(marque=marque, type=type_a, taille=taille, couleur=couleur,
                          matiere=matiere, etat=etat, defauts_et_notes=notes)
            with st.spinner("Réécriture..."):
                try:
                    new = rewrite_description(fields, style)
                    L.update(titre=new["titre"], description=new["description"],
                             hashtags=new.get("hashtags", []))
                    st.session_state.lid += 1
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur : {e}")

        st.caption("Copie rapide (bouton en haut à droite de chaque bloc) :")
        st.code(titre, language=None)
        st.code(desc, language=None)

# ---------- Onglet 2 : bons plans ----------
with tab_deals:
    st.caption(
        "Recherche publique sur Vinted (sans ton compte). Plus la recherche est "
        "précise, plus la comparaison de prix est fiable."
    )
    q = st.text_input("Recherche", placeholder="ex : veste Carhartt L")
    d1, d2, d3 = st.columns(3)
    pmin = d1.number_input("Prix min (€)", 0, 1000, 0)
    pmax = d2.number_input("Prix max (€)", 0, 1000, 50)
    seuil = d3.slider("Bon plan = moins cher que la médiane de (%)", 10, 70, 30)

    if st.button("🔎 Chercher", type="primary"):
        if not q:
            st.error("Tape une recherche.")
        else:
            with st.spinner("Recherche sur Vinted..."):
                try:
                    items = vinted_search(q, pmin or None, pmax or None, 48)
                    items, med = score_deals(items)
                    seen = st.session_state.get("seen", set())
                    for i in items:
                        i["nouveau"] = i["id"] not in seen
                    st.session_state.seen = seen | {i["id"] for i in items}
                    st.session_state.deals = (q, items, med)
                except requests.HTTPError as e:
                    code = e.response.status_code
                    st.error(
                        f"Vinted a refusé la requête (code {code}). Les serveurs "
                        "d'hébergement gratuits sont souvent bloqués par Vinted. "
                        "Essaie plus tard ou lance l'appli sur ton propre ordinateur."
                    )
                except Exception as e:
                    st.error(f"Erreur de recherche : {e}")

    if "deals" in st.session_state:
        query, items, med = st.session_state.deals
        if not items:
            st.info("Aucun résultat.")
        elif med is None:
            st.warning("Trop peu de résultats pour comparer les prix. Élargis la recherche.")
        else:
            st.write(f"Prix médian des résultats : **{med:.0f} €**")
            good = sorted(
                (i for i in items if i.get("ecart", 0) >= seuil),
                key=lambda i: -i["ecart"],
            )
            st.subheader(f"{len(good)} bon(s) plan(s)")

            if good and API_KEY and st.button("🧠 Analyse IA des meilleurs"):
                with st.spinner("Analyse..."):
                    try:
                        by_id = {i["id"]: i for i in items}
                        for p in ai_pick_deals(good, query):
                            it = by_id.get(p.get("id"))
                            if it:
                                st.success(
                                    f"**{it['titre']}** à {it['prix']:.0f} € "
                                    f"(valeur estimée {p.get('valeur_estimee')} €)\n\n"
                                    f"{p.get('raison', '')}\n\n"
                                    f"⚠️ {p.get('risque', '')}\n\n[Voir l'annonce]({it['url']})"
                                )
                    except Exception as e:
                        st.error(f"Erreur : {e}")

            cols = st.columns(3)
            for idx, i in enumerate(good):
                with cols[idx % 3]:
                    if i["photo"]:
                        st.image(i["photo"], use_container_width=True)
                    badge = "🆕 " if i["nouveau"] else ""
                    st.markdown(
                        f"{badge}**{i['prix']:.0f} €** (−{i['ecart']}%)  \n"
                        f"{i['titre']}  \n"
                        f"{i['marque'] or ''} · {i['taille'] or ''} · {i['etat'] or ''}  \n"
                        f"[Voir l'annonce]({i['url']})"
                    )
