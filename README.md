# BG3SE — a Baldur's Gate 3 savegame library and editor

> ## ⚠️ Status: **read-only in practice**
>
> This project can **parse Baldur's Gate 3 savegames completely** — containers,
> resource trees, and the entity-component data that holds character builds. It
> decodes ability scores, classes, subclasses, feats, experience, races and
> items, and cross-checks them against the game's own data files.
>
> **It cannot yet write a savegame that Baldur's Gate 3 will load with your
> character changes applied.** Editing entity-component data produces a file
> the game refuses to load. This was tested exhaustively against a real
> installation — see [The write problem](#the-write-problem).
>
> Use it to **read, inspect and understand** saves. Do not rely on it to edit a
> playthrough you care about.

Everything here — the LSPK package format, the LSF resource format, and the
undocumented LSMF entity-component container — is implemented from scratch in
Python. There is no dependency on LSLib, Divine.exe, or any other Larian tool.

---

## Contents

- [What works](#what-works)
- [The write problem](#the-write-problem) — what was tried, what failed, and why
- [Install and run](#install-and-run)
- [Format documentation](#format-documentation)
- [Implications of editing anything](#implications-of-editing-anything)
- [How to build on this](#how-to-build-on-this)
- [Testing](#testing)

---

## What works

All of the following is validated against real savegames and the installed game
data, not inferred.

| Capability | Status |
|---|---|
| Parse `.lsv` savegames (LSPK v18 containers) | ✅ |
| Parse `.lsf` resource files (v6 and v7) | ✅ |
| Parse the `NewAge` LSMF entity-component blob | ✅ |
| Read ability scores, class, subclass, per-class level | ✅ |
| Read feats per level-up, experience, race | ✅ |
| Name items from the game's root templates (~25,500 entries) | ✅ |
| Browse/search every node and attribute in any `.lsf` | ✅ |
| Browse ~350 ECS component types and their raw elements | ✅ |
| Cross-platform save discovery (Windows / macOS / Linux+Proton) | ✅ |
| Desktop app (native webview) and browser mode | ✅ |
| Write a savegame the game loads **with character edits** | ❌ |

### Validation

The format layer was checked against **111 savegames spanning three engine
versions**, with no version-specific code:

| Source | Saves | Game version | Engine |
|---|---|---|---|
| Local playthrough | 32 | `4.1.1.4854838` | `4.6.300` (Patch 6) |
| Third-party (Nexus) | 1 | `4.1.1.5009956` | `4.6.300` |
| Third-party (Nexus) | 78 | `4.1.1.6995620` | `4.8.0.500` (Patch 8) |
| Locally created | 1 | `4.1.1.7209685` | `4.8.0.700` (Patch 8) |

For every one: all LSF codecs re-encode to exactly their declared byte lengths,
every resource tree survives parse → write → parse, and every ECS blob
re-serialises **byte-identically**. Component counts vary from save to save
(129–362) and are read from the header rather than assumed.

Decoding is cross-validated rather than asserted:

- Class levels match the save summary, including multiclass splits the summary
  only reports as a total — `Wizard (NecromancySchool) 7 / Rogue 2` for a
  character listed simply as "level 9".
- Races come out lore-correct: Karlach `Tiefling_Zariel`, Shadowheart
  `HalfElf_High`, Gale `Human`.
- Feats land on levels 4/8/12 exactly as 5e requires.
- Ability scores: 11 of 12 characters lead in their class's primary ability.
  The exception is a Monk with STR 18 / DEX 16 — correct, because that same
  character's level-4 feat decodes independently as `TavernBrawler`, the build
  that makes Strength the right stat. Two separate decoders agreeing is
  stronger evidence than either alone.

---

## The write problem

This is the honest core of the project. **Everything below was measured against
a real Baldur's Gate 3 installation**, not reasoned about.

### The short version

Two independent mechanisms block editing:

1. **Any repack raises a "tampering or corruption" warning** — even one that
   changes nothing but a few bytes of JSON.
2. **Edits to the entity-component arena additionally prevent the save from
   loading at all** — the game throws an error and returns to the main menu.

Neither checksum has been reproduced, despite roughly 200 algorithm/input
combinations across the two candidate fields.

### The test sequence that produced clean data

Early testing was badly confounded, and it is worth documenting why so nobody
repeats it. The local saves were Patch 6 being loaded by a Patch 8 game, and
**an untouched Patch 6 save warns on its own**. Every early result was measuring
that version gap rather than the editor. Roughly a dozen game loads produced
uninterpretable data before a baseline was established.

The clean sequence uses a savegame **created natively by the current game
version**, edited **in place** (no rename, no relocation — both change the
outcome independently), with **one variable per test**:

| Test | Warning | Loads |
|---|---|---|
| Untouched native Patch 8 save | **no** | yes ← the baseline |
| Save name only (`SaveInfo.json`; `Globals.lsf` byte-identical) | yes | **yes** |
| One ability score changed (ECS arena) | yes | **no** — error, main menu |

### What each result rules in or out

**The container and LSF writers are correct.** A no-op repack — this code
parsing and rewriting a save without changing a single value — loads and plays.
The written file is byte-different from the original (different string-table
references, different attribute ordering, different padding), and the game
accepts it. So rewriting the format is not the problem.

**Non-ECS content edits are accepted, with a warning.** Changing only
`SaveInfo.json` while re-emitting `Globals.lsf` verbatim produces a save that
loads and plays.

**ECS arena edits are rejected outright.** A single integer changed inside the
entity-component data — one ability score — makes the save unloadable. This was
confirmed on a native Patch 8 save with no version-gap noise, edited in place,
with nothing else changed.

### Other things established along the way

| Behaviour | Detail |
|---|---|
| Save folder naming | The load menu displays the **folder** name (the part after `__`), not `SaveInfo.json`'s `Save Name`. |
| Filename must match | If the `.lsv` filename does not match the folder's `__` suffix, the save **hangs at 0%** on load. |
| Relocating a save | A byte-identical copy in a differently-named folder loads, but warns. |
| Migrated saves warn | A Patch 6 save loaded in Patch 8 warns even untouched — this is not corruption. |

### The two checksums

**Candidate 1 — the LSPK header MD5** (offset `0x16`, 16 bytes). Stale after any
repack, which fits it being what raises the warning. Not reproduced. Tried: MD5,
BLAKE2b/2s-128, SHA-1/SHA-256 truncations, xxHash128, MurmurHash3-128 — over the
whole file, the file with the field zeroed, the header alone, the payload region,
the file list block, the decompressed entry table, payloads concatenated in both
file-list order and on-disk order, per-file digests concatenated and re-hashed.
**~120 combinations, no match.**

**Candidate 2 — the LSMF header `uint64`** (offset `0x08`). Sits over the
component arena and is preserved verbatim when patching, which fits it being the
ECS check. Zeroing it changes the failure mode from "won't load" to a hard
**error 223**, so it is read and validated rather than ignored. Tried: xxHash64,
XXH3-64, MD5/SHA-1/BLAKE2b truncations, CRC32 pairs, MurmurHash3 — over the
arena, section B, the body, the whole blob, and the blob with the field zeroed.
**~80 combinations, no match.**

Zeroing the LSPK MD5 does not suppress the warning; zeroing the LSMF hash makes
things worse. Both fields are checked, and neither is satisfied by an obvious
hash of an obvious byte range.

---

## Install and run

Requires Python 3.10+.

```bash
pip install -e .
```

```bash
bgse gui          # native desktop window
bgse web          # same UI in a browser, on 127.0.0.1
bgse list         # find savegames
bgse info <save>  # party summary
bgse build <save> # decoded build: abilities, classes, feats
bgse items <save> # every item, named
bgse verify <save># check every container round-trips
```

On Linux the GTK/WebKit bindings are also needed:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

### Prebuilt Windows binary

A packaged Windows build is attached to the
[Releases](../../releases) page — no Python needed. Extract and run
`BG3SaveEditor.exe`.

To build it yourself:

```bash
pyinstaller packaging/bgse.spec --noconfirm
```

Verified on Windows: `dist/BG3SaveEditor/` at 28.4 MB (a 5.9 MB exe plus its
runtime), launches with the UI bundled. The macOS and Linux targets in
`.github/workflows/build.yml` are written but have not been run.

### Where savegames live

Detected automatically; `bgse where` prints exactly what was searched.

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Larian Studios\Baldur's Gate 3\PlayerProfiles\<profile>\Savegames\` |
| macOS | `~/Documents/Larian Studios/Baldur's Gate 3/PlayerProfiles/<profile>/Savegames/` |
| Linux (Proton) | `<library>/steamapps/compatdata/1086940/pfx/drive_c/users/steamuser/AppData/Local/Larian Studios/...` |

Steam libraries are resolved from `libraryfolders.vdf`, so second-drive installs
are found.

---

## Format documentation

Derived by reading real savegames and validating every assumption against the
data. Useful independently of the editor.

### `.lsv` — LSPK package, version 18

40-byte header, then payloads, then an LZ4-block-compressed file list of
272-byte entries. Payloads are **zstd** in savegames.

```
0x00 char[4] 'LSPK'        0x14 uint8  flags
0x04 uint32  version (18)  0x15 uint8  priority
0x08 uint64  fileListOffset 0x16 byte[16] md5   <- unidentified, see above
0x10 uint32  fileListSize   0x26 uint16 numParts
0x28         payloads begin, 8-byte aligned
```

A savegame contains `meta.lsf`, `Globals.lsf`, `StorySave.bin`,
`SaveInfo.json`, a `.webp` screenshot, and one `.lsf` per cached level.

### `.lsf` — resource file, versions 6 and 7

64-byte header, then five sections. Three quirks matter:

1. **The `keys` section is declared second in the header but written last on
   disk.** Only observable in files with a non-empty keys section (v6 level
   caches); getting it wrong makes every later section fail to decompress.
2. **Framing is mixed.** The string table is a raw LZ4 *block*; `nodes`,
   `attributes` and `values` are LZ4 *frames* (magic `04 22 4d 18`). Detect per
   section rather than assuming.
3. **The `extended` field selects the long node layout only when it equals 1.**
   Savegames use `0`; the game's root templates use `2` with 12-byte nodes.
   Testing "non-zero" silently misreads them.

With the compact layout, nodes and attributes are 12 bytes each, attributes are
tagged with their owning node, and each value directly follows the previous one.

**GUIDs are stored as little-endian 16-bit groups**, not .NET `Guid` byte order.
`3ed74f06-3c60-42dc-83f6-f034cb47c679` is stored as
`06 4f d7 3e | 60 3c | dc 42 | f6 83 | 34 f0 47 cb 79 c6`. Decoding it the .NET
way scrambles the last two groups — confirmed by matching decoded values against
the game's own spelling of companion GUIDs in the Osiris data.

### `NewAge` — the LSMF entity-component container

Character data is **not** in the LSF trees. It lives in a `SCRATCHBUFFER`
attribute of the `NewAge` region:

```
0x00 char[4] 'LSMF'        0x20 uint32 name-blob length
0x04 uint8   major, minor  0x24 uint16 component type count
0x06 uint16  flags         0x26 uint16 unknown (always 32)
0x08 uint64  hash   <- unidentified, gates ECS edits
0x10 uint64  section A size  -> component arena
0x18 uint64  section B size  -> [name blob][type records]
```

Each 48-byte type record:

```
0x00 uint64 name offset    0x18 uint32 element size
0x08 uint32 name length    0x1C uint32 component version
0x0C uint32 entity count   0x20 uint64 element count
0x10 uint64 type hash      0x28 uint64 data offset into section A
```

Two independent checks confirm the layout: the version field matches the `.vN.`
in every one of the ~350 type names, and each array's end lands on the next
array's start. Nothing is compressed — the arena looks high-entropy because it
is dense binary data.

#### The heap and entity references

The fixed arrays do not fill section A. What follows is a **heap** for
variable-length data. An element references it as a pair of absolute
`(begin, end)` arena offsets — a serialised vector — and consecutive elements
chain, so `element[i].end == element[i+1].begin`.

Heap payloads may contain **entity references**, stored as byte offsets into the
`core.v0.EntityId` array. `core.v0.EntityId` is a **reference array, not a
registry**: the same entity appears in many slots (one save has 9,374 slots
holding 1,957 distinct GUIDs, one repeated 99 times). Slot → GUID is exact;
GUID → slot is one-to-many.

#### Confirmed component layouts

```
game.stats.v3.StatsComponent          36-byte elements
  0x08  int32[6]  ability scores, in order STR DEX CON INT WIS CHA

game.stats.v0.ClassesComponent        16-byte elements, 40-byte heap payload
  0x00  guid    class
  0x10  guid    subclass
  0x20  uint32  level in that class
  0x24  uint32  unknown (0x299 throughout)

game.character_creation.v3.LevelUpComponentData   96-byte elements, one per level
  0x00  guid    class chosen at this level
  0x10  guid    subclass  (only on the level it is picked)
  0x20  guid    feat      (only on levels that grant one)

game.experience.v0.ExperienceComponent   12-byte elements
  0x00  int32   current-level XP
  0x04  int32   total XP
```

`game.character_creation.v3.LevelUpComponent` holds, per character, a heap list
of pointers into `LevelUpComponentData` — that is what ties a run of level-ups
to one character.

**`StatsComponent`, `ClassesComponent` and `RaceComponent` are parallel arrays**
— one element per character in the same order — so a row matched through any one
of them reads across all three. This is what allows a character matched by class
to also yield abilities and race without a general entity→name mapping.

#### How these layouts were found

Most were found by **scanning every component's bytes for GUIDs already known
from the game data**, which identifies the component and the field offset
simultaneously — far faster than guessing at structs. Where the data is numeric
rather than GUIDs (ability scores), the same idea works on value *shape*: six
consecutive plausible numbers, cross-checked against an external expectation.

### Game data

`.pak` archives open lazily — header and file list only — so indexing the 13 GB
`Gustav.pak` takes about 0.25 s. Class, race, feat, background, god and origin
definitions are parsed from `.lsx` files (~320 entries), and item root templates
from merged `.lsf` files under `Public/*/RootTemplates/` (~25,500 entries in
about 1.5 s). Both are cached to disk. Without a game install the editor still
works; UUIDs just stay raw.

### `StorySave.bin`

The Osiris story database — quest flags, dialog state, ~7,700 databases. Its
strings are obfuscated with **XOR 0xAD**. It holds narrative state, not
character build data. Currently read-only and unused by the editor.

---

## Implications of editing anything

Read this before using the write path for anything you care about.

- **Editing entity-component data produces a savegame Baldur's Gate 3 will not
  load.** Abilities, classes, subclasses, class levels, feats and experience all
  live here. There is no known workaround.
- **Any write raises the tampering warning**, including writes that change
  nothing meaningful. The warning is not cosmetic — it is the game telling you
  it does not trust the file.
- **A warning does not always mean the file is broken**, and a missing warning
  does not mean it is fine. A migrated Patch 6 save warns while being perfectly
  healthy; an ECS-edited save warns *and* is unloadable. The two are unrelated.
- **Non-ECS edits load, but are unproven at depth.** Changing `SaveInfo.json` or
  an LSF tree attribute produces a save that loads and plays. Nobody has played
  one for any length of time. It may still be subtly wrong.
- **Writes are in-place and byte-surgical where possible.** The ECS arena is
  patched in place, so no offset moves. Growing anything — spawning an item,
  adding a class — would require rebuilding the arena and is not implemented.
- **Backups are automatic.** Every write copies the original to a timestamped
  file under the app data directory first, and writes go via a temp file and an
  atomic replace. This has been exercised repeatedly; restores were verified
  byte-identical every time.

---

## How to build on this

The library is usable as-is for anything read-only. The specific open problems,
in the order they would unlock the most:

### 1. The LSMF header hash (offset `0x08`)

Cracking this is the whole ballgame — it would make character editing work.
Black-box guessing from save files has failed (~80 combinations). The realistic
route is finding the hash function in the game binary. Useful facts: zeroing the
field yields error 223 rather than a silent pass, so it is read; the field is
8 bytes; it sits immediately after a 4-byte magic, two version bytes and a
`uint16`, which is where a struct-writing routine would naturally place a
content digest.

### 2. The LSPK header MD5 (offset `0x16`)

Cracking this would remove the tampering warning from all writes, including the
non-ECS ones that already work. ~120 combinations failed. Same suggestion: find
the writer in the binary rather than guessing.

### 3. Gold

The gold *entities* are identified — `LOOT_Gold_A`
(`1c3c9c74-34a1-4685-989e-410dc080be6f`, stats `OBJ_GoldPile`) — and their GUIDs
resolve into the entity array. The stack size is not found. The best lead is
`game.inventory.v0.ContainerSlotData` (16-byte elements), where an entity
reference sits at offset 0 and **exactly 30 rows reference a save's 30 gold
entities**. Offset 8 looked like a quantity but is not: it reads 3,170 for an
alchemy pouch and 3,179 for a scroll stack, and it is not a monotonic counter
either. Already ruled out: no component holds a currency-range integer stable
across two saves of one playthrough, and neither `StackMemberComponent` nor the
owner components carry an adjacent quantity.

### 4. Feats, appearance, inventory naming

Feats are decoded and editable in the file. Appearance
(`game.character_creation.v3.AppearanceComponent`, 112-byte elements) is a
handful of GUID references to visual and material resources — decoded, but
nothing in it maps to the character creator's choices without a catalogue of
visual resources from the paks. Item root templates resolve to names; quantities
do not.

### Where to start reading

```
src/bgse/formats/lspk.py    LSPK containers (.lsv and .pak), lazy pak reading
src/bgse/formats/lsf.py     LSF resource files, all ~34 attribute types
src/bgse/formats/lsmf.py    the ECS container, heap, entity references
src/bgse/formats/verify.py  self-checks: codec round-trips, tree comparison
src/bgse/gamedata.py        .lsx definitions and root templates from the paks
src/bgse/model.py           the domain layer: party, abilities, classes, feats
src/bgse/api.py             the UI bridge
src/bgse/ui/                the interface (plain HTML/CSS/JS)
```

---

## Testing

```bash
pytest -q
```

21 tests. Container and format tests use synthetic data and run anywhere; tests
that need real savegames skip themselves when none are installed. Several are
cross-validation rather than unit tests — decoded classes must match the save
summary, decoded feats must resolve to real definitions, ability scores must be
in range and not a constant block.

`bgse verify <save>` checks that every container in a save round-trips, and is
the first thing to run if a future patch changes the format.

---

## Licence

MIT.

## Acknowledgements

Format knowledge here was derived independently from savegame data. Norbyte's
LSLib is the established reference toolkit for Larian formats and is worth
consulting for anything this project does not cover.
