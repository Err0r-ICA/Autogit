# GH Autopilot

Gestore intelligente del tuo profilo GitHub, da terminale, con interfaccia colorata (pystyle).
Sincronizza, fa backup, crea, sostituisce e gestisce le tue repository con pochi tasti, senza mai perdere lavoro locale.

## Funzionalità

**Sincronizzazione**
- Ti riconosce: token (portachiavi o file protetto), cartella base, preferenze, ultimo sync
- Sync incrementale (ETag + `pushed_at`): clona le repo mancanti e aggiorna solo quelle con novità
- Aggiornamento sicuro: `fetch --prune`, fast-forward, autostash, aggiornamento delle altre branch, sync dei fork con l'upstream, supporto submodule
- Branch divergenti: strategia `safe` (non tocca nulla) oppure `rebase`
- Pubblica le modifiche locali su tutte le repo (commit automatico + push)

**Download e backup**
- Download ZIP di una o più repo
- Backup mirror completo (`git clone --mirror`)

**Crea e sostituisci**
- Crea repo vuota (README, `.gitignore`, licenza) o da una cartella locale (init + commit + push), anche in organizzazioni
- Sostituisce una repo esistente con un'intera cartella, in 3 modalità:
  1. **Mantieni cronologia**: nuovo commit che sostituisce il contenuto
  2. **Reset totale**: storia azzerata + force push (distruttivo, richiede conferma)
  3. **Nuovo branch**: push su un branch separato, nulla viene toccato
- Backup mirror automatico prima di sostituzioni ed eliminazioni
- Controllo file sensibili (`.env`, chiavi private, token) e file oltre 100 MB prima del caricamento

**Gestione repo**
- Info, rinomina (aggiorna anche la cartella locale e il remote), visibilità, descrizione e topics, archivia/ripristina, branch di default, eliminazione

**Esperienza d'uso**
- Barra di progresso con percentuale, timer, ETA, repo e file gestiti
- Report finale salvato in JSON (ultimi 50) e consultabile dal menu
- Lista repo a pagine con filtro, preferiti (★) e stato locale (● aggiornata, ◐ con novità, ○ non clonata)
- Comandi in linguaggio naturale: `scarica emporio`, `sostituisci emporio46`, `elimina test`, `zip tutte`

## Requisiti

- Python **3.9+**
- **git** installato e nel `PATH`
- Un token GitHub (vedi sotto)
- Facoltativi: [`pystyle`](https://pypi.org/project/pystyle/) (grafica) e [`keyring`](https://pypi.org/project/keyring/) (token nel portachiavi)

## Installazione

```bash
git clone <url-di-questa-repo>
cd gh-autopilot
pip install -r requirements.txt
```

Le dipendenze sono opzionali: senza `pystyle` lo script gira senza colori (o ti propone di installarlo), senza `keyring` il token viene salvato in un file con permessi `600`.

## Token GitHub

Al primo avvio ti viene chiesto di incollare un token (non viene mostrato a schermo). Crealo da <https://github.com/settings/tokens>:

- **Classic**: scope `repo` (aggiungi `delete_repo` se vuoi poter eliminare repo)
- **Fine-grained**: permessi *Contents* e *Administration* in lettura/scrittura

Ordine di ricerca del token:

1. Variabile d'ambiente `GITHUB_TOKEN` o `GH_TOKEN`
2. Variabile `GITHUB_TOKEN` dentro lo script (sconsigliato)
3. Portachiavi di sistema (`keyring`) o file protetto
4. GitHub CLI (`gh auth token`)

> **Non scrivere mai il token nello script e non caricarlo su GitHub.** Lo script blocca comunque i file che contengono token o chiavi private quando pubblichi una cartella.
> Il token viene passato a git solo come header temporaneo: non finisce mai negli URL né in `.git/config`.

## Uso

```bash
python gh_autopilot.py              # modalità interattiva
python gh_autopilot.py --sync       # sync intelligente non interattivo (adatto a cron)
python gh_autopilot.py --sync --full  # controlla tutte le repo, non solo quelle con novità
```

In modalità interattiva premi **invio** per il sync intelligente, scegli un numero del menu oppure scrivi a parole cosa vuoi fare.

### Menu

| N. | Azione |
|----|--------|
| 1 | Sync intelligente (invio) |
| 2 | Sync completo forzato |
| 3 | Scegli repo da clonare/aggiornare |
| 4 | Pubblica modifiche locali (commit + push) |
| 5 | Scarica ZIP |
| 6 | Backup mirror completo |
| 7 | Crea repo (vuota o da cartella) |
| 8 | Sostituisci repo con una cartella |
| 9 | Gestisci repo (rinomina, visibilità, elimina…) |
| 10 | Stato repo locali |
| 11 | Report precedenti |
| 12 | Impostazioni |
| 13 | Ricarica elenco da GitHub |
| 0 | Esci |

### Selezione delle repo

| Input | Effetto |
|-------|---------|
| `1 3 5-8` | seleziona per numero o intervallo |
| `all` | seleziona tutte (dove consentito) |
| `n` / `p` | pagina successiva / precedente |
| testo libero o `/testo` | filtra l'elenco (con ricerca approssimata) |
| `*N` | aggiunge/rimuove la repo N dai preferiti |
| `0` | esci |

### Esempi in linguaggio naturale

```
scarica emporio
sostituisci emporio46
elimina test
zip tutte
backup tutte
pubblica le mie modifiche
```

### Sync automatico con cron

```cron
# ogni giorno alle 9:00
0 9 * * * /usr/bin/python3 /percorso/gh_autopilot.py --sync
```

Serve aver fatto almeno un accesso interattivo (o aver impostato `GITHUB_TOKEN`).

## Dove salva i dati

| Cosa | Percorso |
|------|----------|
| Configurazione, cache, token (se senza keyring), report | Linux/macOS: `~/.config/gh-autopilot/` · Windows: `%APPDATA%\gh-autopilot\` |
| Repo clonate | `<cartella base>/<owner>/<repo>` (default `~/GitHub`) |
| ZIP scaricati | `<cartella base>/_zip/` |
| Backup mirror | `<cartella base>/_mirrors/<owner>/<repo>.git` |
| Report | `.../gh-autopilot/reports/*.json` |

## Impostazioni

Dal menu **12**:

- Cartella base
- Includi fork / repo archiviate / repo di organizzazioni
- Sincronizza i fork con l'upstream
- Gestione submodule
- Backup automatico prima di sostituzioni ed eliminazioni
- Strategia per branch divergenti: `safe` o `rebase`
- Operazioni parallele (1-16 worker)
- Dimentica token e logout

## Sicurezza

- Nessuna operazione di sync cancella lavoro locale: modifiche non committate preservate con autostash, branch divergenti segnalate e non toccate (in modalità `safe`)
- Le operazioni distruttive (reset totale, eliminazione repo) richiedono di digitare il nome completo della repo
- Backup mirror automatico prima di sostituire o eliminare (disattivabile)
- Esclusione guidata di file sensibili (`.env`, `*.pem`, `*.key`, chiavi SSH, token rilevati nel contenuto)

## Risoluzione problemi

- **`git non trovato`**: installa git e assicurati che sia nel `PATH`
- **Rate limit raggiunto**: attendi il tempo indicato e riprova
- **Eliminazione fallita**: il token non ha lo scope `delete_repo`
- **`dubious ownership`**: lo script aggiunge automaticamente la cartella a `safe.directory`
- **Colori o caratteri strani**: usa un terminale con supporto Unicode e ANSI, oppure lavora senza `pystyle`

## Licenza

Scegli la licenza che preferisci (es. MIT) e aggiungila come `LICENSE`.
