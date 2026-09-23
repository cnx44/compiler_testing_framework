# ASPIS Campaign Runner

Questo progetto automatizza il confronto fra un programma compilato normalmente
(`Oracle`) e lo stesso programma protetto con ASPIS. Esegue entrambi i binari,
misura tempo e dimensione, acquisisce lo stato al termine delle funzioni scelte,
inietta fault tramite GDB e classifica gli effetti osservati.

Il progetto comprende:

- un linguaggio di configurazione per descrivere campagne riproducibili;
- una TUI curses per creare, modificare, eseguire e consultare campagne;
- runner separati per il binario Oracle e quello ASPIS;
- injector polimorfici per registri, memoria e variabili globali;
- script Python eseguiti dentro GDB;
- log testuali e riepiloghi JSON utilizzabili anche fuori dalla TUI.

> Il runner non contiene ASPIS: usa un'installazione esterna tramite il relativo
> `aspis.sh`. I plugin ASPIS devono essere stati compilati con una versione LLVM
> compatibile con `clang`, `opt` e `llvm-link` selezionati.

## Indice

- [Architettura](#architettura)
- [Pipeline completa di una campagna](#pipeline-completa-di-una-campagna)
- [Installazione](#installazione)
- [Configurazione della toolchain](#configurazione-della-toolchain)
- [Grammatica delle campagne](#grammatica-delle-campagne)
- [Modalità di fault injection](#modalità-di-fault-injection)
- [Uso della TUI](#uso-della-tui)
- [Uso senza TUI](#uso-senza-tui)
- [Output e interpretazione dei risultati](#output-e-interpretazione-dei-risultati)
- [Timeout](#timeout)
- [Errori comuni](#errori-comuni)
- [Test](#test)
- [Limiti attuali](#limiti-attuali)
- [Come estendere il progetto](#come-estendere-il-progetto)

## Architettura

Il flusso dei dati è il seguente:

```ASCII
                               +-----------+
                               | File .cfg | <-------------------------------------------+
                               +-----------+                                             |
                                     |                                                   |
                                     v                                                   |
                           +------------------+                                          |
                           | config_parser.py |                                          |
                           +------------------+       +------------------------+         |
                                     |                | Environment / .aspis-  |         |
                                     v                |        tui.json        |         |
                        +-----------------------+     +------------------------+         |
                        | Campaign / RunnerTest |                 |                      |
                        +-----------------------+                 |                      |
                                     |                            |                      |
                                     v                            |                      |
                               +-------------+ <------------------+                      |
                               | CampaignRun |                                           |
                               +-------------+                                           |
                                 /         \                                             |
                                /           \                                            |
                               v             v                                           |
                     +--------------+   +-------------+                                  |
                     | OracleRunner |   | AspisRunner |                                  |
                     +--------------+   +-------------+                                  |
                            |                  |                                         |
                            v                  v                                         |
                   +----------------+  +------------------+                              |
                   | Binario Oracle |  | Binario protetto |                              |
                   +----------------+  +------------------+                              |
                            |              /      \                                      |
                            v             /        \                                     |
                +---------------------+  /          \                                    |
                |    Snapshot GDB     |<+            v                                   |
                +---------------------+   +----------------------------------+           |
                         |                |  BaseInjector.create_for_target  |           |
                         |                +----------------------------------+           |
                         |                      /          |          \                  |
                         |                     /           |           \                 |
                         |                    v            v            v                |
                         |   +------------------+ +----------------+ +----------------+  |
                         |   | RegisterInjector | | MemoryInjector | | GlobalInjector |  |
                         |   +------------------+ +----------------+ +----------------+  |
                         |             \                   |                  /          |
                         |              \                  v                 /           |
                         |               +------> +-----------------+ <-----+            |
                         |                        |   Script GDB    |                    |
                         |                        +-----------------+                    |
                         |                                 |                             |
                         v                                 v                             |
                  +-------------------------------+ <------+                             |
                  |  Confronto e classificazione  |                                      |
                  +-------------------------------+                                      |
                             /           \                                               |
                            /             \                                              |
                           v               v                                             |
                       +------+    +-------------------------+                           |
                       | .log |    | campaign_summary_*.json |                           |
                       +------+    +-------------------------+                           |
                           \                 /                                           |
                            \               /                                            |
                             v             v                                             |
                            +--------------+                                             |
                            | front_end.py | --------------------------------------------+
                            +--------------+
```

### Moduli principali

| Modulo | Responsabilità |
|---|---|
| `src/front_end.py` | TUI, selezione file, editor delle configurazioni, impostazioni toolchain e visualizzazione risultati. |
| `src/grammar.lark` | Grammatica Lark autorevole del formato `.cfg`. |
| `src/config_parser.py` | Parsing della grammatica e conversione in dataclass immutabili. Risolve anche i percorsi dei sorgenti. |
| `src/config_builder.py` | Serializzazione canonica delle dataclass nel formato `.cfg`; usata dall'editor. |
| `src/env.py` | Modello della toolchain esterna: ASPIS, LLVM, compiler e debugger. |
| `src/constants.py` | Valori predefiniti condivisi, attualmente il timeout di `5000 ms`. |
| `src/campaign_runner.py` | Compilazione Oracle/ASPIS, esecuzioni normali, snapshot, misure, orchestrazione delle injection e scrittura dei risultati. |
| `src/injector.py` | Factory e classi degli injector, profiling, avvio debugger, timeout e classificazione dell'evento. |
| `src/models.py` | `ExecutionState`, `ExecutionResult` e `TerminationEvent`. |
| `src/gdb_scripts/` | Script caricati dentro GDB per profiling, snapshot e bit flip. |
| `src/aspis_handlers.c` | Handler collegati automaticamente al binario protetto per rendere riconoscibili le detection ASPIS. |
| `test/` | Unit test e test dinamici che avviano realmente GDB. |
| `samples/` | Programmi e configurazioni di esempio. |

`grammar.bnf` è un documento storico e non viene caricato dal programma. Alcune
sue regole non riflettono più il parser corrente: quando c'è una differenza,
fa fede `src/grammar.lark`.

### Modello dei dati

Il parser produce una `Campaign`, che contiene uno o più `RunnerTest`.
Ogni test contiene:

- nome;
- lista dei sorgenti da compilare assieme;
- modalità di protezione dati e control flow;
- tipo di injection;
- timeout per processo;
- numero di ripetizioni;
- flag del compilatore/linker;
- funzioni e variabili da osservare o usare come target.

Le dataclass prodotte dal parser sono `frozen`: una configurazione caricata non
viene modificata accidentalmente durante l'esecuzione. La TUI usa invece una
`ConfigurationDraft` mutabile e la converte nel modello definitivo solo dopo la
validazione.

## Pipeline completa di una campagna

Per ogni blocco `name:` del file `.cfg`, `CampaignRun.run_campaign()` esegue
questi passaggi.

1. Individua il primo sorgente della lista come *entry source*. Questo file
   determina anche l'eventuale file `.values` e il sorgente usato dalla ricerca
   automatica delle globali.
2. Legge gli argomenti da `<entry-source>.values`, se presente.
3. Crea un `OracleRunner` e un `AspisRunner` con lo stesso timeout.
4. Compila entrambi i binari in directory temporanee separate.
5. Esegue prima tutte le ripetizioni Oracle e poi tutte quelle ASPIS.
6. Calcola media, deviazione standard ed errore standard dei tempi; misura la
   dimensione dei due eseguibili.
7. Avvia una nuova esecuzione sotto debugger per ogni funzione configurata e
   acquisisce argomenti, locali, globali richieste e registri vicino al ritorno.
8. Crea gli injector tramite `BaseInjector.create_for_target()`.
9. Esegue il profiling dinamico necessario all'injection.
10. Lancia le injection concorrenti e raccoglie un `ExecutionResult` per ognuna.
11. Classifica i risultati rispetto allo stato Oracle.
12. Scrive il log e un riepilogo JSON, quindi elimina i binari temporanei.

Se un test fallisce, l'errore viene aggiunto al log e la campagna prosegue con il
test successivo. La pulizia delle directory temporanee avviene anche in caso di
eccezione.

### Compilazione Oracle

Il comando concettuale è:

```text
<compiler> -g -O0 <sources...> <compiler_flags...> -o <temporary-binary>
```

L'Oracle è la baseline funzionale e prestazionale. È compilato con simboli di
debug e senza ottimizzazioni perché la raccolta dello stato dipende dalle
informazioni DWARF e da una corrispondenza leggibile tra funzione e sorgente.

### Compilazione ASPIS

Il comando costruito dal runner è equivalente a:

```text
<aspis.sh> -g -o program --build-dir <temporary-dir> \
  [--llvm-bin <llvm-bin>] \
  <data-mode> <control-flow-mode> \
  <aspis_flags...> \
  <sources...> src/aspis_handlers.c \
  <compiler_flags...>
```

Le modalità della configurazione vengono tradotte così:

| Configurazione | Argomento ASPIS |
|---|---|
| `data_protection: eddi` | `--eddi` |
| `data_protection: seddi` | `--seddi` |
| `data_protection: fdsc` | `--fdsc` |
| `data_protection: none` | `--no-dup` |
| `control_flow_protection: cfcss` | `--cfcss` |
| `control_flow_protection: rasm` | `--rasm` |
| `control_flow_protection: inter-rasm` | `--inter-rasm` |
| `control_flow_protection: none` | `--no-cfc` |

In termini generali, EDDI duplica le operazioni sui dati e inserisce controlli
di consistenza; SEDDI applica controlli selettivi; FDSC usa duplicazione completa
con checking selettivo. CFCSS e RASM proteggono il control flow tramite firme;
`inter-rasm` estende il controllo RASM tra funzioni. Il comportamento preciso
resta quello implementato dalla versione esterna di ASPIS in uso. `none`
disabilita soltanto l'asse di protezione corrispondente.

`src/aspis_handlers.c` viene collegato automaticamente e implementa:

- `DataCorruption_Handler()`, che stampa `Data corruption detected`;
- `SigMismatch_Handler()`, che stampa `Control-flow signature mismatch`.

Queste stringhe permettono all'injector di distinguere una detection ASPIS da
un crash generico. Non è quindi necessario ridefinire gli handler nei programmi
di test.

I percorsi relativi contenuti in flag come `-I`, `-L`, `-include`, `-iquote`,
`-isystem` e `--sysroot` vengono normalizzati prima di eseguire ASPIS nella
directory temporanea.

### Snapshot al ritorno della funzione

Lo snapshot non viene preso al `return` generico del processo. Per ogni funzione
richiesta, lo script:

1. inserisce un breakpoint all'ingresso della funzione;
2. usa le line table DWARF per trovare la linea sorgente più alta appartenente
   alla funzione;
3. continua fino a quella linea mentre il frame è ancora attivo;
4. scarica sezioni marcate `@@ARGS@@`, `@@LOCALS@@`, `@@GLOBALS@@` e
   `@@REGISTERS@@`.

Questo consente di leggere locali e argomenti prima che il frame venga distrutto.
Con `main` il punto osservato è quindi il ritorno di `main`. Se la funzione non 
viene chiamata, è stata eliminata/inlined o non dispone di informazioni di debug 
sufficienti, lo snapshot può fallire.

Su Linux e macOS viene usato GDB. Quando nessun debugger è imposto
esplicitamente (il default), viene risolto l'eseguibile `gdb` nel `PATH` di sistema.

## Installazione

### Prerequisiti

Servono:

- Python 3 con supporto `curses`;
- `pip` e un ambiente virtuale consigliato;
- un compilatore C/C++, normalmente `clang`;
- GDB con supporto Python;
- ASPIS compilato;
- una toolchain LLVM compatibile con i plugin ASPIS.

Su sistemi Linux il debugger può richiedere il permesso di usare `ptrace`. In
container o sandbox bisogna inoltre permettere l'avvio dell'inferior da GDB.

### Ambiente Python

Dalla root del progetto:

```shell
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

L'unica dipendenza Python applicativa dichiarata è `lark==1.3.1`.

### Verifica manuale di ASPIS

Prima di usare la TUI è utile verificare lo script esterno direttamente:

```shell
../ASPIS/aspis.sh \
  --llvm-bin ~/llvm-21.1.8/build/bin \
  ../ASPIS/path_to_temp/helloworld-c
```

Se `opt` segnala una versione API errata del plugin, la directory passata a
`--llvm-bin` e i plugin in `ASPIS/build/passes` appartengono a versioni LLVM
incompatibili. Bisogna ricompilare ASPIS contro la toolchain scelta oppure usare
la toolchain con cui i plugin sono stati costruiti.

## Configurazione della toolchain

Avviare la TUI dalla directory che si vuole usare come directory del progetto:

```shell
python -m src.front_end
```

La voce **Configure toolchain** configura:

| Campo | Significato |
|---|---|
| `ASPIS script` | Percorso eseguibile di `aspis.sh`. |
| `LLVM bin directory` | Directory che deve contenere almeno `clang`, `opt` e `llvm-link`. |
| `Oracle compiler` | Comando usato per compilare il binario non protetto. |

| `Additional ASPIS flags` | Opzioni destinate allo script ASPIS, per esempio `--no-cleanup --coarse-grained`. |

Le impostazioni vengono salvate in `.aspis-tui.json` nella directory dalla
quale è stata avviata la TUI. Un esempio equivalente alla toolchain usata nello
sviluppo è:

```json
{
  "aspis_path": "/mnt/misto/projects/ASPIS/aspis.sh",
  "llvm_path": "/home/cnx/llvm-21.1.8/build/bin",
  "vanilla_compiler": "clang",
  "debugger": "gdb",
  "aspis_flags": []
}
```

Il comando mostrato nella home deve assomigliare a:

```text
/mnt/misto/projects/ASPIS/aspis.sh --llvm-bin /home/cnx/llvm-21.1.8/build/bin <mode> <source.c>
```

### Configurazione tramite CLI o ambiente

La TUI accetta anche:

```shell
python -m src.front_end \
  --aspis-path ../ASPIS/aspis.sh \
  --llvm-path ~/llvm-21.1.8/build/bin \
  --compiler clang \
  --debugger gdb \
  --aspis-flags '--no-cleanup --coarse-grained'
```

Le opzioni hanno equivalenti tramite variabili d'ambiente:

| Opzione CLI | Variabile |
|---|---|
| `--aspis-path` | `ASPIS_PATH` |
| `--llvm-path` | `ASPIS_LLVM_PATH` |
| `--compiler` | `ASPIS_COMPILER` |
| `--debugger` | `ASPIS_DEBUGGER` |
| `--aspis-flags` | `ASPIS_FLAGS` |

La precedenza è: argomenti CLI o variabili d'ambiente, configurazione salvata,
valori locali predefiniti. Le opzioni ASPIS sono distinte dai flag di
compilazione: `--coarse-grained` appartiene alla toolchain, mentre `-lm`, `-O2`
e `-DTRACE=1` appartengono al singolo test.

## Grammatica delle campagne

La fonte autorevole è `src/grammar.lark`. Il parser è LALR e l'ordine dei campi
è significativo. Una campagna contiene uno o più test consecutivi; non servono
separatori tra un blocco e il successivo.

La struttura semplificata di un test è:

```text
name: <nome>
programs: [<sorgente>, ...]
data_protection: eddi | seddi | fdsc | none
control_flow_protection: cfcss | rasm | inter-rasm | none
[timeout_ms: <intero positivo>]
[execution_repetitions: <intero positivo>]
[measure_overhead: true|false]
[measurement_repetitions: <intero positivo>]
[compiler_flags: [<flag>, ...]]
[injection: register [data_only: true|false] | memory | global]
[functions: [<funzione>([@]<variabile>[:<lunghezza>], ...)], ...]]
```

### Campi e valori predefiniti

| Campo | Obbligatorio | Default | Note |
|---|---:|---|---|
| `name` | sì | — | Lettere, numeri, `_` e `-`; niente spazi. |
| `programs` | sì | — | Uno o più percorsi `.c`, `.cpp` o `.rs`. |
| `data_protection` | sì | — | `eddi`, `seddi`, `fdsc` o `none`. |
| `control_flow_protection` | sì | — | `cfcss`, `rasm`, `inter-rasm` o `none`. |
| `timeout_ms` | no | `5000` | Limite positivo applicato a ogni processo di esecuzione/debug. |
| `execution_repetitions` | no | `1` | Numero di injection per ciascun injector; default anche per il timing. |
| `measure_overhead` | no | `true` | Abilita le misurazioni e il confronto dell'overhead. |
| `measurement_repetitions` | no | `execution_repetitions` | Numero di campioni temporizzati, indipendente dalle injection. |
| `compiler_flags` | no | `[]` | Flag separati da virgole, passati a entrambi i compilatori. |
| `injection` | no | `register data_only: false` | Strategia di fault injection. |
| `functions` | no | `[main]` | Funzioni sulle quali acquisire stato e iniettare fault. |

Questi sono i default del parser per un campo omesso. L'editor TUI propone
invece `100` ripetizioni quando crea una nuova configurazione e salva il valore
esplicitamente.

Righe vuote tra i test sono ammesse. I nomi di funzioni, variabili e globali
seguono la regola `WORD`: `[A-Za-z0-9_-]+`. Prima di passarli al debugger, il
runner applica una validazione più stretta da identificatore C
(`[A-Za-z_]\w*`).

### Percorsi dei sorgenti

Sono ammessi percorsi non quotati senza spazi:

```text
programs: [../sources/main.c, ../sources/helper.cpp]
```

oppure stringhe JSON con doppi apici:

```text
programs: ["/mnt/data/source files/main.c"]
```

I percorsi relativi sono risolti rispetto alla directory del file `.cfg`, non
rispetto alla directory corrente. La TUI salva percorsi assoluti e quotati; una
configurazione può quindi risiedere separatamente dai sorgenti.

Più sorgenti nello stesso blocco vengono compilati e collegati in un unico
programma. Questo è diverso dal batch, che crea un test indipendente per ogni
file.

### Flag del compilatore

I flag sono separati da virgole:

```text
compiler_flags: [-O2, -lm, -DTRACE=1, -I../include]
```

Ogni flag è un token senza spazi, virgole o parentesi quadre. Per esempio `-lm`
è necessario per programmi che usano `acos`, `sqrt` o `pow` con toolchain che
richiedono il collegamento esplicito di `libm`.

### Funzioni e variabili

Esempi validi:

```text
functions: [main]
functions: [main(result), work(buffer:128, length), helper]
```

La lista ha tre effetti:

- definisce le funzioni di cui acquisire lo snapshot;
- definisce lo scope in cui effettuare l'injection;
- indica le variabili da confrontare e i possibili payload da colpire. Per la modalità `memory` verranno usate solo le variabili standard (es. `buffer`), mentre per la modalità `global` verranno usate solo le variabili precedute da `@` (es. `@counter`).

`buffer:128` associa alla variabile una lunghezza di 128 elementi. Senza
lunghezza, l'injector tenta di inferire la dimensione degli array statici; per un
puntatore o uno scalare non inferibile usa un elemento. Se `functions: []` è
esplicito, non vengono creati injector; l'esecuzione normale conserva comunque
lo snapshot predefinito di `main`. Se per una data modalità di iniezione non viene fornita alcuna variabile compatibile, il framework ricadrà su una selezione casuale della variabile da corrompere.

### Esempio completo

```text
name: protected_math
programs: ["/mnt/misto/projects/progettoES/samples/exe1.c"]
data_protection: eddi
control_flow_protection: cfcss
timeout_ms: 8000
execution_repetitions: 100
compiler_flags: [-O0, -lm]
injection: register data_only: true
functions: [main]
```

### Campagna con più test

```text
name: registers
programs: [../sources/app.c, ../sources/math.c]
data_protection: eddi
control_flow_protection: cfcss
execution_repetitions: 50
compiler_flags: [-lm]
injection: register data_only: false
functions: [main(result)]

name: buffer
programs: [../sources/buffer.c]
data_protection: seddi
control_flow_protection: rasm
timeout_ms: 10000
execution_repetitions: 25
injection: memory
functions: [process(data:256, size)]

name: global-counter
programs: [../sources/global.c]
data_protection: fdsc
control_flow_protection: inter-rasm
execution_repetitions: 25
injection: global
functions: [main(@counter)]
```

### Argomenti del programma: file `.values`

Se il primo sorgente è `program.c`, il runner cerca `program.values` nella
stessa directory. Le righe riconosciute sono:

```text
args[1] = first
args[2] = 42
args[3] = value-with-spaces
```

Gli indici vengono ordinati e i valori sono passati come argomenti distinti al
programma. Le altre righe sono ignorate. L'API Python può anche passare una
lista `args` direttamente a `run_campaign()`; in quel caso la stessa lista
sostituisce i `.values` per tutti i test della campagna.

## Modalità di fault injection

`BaseInjector` definisce il ciclo comune e nasconde a `CampaignRun` i dettagli
dei singoli meccanismi. La factory `create_for_target()` riceve la configurazione
tipizzata e restituisce gli injector concreti necessari.

### Profiling comune

Prima della prima injection di un injector viene eseguito un profiling della
funzione target. Il risultato viene memorizzato e condiviso in modo thread-safe
fra le ripetizioni.

Il profiler:

- ferma il programma all'ingresso della funzione;
- conta dinamicamente le istruzioni attraversate nella specifica esecuzione;
- per l'injection sui registri rileva anche i registri usati dal disassemblato;
- produce `PROFILED_STEPS` e, quando serve, `PROFILED_REGS`.

Ogni injection sceglie casualmente un numero di passi fra `1` e il massimo
profilato. Di conseguenza i risultati non sono deterministici e non viene
attualmente salvato un seed casuale.

### Register injection

```text
injection: register data_only: false
```

Sceglie un registro usato dalla funzione e inverte un bit casuale rispettando
la larghezza architetturale del registro. Con `data_only: false` possono essere
coinvolti anche registri puntatore, aumentando la probabilità di crash o
alterazioni del control flow.

Con:

```text
injection: register data_only: true
```

vengono esclusi registri come stack pointer, frame pointer, program counter e i
principali registri ABI usati per puntatori/argomenti. Esistono fallback specifici
per x86-64, AArch64, ARM e RISC-V.

### Memory injection

```text
injection: memory
functions: [work(buffer:128)]
```

La factory crea un `MemoryInjector` per ogni variabile configurata nella
funzione. L'injector tenta di determinare indirizzo, tipo e dimensione tramite
DWARF, con fallback sui registri ABI per alcuni parametri. Inverte quindi un bit
casuale in una word di quattro byte dentro i limiti calcolati.

Se la funzione non specifica variabili, viene creato un injector generico che
usa un intervallo sicuro dello stack come fallback. L'inferenza dinamica dipende
dalla qualità delle informazioni di debug e dall'ABI della piattaforma.

### Global injection

Target esplicito:

```text
injection: global
functions: [main(@counter)]
```

oppure selezione automatica:

```text
injection: global
functions: [main]
```

La sintassi `@` indica al framework che la variabile specificata è globale. 
Nel primo caso, la factory crea un `GlobalInjector` per ogni variabile globale (preceduta da `@`) configurata nella funzione.
Nel secondo caso (nessuna variabile globale fornita esplicitamente), lo script analizza `info variables`, limita i candidati al
sorgente di ingresso e ne sceglie uno casualmente. Dopo alcuni passi nella
funzione, inverte un bit nei primi otto byte della globale scelta.

### Numero totale delle injection

Per `register` e `global`, in assenza di errori:

```text
totale = execution_repetitions × numero_di_funzioni
```

Per `memory` con variabili configurate:

```text
totale = execution_repetitions × somma_delle_variabili_di_ogni_funzione
```

Le ripetizioni dello stesso injector vengono avviate in modo concorrente tramite un
`ThreadPoolExecutor`. Un'eccezione dell'injector viene registrata come
`Injection error` e non entra nel totale classificato; il totale JSON è quindi
il numero di `ExecutionResult` effettivamente raccolti.

## Uso della TUI

### Selezione dei simboli per sorgente (Tree-sitter)

Installare le dipendenze aggiornate con `python -m pip install -r requirements.txt`.
Nel campo **Functions/variables** un menu elenca i file con lo scope corrente.
Il valore iniziale è `main` per tutti: si può salvare senza aprire questo menu.
Aprire soltanto i file da personalizzare; **Browse source symbols** analizza il
file C, C++ o Rust senza compilazione: scegliere le funzioni, poi i parametri e
le locali oppure le globali (`@G`). È possibile confermare senza variabili e
modificare il testo finale, ad esempio `main(x:4, @G)`. Le liste scorrono con
le frecce; Space seleziona ed Enter conferma.

La scelta viene fatta una volta per sorgente indipendente e riutilizzata per
tutte le combinazioni ASPIS e injection. **Use main for all files** applica
`main` senza target a tutti; **Enter manually** modifica lo scope del file scelto.
Dopo ogni modifica si torna all'elenco. **Done** o Esc chiude il menu mantenendo
le modifiche; annullare l'editor di un singolo file conserva il suo scope precedente.
Le locali sono target dell'injection memory, le globali dell'injection global.
Con **Link files together**, le funzioni selezionate appartengono allo stesso
eseguibile: i nomi devono essere non ambigui nel debugger.

L'analisi è sintattica: commenti e stringhe non diventano simboli. Macro non
espanse, overload, namespace, classi, template e impl Rust possono richiedere
configurazione manuale; i casi omessi sono segnalati. La disponibilità di una
locale dipende dal punto di arresto e dallo scope del debugger. Le lunghezze
degli array si specificano manualmente; le selezioni non garantiscono che una
variabile sia disponibile durante l'injection. Il supporto Rust qui riguarda
la selezione dei simboli: il runner attuale richiede ancora sorgenti C/C++.

Gli errori di lettura/sintassi di un `.cfg` avviato dalla TUI vengono aggiunti al
relativo `.log` come `ERROR: CONFIGURATION`, mostrando anche un messaggio.
Durante la campagna, errori di configurazione quali sorgenti mancanti e nomi
di funzione non validi sono distinti dagli errori operativi; gli altri test
continuano. Errori dell'analizzatore mostrano il motivo e aprono l'input manuale.
Tempi e injection restano nello stesso flusso.

L'integrazione usa i [binding Python ufficiali Tree-sitter](https://tree-sitter.github.io/py-tree-sitter/)
con le grammatiche dedicate C, C++ e Rust.

Avvio:

```shell
source .venv/bin/activate
python -m src.front_end
```

La UI è in inglese. I comandi comuni sono:

- `↑` / `↓` o `j` / `k`: spostamento o scroll;
- `←` / `→` o `h` / `l`: cambio test o risultato selezionato;
- `Enter`: apertura o conferma;
- `Space`: selezione multipla dei file;
- `Esc` o, dove indicato, `q`: ritorno o uscita;
- `s`: selezione della directory corrente nel picker delle directory.

I selettori multipli usano `[ ]` per gli elementi disponibili e `[✓]` per quelli
selezionati, anche nel picker dei sorgenti. In **Fault injection**, Enter o Space
attivano/disattivano una voce e **Done** conferma. **Register · all registers**
scrive `register data_only: false` nel `.cfg`; **Register · data registers only**
scrive `register data_only: true`. Sono due opzioni distinte, non un refuso.

### Run campaign

1. Selezionare un file `.cfg`.
2. La configurazione viene validata e i percorsi relativi vengono risolti dalla
   sua directory.
3. La TUI esegue la campagna con la toolchain attiva.
4. Al termine apre il riepilogo. Se nessun test produce risultati, mostra il log
   completo; `Esc` torna alla selezione e permette di correggere configurazione
   o toolchain.

Nel riepilogo:

- `←` / `→` cambia test;
- `s` apre gli stati classificati come SDC;
- `t` apre gli stati Latent (`h`/`l` cambia test);
- `g` apre il log completo della campagna corrente;
- `Esc` torna alla home.

### Create configuration

**Select files** crea test indipendenti per i sorgenti selezionati;
**Link files together** li compila nello stesso eseguibile. Nel file picker,
`Space` seleziona più file e `Enter` conferma.

Il form espone direttamente i campi della grammatica. **Validate and save**
controlla nome, file, interi positivi e sintassi completa prima di chiedere la
directory e il nome del `.cfg`. In caso d'errore si ritorna allo stesso form con
i valori inseriti ancora presenti.
Se si selezionano più opzioni per le protezioni dati, control flow o injection,
la TUI genera automaticamente una matrice di test indipendenti per tutte le
combinazioni possibili.

Il salvataggio su un file esistente richiede conferma. Nei campi di testo il
valore attuale è modificabile: `Enter` lo conserva, `Ctrl-U` lo svuota,
`Esc` annulla subito e `:q` esce dall'applicazione.

### Create configuration → Batch directory

Il batch seleziona una directory e crea un test indipendente per ogni sorgente
direttamente contenuto al suo interno. La scansione non è ricorsiva. Ogni test
eredita protezioni, timeout, ripetizioni, flag, injection e funzioni del form.
Anche in questo caso, selezioni multiple di protezione o injection generano la
matrice completa delle combinazioni per ogni sorgente.

Se il nome base è `experiment` e la cartella contiene `alpha.c` e `beta.cpp`, i
test generati si chiamano `experiment-alpha` e `experiment-beta`.

Il batch è adatto a programmi autonomi composti da un solo file. Non è adatto a
un progetto nel quale `main.c` deve essere collegato con `helpers.c`: in quel
caso usare **Create configuration → Link files together** e selezionare entrambi i file.

### Edit configuration

Carica un `.cfg` esistente. Se contiene più test, chiede quale modificare. Dalla lista dei test è anche possibile eliminare permanentemente un test selezionando la voce apposita (Delete test) nel form di modifica. Il
test aggiornato viene nuovamente validato e serializzato insieme agli altri.
La cancellazione richiede conferma e non è consentita per l'ultimo test.
I percorsi assoluti rendono la posizione del file di configurazione indipendente
dalla posizione dei sorgenti. Se modifichi i parametri per molteplici file sorgente in un solo colpo (es. da batch), la TUI ti darà la possibilità di applicare una singola configurazione della funzione (es. `main`) a tutti i file contemporaneamente.

### Configure toolchain

Modifica e valida la toolchain descritta nella sezione precedente. Il salvataggio
è rifiutato se lo script ASPIS non esiste/non è eseguibile, mancano binari LLVM,
il compiler non è nel `PATH` o il debugger non è disponibile.

### View campaign results

Carica un `campaign_summary_*.json` già prodotto. Il riepilogo funziona senza
rieseguire la campagna. Il log testuale non è incorporato nel JSON e quindi il
tasto `g` è disponibile solo subito dopo un'esecuzione effettuata nella stessa
sessione TUI.

## Uso senza TUI

Non esiste ancora un comando CLI dedicato all'esecuzione headless, ma i moduli
possono essere usati direttamente da Python:

```python
from pathlib import Path

from src.campaign_runner import CampaignRun
from src.config_parser import parse_runner_config
from src.env import Environment

config = Path("samples/campaign.cfg").resolve()
campaign = parse_runner_config(config)
environment = Environment(
    aspis_path=str(Path("../ASPIS/aspis.sh").resolve()),
    llvm_path=str(Path("~/llvm-21.1.8/build/bin").expanduser()),
    vanilla_compiler="clang",
    gdb_executable="gdb",
)

runner = CampaignRun(campaign, environment, config.with_suffix(".log"))
runner.run_campaign()
print("\n".join(runner.log))
```

`Environment` non espande automaticamente `~`; la TUI lo fa durante la lettura
dei campi. Nel codice Python è quindi preferibile passare percorsi già espansi,
per esempio `str(Path("~/llvm-21.1.8/build/bin").expanduser())`. Se i compiler
flag contengono percorsi relativi, eseguire la campagna dalla directory del
`.cfg`, come fa la TUI.

## Output e interpretazione dei risultati

Eseguendo `campaign.cfg` vengono creati, nella stessa directory:

```text
campaign.log
campaign_summary_YYYYMMDD_HHMMSS.json
```

Il `.log` è pensato per la diagnosi umana. Il JSON conserva risultati strutturati
per analisi successive e per **View campaign results**.

Il JSON viene aggiornato dopo ogni test completato con successo e contiene i
risultati accumulati fino a quel momento. Il nome usa una risoluzione di un
secondo: test completati nello stesso secondo aggiornano lo stesso file; test
più lenti possono produrre più snapshot timestampati. Il file più recente è
quello da consultare per il riepilogo finale. I test falliti compaiono nel log,
ma non hanno un oggetto risultato nel JSON.

Un estratto del log ha questa forma:

```text
RUN NAME: protected_math
Sources: /path/to/exe1.c
Compiler flags: -O0 -lm
Execution repetitions: 100
Per-process timeout: 8000 ms
Args: 0 0
Comparison:
returncode: OK
stdout: OK
stderr: OK
measurements:
  execution_time_seconds: oracle=... s, aspis=... s, difference=... (...%)
  binary_size_bytes: oracle=... bytes, aspis=... bytes, difference=... (...%)
variables:
  OK result: oracle=42 aspis=42
```

`OK` indica uguaglianza fra Oracle e ASPIS; `DIFF` mostra entrambi i valori per
rendere visibile la divergenza. Una riga `ERROR:` indica che quel test non ha
raggiunto la produzione del risultato completo.

### Confronto Oracle/ASPIS

Per ogni variante vengono registrati:

- `returncode`: codice dell'ultima esecuzione temporizzata;
- `stdout` e `stderr`: output dell'ultima esecuzione, ripulito agli estremi;
- `execution_time_seconds`: media dei campioni wall-clock;
- `execution_time_samples_seconds`: tutti i campioni;
- `execution_time_stddev_seconds`: deviazione standard campionaria;
- `execution_time_standard_error_seconds`: errore standard della media;
- `measurement_repetitions`: numero di esecuzioni per la variante;
- `binary_size_bytes`: dimensione esatta dell'eseguibile su disco;
- `variables`, `registers` e `snapshots`: stato acquisito dal debugger.

`difference` contiene, per tempo e dimensione:

```text
absolute = ASPIS - Oracle
percent  = (ASPIS - Oracle) / Oracle × 100
```

Una percentuale positiva indica overhead ASPIS; una negativa indica un valore
ASPIS inferiore. Se il valore Oracle è zero, la percentuale è `null` nel JSON e
`n/a` nella TUI.

Con `measure_overhead: false` ciascuna variante viene eseguita una sola volta
per acquisire output e stato. Il confronto `difference` è vuoto e la TUI mostra
`n/a`; le injection mantengono il proprio numero di ripetizioni.

Il tempo misura soltanto l'esecuzione nativa del programma: non include
compilazione, startup del debugger, profiling o injection. Oracle e ASPIS sono
misurati in due blocchi sequenziali e non c'è una fase esplicita di warm-up;
per benchmark seri conviene aumentare le ripetizioni e considerare deviazione ed
errore standard, non soltanto la media.

La dimensione è quella del binario prodotto con `-g -O0`; include quindi simboli
e scelte della toolchain, oltre alla protezione ASPIS. È utile per confronti fra
configurazioni costruite nello stesso ambiente, non come misura universale del
solo codice macchina protetto.

### Classificazione delle injection

Prima viene determinato l'evento del processo:

| Evento | Significato | Categoria nel riepilogo |
|---|---|---|
| `RETURNED` | Il processo ha raggiunto il punto di snapshot. | Ulteriore confronto: Masked, Latent o SDC. |
| `TIMEOUT` | Il debugger/inferior ha superato `timeout_ms`. | Timeout. |
| `CRASHED` | GDB riporta un segnale, per esempio SIGSEGV o SIGILL. | Crash. |
| `ASPIS_DATA` | È comparsa la stringa dell'handler di corruzione dati. | ASPIS Data. |
| `ASPIS_CF` | È comparsa la stringa dell'handler di control-flow mismatch. | ASPIS CF. |
| `INTERNAL_ERROR` | Lo script debugger ha prodotto un errore fatale o non ha estratto lo stato. | Attualmente contato come Crash. |

Un risultato `RETURNED` viene poi classificato così, in questo ordine:

| Risultato | Condizione |
|---|---|
| `SDC` | `stdout` o `stderr` differiscono dall'Oracle. |
| `Latent` | L'output coincide, ma almeno una variabile configurata differisce. |
| `Masked` | Output e variabili configurate coincidono con l'Oracle. |

SDC significa *Silent Data Corruption*: il fault non è stato segnalato da ASPIS
e ha alterato l'output osservabile. `Latent` indica invece uno stato interno
diverso che non ha ancora modificato l'output. `Masked` significa che, rispetto
alle osservazioni configurate, il fault non ha prodotto effetti visibili.

I registri vengono salvati per diagnosi, ma non partecipano attualmente alla
decisione Masked/Latent/SDC. Una campagna senza variabili configurate può quindi
classificare come Masked un fault che altera soltanto uno stato interno non
osservato.

### Struttura essenziale del JSON

```json
[
  {
    "name": "protected_math",
    "timeout_ms": 8000,
    "oracle": {
      "returncode": 0,
      "stdout": "result: 42",
      "stderr": "",
      "execution_time_seconds": 0.0012,
      "execution_time_samples_seconds": [0.0011, 0.0013],
      "execution_time_stddev_seconds": 0.00014,
      "execution_time_standard_error_seconds": 0.0001,
      "measurement_repetitions": 2,
      "binary_size_bytes": 18432,
      "variables": {"result": "42"},
      "registers": {"rax": "0x2a"},
      "snapshots": {}
    },
    "aspis": {},
    "difference": {
      "execution_time_seconds": {"absolute": 0.0008, "percent": 66.67},
      "binary_size_bytes": {"absolute": 4096, "percent": 22.22}
    },
    "injections": {
      "total": 100,
      "masked": 40,
      "latent": 5,
      "sdc": 10,
      "timeout": 2,
      "crash": 8,
      "aspis_data": 25,
      "aspis_cf": 10,
      "sdc_states": [],
      "latent_states": []
    }
  }
]
```

Nell'output reale `oracle` e `aspis` contengono la stessa struttura completa;
qui `aspis` e `snapshots` sono abbreviati solo per leggibilità.

## Timeout

`timeout_ms` è un limite **per singolo processo**, non per l'intera campagna.
Viene applicato a:

- ogni esecuzione normale Oracle;
- ogni esecuzione normale ASPIS;
- ogni processo di snapshot GDB;
- ogni profiling dell'injector;
- ogni fault injection.

Non viene applicato alla compilazione Oracle o ASPIS. Con molte ripetizioni, la
durata massima teorica della campagna può quindi essere molto maggiore di
`timeout_ms`.

Un timeout durante un'esecuzione normale interrompe quel test e viene riportato
come `ERROR` nel log. Un timeout durante un'injection produce invece un
`TerminationEvent.TIMEOUT` e viene conteggiato separatamente dai crash.

Gli script legacy sotto `src/gdb_scripts/misc/` non leggono il `.cfg`; usano la
variabile d'ambiente `TIMEOUT_MS`, sempre con default `5000`:

```shell
TIMEOUT_MS=10000 src/gdb_scripts/misc/run_campaign.sh ./program reg
```

## Errori comuni

### `undefined reference to acos`, `sqrt` o `pow`

Manca la libreria matematica al link. Aggiungere:

```text
compiler_flags: [-lm]
```

Il flag viene passato sia alla compilazione Oracle sia ad ASPIS.

### `Environment.aspis_path must point to an ASPIS script`

La TUI non trova lo script esterno. Correggere **ASPIS script** in
**Configure toolchain**, verificando che il file esista e sia eseguibile.

### `Wrong API version on plugin ... Got version X, supported version is Y`

I plugin ASPIS e `opt` usano ABI LLVM diverse. Passare la directory corretta in
**LLVM bin directory** e ricompilare ASPIS con la stessa installazione LLVM.

### Il debugger non trova il ritorno della funzione

Controllare che:

- la funzione sia realmente chiamata con gli argomenti scelti;
- il nome nella configurazione coincida con il simbolo di debug;
- il binario contenga informazioni DWARF;
- esista una linea sorgente finale raggiungibile nella funzione;
- ottimizzazioni o trasformazioni ASPIS non abbiano eliminato il frame atteso.

Il runner usa già `-g -O0`, ma codice inlined, funzioni mai eseguite o line table
incomplete possono comunque impedire il breakpoint.

### GDB termina all'avvio con codice `127` o `ptrace` denied

È generalmente un problema dell'ambiente, non della grammatica. Verificare
permessi `ptrace`, policy del container/sandbox, loader dinamico e possibilità di
eseguire direttamente il binario temporaneo.

### Troppi timeout

Un timeout può indicare un loop infinito causato dal fault, ma anche un valore
troppo basso per il debugger. Aumentare `timeout_ms` e confrontare il tempo di
profiling/esecuzione senza injection prima di interpretarlo come effetto del
fault.

### `No global variables found to inject`

Specificare una globale esplicita aggiungendo `@variabile` alla dichiarazione della
funzione (es. `functions: [main(@counter)]`), oppure verificare che la globale
sia presente nelle informazioni di debug del primo sorgente elencato in
`programs`.

### Configurazione valida ma sorgente non trovato

Ricordare che i percorsi relativi sono risolti dalla directory del `.cfg`. Usare
virgolette doppie per percorsi con spazi.

## Test

Eseguire tutta la suite dalla root:

```shell
source .venv/bin/activate
python -m unittest discover -s test -p 'test_*.py' -v
```

Per i test principali, senza i test dinamici che avviano davvero GDB:

```shell
python -m unittest \
  test.test_campaign_runner \
  test.test_injector \
  test.test_config_parser \
  test.test_config_builder \
  test.test_front_end -v
```

I test in `test/test_dynamic_inference.py` compilano piccoli programmi C e
avviano GDB. Possono fallire in ambienti che vietano `ptrace` o l'esecuzione
dell'inferior, anche quando gli unit test sono verdi.

## Limiti attuali

- Il parser e la TUI accettano `.rs`, ma `ProgramRunner` compila soltanto `.c` e
  `.cpp`. Un test composto esclusivamente da Rust non è attualmente eseguibile;
  anche il batch va quindi usato solo con C/C++.
- La selezione batch non è ricorsiva e tratta ogni file come programma autonomo.
- La scelta del punto di injection è casuale e il seed non viene salvato: una
  singola injection non è riproducibile bit per bit.
- I tempi Oracle e ASPIS sono raccolti in blocchi sequenziali, senza warm-up o
  alternanza, quindi risentono del rumore di sistema.
- La classificazione Latent confronta solo le variabili indicate nella
  configurazione; i registri sono informativi.
- `INTERNAL_ERROR` è attualmente aggregato ai crash nel riepilogo.
- Un timeout di compilazione non è configurabile.
- L'inferenza memory/global dipende da DWARF, ABI e capacità del debugger; non
  tutte le architetture o forme di dato sono equivalenti.
- Il punto di ritorno è approssimato con la linea DWARF più alta della funzione:
  funzioni con ritorni anticipati o line table insolite possono non raggiungerlo.
- Poiché `aspis_handlers.c` viene collegato automaticamente, un programma che
  definisce gli stessi handler può produrre simboli duplicati al link.
- Gli script in `src/gdb_scripts/misc/` sono una pipeline legacy separata dalla
  TUI e dalla grammatica principale.

## Come estendere il progetto

### Aggiungere una modalità di injection

La modifica tipica attraversa questi livelli:

1. aggiungere la dataclass di configurazione in `config_parser.py`;
2. estendere `grammar.lark` e `RunnerConfigTransformer`;
3. aggiornare `config_builder.py` per il round-trip;
4. esporre la scelta nell'editor della TUI;
5. creare una sottoclasse di `BaseInjector` con `setup` e `gdb_script`;
6. aggiungere il caso alla factory `BaseInjector.create_for_target()`;
7. implementare lo script GDB corrispondente;
8. aggiungere test di parser, factory, comando debugger e classificazione.

`CampaignRun` non dovrebbe conoscere i dettagli concreti dell'injector: deve
continuare a chiedere alla factory gli oggetti necessari e invocare soltanto
l'interfaccia comune.

### Aggiungere una metrica

Una nuova metrica richiede normalmente:

1. raccolta in `ProgramRunner.execute()` o `compile()`;
2. confronto in `_measurement_differences()`;
3. output nel log;
4. rendering nel riepilogo TUI;
5. test sul valore assoluto, percentuale e caso Oracle uguale a zero.

### Modificare la grammatica

Aggiornare sempre insieme:

- `src/grammar.lark`;
- transformer e dataclass in `src/config_parser.py`;
- serializzatore in `src/config_builder.py`;
- form/validazione in `src/front_end.py`;
- test di parsing e round-trip.

Il principio da preservare è che una configurazione creata o modificata dalla
TUI debba essere accettata dallo stesso parser usato per eseguire le campagne.
