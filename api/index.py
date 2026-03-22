from flask import Flask, request, jsonify, send_from_directory
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import base64
import os
import re

app = Flask(__name__, static_folder="../public", static_url_path="")

# ── Config ──────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_CREDENTIALS = json.loads(os.environ.get("GOOGLE_CREDENTIALS", "{}"))
SHEET_ID = os.environ.get("SHEET_ID", "1KMh4GHJzTKXEmYWoH3jGbVG21_V-fJqcorAlM_mvIK0")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Produse Royal Axix — mapare cod → nume
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
                    "text": """Ești un asistent pentru firma Royal Axix care vinde produse de curățenie Konga/Lebon.
                    
Extrage din această factură DOAR produsele, fără prețuri.
Răspunde EXCLUSIV cu JSON valid, fără text extra:

{
  "numar_factura": "...",
  "data": "...",
  "produse": [
    {
      "cod": "...",
      "nume": "...",
      "aroma": "...",
      "dimensiune": "...",
      "cantitate": 0
    }
  ]
}

Reguli:
- Extrage codul numeric din denumirea produsului (ex: 2401, 4001, 3101 etc.)
- Pentru aromă: extrage DOAR ce scrie explicit în factură (ex: LILIAC, SPRING, OCEAN). Dacă nu e menționată, pune null
- Pentru dimensiune: extrage exact (750ML, 1L, 5L etc.)
- cantitate = numărul de bucăți
- Nu inventa arome care nu sunt scrise explicit în factură"""
                }
            ]
        }]
    )
    
    raw = response.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

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
            if i < 4:  # skip header rows
                continue
            r_cod = str(row[0]).strip()
            r_aroma = str(row[2]).strip().lower()
            r_dim = str(row[3]).strip().upper()
            
            if r_cod == cod and r_dim == dim:
                if aroma and aroma.lower() not in r_aroma:
                    continue
                
                # Stoc curent în coloana G (index 6)
                old_stock = int(row[6]) if row[6] and row[6].isdigit() else 0
                new_stock = old_stock + cant
                
                # Update celula stoc (coloana G = col 7, row i+1 în sheets)
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
