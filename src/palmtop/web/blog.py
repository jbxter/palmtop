"""Zero-dependency blog engine.

Reads markdown files from static/blog/posts/, parses simple frontmatter,
converts markdown to HTML, and wraps in a styled template.

All identity (site name, base URL, LinkedIn link, footer) comes from
PersonaConfig — nothing is hardcoded.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from palmtop.persona import PersonaConfig

POSTS_DIR = Path(__file__).parent / "static" / "blog" / "posts"


# ── Frontmatter parser ──────────────────────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse --- delimited frontmatter. Returns (meta dict, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 3 :].strip()
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip().strip('"').strip("'")
    return meta, body


# ── Minimal markdown → HTML ─────────────────────────────────────────


def _md_to_html(md: str) -> str:
    """Convert a subset of markdown to HTML. No dependencies."""
    lines = md.split("\n")
    html_parts: list[str] = []
    in_code = False
    in_list = False
    in_blockquote = False
    paragraph: list[str] = []

    def flush_paragraph():
        if paragraph:
            text = " ".join(paragraph)
            html_parts.append(f"<p>{_inline(text)}</p>")
            paragraph.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    def close_blockquote():
        nonlocal in_blockquote
        if in_blockquote:
            html_parts.append("</blockquote>")
            in_blockquote = False

    for line in lines:
        # Fenced code blocks
        if line.strip().startswith("```"):
            if in_code:
                html_parts.append("</code></pre>")
                in_code = False
            else:
                flush_paragraph()
                close_list()
                close_blockquote()
                lang = line.strip()[3:].strip()
                cls = f' class="language-{lang}"' if lang else ""
                html_parts.append(f"<pre><code{cls}>")
                in_code = True
            continue

        if in_code:
            escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html_parts.append(escaped)
            continue

        stripped = line.strip()

        # Empty line — flush paragraph
        if not stripped:
            flush_paragraph()
            close_list()
            close_blockquote()
            continue

        # Headers
        hm = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if hm:
            flush_paragraph()
            close_list()
            close_blockquote()
            level = len(hm.group(1))
            html_parts.append(f"<h{level + 1}>{_inline(hm.group(2))}</h{level + 1}>")
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}\s*$", stripped):
            flush_paragraph()
            close_list()
            close_blockquote()
            html_parts.append("<hr>")
            continue

        # Unordered list items
        lm = re.match(r"^[-*+]\s+(.*)", stripped)
        if lm:
            flush_paragraph()
            close_blockquote()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{_inline(lm.group(1))}</li>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            flush_paragraph()
            close_list()
            content = stripped[1:].strip()
            if not in_blockquote:
                html_parts.append("<blockquote>")
                in_blockquote = True
            html_parts.append(f"<p>{_inline(content)}</p>")
            continue

        # Regular text — accumulate into paragraph
        close_list()
        close_blockquote()
        paragraph.append(stripped)

    # Flush remaining
    flush_paragraph()
    close_list()
    close_blockquote()
    if in_code:
        html_parts.append("</code></pre>")

    return "\n".join(html_parts)


def _render_link(m: re.Match) -> str:
    """Render a markdown link, dropping disallowed (e.g. javascript:) schemes."""
    label, url = m.group(1), m.group(2)
    if url.strip().lower().startswith(("javascript:", "data:", "vbscript:")):
        return label  # unsafe scheme — render as plain text, no anchor
    href = url.replace('"', "%22")
    return f'<a href="{href}">{label}</a>'


def _inline(text: str) -> str:
    """Process inline markdown: bold, italic, code, links, em-dash.

    Text is HTML-escaped first so raw markup in a post can't inject HTML, and
    link schemes are restricted (defense in depth alongside the site CSP).
    """
    text = html.escape(text, quote=False)
    # Code spans first (protect from other processing)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Links: [text](url) — scheme-checked
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _render_link, text)
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    # Italic: *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    # Em-dash
    text = text.replace(" -- ", " &mdash; ")
    return text


# ── Post data model ─────────────────────────────────────────────────


@dataclass
class BlogPost:
    slug: str
    title: str
    date: str
    description: str
    body_html: str
    tags: list[str]
    image: str = ""  # custom OG image URL (optional)
    base_url: str = ""  # set from persona.domain at load time

    @property
    def url(self) -> str:
        return f"/blog/{self.slug}"

    @property
    def og_image(self) -> str:
        default = f"{self.base_url}/static/og-blog.jpg" if self.base_url else ""
        return self.image or default

    @property
    def iso_date(self) -> str:
        """Date in ISO 8601 for article:published_time."""
        if self.date and len(self.date) == 10:
            return f"{self.date}T00:00:00-07:00"
        return self.date


def load_post(
    slug: str,
    persona: PersonaConfig | None = None,
) -> BlogPost | None:
    """Load a single post by slug."""
    p = persona or PersonaConfig()
    base = f"https://{p.domain}" if p.domain else ""

    path = POSTS_DIR / f"{slug}.md"
    if not path.exists():
        return None
    meta, body = _parse_frontmatter(path.read_text())
    if meta.get("draft", "").lower() == "true":
        return None
    tags_raw = meta.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    image = meta.get("image", "")
    if image and not image.startswith("http"):
        image = f"{base}{image}"
    return BlogPost(
        slug=slug,
        title=meta.get("title", slug.replace("-", " ").title()),
        date=meta.get("date", ""),
        description=meta.get("description", ""),
        body_html=_md_to_html(body),
        tags=tags,
        image=image,
        base_url=base,
    )


def list_posts(persona: PersonaConfig | None = None) -> list[BlogPost]:
    """List all published posts, newest first."""
    posts = []
    if not POSTS_DIR.exists():
        return posts
    for path in sorted(POSTS_DIR.glob("*.md"), reverse=True):
        post = load_post(path.stem, persona=persona)
        if post:
            posts.append(post)
    # Sort by date descending
    posts.sort(key=lambda p: p.date, reverse=True)
    return posts


# ── HTML templates ──────────────────────────────────────────────────


def _head_index(
    title: str,
    description: str,
    url: str,
    persona: PersonaConfig,
) -> str:
    """HTML head for the blog index page (og:type = website)."""
    base = f"https://{persona.domain}" if persona.domain else ""
    og_image = f"{base}/static/og-blog.jpg" if base else ""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <meta property="og:site_name" content="{persona.name}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{description}">
  <meta property="og:image" content="{og_image}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{base}{url}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{description}">
  <meta name="twitter:image" content="{og_image}">
  <link rel="icon" href="/static/favicon.ico" sizes="32x32">
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <link rel="canonical" href="{base}{url}">
  <link rel="stylesheet" href="/static/style.css?v=1">
  <link rel="stylesheet" href="/static/blog.css?v=1">
</head>
"""


def _head_article(post: BlogPost, persona: PersonaConfig) -> str:
    """HTML head for an individual blog post (og:type = article)."""
    base = f"https://{persona.domain}" if persona.domain else ""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{post.title} &mdash; {persona.name}</title>
  <meta name="description" content="{post.description}">
  <meta property="og:site_name" content="{persona.name}">
  <meta property="og:title" content="{post.title}">
  <meta property="og:description" content="{post.description}">
  <meta property="og:image" content="{post.og_image}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{base}{post.url}">
  <meta property="article:published_time" content="{post.iso_date}">
  <meta property="article:author" content="{base}">
  <meta property="article:section" content="Technology">
  {_article_tags_meta(post.tags)}
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{post.title}">
  <meta name="twitter:description" content="{post.description}">
  <meta name="twitter:image" content="{post.og_image}">
  <meta name="twitter:label1" content="Written by">
  <meta name="twitter:data1" content="{persona.name}">
  <meta name="twitter:label2" content="Published">
  <meta name="twitter:data2" content="{post.date}">
  <link rel="icon" href="/static/favicon.ico" sizes="32x32">
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <link rel="canonical" href="{base}{post.url}">
  <link rel="stylesheet" href="/static/style.css?v=1">
  <link rel="stylesheet" href="/static/blog.css?v=1">
</head>
"""


def _article_tags_meta(tags: list[str]) -> str:
    """Generate article:tag meta tags."""
    return "\n  ".join(f'<meta property="article:tag" content="{t}">' for t in tags)


def _nav_html(persona: PersonaConfig) -> str:
    """Build the site nav from persona config."""
    linkedin = ""
    if persona.linkedin_url:
        linkedin = (
            f'      <a href="{persona.linkedin_url}" target="_blank" rel="noopener" class="nav-link">LinkedIn</a>'
        )

    return f"""\
<nav class="site-nav">
  <div class="container">
    <a href="/" class="nav-brand">{persona.name}</a>
    <div class="nav-links">
      <a href="/blog" class="nav-link active">Blog</a>
{linkedin}
    </div>
  </div>
</nav>
"""


def _footer_html(persona: PersonaConfig) -> str:
    """Build the site footer from persona config."""
    parts = [f"&copy; 2026 {persona.name}."]
    if persona.location:
        parts.append(f"Built with care in {persona.location}.")
    return f"""\
<footer>
  <div class="container">
    <p>{" ".join(parts)}</p>
  </div>
</footer>"""


def render_post_page(
    post: BlogPost,
    persona: PersonaConfig | None = None,
) -> str:
    """Render a single blog post to full HTML."""
    p = persona or PersonaConfig()
    head = _head_article(post, p)
    nav = _nav_html(p)
    footer = _footer_html(p)

    tags_html = ""
    if post.tags:
        tags_html = (
            '<div class="post-tags">' + "".join(f'<span class="post-tag">{t}</span>' for t in post.tags) + "</div>"
        )

    return f"""{head}
<body>
{nav}
<article class="blog-post">
  <div class="container">
    <header class="post-header">
      <time class="post-date">{post.date}</time>
      <h1>{post.title}</h1>
      {f'<p class="post-description">{post.description}</p>' if post.description else ""}
      {tags_html}
    </header>
    <div class="post-body">
      {post.body_html}
    </div>
    <footer class="post-footer">
      <a href="/blog">&larr; All posts</a>
    </footer>
  </div>
</article>
{footer}
</body>
</html>"""


def render_blog_index(
    posts: list[BlogPost],
    persona: PersonaConfig | None = None,
) -> str:
    """Render the blog listing page."""
    p = persona or PersonaConfig()
    head = _head_index(
        title=f"Blog — {p.name}",
        description="Notes on building software and figuring things out.",
        url="/blog",
        persona=p,
    )
    nav = _nav_html(p)
    footer = _footer_html(p)

    if not posts:
        listing = '<p class="empty">No posts yet. Check back soon.</p>'
    else:
        cards = []
        for post in posts:
            tags_html = ""
            if post.tags:
                tags_html = (
                    '<div class="post-tags">'
                    + "".join(f'<span class="post-tag">{t}</span>' for t in post.tags)
                    + "</div>"
                )
            cards.append(f"""\
    <a href="{post.url}" class="post-card">
      <time class="post-date">{post.date}</time>
      <h3>{post.title}</h3>
      <p>{post.description}</p>
      {tags_html}
    </a>""")
        listing = "\n".join(cards)

    return f"""{head}
<body>
{nav}
<section class="blog-index">
  <div class="container">
    <header class="blog-header">
      <h1>Blog</h1>
      <p class="blog-subtitle">Notes on building software and figuring things out.</p>
    </header>
    <div class="post-list">
{listing}
    </div>
  </div>
</section>
{footer}
</body>
</html>"""
