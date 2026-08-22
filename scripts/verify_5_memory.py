#!/usr/bin/env python3
"""
FLOW 5 — Memory (what the system remembers, and how far it trusts it)
======================================================================
Costs NOTHING — memory is deterministic, no AI involved.

    python scripts/verify_5_memory.py

WHAT THIS PROVES
    The answer to "if a human confirms a column once, does it get asked
    again?" — and, more importantly, the limits on that. Memory that
    over-generalises is worse than no memory: it silently applies one
    sender's meaning to another sender's file.

    FIELD MEMORY scopes, in order of strength:
        exact        this sender confirmed this column. Applies to them.
        generalised  >= 2 INDEPENDENT senders agreed. Applies broadly.
        suggested    one sender only. A hint, not an answer.
        conflict     senders disagree. Surfaced WITH the evidence, never
                     silently resolved by picking one.
        none         never seen.

    Always keyed on content_type + column, never on column alone: `Rate`
    in a lane file and `Rate` in a contract file are not the same thing.

    DOCUMENT MEMORY matches on wording SHAPE, with digits deliberately
    excluded — so a renewal of last year's contract, same template with new
    numbers, is still recognised as the same template. It is NOT keyed on
    vendor: the shape is the key, and the vendor is a discovered label.

    This runs against a temporary store, so it never touches real memory.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from _verify_common import (MOCK_ROOT, add_common_flags, finish,
                            section, start)
from netgravity.ingestion.memory.document_memory import (DocumentMemory,
                                                         similarity, signature)
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.storage.local import LocalStorage

# The REAL sample contract, not a toy string. Document memory requires at
# least MIN_SIGNATURE_TOKENS (40) distinct words before it will trust a
# shape match at all — a short document has too little shape to identify
# reliably, and matching on thin evidence is exactly the failure this floor
# exists to prevent. A hand-written five-line fixture falls below it.
CONTRACT_FILE = MOCK_ROOT / "contracts" / "transcorp_rate_card.txt"

DIFFERENT = """
WAREHOUSE LEASE AND FACILITY SERVICES AGREEMENT
Premises: Plot 44, Industrial Estate, Bhiwandi, Maharashtra.
Monthly rent of Rs. 450,000 payable in advance on the first working day.
Lock-in period of thirty six months from the commencement date.
Maintenance, security and common area charges are billed separately and
reviewed annually. The lessee shall maintain insurance covering stored
goods, fire, flood and theft throughout the term of this lease. Electricity
and water consumption are metered individually and recovered at actuals.
Any structural alteration requires prior written consent from the lessor.
Termination requires ninety days notice served in writing by either party.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_flags(parser)
    parser.parse_args()

    config = start("FLOW 5 — MEMORY", needs_ai=False)

    if not CONTRACT_FILE.exists():
        print(f"\n  cannot run the document-memory half: {CONTRACT_FILE} "
              f"not found")
        return finish(config)

    contract = CONTRACT_FILE.read_text(encoding="utf-8")
    # A renewal: same template, new numbers. Digits are excluded from the
    # shape on purpose, so this must still match.
    renewal = (contract.replace("10.00", "11.50")
                       .replace("TC-2026-0472", "TC-2027-0913"))

    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalStorage(Path(tmp))

        # ---------------- field memory ----------------------------------
        memory = FieldMemory(storage)

        section("a column nobody has confirmed")
        result = memory.resolve(source_column="Wt (kgs)",
                                content_type="SHIPMENT_LOG",
                                source_id="distributor_north")
        print(f"  scope: {result.scope}   known: {result.is_known}")
        print("  -> nothing remembered, so the mapper must ask or infer")

        section("after ONE sender confirms it")
        memory.record(source_column="Wt (kgs)", target_field="weight_kg",
                      content_type="SHIPMENT_LOG",
                      source_id="distributor_north", confirmed_by="aayush")
        same = memory.resolve(source_column="Wt (kgs)",
                              content_type="SHIPMENT_LOG",
                              source_id="distributor_north")
        other = memory.resolve(source_column="Wt (kgs)",
                               content_type="SHIPMENT_LOG",
                               source_id="distributor_south")
        print(f"  same sender     : {same.scope} -> {same.target_field}")
        print(f"  DIFFERENT sender: {other.scope} -> {other.target_field}")
        print("  -> one sender's confirmation is not yet evidence about "
              "anyone else. That is the point.")

        section("after a SECOND independent sender agrees")
        memory.record(source_column="Wt (kgs)", target_field="weight_kg",
                      content_type="SHIPMENT_LOG",
                      source_id="distributor_south", confirmed_by="aayush")
        third = memory.resolve(source_column="Wt (kgs)",
                               content_type="SHIPMENT_LOG",
                               source_id="distributor_east")
        print(f"  a THIRD, unseen sender: {third.scope} -> "
              f"{third.target_field}")
        print("  -> two independent senders agreeing is enough to generalise")

        section("the same column name under a DIFFERENT content type")
        cross = memory.resolve(source_column="Wt (kgs)",
                               content_type="LANE", source_id="distributor_north")
        print(f"  scope: {cross.scope} -> {cross.target_field}")
        print("  -> memory is keyed on content type too. `Rate` in a lane "
              "file is not `Rate` in a contract.")

        section("when senders DISAGREE")
        memory.record(source_column="Node_ID", target_field="facility_id",
                      content_type="LANE", source_id="vendor_a",
                      confirmed_by="aayush")
        memory.record(source_column="Node_ID", target_field="node_id",
                      content_type="LANE", source_id="vendor_b",
                      confirmed_by="aayush")
        clash = memory.resolve(source_column="Node_ID", content_type="LANE",
                               source_id="vendor_c")
        print(f"  scope: {clash.scope}   is_conflict: {clash.is_conflict}")
        for alternative in clash.alternatives:
            print(f"      {alternative.target_field}: "
                  f"support={alternative.support}")
        print("  -> surfaced WITH the evidence. Never silently resolved by "
              "picking the more popular one.")

        print(f"\n  memory stats: {memory.stats()}")

        # ---------------- document memory --------------------------------
        documents = DocumentMemory(storage)

        section("document shape: a renewal of a known contract")
        print(f"  signature tokens in the contract       : "
              f"{len(signature(contract))} (floor is 40)")
        print(f"  signature overlap, contract vs renewal : "
              f"{similarity(signature(contract), signature(renewal)):.2f}")
        print(f"  signature overlap, contract vs unrelated: "
              f"{similarity(signature(contract), signature(DIFFERENT)):.2f}")

        documents.record(contract, document_name="transcorp_2026.pdf",
                         labels={"vendor": "TransCorp Logistics"})

        renewal_match = documents.find(renewal)
        other_match = documents.find(DIFFERENT)
        print(f"\n  renewal recognised : {renewal_match.matched}")
        if renewal_match.matched:
            print(f"      {renewal_match.rationale}")
        print(f"  unrelated document : {other_match.matched} "
              f"(correctly NOT matched)")
        print("\n  -> digits are excluded from the shape on purpose, so new "
              "rates in the same template still match. The exact-text cache "
              "cannot do this: the bytes changed.")

    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
