# evcc Ladekosten-Report – Dokumentation

[🇬🇧 English](DOCS.md) | [🇩🇪 Deutsch](DOCS.de.md)

## Was macht dieses Add-on?

Es ruft über die REST-API deiner evcc-Instanz (`/api/sessions`) die Ladevorgänge
eines Monats ab, filtert sie auf die von dir konfigurierten Fahrzeuge und
erzeugt daraus ein PDF mit:

- Datum, Beginn-/Endzeit je Ladevorgang
- Zählerstand Start / Ende (kWh)
- geladener Energiemenge
- berechnetem Erstattungsbetrag (Strompreispauschale oder tatsächlicher Tarif)
- Plausibilitätshinweisen bei Abweichungen zwischen Zählerdifferenz und
  gemeldeter Energie
- Unterschriftsfeld

Reports können manuell über die Weboberfläche (im Home-Assistant-Sidebar unter
"Charging Costs" via Ingress erreichbar) erzeugt werden, oder automatisch am
2. Tag jedes Monats für den Vormonat (siehe Option `auto_generate`).

In der Liste vorhandener Reports lässt sich jeder Report per Checkbox als
"Eingereicht" markieren (z. B. sobald er beim Arbeitgeber abgegeben wurde)
und über den Button "Löschen" wieder entfernen. Ladevorgänge, die über
Mitternacht laufen (z. B. 17:28 Uhr bis 05:00 Uhr am Folgetag), werden in
der Ende-Spalte mit "+1" gekennzeichnet (bzw. "+2", "+3", ... bei mehr als
einem Tag Differenz).

Für Methode "Tatsächliche Kosten" wird eine **Tarifhistorie** direkt auf der
Add-on-Seite gepflegt (siehe unten).

Alle PDFs landen zusätzlich unter `/share/evcc_ladekosten/` und sind damit auch
über den Datei-Explorer / Samba-Add-on erreichbar.

Die Oberfläche folgt der in der Option `language` gewählten Sprache (Englisch
oder Deutsch, siehe [Konfiguration](#konfiguration) unten) und passt sich
automatisch der Hell-/Dunkel-Einstellung deines Browsers bzw. Betriebssystems
an (`prefers-color-scheme`). Ein direkter Zugriff auf das in Home Assistant
ausgewählte Theme ist aus einem Ingress-iFrame heraus technisch nicht
möglich; die System-/Browser-Präferenz ist die bestmögliche Annäherung und
deckt sich bei den meisten Setups mit der Home-Assistant-Einstellung.

## Hintergrund: warum Zählerstände wichtig sind

Seit dem BMF-Schreiben vom 11.11.2025 (gültig ab 01.01.2026) entfallen die alten
Monatspauschalen für das Laden eines Dienstwagens zuhause. Eine steuerfreie
Erstattung gibt es nur noch gegen Nachweis der tatsächlich geladenen kWh.
Zwei Methoden sind zulässig, müssen aber pro Kalenderjahr einheitlich
angewendet werden:

- **Strompreispauschale** (2026: 0,34 €/kWh) – ein einfacher, nicht zwingend
  geeichter Zähler genügt.
- **Tatsächliche Kosten** (eigener Haushaltstarif) – hierfür verlangt das BMF
  einen eichrechtskonformen (MID-zertifizierten) Zähler.

Dieses Add-on ersetzt keine steuerliche Beratung. Bitte die gewählte Methode
mit HR/Lohnbuchhaltung abstimmen.

## Tarifhistorie (Methode "Tatsächliche Kosten")

Auf der Add-on-Seite gibt es eine Karte "Tarifhistorie", in der du beliebig
viele Zeiträume mit Startdatum und Preis (€/kWh) hinterlegen kannst. Für
jeden Ladevorgang wird automatisch der Satz verwendet, dessen Startdatum am
nächsten am (aber nicht nach dem) Ladedatum liegt – ein Tarifwechsel mitten
im Monat wirkt sich also korrekt nur auf die Ladevorgänge danach aus.

Beispiel: Eintrag "01.01.2020 → 0,2614 €/kWh" und "15.08.2026 → 0,31 €/kWh"
sorgt dafür, dass alle Ladevorgänge bis zum 14.08.2026 mit 0,2614 €/kWh und
ab dem 15.08.2026 mit 0,31 €/kWh berechnet werden.

Der jeweils angewandte Satz wird zur Nachvollziehbarkeit als eigene Spalte
"Satz (€/kWh)" in der PDF-Tabelle ausgewiesen. Liegt ein Ladevorgang vor dem
ältesten hinterlegten Zeitraum, wird ersatzweise dieser älteste Satz
verwendet und im PDF als Plausibilitätshinweis vermerkt, statt die
Report-Erstellung abzubrechen.

Wurden im Report-Zeitraum mehrere Tarife angewandt, druckt das PDF zusätzlich
eine kleine Tabelle "Im Zeitraum verwendete Tarife" mit genau den relevanten
Einträgen (Startdatum + Preis) mit ab – nicht die komplette Tarifhistorie,
sondern nur das, was für diesen Report tatsächlich zum Einsatz kam. Das gilt
auch dann korrekt, wenn ein älterer und ein neuerer Tarifeintrag zufällig
denselben Preis haben (z. B. unveränderter Preis bei einem Anbieterwechsel):
Es wird der tatsächlich herangezogene Eintrag angezeigt, nicht jeder Eintrag
mit passendem Preis.

**Belege als Anlage:** Pro Tarifzeitraum kann optional ein PDF-Beleg
hochgeladen werden (z. B. Stromvertrag oder Preisanpassungsschreiben). Sobald
ein Zeitraum mit Beleg für einen Report relevant ist, wird der Beleg
automatisch als Anlage an das erzeugte PDF angehängt – unabhängig davon, ob
im Zeitraum ein Tarifwechsel stattfand oder nur ein einziger Tarif galt, und
auch bei mehreren gleichzeitig relevanten Belegen (je einer pro Zeitraum,
mit eigener Trennseite). Ein defekter oder kein gültiges PDF wird nicht
angehängt, sondern als Plausibilitätshinweis im Report vermerkt.

Beim allerersten Start legt das Add-on automatisch einen Startwert an
(01.01.2020, 0,2614 €/kWh), damit nichts abbricht, solange du noch keine eigenen Einträge gepflegt hast.

## Konfiguration

| Option | Beschreibung |
|---|---|
| `evcc_url` | Basis-URL deiner evcc-Instanz, z. B. `http://homeassistant.local:7070` |
| `vehicles` | Liste der Fahrzeug-Titel (wie in evcc unter `vehicles -> title` benannt), deren Ladevorgänge in den Report aufgenommen werden |
| `method` | `pauschale` oder `actual` |
| `rate_ct_per_kwh` | Cent/kWh bei `method: pauschale` |
| `employee` | Standardname für den Report-Header |
| `vehicle` | Anzeigetext im Report-Header (z. B. "Seat, WI-XX 1234") – unabhängig vom Filter `vehicles` oben |
| `language` | Sprache der Oberfläche und der PDFs: `en` (Englisch, Standard) oder `de` (Deutsch) |
| `auto_generate` | Automatische Erstellung am 2. jeden Monats für den Vormonat |
| `notify_on_generate` | Persistent Notification in Home Assistant nach Erstellung |
| `footnote_pauschale` | Eigener Hinweistext unter der Tabelle bei Methode `pauschale`. Leer lassen für den Standardtext (BMF-Verweis). |
| `footnote_actual` | Eigener Hinweistext unter der Tabelle bei Methode `actual`. Leer lassen für den Standardtext (BMF-Verweis). |
| `include_chart` | Ob das Verlaufsdiagramm bei automatisch erstellten Reports mit ausgegeben wird. Standard: nein. Bei manueller Erstellung über die Weboberfläche gibt es dafür eine eigene Checkbox (ebenfalls standardmäßig deaktiviert). |

Über die Weboberfläche kannst du Monat/Jahr, Methode, Mitarbeiter und Fahrzeug
pro Report auch einmalig überschreiben, ohne die Konfiguration zu ändern.

**Voreingestellte Defaults für dieses Setup:** `evcc_url: http://homeassistant.local:7070`,
`vehicles: ["Seat"]` (Titel deines Fahrzeugs in der evcc-Config). Passe das an,
falls sich deine evcc-Config ändert (z. B. bei einem zweiten Fahrzeug oder
Fahrzeugwechsel).

Warum Fahrzeug statt Ladepunkt? So werden auch Ladevorgänge desselben Fahrzeugs
an unterschiedlichen (Home-)Ladepunkten korrekt erfasst, und andere Fahrzeuge,
die zufällig am selben Ladepunkt laden, bleiben zuverlässig außen vor.

## Bekannte Einschränkungen

- Die evcc-API muss vom Add-on aus per HTTP erreichbar sein (gleiches
  Heimnetz bzw. gleicher Host).
- Für die Methode `actual` liegt die Verantwortung für einen
  eichrechtskonformen Zähler und die korrekte Tarifpflege beim Nutzer.
- Die Tarifhistorie liegt in `/data/tariffs.json` im persistenten
  Add-on-Datenspeicher (nicht in `/share`) und übersteht Neustarts sowie
  Updates, ist aber nicht direkt über den Datei-Explorer sichtbar –
  Pflege ausschließlich über die Weboberfläche.
- Hochgeladene Tarifbelege liegen entsprechend in `/data/tariff_docs/`,
  ebenfalls persistent und nicht über `/share` sichtbar.
