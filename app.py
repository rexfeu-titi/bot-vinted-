import base64
import io
import json
import os

import anthropic
import streamlit as st
from PIL import Image

MODEL = "claude-sonnet-5-5"  # modifiable si besoin

SYSTEM_PROMPT = """Tu es un expert de la vente d'occasion sur Vinted en France.
À partir des photos et des infos fournies, tu rédiges une annonce efficace.

Règles :
- Titre : max 60 caractères, avec marque, type d'article, taille et un atout.
- Description : claire, honnête, en français, avec le style demandé. Mentionne l'état,
  la taille, la matière si visible, les défauts éventuels, et les conditions
  (envoi rapide, lot possible...). N'invente aucune information que tu ne vois pas
  sur les photos et qui n'est pas fournie.
- Hashtags : 5 à 8, pertinents.
- Prix : une suggestion réaliste en euros pour de l'occasion.

Réponds UNIQUEMENT avec un objet JSON, sans texte autour ni balises Markdown :
{"titre": "...", "description": "...", "categorie": "...", "hashtags": ["#..."], "prix_suggere": 0, "conseil": "..."}
"""


def prepare_image(uploaded_file) -> dict:
    """Redimensionne l'image et la convertit en bloc image base64 pour l'API."""
    img = Image.open(uploaded_file).convert("RGB")
    img.thumbnail((1568, 1568))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    data = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def generate_listing(api_key, files, infos) -> dict:
    client = anthropic.Anthropic(api_key=api_key)

    content = [prepare_image(f) for f in files]
    content.append(
        {
            "type": "text",
            "text": (
                "Infos sur l'article :\n"
                f"- Marque : {infos['marque'] or 'non précisée'}\n"
                f"- Type d'article : {infos['type'] or 'à déduire des photos'}\n"
                f"- Taille : {infos['taille'] or 'non précisée'}\n"
                f"- État : {infos['etat']}\n"
                f"- Prix souhaité : {infos['prix'] or 'à suggérer'}\n"
                f"- Défauts / remarques : {infos['remarques'] or 'aucun'}\n"
                f"- Style de description : {infos['style']}\n"
            ),
        }
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ---------------------------- Interface ----------------------------

st.set_page_config(page_title="Vinted Bot", page_icon="🛍️", layout="wide")
st.title("🛍️ Générateur d'annonces Vinted")

if "history" not in st.session_state:
    st.session_state.history = []

with st.sidebar:
    st.header("Configuration")
    api_key = st.text_input(
        "Clé API Anthropic",
        type="password",
        value=os.getenv("ANTHROPIC_API_KEY", ""),
        help="Ou définis la variable d'environnement ANTHROPIC_API_KEY.",
    )
    style = st.selectbox(
        "Style de description",
        ["Simple et sympa", "Professionnel", "Court et efficace", "Détaillé"],
    )
    st.divider()
    st.caption("Historique de la session")
    for i, item in enumerate(reversed(st.session_state.history)):
        if st.button(item["titre"][:40], key=f"hist_{i}"):
            st.session_state.current = item

col_form, col_result = st.columns(2)

with col_form:
    st.subheader("1. Ton article")
    files = st.file_uploader(
        "Photos (jusqu'à 5)",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )
    if files:
        files = files[:5]
        st.image(files, width=120)

    c1, c2 = st.columns(2)
    marque = c1.text_input("Marque")
    type_article = c2.text_input("Type d'article", placeholder="ex : veste en jean")
    taille = c1.text_input("Taille")
    prix = c2.text_input("Prix souhaité (€)")
    etat = st.selectbox(
        "État",
        [
            "Neuf avec étiquette",
            "Neuf sans étiquette",
            "Très bon état",
            "Bon état",
            "Satisfaisant",
        ],
        index=2,
    )
    remarques = st.text_area("Défauts / remarques", height=80)

    if st.button("✨ Générer l'annonce", type="primary", use_container_width=True):
        if not api_key:
            st.error("Ajoute ta clé API dans la barre latérale.")
        elif not files:
            st.error("Ajoute au moins une photo.")
        else:
            infos = {
                "marque": marque,
                "type": type_article,
                "taille": taille,
                "prix": prix,
                "etat": etat,
                "remarques": remarques,
                "style": style,
            }
            with st.spinner("Rédaction en cours..."):
                try:
                    result = generate_listing(api_key, files, infos)
                    st.session_state.current = result
                    st.session_state.history.append(result)
                except json.JSONDecodeError:
                    st.error("La réponse n'était pas au bon format, réessaie.")
                except anthropic.APIError as e:
                    st.error(f"Erreur API : {e}")

with col_result:
    st.subheader("2. Ton annonce")
    result = st.session_state.get("current")
    if result:
        titre = st.text_input("Titre", value=result.get("titre", ""))
        st.caption(f"{len(titre)}/60 caractères")
        st.text_area(
            "Description",
            value=result.get("description", "")
            + "\n\n"
            + " ".join(result.get("hashtags", [])),
            height=300,
        )
        m1, m2 = st.columns(2)
        m1.metric("Prix suggéré", f"{result.get('prix_suggere', '?')} €")
        m2.write(f"**Catégorie :** {result.get('categorie', '-')}")
        if result.get("conseil"):
            st.info(result["conseil"])

        st.caption("Copie rapide (bouton en haut à droite du bloc) :")
        st.code(titre, language=None)
        st.code(
            result.get("description", "") + "\n\n" + " ".join(result.get("hashtags", [])),
            language=None,
        )
    else:
        st.write("Ton annonce apparaîtra ici.")
