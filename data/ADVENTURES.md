# Adventures

Full, multi-scene adventures meant to be *played* rather than to spotlight a
single feature (for the one-feature-each scenarios, see `DEMOS.md`). Each is a
self-contained session with its own party (levels 3–4), a branching or
trap-laden scene graph where every scene is reachable, and a boss in a terminal
scene. Tuned to be winnable but not gentle.

Drive one from the terminal:

```bash
python -m src.main data/adventures/emberdeep_mine.json
```

In any session, the CLI commands let you watch the engine work: `/help`,
`/state` (HP, slots, inventory, NPCs, combat order), `/hud`, `/recap`,
`/roll <notation>`, `/undo`, `/trace`, `/full_trace`, `/save [name]`, `/quit`.
The game autosaves to `saves/autosave.json` after every turn.

> **A note on dice.** Outcomes are real rolls, so runs vary — a persuasion can
> fail, a save can drop a PC, a boss can crit. The adventures are *tuned* to be
> beatable, not scripted; if a roll goes against you, keep playing or relaunch.
> The hard guarantees (slot economy, gates, turn order, redaction) hold
> regardless of dice.

## The adventures

| File | Theme | Engine features exercised |
|---|---|---|
| `emberdeep_mine.json` | Descend a dragon-held dwarven mine | Combat; **branching paths** (drain-the-sluice flag gate vs. a cave-in route); three **hazards** (DEX/CON saves, half-on-success); cold-themed **spells**; a young-red-dragon boss in a terminal scene |
| `the_velvet_mask.json` | A masquerade heist for a damning ledger | **Social** (`disposition_dc` to talk past Crispin / Bront / Vael); **stealth** (low-`alertness_dc` kitchen guards = ambush); an **answer gate** (passphrase "nightingale"); an optional-combat boss |
| `tomb_of_the_sunken_king.json` | A cursed trap-tomb crawl | Four **hazards** (including a repeating undertow); **reinforcements** gated on `crypt_disturbed`; a two-half **sigil flag gate** into the vault; undead plus a mummy-king boss |
| `stormhold_keep.json` | Storm a raider-held keep by two roads | **Branching geography** — a hard ramparts route and a quiet crypt route that **rejoin** at a shared finale; **social** (`disposition_dc` on the raiders); **stealth** (low-`alertness_dc` crypt skeletons = ambush); spells; a bandit-captain boss in a terminal scene |

## Launch / validate

**Emberdeep Mine**

```bash
python -m src.main data/adventures/emberdeep_mine.json
python -m src.validate data/adventures/emberdeep_mine.json
```

**The Velvet Mask**

```bash
python -m src.main data/adventures/the_velvet_mask.json
python -m src.validate data/adventures/the_velvet_mask.json
```

**Tomb of the Sunken King**

```bash
python -m src.main data/adventures/tomb_of_the_sunken_king.json
python -m src.validate data/adventures/tomb_of_the_sunken_king.json
```

**Stormhold Keep**

```bash
python -m src.main data/adventures/stormhold_keep.json
python -m src.validate data/adventures/stormhold_keep.json
```

Validate all of them at once:

```bash
python -m src.validate data/adventures/*.json
```
