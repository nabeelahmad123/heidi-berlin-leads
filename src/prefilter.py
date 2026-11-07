"""Free first-pass filter: flag listings that are clearly not property managers.

Rule: a non-manager keyword in the name AND no "verwalt" in the name.
("Schik Finanzdienstleistungen & Hausverwaltung" stays; "AXA Versicherung" goes.)
Borderline cases (estate agents that also manage) are left for the LLM step.
Adds column `prefilter` (keep / exclude:<reason>) to data/companies.csv.
"""
import re
import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
EXCLUDE = {
    "insurance": r"versicherung|\baxa\b|\bdbv\b|allianz|ergo\b|signal iduna|huk",
    "cleaning": r"reinigung|clean|gebäudereinig|glasreinig",
    "caretaker": r"hausmeister|hauswart",
    "clearance/moving": r"wohnungsauflösung|entrümpel|umzug",
    "legal/tax": r"rechtsanw|anwalt|kanzlei|notar|steuerber",
    "finance": r"\bbank\b|sparkasse|kredit|finanzierung",
    "trades": r"sanitär|heizungsbau|elektro|dachdeck|maler|tischler|schlüsseldienst|notdienst|notruf",
}


def classify(name):
    low = str(name).lower()
    if "verwalt" in low:
        return "keep"
    for reason, pattern in EXCLUDE.items():
        if re.search(pattern, low):
            return f"exclude:{reason}"
    return "keep"


def main():
    path = DATA / "companies.csv"
    c = pd.read_csv(path, dtype=str).fillna("")
    c["prefilter"] = c["name"].map(classify)
    c.to_csv(path, index=False)
    print(c["prefilter"].value_counts().to_string(), file=sys.stderr)


if __name__ == "__main__":
    main()
