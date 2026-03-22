from flask import Flask, request, jsonify, send_from_directory
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import re

app = Flask(__name__, static_folder="../public", static_url_path="")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS = json.loads(os.environ.get("GOOGLE_CREDENTIALS", "{}"))
SHEET_ID = os.environ.get("SHEET_ID", "1KMh4GHJzTKXEmYWoH3jGbVG21_V-fJqcorAlM_mvIK0")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

PRODUCT_MAP = {
    "2939": "Mold-Off Anti-Mucegai",
    "4001": "Softner Balsam Rufe",
    "2403": "Carpet Automat",
    "2401": "Carpet Manual",
    "2341": "Clever Enzymatic",
    "3101": "Hipoclorit Clor Parfumat",
    "2001": "Crystal Clear",
    "2901": "Plita Gel Degresant",
    "2120": "Hard Detergent-Dezinfectant",
    "2340": "Clever Automat Detergent Rufe",
    "2105": "Clean Dish Detergent Masina Vase",
    "2910": "Floor Shine Manual Pardoseli",
    "3151": "Interi Air Odorizant",
    "3005": "Oxygen Inalbitor Rufe",
    "1111": "Classic Sapun Lichid",
}

PRODUCT_MAP_STR = "\n".join([f"{cod} = {nume}" for cod, nume in PRODUCT_MAP.items()])


def get_sheet():
    creds = Credentials.from_service_account_info(GOOGLE_CREDENTIALS, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet("Inventar")


def extract_invoice_data(pdf_base64):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_base64,
                    }
                },
                {
                    "type": "text",
                    "text": f"""Ești un asistent pentru firma Royal Axix care vinde produse de curățenie Konga/Lebon.

Extrage din această factură DOAR produsele, fără prețuri.
Răspunde EXCLUSIV cu JSON valid, fără text extra:

{{
  "numar_factura": "...",
  "data": "...",
  "produse": [
    {{
      "cod": "...",
      "nume": "...",
      "aroma": "...",
      "aroma_auto": true,
      "dimensiune": "...",
      "cantitate": 0
    }}
  ]
}}

Produse cunoscute (cod → nume):
{PRODUCT_MAP_STR}

Reguli:
- Extrage codul numeric din denumirea produsului (ex: 2401, 4001, 3101 etc.)
- Pentru aromă: dacă e menționată EXPLICIT în denumire (ex: LILIAC, SPRING, OCEAN, VANILLA, FLORAL, LAVANDA), pune aroma și setează aroma_auto: true
- Dacă aroma NU e menționată, pune aroma: null și aroma_auto: false
- Pentru dimensiune: extrage exact (750ML, 1L, 5L etc.)
- cantitate = numărul întreg de bucăți
- Nu inventa arome care nu sunt scrise explicit"""
                }
            ]
        }]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)


def process_chat(history, existing_products):
    """
    Conversational endpoint. Understands free Romanian text about products.
    When user confirms, returns CONFIRMED + JSON.
    Validates products against known PRODUCT_MAP.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build context string about existing products
    existing_context = ""
    if existing_products:
        lines = []
        for p in existing_products:
            lines.append(f"  - {p.get('cod','?')} | {p.get('nume','?')} | {p.get('aroma','—')} | {p.get('dimensiune','?')} | {p.get('cantitate','?')} buc")
        existing_context = f"""
Produse deja în tabel (din factură PDF):
{chr(10).join(lines)}

Utilizatorul vrea să ADAUGE produse noi la acestea, nu să le înlocuiască.
"""
    else:
        existing_context = "Tabelul este gol momentan — utilizatorul introduce produse de la zero."

    system_prompt = f"""Ești un asistent de inventar pentru firma Royal Axix (produse curățenie Konga/Lebon).

{existing_context}

PRODUSE DISPONIBILE (LISTA COMPLETĂ - cod → nume):
{PRODUCT_MAP_STR}

SARCINA TA:
1. Utilizatorul îți spune ce produse vrea să adauge (în limbaj liber, română)
2. Tu înțelegi și reformulezi ce ai înțeles ca să confirmi
3. Dacă ceva e NECLAR (produs nerecunoscut, cantitate lipsă, dimensiune lipsă) → ÎNTREABĂ, nu presupune
4. Dacă produsul nu e în lista de mai sus → spune că nu îl recunoști și întreabă cum se numește exact
5. Când utilizatorul confirmă (zice "da", "corect", "asta e tot", "gata", "ok") → returnezi JSON

REGULI PENTRU JSON:
- Fiecare combinație produs + aromă + dimensiune = un rând separat
- Dacă zice "sapun liliac, spring, portocale câte 8 la 1L" → 3 rânduri separate cu cantitate 8 fiecare
- Dacă zice "18 Softner 1L: ocean 4, liliac 4, floral 5, lavanda 5" → 4 rânduri
- Potrivește cu codul din lista de mai sus (ex: "sapun lichid" / "classic" → cod 1111)

CÂND UTILIZATORUL CONFIRMĂ, răspunde EXACT în acest format (nimic altceva):
CONFIRMED
{{"produse": [{{"cod": "...", "nume": "...", "aroma": "...", "dimensiune": "...", "cantitate": 0}}]}}

Altfel răspunde normal, concis, în română. Folosește bullet points când listezi produse."""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=system_prompt,
        messages=history
    )

    reply_raw = response.content[0].text.strip()

    if reply_raw.startswith("CONFIRMED"):
        try:
            json_part = reply_raw[len("CONFIRMED"):].strip()
            parsed = json.loads(json_part)
            produse = parsed.get("produse", [])

            # Build friendly HTML summary
            lines = []
            for p in produse:
                lines.append(f"• <strong>{p.get('cantitate')} buc</strong> — {p.get('nume','?')} {p.get('dimensiune','')} <em>{p.get('aroma','')}</em>")

            friendly_html = (
                f"✅ Am adăugat <strong>{len(produse)} {'rând' if len(produse)==1 else 'rânduri'}</strong> în tabelul de verificare:<br><br>"
                + "<br>".join(lines)
                + "<br><br>Poți vedea tabelul complet în tab-ul <strong>Factură PDF</strong>. "
                + "Vrei să mai adaugi ceva?"
            )

            return {
                "reply": friendly_html,
                "reply_raw": f"Am confirmat {len(produse)} produse.",
                "produse": produse
            }

        except Exception as e:
            return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}

    return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}


def update_sheet(produse):
    ws = get_sheet()
    all_data = ws.get_all_values()

    results = []
    for produs in produse:
        cod = str(produs.get("cod", "")).strip()
        aroma = produs.get("aroma")
        dim = str(produs.get("dimensiune", "")).strip().upper()
        cant = int(produs.get("cantitate", 0))

        found = False
        for i, row in enumerate(all_data):
            if i < 4:
                continue
            r_cod = str(row[0]).strip()
            r_aroma = str(row[2]).strip().lower()
            r_dim = str(row[3]).strip().upper()

            if r_cod == cod and r_dim == dim:
                if aroma and aroma.lower() not in r_aroma:
                    continue

                old_stock = int(row[6]) if row[6] and row[6].isdigit() else 0
                new_stock = old_stock + cant
                ws.update_cell(i + 1, 7, new_stock)

                results.append({
                    "cod": cod,
                    "nume": produs.get("nume"),
                    "aroma": row[2],
                    "dimensiune": row[3],
                    "cantitate_adaugata": cant,
                    "stoc_nou": new_stock,
                    "status": "updated"
                })
                found = True
                break

        if not found:
            results.append({
                "cod": cod,
                "nume": produs.get("nume"),
                "aroma": aroma or "—",
                "dimensiune": dim,
                "cantitate_adaugata": cant,
                "status": "not_found",
                "mesaj": "Produs negăsit — verifică manual aroma"
            })

    return results


@app.route("/")
def index():
    return send_from_directory("../public", "index.html")


@app.route("/api/extract", methods=["POST"])
def extract():
    try:
        data = request.json
        pdf_b64 = data.get("pdf_base64")
        if not pdf_b64:
            return jsonify({"error": "Lipsește PDF-ul"}), 400
        invoice_data = extract_invoice_data(pdf_b64)
        return jsonify({"success": True, "data": invoice_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data = request.json
        history = data.get("history", [])
        existing_products = data.get("existing_products", [])

        if not history:
            return jsonify({"error": "Lipsește istoricul conversației"}), 400

        result = process_chat(history, existing_products)
        return jsonify({
            "success": True,
            "reply": result["reply"],
            "reply_raw": result.get("reply_raw", result["reply"]),
            "produse": result.get("produse", [])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update-sheet", methods=["POST"])
def update():
    try:
        data = request.json
        produse = data.get("produse", [])
        if not produse:
            return jsonify({"error": "Nu există produse de actualizat"}), 400

        results = update_sheet(produse)
        updated = [r for r in results if r["status"] == "updated"]
        not_found = [r for r in results if r["status"] == "not_found"]

        return jsonify({
            "success": True,
            "actualizate": len(updated),
            "negasite": len(not_found),
            "rezultate": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
