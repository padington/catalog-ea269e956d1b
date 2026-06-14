"""Render a static, self-contained HTML catalogue into site/index.html.

No external CDN: the minimal CSS/JS is inlined. Client-side category filtering
is plain JS. A reel with multiple categories is rendered once per category.
"""

import datetime as dt
import html
import os

import db as dbm

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(HERE, "site")

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reels Catalogue</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #fafafa; color: #222; }
  header { padding: 1rem; background: #fff; border-bottom: 1px solid #ddd; position: sticky; top: 0; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  .filters button { margin: 2px; padding: .3rem .7rem; border: 1px solid #ccc;
    border-radius: 999px; background: #fff; cursor: pointer; font-size: .85rem; }
  .filters button.active { background: #222; color: #fff; border-color: #222; }
  .sort { margin-top: .5rem; font-size: .8rem; color: #888; }
  .sort button { margin: 0 2px; padding: .2rem .6rem; border: 1px solid #ccc;
    border-radius: 999px; background: #fff; cursor: pointer; font-size: .8rem; }
  .sort button.active { background: #222; color: #fff; border-color: #222; }
  section { padding: 1rem; }
  h2 { font-size: 1rem; text-transform: capitalize; border-bottom: 2px solid #eee; padding-bottom: .25rem; }
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
  .card iframe.embed { width: 100%; height: 540px; border: 0; background: #000; display: block; }
  .open { display: inline-block; margin-top: .4rem; font-size: .7rem; color: #3897f0; }
</style>
</head>
<body>
<header>
  <h1>Reels Catalogue ({{ total }} reels)</h1>
  <div class="filters">
    <button class="active" data-cat="all" onclick="filterCat('all', this)">all</button>
    {% for cat in categories %}
    <button data-cat="{{ cat }}" onclick="filterCat('{{ cat }}', this)">{{ cat }}</button>
    {% endfor %}
  </div>
  <div class="sort">sort:
    <button class="active" data-sort="new" onclick="sortReels('new', this)">newest</button>
    <button data-sort="old" onclick="sortReels('old', this)">oldest</button>
  </div>
</header>

{% for cat in categories %}
<section class="cat-section" data-cat="{{ cat }}">
  <h2>{{ cat }} ({{ grouped[cat]|length }})</h2>
  <div class="grid">
    {% for r in grouped[cat] %}
    <div class="card" data-code="{{ r.shortcode }}" data-ts="{{ r.taken_at or 0 }}">
      {% if r.shortcode %}
      <div class="thumb" onclick="playReel(this)">
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
        {% if r.url %}<a class="open" href="{{ r.url }}" target="_blank" rel="noopener">open on Instagram &#8599;</a>{% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</section>
{% endfor %}

<script>
function filterCat(cat, btn) {
  document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.cat-section').forEach(s => {
    s.style.display = (cat === 'all' || s.dataset.cat === cat) ? '' : 'none';
  });
}
function sortReels(mode, btn) {
  document.querySelectorAll('.sort button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.grid').forEach(grid => {
    var cards = Array.prototype.slice.call(grid.querySelectorAll('.card'));
    cards.sort(function (a, b) {
      var ta = +a.dataset.ts || 0, tb = +b.dataset.ts || 0;
      return mode === 'new' ? tb - ta : ta - tb;
    });
    cards.forEach(c => grid.appendChild(c));
  });
}
function playReel(thumb) {
  var card = thumb.closest('.card');
  var code = card && card.dataset.code;
  if (!code) return;
  var f = document.createElement('iframe');
  f.src = 'https://www.instagram.com/reel/' + code + '/embed';
  f.className = 'embed';
  f.loading = 'lazy';
  f.setAttribute('scrolling', 'no');
  f.setAttribute('allowtransparency', 'true');
  thumb.replaceWith(f);
}
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

    grouped = {}
    for r in reels:
        r["snippet"] = _snippet(r.get("caption"))
        ts = r.get("taken_at")
        r["shared_date"] = (
            dt.datetime.fromtimestamp(ts).strftime("%b %d, %Y") if ts else None
        )
        cats = r.get("categories") or ["other"]
        for cat in cats:
            grouped.setdefault(cat, []).append(r)

    categories = sorted(grouped.keys())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    rendered = Template(TEMPLATE).render(
        total=len(reels), categories=categories, grouped=grouped
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print(f"wrote {out_path} ({len(reels)} reels, {len(categories)} categories)")


if __name__ == "__main__":
    build(os.environ.get("REELS_DB", "reels.db"))
