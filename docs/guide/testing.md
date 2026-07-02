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
Ti fornisco i miei dati di contatto e quelli della mia collega, confermami
di averli ricevuti ripetendoli uno per uno in un elenco puntato:

Mi chiamo Pinco Pallino e la mia collega si chiama Futura Incognita
(my name is Pinco Pallino, her name is Futura Incognita).
La mia email è pinco.pallino@prova.invalid, il telefono è +39 333 0000001
e il mio IBAN per i rimborsi è IT00A0000000000000000000001.
```

**Expected log**
```
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PERSON, PRIVATE_PHONE] — 4 category(ies) [DEBUG: output NOT de-masked]
```

**Pass condition** — La risposta è un elenco puntato che riporta esattamente
`[PRIVATE_PERSON_1]`, `[PRIVATE_PERSON_2]`, `[PRIVATE_EMAIL_1]`, `[PRIVATE_PHONE_1]`,
`[ACCOUNT_NUMBER_1]` al posto dei valori reali. Nessun dato in chiaro nell'output.

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
Leggi il file anagrafica.csv e ripetimi riga per riga tutti i valori presenti,
inclusi email, telefono e IBAN di ciascun record.
```

**Expected log**
```
[DEBUG] msg[tool].text detected: PRIVATE_EMAIL='...'@..., PRIVATE_PHONE='...'@..., ACCOUNT_NUMBER='...'@...
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PHONE] — 3 category(ies)
```

> **Nota** — I nomi `Tizio Primo` / `Caia Seconda` / `Sempronio Terzo` non contengono
> frasi trigger e potrebbero non essere rilevati da NER con confidenza sufficiente:
> email, telefono e IBAN sono i segnali affidabili in questo scenario.

**Pass condition** — Claude elenca riga per riga i valori ma email, telefoni e IBAN
appaiono come placeholder (`[PRIVATE_EMAIL_N]`, `[PRIVATE_PHONE_N]`, `[ACCOUNT_NUMBER_N]`).
Nessun valore reale nell'output.

---

## TC-03 · Bash — secret nell'output di un comando

Verifica che PII nell'output di un comando shell venga intercettata prima che Claude la legga.
Nessun setup richiesto: i valori sono hardcoded direttamente nel comando.

**Prompt**
```
Esegui questo comando Bash e ripetimi esattamente l'output riga per riga:
echo "APP_SECRET=sk-ant-api01-test0000000000000000000000000000000000000000000000000"
echo "OWNER_EMAIL=pinco.pallino@prova.invalid"
```

**Expected log**
```
[DEBUG] msg[user].content detected: secret='sk-ant-api01-test...'@..., private_email='pinco.pallino@prova.invalid'@...
PII masked: [PRIVATE_EMAIL, SECRET] — 2 category(ies) [DEBUG: output NOT de-masked]
```

**Pass condition** — Claude ripete le due righe ma riporta `[SECRET_1]` e `[PRIVATE_EMAIL_1]`
al posto dei valori reali. Se NER non rileva `SECRET` annotare il risultato (utile per calibrare soglie).

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

Ripetimi uno per uno i valori presenti in ogni colonna del risultato.
```

**Expected log**
```
PII masked: [ACCOUNT_NUMBER, PRIVATE_EMAIL, PRIVATE_PERSON, PRIVATE_PHONE, SECRET] — 5 category(ies)
```

**Pass condition** — Claude elenca i 5 valori colonna per colonna ma ognuno appare come
placeholder. Tutte e 5 le categorie PII coperte in un test solo, nessun dato in chiaro.

---

## TC-05 · Headroom — testo naturale lungo (Kompress)

Verifica che Kompress si attivi su un documento di testo lungo passato direttamente
nel messaggio. Il testo puro è il dominio naturale di Kompress (ModernBERT NLP);
non deve contenere JSON o codice strutturato.

**Prompt**
```
Analizza questo documento e dimmi qual è il principale collo di bottiglia
del processo descritto e quale azione correttiva è pianificata per risolverlo:

Il progetto prevede l'installazione di un sistema di monitoraggio energetico
distribuito presso tre siti produttivi. Ciascun sito è dotato di contatori
intelligenti collegati a un concentratore locale che raccoglie le misure ogni
quindici minuti e le trasmette al sistema centrale tramite connessione VPN
dedicata. Il sistema centrale archivia le misure in un database time-series,
le elabora in tempo reale per calcolare indicatori di consumo, e le distribuisce
a tre applicazioni downstream: il portale di reportistica aziendale, il sistema
di fatturazione interna e il cruscotto operativo del team tecnico.

Il processo di elaborazione si articola in quattro fasi sequenziali. La prima
fase è la raccolta: il servizio di ingestione riceve le misure dai concentratori,
verifica l'integrità del timestamp e del valore, e le inserisce nella coda di
elaborazione. La seconda fase è la validazione: ogni misura viene confrontata
con i valori storici dello stesso punto di misura per rilevare anomalie
statistiche — picchi improvvisi, valori nulli, sequenze piatte prolungate —
che potrebbero indicare un malfunzionamento del contatore. La terza fase è
l'aggregazione: le misure validate vengono aggregate su finestre temporali di
quindici minuti, un'ora, un giorno e un mese per alimentare i diversi livelli
del sistema di reportistica. La quarta fase è la distribuzione: gli aggregati
vengono pubblicati su un bus di messaggi interno e consumati dalle applicazioni
downstream secondo le proprie finestre di aggiornamento.

Il principale collo di bottiglia identificato in fase di test è la latenza della
fase di validazione statistica quando il sistema deve gestire misure in recupero
dopo un'interruzione della connettività. In tali scenari, il concentratore
trasmette al ripristino tutte le misure accumulate nel buffer locale, generando
un burst di ingresso che può raggiungere seimila messaggi al minuto per sito.
Il servizio di validazione è dimensionato per gestire mille messaggi al minuto
in condizioni normali e non dispone attualmente di un meccanismo di throttling
adattivo.

Le azioni correttive pianificate includono l'introduzione di un buffer elastico
nella coda di ingestione, il ridimensionamento del pool di worker del servizio
di validazione, e l'implementazione di un meccanismo di priorità che garantisca
l'elaborazione in tempo reale anche durante i periodi di recupero.
```

**Expected log**
```
Transform content_router: X -> Y tokens (saved Z) [Nms]
```

**Pass condition** — `saved` > 0. Claude identifica il collo di bottiglia (burst
di misure in recupero che supera la capacità del servizio di validazione) e
l'azione correttiva (buffer elastico + throttling adattivo).

---

## TC-06 · Headroom — JSON array da Bash (SmartCrusher)

Verifica che SmartCrusher (compressore Rust strutturale) si attivi su un
`tool_result` che contiene un JSON array top-level. SmartCrusher triggera solo
su `[{...},{...},...]` come primo carattere del contenuto; il wrapper MCP
`{"ok":true,"data":{...}}` lo impedisce, quindi si usa Bash con Python inline
che stampa l'array grezzo senza wrapper.

**Prompt**
```
Esegui questo comando Bash e dimmi quanti oggetti contiene l'array,
quanti hanno stato ERRORE e quanti hanno stato ELABORATO:

python -c "
import json
data = [{'id': i, 'codice': f'EVT_{i:03d}', 'stato': ['ELABORATO','IN_ATTESA','ERRORE'][i % 3], 'ts': f'2026-07-02T{8 + i // 60:02d}:{i % 60:02d}:00Z', 'descrizione': f'Descrizione del record numero {i} generata per il test di compressione SmartCrusher proxy.'} for i in range(1, 51)]
print(json.dumps(data))
"
```

**Expected log**
```
Transform content_router: X -> Y tokens (saved Z) [Nms]
```

> **Nota** — SmartCrusher non emette una riga di log separata visibile come Kompress;
> la compressione appare nel `saved Z` del `content_router`. Se `saved 0` il detector
> non ha riconosciuto il tipo `JSON_ARRAY` (verificare che l'output del comando
> inizi con `[` senza prefissi di linea).

**Pass condition** — `saved` > 0. Claude risponde: 50 oggetti, distribuzione
stati 17 ELABORATO / 17 IN_ATTESA / 16 ERRORE (o equivalente con range 1–50).

---

## TC-07 · Headroom — risultato SQL verboso da tool

Verifica che SmartCrusher compatti il `tool_result` di una query MCP anche quando
il wrapper MCP è `{"ok":true,"data":{"rows":[...]}}` (oggetto root, non array).
A partire da v1.4.0 il plugin chiama `compact_document_json()` direttamente sul
blocco, che cammina ricorsivamente e trova l'array `rows` annidato, convertendolo
in CSV+schema senza perdere righe (lossless).

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

**Pass condition** — `saved` > 0. Claude risponde correttamente: 50 righe,
distribuzione stati 17 ELABORATO / 17 IN_ATTESA / 16 ERRORE.

---

## TC-08 · Headroom — sorgente del proxy

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

> La colonna **Rilevato** riporta solo le entità originate dal testo del test,
> escludendo quelle iniettate dal system prompt di Claude Code (username, nome
> utente, data corrente) che occupano i primi slot del contatore ma non
> appartengono al caso di test.

| ID | Descrizione | Filtro atteso | Rilevato (dal testo test) | Log ✓ | Risposta ✓ |
|----|-------------|---------------|---------------------------|-------|------------|
| TC-01 | Chat PII multipla | PERSON × 2, EMAIL, PHONE, IBAN | PERSON: 'Pinco Pallino' ×2→1ph, 'Futura Incognita' ×2→1ph · EMAIL ×1 · PHONE ×1 · IBAN ×1 | ✓ | ✓ |
| TC-02 | File CSV | EMAIL × 3, PHONE × 3, IBAN × 3 | EMAIL ×3 · PHONE ×3 · IBAN ×3 · PERSON: 'Primo' ×1, 'Caia' ×1, 'Sempronio Terzo' ×1 (Tizio/Seconda single-word, dropped) | ✓ | ✓ |
| TC-03 | Bash / echo | SECRET, EMAIL | EMAIL ×1 ✓ (mascherata nel prompt) · SECRET `sk-ant-api01-test...`: non rilevato dal NER → Claude vede la chiave in chiaro → rifiuta il Bash per safety proprie (non per il proxy) · Bash mai eseguito, nessun tool_result · FP: `-anthropic-billing:` nel system prompt → PRIVATE_URL ogni request | ~ | ✗ |
| TC-04 | SQL FROM DUAL | PERSON, EMAIL, PHONE, IBAN, SECRET | EMAIL ×1 ✓ · PHONE ×1 ✓ · PERSON: mancato nel prompt (no trigger SQL), rilevato nel result ma Claude già esposto · IBAN: mangled → `IT[PRIVATE_PHONE_1]` (classificato come PHONE) · SECRET `sk-secret-internal-key-000`: non rilevato (formato non riconosciuto dal NER) · FP: `anubi` (nome connessione) → PRIVATE_PERSON | ~ | ✗ |
| TC-05 | Headroom — testo naturale (Kompress) | saved > 0 | 3 chunk paralleli · words 1160/2199/1236 · ratio 0.68/0.79/0.75 · saved 1383 token (12.1%) | ✓ | ✓ |
| TC-06 | Headroom — JSON array Bash (SmartCrusher) | saved > 0 | | | |
| TC-07 | Headroom — SQL 50 righe MCP (compact_document_json) | saved > 0 | | | |
| TC-08 | Headroom — sorgente proxy | saved > 0 | | | |
