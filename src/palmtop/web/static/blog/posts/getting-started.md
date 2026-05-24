---
title: Getting Started
date: 2025-01-01
description: An example blog post showing how the Palmtop blog engine works. Replace this with your own content.
tags: getting-started, example
---

This is an example post to show how the blog engine works.

## How blog posts work

Blog posts are markdown files in `src/palmtop/web/static/blog/posts/`. Each file needs frontmatter between `---` markers at the top:

- **title**: The post title (shown on the index and in the browser tab)
- **date**: Publication date in YYYY-MM-DD format
- **description**: A short summary (used for social sharing and the index page)
- **tags**: Comma-separated tags

## Markdown features

The blog engine supports:

- **Bold** and *italic* text
- [Links](https://example.com)
- Headers (h2 through h4)
- Bullet lists and numbered lists
- Code blocks with triple backticks
- Blockquotes
- Horizontal rules

## What to write about

Write about whatever matters to you. The blog engine is zero-dependency -- no markdown libraries, no YAML parsers. It runs anywhere, including on a phone via Termux.

Replace this post with your own content and start writing.
