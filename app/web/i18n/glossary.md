# Game words: never translated

The app's text is translated (one table per language in this folder), but the words of the game stay exactly as
the game writes them in English, in every language. The team plays with English, French and Korean game clients and
talks about the game with these words. Write the sentence around them in your language and leave them as they are:

> French: "Combat commencé avec peu de munitions", but "Tu t'es fait knock", "third party à 12:40", "Ring 2 se ferme".

| Kind | Keep as is |
| --- | --- |
| Legends | Bangalore, Wraith, Mad Maggie, Seer, Sparrow, ... (every legend name) |
| Weapons | R-301, Wingman, Peacekeeper, ... (every weapon name) |
| Maps and places | World's Edge, Storm Point, E-District, Olympus, Broken Moon, Kings Canyon, and every POI name |
| Ring | ring, Ring 1, Ring 2, ... (also "inside / outside ring 2": only "inside / outside" is translated) |
| Fights | knock, knocked, kill, third party, squad, squad wiped, headshot |
| Map items | care package, harvester, stronghold, EVO |
| Respawn | banner, beacon, respawn, dropship |
| Modes | Ranked, Pubs, Wildcard, Scrims, Scrim |
| Ranked | RP, tier names (Rookie, Bronze, Silver, Gold, Platinum, Diamond, Master, Predator) |
| Items and abilities | Shield cell, Shield battery, Syringe, Med kit, Phoenix kit, heal(s), Ultimate, Tactical |
| HUD words | squads left, kill feed, legend select, and any other word the game shows on screen |
| Team words | Coach, IGL, Fragger, Anchor, Support, Sub, Loot Book, EEC, POV |

Also never translated: what the scanner read (scan data), file names, player names, and anything sent to the team
hub. Only the words the player reads in the app change.

## Adding a language

1. Copy `fr.json` to `<code>.json` (two letters, e.g. `ko.json`) and set `"_language"` to the language's own name.
2. Translate the values; keep every `{name}` as it is (it is replaced by a number, a name or a time).
3. Keys you leave out show in English. `python tools/i18n_keys.py` lists what is missing.

The language then shows in Settings → Language and in the first-run setup by itself.
