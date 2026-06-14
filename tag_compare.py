"""A/B demo: what do transcripts add over caption-only tagging?

For each transcribed reel, run the SAME categorizer/tagger twice:
  A) caption only            (what we have today)
  B) caption + transcript    (what ASR unlocks)
and print the delta (categories/tags B has that A doesn't).
"""
import json
import sqlite3

import categorize as cz
import tags as tagsmod

c = sqlite3.connect("reels.db")
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT pk, shortcode, caption, transcript FROM reels "
    "WHERE transcript IS NOT NULL AND transcript != '' ORDER BY rowid LIMIT 5"
))

out = []
for r in rows:
    cap = (r["caption"] or "").strip()
    tr = (r["transcript"] or "").strip()
    combined = (cap + "\n" + tr).strip()

    a_cats = cz.categorize_caption(cap) if cap else ["other"]
    a_tags = tagsmod.generate_tags(cap) if cap else []
    description = cz._describe_reel(combined)
    b_cats = cz.categorize_caption(combined)
    b_tags = tagsmod.generate_tags(combined)

    new_cats = [x for x in b_cats if x not in set(a_cats)]
    new_tags = [t for t in b_tags if t not in set(a_tags)]

    out.append({
        "pk": r["pk"], "code": r["shortcode"],
        "caption": cap[:80], "transcript_chars": len(tr),
        "transcript_head": tr[:120],
        "description": description,
        "a_cats": a_cats, "a_tags": a_tags,
        "b_cats": b_cats, "b_tags": b_tags,
        "new_cats": new_cats, "new_tags": new_tags,
    })

print(json.dumps(out, ensure_ascii=False, indent=2))
