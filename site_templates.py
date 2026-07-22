"""
Site-template registry for Claude Code builds.

When the user voice-triggers a build ("build me a portfolio site"), JARVIS
classifies the request and injects a real reference template URL + style
fingerprint into the Claude Code prompt. Claude Code can then WebFetch the
reference to study its layout, palette, and typography — producing output
that looks professional out of the gate instead of a bare scaffold.

Each entry includes:
- keywords: substrings used to match user requests
- reference_url: a single high-quality template Claude Code should study
- style: a short prose fingerprint (palette, typography, layout)
"""

from dataclasses import dataclass


@dataclass
class SiteTemplate:
    name: str
    keywords: tuple[str, ...]
    reference_url: str
    style: str


TEMPLATES: list[SiteTemplate] = [
    SiteTemplate(
        name="portfolio",
        keywords=("portfolio", "personal site", "personal website", "about me",
                  "resume site", "developer site", "designer site"),
        reference_url="https://html5up.net/dimension",
        style=(
            "Minimal personal portfolio. Single-page with hero (name + tagline), "
            "compact bio, project grid (3-6 cards), contact footer. "
            "Palette: deep navy or near-black background with one warm accent "
            "(amber/coral/gold). Typography: large serif or geometric sans for "
            "headers (Playfair, Inter, Space Grotesk), system font for body. "
            "Generous whitespace, subtle hover transforms on cards."
        ),
    ),
    SiteTemplate(
        name="saas_landing",
        keywords=("saas", "startup", "landing page", "product page", "marketing site",
                  "pricing page", "feature page"),
        reference_url="https://stripe.com",
        style=(
            "Modern SaaS landing. Sticky nav with logo + 3-5 links + CTA button. "
            "Hero (headline + sub + primary CTA + secondary CTA + product mockup), "
            "trusted-by logo row, 3-column feature grid, testimonial section, "
            "pricing 3-tier cards, FAQ accordion, large footer. "
            "Palette: cool slate/zinc neutrals + ONE vivid accent (indigo, "
            "violet, or teal). Inter or similar sans-serif. Subtle gradients, "
            "soft shadows, generous padding, 16px+ base font size."
        ),
    ),
    SiteTemplate(
        name="blog",
        keywords=("blog", "writing site", "essays site", "articles site", "journal"),
        reference_url="https://paulgraham.com",
        style=(
            "Reader-focused blog. Single-column with max-width ~680px, "
            "list of posts (title + date + 1-line excerpt) on the home page, "
            "minimal nav (home, archive, about). "
            "Palette: warm off-white background, near-black text, blue link color. "
            "Typography: serif body (Georgia, Source Serif, EB Garamond), "
            "sans-serif headers optional. No sidebars, no ads, no animation."
        ),
    ),
    SiteTemplate(
        name="dashboard",
        keywords=("dashboard", "admin panel", "analytics", "data viewer",
                  "internal tool", "control panel"),
        reference_url="https://tailwindui.com/components/application-ui",
        style=(
            "App dashboard layout. Left sidebar nav (icon + label, collapsible), "
            "top bar (search + user menu + notifications), main grid with stat "
            "cards (large number + delta + sparkline) and data table or chart. "
            "Palette: light gray (#f8fafc) page bg, white panel bg, slate text, "
            "single accent for primary actions. Inter sans, 14px base. "
            "Dense but legible; subtle borders, no heavy shadows."
        ),
    ),
    SiteTemplate(
        name="ecommerce",
        keywords=("shop", "store", "ecommerce", "e-commerce", "products page",
                  "product catalog", "merch"),
        reference_url="https://apple.com/shop",
        style=(
            "Clean product storefront. Top nav with category links and cart, "
            "hero product feature, product grid (image + name + price + 'Add'), "
            "category filter sidebar or chip row, footer with shop info. "
            "Palette: white background, near-black text, ONE brand accent. "
            "Inter or SF Pro. Large product imagery, generous whitespace, "
            "subtle hover lift on cards."
        ),
    ),
    SiteTemplate(
        name="docs",
        keywords=("docs", "documentation", "knowledge base", "wiki", "reference site"),
        reference_url="https://tailwindcss.com/docs/installation",
        style=(
            "Documentation site. Left sidebar nav (collapsible sections + links), "
            "main content column (max-width ~720px) with prose, code blocks "
            "(syntax-highlighted, with copy button), right-side TOC. "
            "Palette: white bg, near-black text, slate code block bg, "
            "ONE accent for links. Inter for prose, JetBrains Mono for code. "
            "Bottom prev/next nav, search at top."
        ),
    ),
    SiteTemplate(
        name="agency",
        keywords=("agency", "studio", "consulting", "freelance", "design firm"),
        reference_url="https://www.basic.agency",
        style=(
            "Creative agency. Bold typography-first hero (oversized headline, "
            "no image), case study grid with hover-reveal imagery, services list, "
            "team section, large contact CTA. "
            "Palette: monochrome (black on white or white on black) + one bright "
            "accent. Large display font (PP Neue Machina, Söhne, or Helvetica "
            "Neue Bold). Generous vertical rhythm, deliberate negative space."
        ),
    ),
    SiteTemplate(
        name="default_web",
        keywords=("website", "site", "page", "web", "landing"),
        reference_url="https://vercel.com",
        style=(
            "Modern minimal web page. Top nav, hero with clear headline, "
            "3-column feature grid, footer. Palette: near-black on white with "
            "ONE accent. Inter font. Subtle gradients, soft shadows, "
            "generous whitespace, mobile responsive via Tailwind."
        ),
    ),
]


def match_template(request_text: str) -> SiteTemplate | None:
    """Find the best-matching site template for a user request.

    Two-pass: first try the specific templates by keyword score; only fall
    back to default_web if no specific template hit. This prevents generic
    terms ('website', 'site') from outscoring a more specific category
    that matched on its discriminating word.
    """
    text = request_text.lower()
    best: tuple[int, SiteTemplate] | None = None
    for tpl in TEMPLATES:
        if tpl.name == "default_web":
            continue
        score = sum(1 for kw in tpl.keywords if kw in text)
        if score > 0 and (best is None or score > best[0]):
            best = (score, tpl)
    if best:
        return best[1]
    # No specific category hit — use default_web only if the request is
    # web-flavored at all.
    default = next((t for t in TEMPLATES if t.name == "default_web"), None)
    if default and any(kw in text for kw in default.keywords):
        return default
    return None


def render_template_block(tpl: SiteTemplate) -> str:
    """Render a reference-template section for inclusion in a Claude Code prompt."""
    return (
        "REFERENCE TEMPLATE — STUDY THIS BEFORE WRITING ANY CODE:\n"
        f"  URL: {tpl.reference_url}\n"
        f"  Category: {tpl.name}\n"
        f"  Style fingerprint: {tpl.style}\n"
        "\n"
        "Use WebFetch on the reference URL FIRST. Study its layout structure, "
        "color palette, typography choices, spacing, and component patterns. "
        "Then build the user's actual request using a similar visual language. "
        "Do NOT copy the reference's copy/content — only adopt its aesthetic.\n"
    )
