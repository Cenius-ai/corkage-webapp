# Installing and running Corkage

Corkage is one file of standard-library Python. There is no runtime dependency,
no database server, no service and no network access at any point.

## 1. Prerequisites (listed, never installed by these scripts)

| Need | Why | Check |
| --- | --- | --- |
| Python 3.9 or newer | `python3` runs `corkage.py` | `python3 -V` |
| A writable home directory | holds `~/.corkage/store.json` (or `$CORKAGE_HOME`) | `echo $HOME` |
| `pytest` (tests only) | runs the test suite | `python3 -m pytest --version` |

If `pytest` is missing, install it with your own Python tooling
(`python3 -m pip install -r requirements.txt`). Corkage itself needs nothing
installed - no system packages, no virtualenv, no sudo.

## 2. Install and seed (one command, then it exits)

```bash
bash install.sh
```

`install.sh` runs four non-interactive steps and **exits** - it never starts a
long-running process:

1. refreshes `pip`/`setuptools`/`wheel` (skipped with a note when offline),
2. installs the dependencies declared in `requirements.txt` (the test runner only),
3. installs the `corkage` console entry point (`pip install -e .`), and
4. seeds the demo cellar with `corkage demo` (idempotent).

If step 3 fails, the tool still works as `python3 corkage.py ...`.

Prefer no install at all? Skip this step; `python3 corkage.py` is the whole tool.
To seed the demo cellar by hand:

```bash
python3 corkage.py demo
```

## 3. Set your own cellar passphrase (a real cellar, not the demo)

```bash
CORKAGE_HOME="$HOME/.corkage-mine" python3 corkage.py init
```

`init` prompts twice, needs at least 4 characters, and stores only a 16-byte
random salt plus a PBKDF2-HMAC-SHA256 hash (120000 iterations). It refuses to
overwrite an existing cellar (`cellar already initialised at <path>`, exit 3).
Non-interactively, set `CORKAGE_PASSPHRASE` and `init` uses it without prompting.

## 4. Run

```bash
python3 corkage.py --help          # usage, examples and the exit-code contract
python3 corkage.py cork list       # or: corkage cork list, if step 3 succeeded
python3 corkage.py reading list
python3 corkage.py reminder list
```

Every command unlocks the cellar from `--passphrase`, then `$CORKAGE_PASSPHRASE`,
then a hidden prompt (one stdin line when there is no terminal).

## 5. Test

```bash
python3 -m pytest
python3 -m unittest discover -s tests
sh demo.sh                         # non-interactive end-to-end transcript
```

## Demo accounts

| Username | Password |
| --- | --- |
| cenius | cenius |

`corkage demo` writes the demo cellar with the passphrase `cenius` (the standard
preview credential). Demo data is for preview and evaluation; use your own
passphrase and your own `CORKAGE_HOME` for a real cellar.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `no cellar yet - run corkage init first` (exit 3) | no store at `$CORKAGE_HOME/store.json`; run `python3 corkage.py init`, or `python3 corkage.py demo` for the demo cellar |
| `could not unlock the cellar (wrong passphrase)` (exit 3) | wrong passphrase; it is never recoverable, so use a cellar whose passphrase you know |
| `cellar already initialised at ...` (exit 3) | `init` never overwrites; point `CORKAGE_HOME` at another directory |
| `cellar file at ... could not be read` (exit 3) | the JSON was hand-edited into invalid JSON; restore it from your backup |
| `no passphrase given - use --passphrase or set CORKAGE_PASSPHRASE` (exit 3) | run non-interactively without a passphrase; add `--passphrase` or the env var |
| Unwanted colour in a pipe | set `NO_COLOR=1` or `CORKAGE_COLOR=never` |
