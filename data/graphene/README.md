# Graphene site source data

These files are the asset data for revision 1 of the World Model. They are copied unchanged
from `sheon-sek/graphene-demo-twin-2` at commit `0b43080`, which is the only thing this
repository takes from that project (see AGENTS.md).

| File | Origin | What it is |
| --- | --- | --- |
| `ignition/real-graphene-demo-udt-definitions.json` | `reference/graphene/` | Ignition UDT type definitions exported from the live gateway |
| `ignition/real-graphene-demo-tag-instances.json` | `reference/graphene/` | Ignition tag instances exported from the live gateway |
| `ignition/demo-twin-supplement-tag-instances.json` | `reference/graphene/` | Tags the live gateway has but the export predates (RCMS425, per-hall PUE) |
| `plant-design.json` | `plant-design/` | Rooms, shafts, asset placement and typed connections (Plant Design rev 0.5) |

Do not edit these files. `gws_world_model.importers.graphene` reads them to build revision 1,
and point export paths must stay byte-identical to the Ignition export.
