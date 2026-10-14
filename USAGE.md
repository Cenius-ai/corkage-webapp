# Using Corkage

Everything below is copy-pasteable. `CORKAGE_PASSPHRASE` is used in the examples so
you can see the output; omit it to be prompted instead (hidden on a terminal).

```bash
export CORKAGE_HOME="$HOME/cellar-demo"
export CORKAGE_PASSPHRASE="cellar-pass"
python3 corkage.py init
```

## Log a cork and read the log

```bash
$ python3 corkage.py cork add --bottle "Barolo 2016" --condition soaked \
      --note "stained halfway up, slid out clean"
logged cork #1 for Barolo 2016 (soaked)
decant reminder #1 for Barolo 2016 due 2026-11-13 (auto: soaked cork)

$ python3 corkage.py cork add --bottle "Rioja Reserva 2012" --condition sound \
      --note "snug fit, firm and dry"
logged cork #2 for Rioja Reserva 2012 (sound)

$ python3 corkage.py cork list
ID  BOTTLE               CONDITION  EXTRACTION NOTE                     LOGGED (UTC)
-------------------------------------------------------------------------------
2   Rioja Reserva 2012   sound      snug fit, firm and dry              2026-10-14 19:31 UTC
1   Barolo 2016          soaked     stained halfway up, slid out clean  2026-10-14 19:30 UTC
```

The log is newest first. Conditions are `sound`, `soaked`, `crumbly` and `moldy`,
matched case-insensitively; anything else is refused with one line naming the
allowed values and exit code 2, and the log stays untouched.

With no entries yet:

```bash
$ python3 corkage.py cork list
No cork entries yet - add one with: corkage cork add --bottle NAME --condition sound
```

## Record the cellar climate

```bash
$ python3 corkage.py reading add --temp 13.5 --humidity 68 --note "after racking"
recorded reading #1: 13.5 C, 68% humidity

$ python3 corkage.py reading add --temp 16.8 --humidity 79 --note "heatwave, added a fan"
recorded reading #2: 16.8 C, 79% humidity

$ python3 corkage.py reading list
ID  TEMP    HUMIDITY  NOTE                   RECORDED (UTC)
-----------------------------------------------------------------
2   16.8 C  79%       heatwave, added a fan  2026-10-14 19:33 UTC
1   13.5 C  68%       after racking          2026-10-14 19:31 UTC
```

`--temp` accepts one decimal in Celsius between -5 and 45; `--humidity` is a whole
percent between 0 and 100. Out-of-range and non-numeric values are reported by flag
name, one line, exit code 2, with no traceback:

```bash
$ python3 corkage.py reading add --temp 90 --humidity 68
--temp must be between -5 and 45 C (got 90)

$ python3 corkage.py reading add --temp warm --humidity 68
--temp needs a number like 13.5 (got 'warm')
```

## Soak and decant reminders

```bash
$ python3 corkage.py reminder add --kind soak --due 2026-11-01 --bottle "Zinfandel 2014" \
      --note "dry cork from a cold cellar"
added soak reminder #2 for Zinfandel 2014 due 2026-11-01

$ python3 corkage.py reminder list
ID  KIND    BOTTLE           DUE         STATUS    NOTE
------------------------------------------------------------------
2   soak    Zinfandel 2014   2026-11-01  in 18d    dry cork from a cold cellar
1   decant  Barolo 2016      2026-11-13  in 30d    auto: soaked cork logged 2026-10-14

$ python3 corkage.py reminder done --id 1
reminder #1 marked done (decant Barolo 2016, was due 2026-11-13)
```

Past due dates are marked `OVERDUE` (the word is always printed, colour or not):

```bash
$ python3 corkage.py reminder add --kind decant --due 2020-01-01 --bottle "Rioja Reserva 2012"
$ python3 corkage.py reminder list
ID  KIND    BOTTLE               DUE         STATUS   NOTE
--------------------------------------------------------------
2   decant  Rioja Reserva 2012   2020-01-01  OVERDUE  -
1   soak    Zinfandel 2014       2026-11-01  in 18d   dry cork from a cold cellar
```

An unknown id is one line with exit code 2:

```bash
$ python3 corkage.py reminder done --id 999
no pending reminder with id 999
```

Adding a **soaked** or **crumbly** cork queues one pending decant reminder for that
bottle, due 30 days after the save date, so the nudge arrives without extra typing.
A second soaked cork for the same bottle reports the nudge already pending instead
of duplicating it.

## Types of failure, and the exit codes

| Command | stderr (one line) | Exit |
| --- | --- | --- |
| `cork add --condition mushy` | `unknown --condition 'mushy' - choose one of: sound, soaked, crumbly, moldy` | 2 |
| `cork add` (no `--bottle`) | `missing required --bottle (bottle name)` + the command usage | 2 |
| `reading add --temp 90` | `--temp must be between -5 and 45 C (got 90)` | 2 |
| `reminder add --due 01/06/2024` | `--due needs a date in YYYY-MM-DD format (got '01/06/2024')` | 2 |
| `reminder add` (no `--due`) | `missing required --due (YYYY-MM-DD)` + the command usage | 2 |
| `reading add --bogus` | `unrecognized arguments: --bogus` + the command usage | 2 |
| any command with the wrong passphrase | `could not unlock the cellar (wrong passphrase)` | 3 |
| any command before `init` | `no cellar yet - run corkage init first` | 3 |
| `init` on an existing cellar | `cellar already initialised at <path>` | 3 |

`CORKAGE_DEBUG=1` adds the traceback behind an unexpected (exit 1) failure; the
normal output never shows one.

## The demo cellar

```bash
python3 corkage.py demo            # create/refresh, passphrase: cenius
python3 corkage.py demo --reset    # rebuild from scratch
python3 corkage.py cork list --passphrase cenius
```

`demo` never overwrites a cellar that uses a different passphrase: it prints one
line and exits 0, leaving your real cellar alone. Point `CORKAGE_HOME` at an empty
directory to seed a demo cellar somewhere else.

## Reading the store yourself

`$CORKAGE_HOME/store.json` is plain JSON, so `jq` and `python3 -m json.tool` work on
it. Back it up by copying that one file; the passphrase is not recoverable, so keep
a copy of the passphrase too.
