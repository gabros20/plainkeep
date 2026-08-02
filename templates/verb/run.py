#!/usr/bin/env python3
"""
plainkeep {{name}} — {{summary}}

SCAFFOLDED by `plainkeep new verb`. This is a stub — implement main(). Keep the contract:
  • Iron Law (§4): the model decides WHAT; this verb guarantees WHERE/HOW. The verb owns placement —
    never take a path from the caller and write it blindly.
  • Any path you write MUST be classifiable by the guardrail path-wall — inside ~/plainkeep, ~/files, or the
    current task's ONE ~/work repo. Nothing external is transmitted; verbs that could must draft only.
  • Declared risk is `{{risk}}` in cmd.json. New verbs default to `confirm`, so the guardrail makes a
    human re-run with --yes until you deliberately lower it. Read skills/operate-plainkeep/SKILL.md and an
    existing bin/<verb>/run.py before extending. Regenerate the surface with `plainkeep index --manifest`.

This is a PLUGIN verb (plugins/local/{{name}}/) — user-owned, survives `script/update`. It reaches
the engine's shared lib via PLAINKEEP_ENGINE (the dispatcher always exports it); it re-enters through
`plainkeep {{name}}`, so the guardrail + logs still gate it — never import lib to skip the dispatcher.
"""
import os
import sys
from pathlib import Path

# WHERE `lib` IS. A plugin verb lives in the VAULT (plugins/<pack>/<verb>/) and the engine does not,
# so this is the one thing a plugin genuinely cannot work out for itself: its own `__file__` is under
# the data root, and the engine could be any installed version. PLAINKEEP_ENGINE is what the
# dispatcher exports for exactly this, and the dispatcher REPLACES any value the caller had — so a
# plugin loading through it loads the engine that gated it, never one a caller substituted.
#
# It read PLAINKEEP_HOME through Phase 1, which was the "engine lives in the vault" assumption
# (ADR-014): after the engine moved, `$PLAINKEEP_HOME/bin` is a directory a vault does not have, and
# every scaffolded plugin would have died on the import. There is deliberately NO fallback — a plugin
# reached outside a dispatch has not been gated either, and guessing a path is how a verb ends up
# importing a `lib` nobody validated.
_ENGINE = os.environ.get("PLAINKEEP_ENGINE")
if not _ENGINE:
    sys.stderr.write("plainkeep {{name}}: PLAINKEEP_ENGINE is unset — run this through "
                     "`plainkeep {{name}}`, which selects the engine and the vault and gates the "
                     "verb\n")
    raise SystemExit(2)
sys.path.insert(0, str(Path(_ENGINE) / "bin"))
from lib import paths  # noqa: E402,F401  (most verbs need paths — keep or drop)


def main(argv):
    print("plainkeep {{name}}: not implemented yet — edit plugins/local/{{name}}/run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
