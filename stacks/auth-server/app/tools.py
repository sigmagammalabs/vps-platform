"""Programme, die ueber diesen Server lizenziert werden - die einzige Quelle der Wahrheit.

Neues Programm hinzufuegen: einen Eintrag in TOOLS ergaenzen (ID = was der Client als
"tool_id" sendet, Bezeichnung = was im Admin-Dashboard erscheint) und den Server neu
starten. Dashboard-Formular, Filter, Eingabepruefung und Verifikation folgen automatisch.

"all" (UNIVERSAL) gibt es nur als Lizenz-Typ: Ein solcher Schluessel gilt fuer jedes
Programm in TOOLS. Ein Programm darf "all" nie als eigene Kennung anfragen.
"""

from typing import Dict, List

UNIVERSAL = "all"
UNIVERSAL_LABEL = "Alle Tools (Universal)"

TOOLS: Dict[str, str] = {
    "tradingview_ai": "TradingView AI",
    "telefon_akquise": "BCS Telefon-Akquise & Lead-Cockpit",
    "premarket-screener": "Pre-Market Screener",
    "arbitrage-scout": "Arbitrage Scout",
}


def is_known_license_tool(tool_id: str) -> bool:
    """Gueltiger Wert fuer eine Lizenz: ein Programm aus TOOLS oder die Universal-Lizenz."""
    return tool_id == UNIVERSAL or tool_id in TOOLS


def tool_label(tool_id: str) -> str:
    """Anzeigename; unbekannte (aeltere) IDs erscheinen unveraendert."""
    if tool_id == UNIVERSAL:
        return UNIVERSAL_LABEL
    return TOOLS.get(tool_id, tool_id)


def tool_options() -> List[Dict[str, str]]:
    """Auswahlliste fuer das Formular (Universal zuerst)."""
    return [{"id": UNIVERSAL, "label": UNIVERSAL_LABEL}] + [{"id": k, "label": v} for k, v in TOOLS.items()]
