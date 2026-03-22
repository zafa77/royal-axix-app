from flask import Flask, request, jsonify, send_from_directory
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import re
import unicodedata
from datetime import datetime

app = Flask(__name__, static_folder="../public", static_url_path="")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS = json.loads(os.environ.get("GOOGLE_CREDENTIALS", "{}"))
SHEET_ID = os.environ.get("SHEET_ID", "1KMh4GHJzTKXEmYWoH3jGbVG21_V-fJqcorAlM_mvIK0")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Cache
_inventory_cache = None
_product_map_cache = None

# ── Text normalization ─────────────────────────────────────
def normalize(text):
    if not text:
        return ""
    text = str(text).lower().strip()
    nfkd = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    text = re.sub(r"\s+", " ", text)
    return text

def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.85
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    overlap = len(ba & bb)
    return (2.0 * overlap) / (len(ba) + len(bb))

def fuzzy_match(query, candidates, threshold=0.55):
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
def get_client():
    creds = Credentials.from_service_account_info(GOOGLE_CREDENTIALS, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(name="Inventar"):
    client = get_client()
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet(name)

def get_workbook():
    client = get_client()
    return client.open_by_key(SHEET_ID)

# ── Load Product Map from "Produse" sheet ──────────────────
def load_product_map():
    global _product_map_cache
    ws = get_sheet("Produse")
    all_data = ws.get_all_values()
    product_map = {}
    for i, row in enumerate(all_data):
        if i == 0:
            continue  # skip header
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            cod = str(row[0]).strip()
            nume = str(row[1]).strip()
            product_map[cod] = nume
    _product_map_cache = product_map
    return product_map

def get_product_map():
    global _product_map_cache
    if _product_map_cache is None:
        load_product_map()
    return _product_map_cache

# ── Load Inventory from "Inventar" sheet ───────────────────
def load_inventory():
    global _inventory_cache
    ws = get_sheet("Inventar")
    all_data = ws.get_all_values()
    inventory = {}
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
    inv = get_inventory_cached()
    if cod not in inv:
        return []
    rows = inv[cod]["rows"]
    if dim:
        dim_norm = normalize(dim)
        rows = [r for r in rows if normalize(r["dim"]) == dim_norm]
    return list(dict.fromkeys([r["aroma"] for r in rows if r["aroma"]]))

def build_inventory_context():
    inv = get_inventory_cached()
    lines = []
    for cod, data in inv.items():
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

# ── Istoric (Log) ──────────────────────────────────────────
def ensure_istoric_headers():
    """Make sure Istoric sheet has headers."""
    try:
        ws = get_sheet("Istoric")
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != "Data":
            ws.update("A1:G1", [["Data", "Ora", "Sursa", "Nr. Factura", "Produse procesate", "Total bucati", "Status"]])
    except Exception:
        pass

def log_to_istoric(sursa, nr_factura, produse, rezultate):
    """Append a row to Istoric sheet."""
    try:
        ensure_istoric_headers()
        ws = get_sheet("Istoric")
        now = datetime.now()
        data_str = now.strftime("%d.%m.%Y")
        ora_str = now.strftime("%H:%M")
        total_bucati = sum(p.get("cantitate", 0) for p in produse)
        actualizate = len([r for r in rezultate if r.get("status") == "updated"])
        negasite = len([r for r in rezultate if r.get("status") == "not_found"])
        status_str = f"✅ {actualizate} actualizate" + (f", ⚠️ {negasite} negăsite" if negasite > 0 else "")
        ws.append_row([data_str, ora_str, sursa, nr_factura or "—", len(produse), total_bucati, status_str])
    except Exception as e:
        print(f"Eroare log Istoric: {e}")

def get_istoric():
    """Get all rows from Istoric sheet."""
    try:
        ws = get_sheet("Istoric")
        all_data = ws.get_all_values()
        if len(all_data) <= 1:
            return []
        headers = all_data[0]
        rows = []
        for row in all_data[1:]:
            # Pad row if needed
            while len(row) < len(headers):
                row.append("")
            rows.append(dict(zip(headers, row)))
        return list(reversed(rows))  # Most recent first
    except Exception as e:
        return []

# ── PDF Extraction ─────────────────────────────────────────
def extract_invoice_data(pdf_base64):
    product_map = get_product_map()
    product_map_str = "\n".join([f"{cod} = {nume}" for cod, nume in product_map.items()])

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-5",
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
{product_map_str}

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
    product_map = get_product_map()
    product_map_str = "\n".join([f"{cod} = {nume}" for cod, nume in product_map.items()])
    inventory_context = build_inventory_context()

    existing_context = ""
    if existing_products:
        lines = []
        for p in existing_products:
            lines.append(f"  - {p.get('cod','?')} | {p.get('nume','?')} | {p.get('aroma','—')} | {p.get('dimensiune','?')} | {p.get('cantitate','?')} buc")
        existing_context = f"Produse deja în tabel (din factură PDF):\n" + "\n".join(lines) + "\n\nUtilizatorul vrea să ADAUGE produse noi la acestea."
    else:
        existing_context = "Tabelul este gol — utilizatorul introduce produse de la zero."

    system_prompt = f"""Ești un asistent de inventar pentru firma Royal Axix (produse curățenie Konga/Lebon).

{existing_context}

INVENTARUL COMPLET DIN GOOGLE SHEETS (cod | produs | dimensiune | arome disponibile):
{inventory_context}

PRODUSE DISPONIBILE (cod → nume):
{product_map_str}

SARCINA TA:
1. Utilizatorul îți spune ce produse vrea să adauge (limbaj liber, română)
2. Tu înțelegi și reformulezi ce ai înțeles
3. VALIDARE: Verifică că produsul, dimensiunea și aroma există în inventarul de mai sus
4. Dacă aroma NU există → spune care arome sunt disponibile pentru acel produs+dimensiune
5. Fii tolerant la greșeli de scriere, CAPS, lipsă diacritice
6. Dacă ceva e neclar → întreabă
7. Când utilizatorul confirmă → returnezi JSON

TOLERANȚĂ:
- "floral" = "Floral" = "FLORAL" ✓
- "fara parfum" = "Fără parfum" ✓
- "trandafr" ≈ "Trandafir" ✓
- "jasmim vanila" ≈ "Jasmin Vanilla" ✓

CÂND UTILIZATORUL CONFIRMĂ, răspunde EXACT în acest format (nimic altceva):
CONFIRMED
{{"produse": [{{"cod": "...", "nume": "...", "aroma": "...", "dimensiune": "...", "cantitate": 0}}]}}

Folosește aroma EXACTĂ din inventar. Altfel răspunde normal, concis, în română."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-5",
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
                        p["aroma"] = best
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

            return {"reply": friendly_html, "reply_raw": f"Am confirmat {len(corrected)} produse.", "produse": corrected}

        except Exception as e:
            return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}

    return {"reply": reply_raw, "reply_raw": reply_raw, "produse": []}

# ── Sheet Update ───────────────────────────────────────────
def update_sheet(produse, sursa="PDF", nr_factura=""):
    ws = get_sheet("Inventar")
    all_data = ws.get_all_values()

    results = []
    undo_log = []  # Store old values for undo

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

            if aroma:
                score = similarity(aroma, r_aroma)
                if score < 0.55:
                    continue

            old_stock = int(row[6]) if len(row) > 6 and row[6] and row[6].isdigit() else 0
            new_stock = old_stock + cant
            ws.update_cell(i + 1, 7, new_stock)

            # Save undo info
            undo_log.append({"row_index": i + 1, "old_stock": old_stock, "new_stock": new_stock})

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

    # Log to Istoric
    log_to_istoric(sursa, nr_factura, produse, results)

    return results, undo_log

def undo_update(undo_log):
    """Restore previous stock values."""
    ws = get_sheet("Inventar")
    for entry in undo_log:
        ws.update_cell(entry["row_index"], 7, entry["old_stock"])

# ── Routes ─────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("../public", "index.html")

@app.route("/api/inventory", methods=["GET"])
def get_inventory_route():
    try:
        inv = load_inventory()
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
        # Reset caches to get fresh data
        global _inventory_cache, _product_map_cache
        _inventory_cache = None
        _product_map_cache = None
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
        sursa = data.get("sursa", "PDF")
        nr_factura = data.get("nr_factura", "")
        if not produse:
            return jsonify({"error": "Nu există produse"}), 400

        results, undo_log = update_sheet(produse, sursa, nr_factura)
        updated = [r for r in results if r["status"] == "updated"]
        not_found = [r for r in results if r["status"] == "not_found"]

        return jsonify({
            "success": True,
            "actualizate": len(updated),
            "negasite": len(not_found),
            "rezultate": results,
            "undo_log": undo_log
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/undo", methods=["POST"])
def undo():
    try:
        data = request.json
        undo_log = data.get("undo_log", [])
        if not undo_log:
            return jsonify({"error": "Nu există date de anulat"}), 400
        undo_update(undo_log)
        return jsonify({"success": True, "message": f"Am restaurat {len(undo_log)} valori."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/istoric", methods=["GET"])
def istoric():
    try:
        rows = get_istoric()
        return jsonify({"success": True, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
