"""Write a cleaned DXF by re-reading the original and excluding deleted IDs.

We never mutate the source; we always re-parse from disk so a fresh export
always reflects the original geometry minus the current delete reservation.
This makes undo trivial (just clear the delete list).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ezdxf import recover

log = logging.getLogger(__name__)


def export_clean_dxf(source: Path | str, deleted_ids: set[str], dest: Path | str) -> int:
    """Copy ``source`` to ``dest`` minus the modelspace entities with the
    given deterministic IDs (``e00001`` etc., assigned at parse time).

    Returns the number of entities actually removed.
    """

    doc, _auditor = recover.readfile(str(source))
    msp = doc.modelspace()

    to_remove = []
    for idx, e in enumerate(msp):
        eid = f"e{idx:05d}"
        if eid in deleted_ids:
            to_remove.append(e)

    for e in to_remove:
        msp.delete_entity(e)

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dest))
    log.info("exported %s (removed %d / %d entities)", dest, len(to_remove), sum(1 for _ in msp) + len(to_remove))
    return len(to_remove)
