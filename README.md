# 👑 Royal Axix — Procesare Facturi → Inventar Google Sheets

## Structura proiectului
```
royal-axix-invoice-app/
├── api/
│   └── index.py          # Backend Flask + Claude API + Google Sheets
├── public/
│   └── index.html        # Frontend aplicație web
├── requirements.txt      # Dependențe Python
├── vercel.json           # Configurare Vercel
└── README.md
```

## Variabile de mediu necesare în Vercel

Mergi în Vercel → Project → Settings → Environment Variables și adaugă:

| Variabilă | Valoare |
|-----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key-ul tău |
| `GOOGLE_CREDENTIALS` | Conținutul fișierului JSON (tot) |
| `SHEET_ID` | `1KMh4GHJzTKXEmYWoH3jGbVG21_V-fJqcorAlM_mvIK0` |

## Deploy pe Vercel

1. Pune codul pe GitHub
2. Conectează repository-ul în Vercel
3. Adaugă variabilele de mediu
4. Deploy automat!
