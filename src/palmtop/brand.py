"""Branded HTML email template — driven by PersonaConfig.

Used by EmailTool (all outgoing emails) and LeadOutreach (booking
CTAs).  Keep this in sync with web/static/style.css.
"""

from __future__ import annotations

import html as _html

from palmtop.persona import PersonaConfig, BrandConfig


# ── Default brand (used when no persona is available) ──────────────
_DEFAULT_BRAND = BrandConfig()


def _paragraphs_html(text: str, brand: BrandConfig) -> str:
    """Convert plain text paragraphs to styled HTML <p> tags."""
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "\n".join(
        f'<p style="margin:0 0 16px;line-height:1.6;'
        f'font-size:15px;color:{brand.text};">'
        f"{_html.escape(p)}</p>"
        for p in paragraphs
    )


def booking_buttons_html(
    options: list[dict],
    brand: BrandConfig | None = None,
) -> str:
    """Generate HTML rows for booking link buttons."""
    b_ = brand or _DEFAULT_BRAND
    rows = []
    for b in options:
        label = b["name"]
        if b.get("duration"):
            label += f" ({b['duration']})"
        rows.append(
            f'<tr><td style="padding:6px 0;">'
            f'<a href="{b["url"]}" target="_blank" '
            f'style="display:inline-block;padding:10px 20px;'
            f"background:{b_.accent};color:{b_.bg};font-weight:600;"
            f"font-size:14px;font-family:{b_.font};"
            f'text-decoration:none;border-radius:6px;">'
            f"{label}</a>"
            f'<br><span style="font-size:12px;color:{b_.text_muted};">'
            f'{b["desc"]}</span>'
            f"</td></tr>"
        )
    return "\n".join(rows)


def build_email_html(
    body_text: str,
    booking_options: list[dict] | None = None,
    persona: PersonaConfig | None = None,
) -> str:
    """Build a branded HTML email from persona config.

    Args:
        body_text: Plain-text email body (paragraphs separated by blank lines).
        booking_options: Optional list of booking link dicts.  If provided,
            a booking section with styled buttons is added.
        persona: PersonaConfig for branding.  Falls back to defaults.
    """
    p = persona or PersonaConfig()
    brand = p.brand

    body_html = _paragraphs_html(body_text, brand)

    owner_ref = p.owner_name or "us"
    domain_url = f"https://{p.domain}" if p.domain else ""
    domain_label = p.domain or ""
    location_line = f"Built with care in {p.location}" if p.location else ""

    # Optional booking section
    booking_section = ""
    if booking_options:
        buttons = booking_buttons_html(booking_options, brand)
        booking_header = f"Book a time with {owner_ref}"
        booking_section = f"""\
  <!-- Booking section -->
  <tr>
    <td style="padding:0 32px 32px;">
      <table cellpadding="0" cellspacing="0" border="0" style="width:100%;background:{brand.bg};border:1px solid {brand.border};border-radius:8px;padding:20px;">
        <tr>
          <td style="padding:0 0 12px;">
            <p style="margin:0;font-size:14px;font-weight:600;color:{brand.accent};font-family:{brand.font};">{booking_header}</p>
          </td>
        </tr>
        {buttons}
      </table>
    </td>
  </tr>"""

    # Footer content
    footer_parts = []
    if domain_url:
        footer_parts.append(
            f'<a href="{domain_url}" style="color:{brand.accent};text-decoration:none;">'
            f"{domain_label}</a>"
        )
    if location_line:
        footer_parts.append(location_line)
    footer_html = " &middot; ".join(footer_parts) if footer_parts else ""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{brand.bg};font-family:{brand.font};-webkit-font-smoothing:antialiased;">

<!-- Wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{brand.bg};">
<tr><td align="center" style="padding:24px 16px;">

<!-- Main card -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:{brand.surface};border:1px solid {brand.border};border-radius:12px;overflow:hidden;">

  <!-- Header -->
  <tr>
    <td style="padding:32px 32px 24px;border-bottom:2px solid {brand.accent};">
      <h1 style="margin:0;font-size:24px;font-weight:700;color:{brand.text};letter-spacing:-0.03em;font-family:{brand.font};">{p.name}</h1>
      <p style="margin:4px 0 0;font-size:14px;color:{brand.text_muted};font-family:{brand.font};">{p.tagline}</p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:32px;">
      {body_html}
    </td>
  </tr>

{booking_section}

  <!-- Footer -->
  <tr>
    <td style="padding:20px 32px;border-top:1px solid {brand.border};text-align:center;">
      <p style="margin:0;font-size:12px;color:{brand.text_muted};font-family:{brand.font};">
        {footer_html}
      </p>
    </td>
  </tr>

</table>
<!-- /Main card -->

</td></tr>
</table>
<!-- /Wrapper -->

</body>
</html>"""
