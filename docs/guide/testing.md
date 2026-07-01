# Usage Testing

Manual test cases for validating PII masking and context compression through the proxy.

> **Debug mode active** — `debug_input_only: true` enabled in `plugins/manifest.yaml`.
> PII placeholders (`[PRIVATE_PERSON_1]` etc.) appear directly in Claude's replies,
> confirming masking fired without inspecting logs. Disable before production use.

---

## How to read these tests

| Field | Meaning |
|-------|---------|
| **Prompt** | Testo da inviare nella chat di Claude Code, pronto all'uso |
| **Expected log** | Riga del log proxy che conferma il filtro |
| **Pass condition** | Cosa deve (o non deve) contenere la risposta |

---

## TC-01 · Chat — PII multipla in un messaggio

Verifica che NER e regex fallback cooperino su un messaggio con più tipi di PII
in italiano e in inglese nella stessa frase.

**Prompt**
```
Mi chiamo Pinco Pallino e la mia collega si chiama Futura Incognita
(my name is Pinco Pallino, her name is Futura Incognita).
Puoi inviarmi i risultati a pinco.pallino@prova.invalid oppure chiamarmi al +39 333 0000001.
Il mio IBAN per i rimborsi è IT00A0000000000000000000001.
Detto questo, puoi spiegarmi la differenza tra una lista e un dizionario in Python?
```

**Expected log**
```
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PERSON, PRIVATE_PHONE] — 4 category(ies) [DEBUG: output NOT de-masked]
```

**Pass condition** — `[PRIVATE_PERSON_1]`, `[PRIVATE_PERSON_2]`, `[PRIVATE_EMAIL_1]`,
`[PRIVATE_PHONE_1]`, `[ACCOUNT_NUMBER_1]` compaiono nella risposta. Claude risponde
comunque alla domanda Python. Nessun valore reale nell'output.

---

## TC-02 · File reading — dati anagrafici

Verifica che il masker intervenga sul contenuto del file restituito come `tool_result`.

**Setup** — Crea `anagrafica.csv`:
```
id,nome,cognome,email,telefono,iban
1,Tizio,Primo,tizio.primo@prova.invalid,+39 333 0000001,IT00A0000000000000000000001
2,Caia,Seconda,caia.seconda@prova.invalid,+39 333 0000002,IT00A0000000000000000000002
3,Sempronio,Terzo,sempronio.terzo@prova.invalid,+39 333 0000003,IT00A0000000000000000000003
```

**Prompt**
```
Leggi il file anagrafica.csv e dimmi quante righe contiene e quali colonne ha.
```

**Expected log**
```
[DEBUG] msg[tool].text detected: PRIVATE_EMAIL='...'@..., PRIVATE_PHONE='...'@..., ACCOUNT_NUMBER='...'@...
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PHONE] — 3 category(ies)
```

> **Nota** — I nomi `Tizio Primo` / `Caia Seconda` / `Sempronio Terzo` non contengono
> frasi trigger e potrebbero non essere rilevati da NER con confidenza sufficiente:
> email, telefono e IBAN sono i segnali affidabili in questo scenario.

**Pass condition** — Claude descrive struttura (3 righe, 6 colonne) ma email, telefoni e IBAN
appaiono come placeholder. Nessun valore reale nell'output.

---

## TC-03 · Bash — secret in variabile d'ambiente

Verifica che PII nell'output di un comando shell venga intercettata prima che Claude la legga.

**Setup** — Prima di aprire VS Code, imposta le variabili nella shell:
```powershell
$env:APP_SECRET = "sk-ant-api01-test0000000000000000000000000000000000000000000000000"
$env:OWNER_EMAIL = "pinco.pallino@prova.invalid"
```

**Prompt**
```
Esegui il comando seguente e dimmi cosa contengono quelle variabili:
echo "SECRET=$env:APP_SECRET EMAIL=$env:OWNER_EMAIL"
```

**Expected log**
```
[DEBUG] msg[tool].text detected: SECRET='sk-ant-api01-test...'@..., PRIVATE_EMAIL='pinco.pallino@prova.invalid'@...
```

**Pass condition** — Claude riporta `[SECRET_1]` e `[PRIVATE_EMAIL_1]`; i valori reali
non compaiono. Se NER non rileva `SECRET` annotare il risultato (utile per calibrare soglie).

---

## TC-04 · Database — SELECT FROM DUAL con PII sintetica

Verifica il masking su dati tabulari restituiti da uno strumento di database,
senza toccare tabelle reali. Tutte le 5 categorie PII in un singolo test.

**Prompt**
```
Connettiti a un database disponibile e poi esegui questa query:

SELECT
  'Pinco Pallino'                                       AS nome_cognome,
  'pinco.pallino@prova.invalid'                         AS email,
  '+39 333 0000001'                                     AS telefono,
  'IT00A0000000000000000000001'                         AS iban,
  'sk-secret-internal-key-000'                          AS api_key
FROM DUAL

Descrivi il risultato.
```

**Expected log**
```
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PERSON, PRIVATE_PHONE, SECRET] — 5 category(ies)
```

**Pass condition** — Claude descrive la struttura (5 colonne, 1 riga) ma ogni valore
appare come placeholder. Tutte e 5 le categorie PII coperte in un test solo.

---

## TC-05 · Headroom — payload JSON lungo

Verifica che il compressore si attivi quando il corpo del messaggio è un JSON verboso
(tipico di risposte API o log strutturati passati in chat).

**Prompt**
```
Analizza questo log di sessione e dimmi quali fasi hanno impiegato più tempo:

{
  "sessione_id": "sess-2026-001",
  "ambiente": "sviluppo",
  "timestamp_inizio": "2026-07-01T09:00:00Z",
  "eventi": [
    {"seq":1,"tipo":"AVVIO","modulo":"proxy","ms":120,"esito":"OK","note":"processo avviato, bind porta 8090 completato"},
    {"seq":2,"tipo":"AUTH","modulo":"rotator","ms":45,"esito":"OK","note":"pool connessioni inizializzato, keepalive 30s"},
    {"seq":3,"tipo":"PLUGIN_LOAD","modulo":"onnx_pii_masker","ms":3200,"esito":"OK","note":"modello int8 caricato, backend onnx-cpu"},
    {"seq":4,"tipo":"PLUGIN_LOAD","modulo":"headroom_compressor","ms":890,"esito":"OK","note":"kompress warmed up in background thread"},
    {"seq":5,"tipo":"PLUGIN_LOAD","modulo":"smart_budget_guard","ms":1100,"esito":"OK","note":"tiktoken cl100k_base pre-caricato, hydration SQLite completata"},
    {"seq":6,"tipo":"RICHIESTA","modulo":"router","ms":12,"payload_tokens":441,"esito":"OK","note":"routing verso anthropic api, modello claude-sonnet-5"},
    {"seq":7,"tipo":"PII_SCAN","modulo":"onnx_pii_masker","ms":280,"entita_trovate":2,"esito":"MASKED","note":"PRIVATE_PERSON x1, PRIVATE_EMAIL x1"},
    {"seq":8,"tipo":"COMPRESSIONE","modulo":"headroom","ms":34,"token_in":441,"token_out":441,"esito":"SKIP","note":"sotto soglia min_tokens_to_compress"},
    {"seq":9,"tipo":"UPSTREAM","modulo":"anthropic","ms":2100,"model":"claude-sonnet-5","esito":"OK","note":"streaming completato, 312 token output"},
    {"seq":10,"tipo":"DEMASKING","modulo":"shield_sanitizer","ms":5,"sostituzioni":2,"esito":"OK","note":"vault lookup 2/2 trovati"},
    {"seq":11,"tipo":"RICHIESTA","modulo":"router","ms":11,"payload_tokens":10732,"esito":"OK","note":"seconda richiesta, contesto accumulato"},
    {"seq":12,"tipo":"PII_SCAN","modulo":"onnx_pii_masker","ms":950,"entita_trovate":8,"esito":"MASKED","note":"PRIVATE_PERSON x2, PRIVATE_EMAIL x1, PRIVATE_PHONE x1, ACCOUNT_NUMBER x1, SECRET x1"},
    {"seq":13,"tipo":"COMPRESSIONE","modulo":"headroom","ms":280,"token_in":10732,"token_out":8901,"esito":"COMPRESSED","note":"kompress attivo, ratio 0.83"},
    {"seq":14,"tipo":"UPSTREAM","modulo":"anthropic","ms":3800,"model":"claude-sonnet-5","esito":"OK","note":"streaming completato, 874 token output"},
    {"seq":15,"tipo":"DEMASKING","modulo":"shield_sanitizer","ms":12,"sostituzioni":8,"esito":"OK","note":"vault lookup 8/8 trovati"}
  ],
  "sommario":{"richieste_totali":2,"pii_intercettate":10,"token_risparmiati":1831,"latenza_media_ms":2340,"costo_stimato_usd":0.0047}
}
```

**Expected log**
```
Transform content_router: X -> Y tokens (saved Z) [Nms]
```

**Pass condition** — `saved` > 0. Claude identifica correttamente le fasi più lente
(PLUGIN_LOAD onnx_pii_masker a 3200 ms e UPSTREAM seconda richiesta a 3800 ms).

---

## TC-06 · Headroom — risultato SQL verboso da tool

Verifica che la compressione scatti quando il `tool_result` di una query restituisce
molte righe. Usa `CONNECT BY LEVEL` su DUAL per generare dati senza toccare tabelle reali.

**Prompt**
```
Connettiti a un database disponibile e poi esegui questa query.
Dimmi quante righe ha restituito e qual è la distribuzione degli stati:

SELECT
  level                                                                AS id,
  'EVENTO_' || TO_CHAR(level, 'FM000')                               AS codice,
  TO_CHAR(SYSDATE - level, 'YYYY-MM-DD')                             AS data_riferimento,
  CASE MOD(level, 3)
    WHEN 0 THEN 'ELABORATO'
    WHEN 1 THEN 'IN_ATTESA'
    ELSE        'ERRORE'
  END                                                                  AS stato,
  'Descrizione estesa del record numero ' || level
    || ' generata per il test di compressione del contesto proxy.'    AS descrizione
FROM DUAL
CONNECT BY level <= 50
```

**Expected log**
```
Transform content_router: X -> Y tokens (saved Z) [Nms]
```

**Pass condition** — `saved` > 0. Claude risponde correttamente: 50 righe, distribuzione
stati 17 ELABORATO / 17 IN_ATTESA / 16 ERRORE (o equivalente). Nessuna troncatura
nel conteggio o nella distribuzione.

---

## TC-07 · Headroom — sorgente del proxy

Verifica la compressione su un file sorgente lungo letto direttamente da Claude Code.
Usa il file più grande del progetto, già pubblico su GitHub.

**Prompt**
```
Leggi il file plugins/installed/onnx_pii_masker.py e spiegami in modo conciso
come funziona la strategia di deduplicazione delle entità PII in _dedup_by_span:
perché ordina per dimensione dello span e non per posizione nel testo?
```

**Expected log**
```
Transform content_router: X -> Y tokens (saved Z) [Nms]
```

**Pass condition** — `saved` > 0 (il file supera abbondantemente la soglia di compressione).
Claude risponde correttamente: `_dedup_by_span` ordina per dimensione discendente perché
l'entità più grande (tipicamente il match regex dell'intero nome) deve "vincere" sui
frammenti più piccoli prodotti dall'NER, che coprono porzioni sovrapposte dello stesso span.

---

## Checklist di verifica

| ID | Descrizione | Filtro atteso | Log ✓ | Risposta ✓ |
|----|-------------|---------------|-------|------------|
| TC-01 | Chat PII multipla | PERSON × 2, EMAIL, PHONE, IBAN | | |
| TC-02 | File CSV | EMAIL × 3, PHONE × 3, IBAN × 3 | | |
| TC-03 | Bash / env var | SECRET, EMAIL | | |
| TC-04 | SQL FROM DUAL | PERSON, EMAIL, PHONE, IBAN, SECRET | | |
| TC-05 | Headroom — JSON | headroom log, saved > 0 | | |
| TC-06 | Headroom — SQL 50 righe | headroom log, saved > 0 | | |
| TC-07 | Headroom — sorgente proxy | headroom log, saved > 0 | | |
