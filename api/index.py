from flask import Flask, request, jsonify, send_from_directory
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import re
import unicodedata

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

# ── Cache for inventory data ───────────────────────────────
_inventory_cache = None

# ── Text normalization ─────────────────────────────────────
def normalize(text):
    """Lowercase, remove diacritics, strip whitespace."""
    if not text:
        return ""
    text = str(text).lower().strip()
    # Remove diacritics
    nfkd = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Collapse multiple spaces
    text = re.sub(r"\s+", " ", text)
    return text

def similarity(a, b):
    """
    Simple similarity score between two strings (0.0 to 1.0).
    Uses character n-gram overlap for fuzzy matching.
    """
    a, b = normalize(a), normalize(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    # Check if one contains the other
    if a in b or b in a:
        return 0.85

    # Bigram overlap
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))

    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0

    overlap = len(ba & bb)
    score = (2.0 * overlap) / (len(ba) + len(bb))
    return score

def fuzzy_match(query, candidates, threshold=0.55):
    """
    Find best matching candidate for query.
    Returns (best_match, score) or (None, 0) if no match above threshold.
    """
    query_norm = normalize(query)
    best_match = None
    best_score = 0.0

    for candidate in candidates:
        score = similarity(query_norm, normalize(candidate))
        if score > best_score:
            best_score = score
            best_match = candidate

    if best_score >= threshold:
        return best_match, best_score
    return None, 0.0

# ── Google Sheets ──────────────────────────────────────────
def get_sheet():
    creds = Credentials.from_service_account_info(GOOGLE_CREDENTIALS, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet("Inventar")

def load_inventory():
    """
    Load full inventory from Sheets and build a structured lookup.
    Returns dict: { cod: { nume, dimensiuni: [dim, ...], arome_per_dim: { dim: [aroma, ...] } } }
    """
    global _inventory_cache
    ws = get_sheet()
    all_data = ws.get_all_values()

    inventory = {}  # cod -> { nume, rows: [(aroma, dim, row_index)] }

    for i, row in enumerate(all_data):
        if i < 4:
            continue
        if len(row) < 4:
            continue
        cod = str(row[0]).strip()
        nume = str(row[1]).strip()
        aroma = str(row[2]).strip()
        dim = str(row[3]).strip().upper()

        if not cod or not dim:
            continue

        if cod not in inventory:
            inventory[cod] = {"nume": nume, "rows": []}
        inventory[cod]["rows"].append({"aroma": aroma, "dim": dim, "row_index": i})

    _inventory_cache = inventory
    return inventory

def get_inventory_cached():
    global _inventory_cache
    if _inventory_cache is None:
        load_inventory()
    return _inventory_cache

def get_aromas_for_product(cod, dim=None):
    """Get list of available aromas for a product code, optionally filtered by dimension."""
    inv = get_inventory_cached()
    if cod not in inv:
        return []
    rows = inv[cod]["rows"]
    if dim:
        dim_norm = normalize(dim)
        rows = [r for r in rows if normalize(r["dim"]) == dim_norm]
    return list(dict.fromkeys([r["aroma"] for r in rows if r["aroma"]]))

def get_dims_for_product(cod):
    """Get list of available dimensions for a product code."""
    inv = get_inventory_cached()
    if cod not in inv:
        return []
    return list(dict.fromkeys([r["dim"] for r in inv[cod]["rows"]]))

def build_inventory_context():
    """Build a text summary of inventory for AI prompt."""
    inv = get_inventory_cached()
    lines = []
    for cod, data in inv.items():
        # Group aromas by dimension
        dim_aromas = {}
        for r in data["rows"]:
            d = r["dim"]
            if d not in dim_aromas:
                dim_aromas[d] = []
            if r["aroma"] and r["aroma"] not in dim_aromas[d]:
                dim_aromas[d].append(r["aroma"])
        for dim, aromas in dim_aromas.items():
            aroma_str = ", ".join(aromas) if aromas else "—"
            lines.append(f"  {cod} | {data['nume']} | {dim} | Arome: {aroma_str}")
    return "\n".join(lines)

# ── PDF Extraction ─────────────────────────────────────────
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
- Pentru aromă: dacă e menționată EXPLICIT în denumire (ex: LILIAC, SPRING, OCEAN), pune aroma și setează aroma_auto: true
- Dacă aroma NU e menționată, pune aroma: null și aroma_auto: false
- Pentru dimensiune: extrage exact (750ML, 1L, 5L etc.)
- cantitate = numărul întreg de bucăți"""
                }
            ]
        }]
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ── Chat Processing ────────────────────────────────────────
def process_chat(history, existing_products):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load real inventory for validation context
    inventory_context = build_inventory_context()

    existing_context = ""
    if existing_products:
        lines = []
        for p in existing_products:
            lines.append(f"  - {p.get('cod','?')} | {p.get('nume','?')} | {p.get('aroma','—')} | {p.get('dimensiune','?')} | {p.get('cantitate','?')} buc")
        existing_context = f"""
Produse deja în tabel (din factură PDF):
{chr(10).join(lines)}

Utilizatorul vrea să ADAUGE produse noi la acestea.
"""
    else:
        existing_context = "Tabelul este gol — utilizatorul introduce produse de la zero."

    system_prompt = f"""Ești un asistent de inventar pentru firma Royal Axix (produse curățenie Konga/Lebon).

{existing_context}

INVENTARUL COMPLET DIN GOOGLE SHEETS (cod | produs | dimensiune | arome disponibile):
{inventory_context}

SARCINA TA:
1. Utilizatorul îți spune ce produse vrea să adauge (în limbaj liber, română)
2. Tu înțelegi și reformulezi ce ai înțeles
3. VALIDARE OBLIGATORIE: Verifică că produsul, dimensiunea și aroma există în inventarul de mai sus
4. Dacă aroma NU există → spune care arome sunt disponibile pentru acel produs+dimensiune
5. Fii tolerant la greșeli de scriere, CAPS, lipsă diacritice — încearcă să înțelegi intenția
6. Dacă ceva e complet neclar → întreabă
7. Când utilizatorul confirmă → returnezi JSON

EXEMPLE DE TOLERANȚĂ:
- "floral" = "Floral" = "FLORAL" ✓
- "fara parfum" = "Fără parfum" ✓  
- "trandafr" ≈ "Trandafir" ✓ (greșeală de tastare)
- "jasmim vanila" ≈ "Jasmin Vanilla" ✓

CÂND UTILIZATORUL CONFIRMĂ, răspunde EXACT în acest format (nimic altceva):
CONFIRMED
{{"produse": [{{"cod": "...", "nume": "...", "aroma": "...", "dimensiune": "...", "cantitate": 0}}]}}

Folosește aroma EXACTĂ din inventar (nu cea scrisă de utilizator).
Altfel răspunde normal, concis, în română."""

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

            # Extra server-side fuzzy validation + correction
            corrected = []
            warnings = []
            for p in produse:
                cod = str(p.get("cod", "")).strip()
                aroma_raw = p.get("aroma", "")
                dim_raw = str(p.get("dimensiune", "")).strip().upper()

                available_aromas = get_aromas_for_product(cod, dim_raw)

                if available_aromas and aroma_raw:
                    best, score = fuzzy_match(aroma_raw, available_aromas)
                    if best:
                        p["aroma"] = best  # Use exact name from Sheets
                    else:
                        warnings.append(f"Aroma '{aroma_raw}' negăsită pentru {p.get('nume')} {dim_raw}")

                corrected.append(p)

            lines = []
            for p in corrected:
                lines.append(f"• <strong>{p.get('cantitate')} buc</strong> — {p.get('nume','?')} {p.get('dimensiune','')} <em>{p.get('aroma','')}</em>")

            warn_html = ""
            if warnings:
                warn_html = "<br><br>⚠️ <strong>Atenție:</strong> " + "; ".join(warnings)

            friendly_html = (
                f"✅ Am adăugat <strong>{len(corrected)} {'rând' if len(corrected)==1 else 'rânduri'}</strong> în tabel:<br><br>"
                + "<br>".join(lines)
                + warn_html
                + "<br><br>Poți vedea tabelul în tab-ul <strong>Factură PDF</strong>. Vrei să mai adaugi ceva?"
            )

            return {
                "reply": friendly_html,
                "reply_raw": f"Am confirmat {len(corrected)} produse.",
                "produse": corrected
            }

        except Exception as e:
            return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}

    return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}

# ── Sheet Update ───────────────────────────────────────────
def update_sheet(produse):
    ws = get_sheet()
    all_data = ws.get_all_values()

    results = []
    for produs in produse:
        cod = str(produs.get("cod", "")).strip()
        aroma = produs.get("aroma", "")
        dim = str(produs.get("dimensiune", "")).strip().upper()
        cant = int(produs.get("cantitate", 0))

        found = False
        for i, row in enumerate(all_data):
            if i < 4:
                continue
            r_cod = str(row[0]).strip()
            r_aroma = str(row[2]).strip()
            r_dim = str(row[3]).strip().upper()

            if r_cod != cod or r_dim != dim:
                continue

            # Fuzzy match aroma
            if aroma:
                score = similarity(aroma, r_aroma)
                if score < 0.55:
                    continue

            old_stock = int(row[6]) if len(row) > 6 and row[6] and row[6].isdigit() else 0
            new_stock = old_stock + cant
            ws.update_cell(i + 1, 7, new_stock)

            results.append({
                "cod": cod,
                "nume": produs.get("nume"),
                "aroma": r_aroma,
                "dimensiune": r_dim,
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
                "mesaj": "Produs negăsit — verifică manual"
            })

    return results

# ── Routes ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("../public", "index.html")

@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    """Returns inventory summary for frontend use."""
    try:
        inv = load_inventory()  # Always fresh on explicit call
        summary = {}
        for cod, data in inv.items():
            dims = {}
            for r in data["rows"]:
                d = r["dim"]
                if d not in dims:
                    dims[d] = []
                if r["aroma"] and r["aroma"] not in dims[d]:
                    dims[d].append(r["aroma"])
            summary[cod] = {"nume": data["nume"], "dimensiuni": dims}
        return jsonify({"success": True, "inventory": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
            return jsonify({"error": "Lipsește istoricul"}), 400
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
            return jsonify({"error": "Nu există produse"}), 400
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
