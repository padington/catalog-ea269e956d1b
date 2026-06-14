"""Render a static, self-contained HTML catalogue into site/index.html.

No external CDN: the minimal CSS/JS is inlined. All filtering is client-side:
- base category chips (with counts) — click one to drill into its tags
- a per-category row of fine-grained tag chips, hidden until a category is picked
- a free-text search box matching captions, tags and category names
- a per-reel expand button opening the Instagram embed in a modal lightbox
- newest/oldest sort

Each reel is rendered exactly once; visibility is toggled by JS.
"""

import datetime as dt
import html
import json
import os
from collections import Counter

import db as dbm

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(HERE, "site")

# Max fine-tag chips offered per category in the drill-down row.
MAX_TAGS_PER_CAT = 40

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reels Catalogue</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #fafafa; color: #222; }
  header { padding: 1rem; background: #fff; border-bottom: 1px solid #ddd; position: sticky; top: 0; z-index: 5; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  .search { width: 100%; max-width: 360px; padding: .45rem .7rem; font-size: .9rem;
    border: 1px solid #ccc; border-radius: 999px; margin-bottom: .6rem; box-sizing: border-box; }
  .chips button { margin: 2px; padding: .3rem .7rem; border: 1px solid #ccc;
    border-radius: 999px; background: #fff; cursor: pointer; font-size: .85rem; }
  .chips button.active { background: #222; color: #fff; border-color: #222; }
  .chips button .ct { color: #999; font-size: .75rem; margin-left: .2rem; }
  .chips button.active .ct { color: #ccc; }
  .subtags { margin-top: .5rem; display: none; }
  .subtags.open { display: block; }
  .subtags button { font-size: .8rem; padding: .2rem .6rem; }
  .subtags button.active { background: #3897f0; color: #fff; border-color: #3897f0; }
  .bar { margin-top: .5rem; font-size: .8rem; color: #888; }
  .bar button { margin: 0 2px; padding: .2rem .6rem; border: 1px solid #ccc;
    border-radius: 999px; background: #fff; cursor: pointer; font-size: .8rem; }
  .bar button.active { background: #222; color: #fff; border-color: #222; }
  #count { margin-left: .5rem; }
  section { padding: 1rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1rem; }
  .card { background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; overflow: hidden;
    display: flex; flex-direction: column; }
  .card img { width: 100%; aspect-ratio: 9/16; object-fit: cover; background: #eee; display: block; }
  .card .body { padding: .5rem; font-size: .8rem; flex: 1; }
  .card .by { color: #888; font-size: .7rem; margin-top: .25rem; }
  .card a { text-decoration: none; color: inherit; }
  .thumb { position: relative; cursor: pointer; background: #eee; aspect-ratio: 9/16; }
  .thumb .play { position: absolute; inset: 0; margin: auto; width: 3rem; height: 3rem;
    display: flex; align-items: center; justify-content: center; border-radius: 999px;
    background: rgba(0,0,0,.55); color: #fff; font-size: 1.1rem; pointer-events: none; }
  .open { display: inline-block; margin-top: .4rem; font-size: .7rem; color: #3897f0; }
  .ctags { margin-top: .35rem; line-height: 1.6; }
  .ctags .t { display: inline-block; font-size: .68rem; color: #555; background: #f0f0f0;
    border-radius: 999px; padding: .05rem .45rem; margin: 1px; cursor: pointer; }
  .ctags .t:hover { background: #e2e2e2; }
  .modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    width: 100%; height: 100%; background: rgba(0,0,0,.88); z-index: 50;
    align-items: center; justify-content: center; }
  .modal.open { display: flex; flex-direction: column; padding: 3rem 0 1rem; box-sizing: border-box; }
  .modal-inner { position: relative; width: min(96vw, 640px); aspect-ratio: 100/128;
    max-height: 74vh; overflow: hidden; border-radius: 10px; background: #000; flex: 0 0 auto; }
  .modal-inner #modal-body { width: 100%; height: 100%; overflow: hidden; }
  /* Oversize the iframe and clip the bottom so Instagram's likes/comments footer is cropped off. */
  .modal-inner iframe { width: 100%; height: calc(100% + 180px); border: 0; background: #000; display: block; }
  .modal-close { position: absolute; top: -2.6rem; right: 0; width: 2.2rem; height: 2.2rem;
    border: 0; border-radius: 999px; background: #fff; color: #222; font-size: 1.3rem;
    line-height: 1; cursor: pointer; }
  .modal-tags { width: min(96vw, 640px); margin-top: .6rem; text-align: center; line-height: 1.8;
    flex: 0 1 auto; overflow-y: auto; }
  .modal-tags .t { display: inline-block; font-size: .72rem; color: #eee; background: rgba(255,255,255,.16);
    border-radius: 999px; padding: .1rem .55rem; margin: 2px; cursor: pointer; }
  .modal-tags .t:hover { background: rgba(255,255,255,.3); }
</style>
</head>
<body>
<header>
  <h1>Reels Catalogue ({{ total }} reels)</h1>
  <input id="search" class="search" type="search" placeholder="search tags or captions..."
         oninput="onSearch(this.value)">
  <div class="chips" id="cats">
    <button class="active" data-cat="all" onclick="selectCat('all', this)">all<span class="ct">{{ total }}</span></button>
    {% for cat, n in cat_counts %}
    <button data-cat="{{ cat }}" onclick="selectCat('{{ cat }}', this)">{{ cat }}<span class="ct">{{ n }}</span></button>
    {% endfor %}
  </div>
  <div class="chips subtags" id="subtags"></div>
  <div class="bar">sort:
    <button class="active" data-sort="new" onclick="sortReels('new', this)">newest</button>
    <button data-sort="old" onclick="sortReels('old', this)">oldest</button>
    <span id="count"></span>
  </div>
</header>

<section>
  <div class="grid" id="grid">
    {% for r in reels %}
    <div class="card" data-code="{{ r.shortcode }}" data-ts="{{ r.taken_at or 0 }}"
         data-cats="{{ r.cats_attr }}" data-tags="{{ r.tags_attr }}" data-text="{{ r.text_attr }}">
      {% if r.shortcode %}
      <div class="thumb" onclick="openModal('{{ r.shortcode }}')">
        {% if r.thumbnail_url %}<img src="{{ r.thumbnail_url }}" alt="" loading="lazy">{% endif %}
        <span class="play">&#9654;</span>
      </div>
      {% elif r.thumbnail_url %}
      <a href="{{ r.url }}" target="_blank" rel="noopener"><img src="{{ r.thumbnail_url }}" alt="" loading="lazy"></a>
      {% endif %}
      <div class="body">
        {{ r.snippet }}
        {% if r.shared_by %}<div class="by">shared by {{ r.shared_by }}</div>{% endif %}
        {% if r.shared_date %}<div class="by">shared {{ r.shared_date }}</div>{% endif %}
        {% if r.tags %}<div class="ctags">{% for t in r.tags[:8] %}<span class="t" onclick="searchTag('{{ t }}')">{{ t }}</span>{% endfor %}</div>{% endif %}
        {% if r.url %}<a class="open" href="{{ r.url }}" target="_blank" rel="noopener">open on Instagram &#8599;</a>{% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</section>

<div class="modal" id="modal" onclick="closeModal(event)">
  <div class="modal-inner" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div id="modal-body"></div>
  </div>
  <div id="modal-tags" class="modal-tags" onclick="event.stopPropagation()"></div>
</div>

<script>
var CAT_TAGS = {{ cat_tags_json }};
var state = { cat: 'all', tag: null, q: '' };

function applyFilters() {
  var shown = 0;
  document.querySelectorAll('#grid .card').forEach(function (c) {
    var cats = (c.dataset.cats || '').split(' ');
    var tags = (c.dataset.tags || '').split(' ');
    var okCat = state.cat === 'all' || cats.indexOf(state.cat) !== -1;
    var okTag = !state.tag || tags.indexOf(state.tag) !== -1;
    var okQ = !state.q || (c.dataset.text || '').indexOf(state.q) !== -1;
    var vis = okCat && okTag && okQ;
    c.style.display = vis ? '' : 'none';
    if (vis) shown++;
  });
  document.getElementById('count').textContent = shown + ' shown';
}

function renderSubtags() {
  var box = document.getElementById('subtags');
  box.innerHTML = '';
  var list = (state.cat !== 'all' && CAT_TAGS[state.cat]) ? CAT_TAGS[state.cat] : [];
  if (!list.length) { box.classList.remove('open'); return; }
  list.forEach(function (pair) {
    var b = document.createElement('button');
    b.innerHTML = pair[0] + '<span class="ct">' + pair[1] + '</span>';
    b.onclick = function () { selectTag(pair[0], b); };
    if (state.tag === pair[0]) b.classList.add('active');
    box.appendChild(b);
  });
  box.classList.add('open');
}

function selectCat(cat, btn) {
  document.querySelectorAll('#cats button').forEach(function (b) { b.classList.remove('active'); });
  btn.classList.add('active');
  state.cat = cat;
  state.tag = null;
  renderSubtags();
  applyFilters();
}

function selectTag(tag, btn) {
  if (state.tag === tag) { state.tag = null; } else { state.tag = tag; }
  document.querySelectorAll('#subtags button').forEach(function (b) { b.classList.remove('active'); });
  if (state.tag && btn) btn.classList.add('active');
  applyFilters();
}

function searchTag(tag) {
  document.getElementById('search').value = tag;
  onSearch(tag);
}

function onSearch(v) {
  state.q = (v || '').trim().toLowerCase();
  applyFilters();
}

function sortReels(mode, btn) {
  document.querySelectorAll('.bar button[data-sort]').forEach(function (b) { b.classList.remove('active'); });
  btn.classList.add('active');
  var grid = document.getElementById('grid');
  var cards = Array.prototype.slice.call(grid.querySelectorAll('.card'));
  cards.sort(function (a, b) {
    var ta = +a.dataset.ts || 0, tb = +b.dataset.ts || 0;
    return mode === 'new' ? tb - ta : ta - tb;
  });
  cards.forEach(function (c) { grid.appendChild(c); });
}

function openModal(code) {
  if (!code) return;
  document.getElementById('modal-body').innerHTML =
    '<iframe src="https://www.instagram.com/reel/' + code + '/embed/" scrolling="no"' +
    ' allow="autoplay; encrypted-media; clipboard-write; picture-in-picture" allowfullscreen></iframe>';
  var box = document.getElementById('modal-tags');
  box.innerHTML = '';
  var card = document.querySelector('.card[data-code="' + code + '"]');
  var tags = card ? (card.dataset.tags || '').split(' ').filter(Boolean) : [];
  tags.forEach(function (t) {
    var chip = document.createElement('span');
    chip.className = 't';
    chip.textContent = t;
    chip.onclick = function () { closeModal(); searchTag(t); };
    box.appendChild(chip);
  });
  document.getElementById('modal').classList.add('open');
}
function closeModal(e) {
  if (e && e.type === 'click' && e.target.id !== 'modal') return;
  document.getElementById('modal').classList.remove('open');
  document.getElementById('modal-body').innerHTML = '';
  document.getElementById('modal-tags').innerHTML = '';
}
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeModal(); });

applyFilters();
</script>
</body>
</html>
"""


def _snippet(caption, n=140):
    caption = (caption or "").strip()
    return html.escape(caption[:n] + ("..." if len(caption) > n else "")) or "(no caption)"


def build(db_path="reels.db", out_dir=SITE_DIR):
    from jinja2 import Template

    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    reels = dbm.all_reels(conn)

    cat_counter = Counter()
    cat_tag_counter = {}  # category -> Counter(tag)
    for r in reels:
        r["snippet"] = _snippet(r.get("caption"))
        ts = r.get("taken_at")
        r["shared_date"] = (
            dt.datetime.fromtimestamp(ts).strftime("%b %d, %Y") if ts else None
        )
        cats = r.get("categories") or ["other"]
        tags = r.get("tags") or []
        r["cats_attr"] = html.escape(" ".join(cats), quote=True)
        r["tags_attr"] = html.escape(" ".join(tags), quote=True)
        searchable = " ".join([r.get("caption") or ""] + tags + cats).lower()
        r["text_attr"] = html.escape(searchable, quote=True)
        for c in cats:
            cat_counter[c] += 1
            tc = cat_tag_counter.setdefault(c, Counter())
            for t in tags:
                tc[t] += 1

    cat_counts = cat_counter.most_common()
    cat_tags = {
        c: tc.most_common(MAX_TAGS_PER_CAT) for c, tc in cat_tag_counter.items()
    }

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    rendered = Template(TEMPLATE).render(
        total=len(reels),
        reels=reels,
        cat_counts=cat_counts,
        cat_tags_json=json.dumps(cat_tags),
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print(f"wrote {out_path} ({len(reels)} reels, {len(cat_counts)} categories)")


if __name__ == "__main__":
    build(os.environ.get("REELS_DB", "reels.db"))
