"""ASCII diagrams in an article, redrawn as SVG by a hosted model.

A diagram drawn with box characters is wider than a phone, and no reflow
helps it. At prepare time a hosted model (``DIAGRAM_LLM_*``, Claude Sonnet
through OpenRouter by default) reads each wide one and draws it as an SVG
that fits a phone. The local chat model lost boxes and set labels on top of
each other in a trial, so this one call goes out.

The model's SVG is never served as it came. It is parsed and rebuilt from
an allowlist of drawing elements and attributes: no script, style, link,
foreign content or outside reference survives, and every colour becomes
``currentColor`` so the drawing follows the reader's light and dark theme.
When the drawing fails (no credits left, an API error, a reply that does
not parse or draws nothing), the diagram's place shows ``diagram-error.svg``
instead: the ASCII drawing is what could not be read on a phone. Without a
hosted model configured, nothing is drawn and the ASCII diagram stays.
"""

from __future__ import annotations

import html
import logging
import re
from importlib.resources import files

from lxml import etree
from openai import AsyncOpenAI

from .config import Settings

log = logging.getLogger(__name__)

_PRE = re.compile(r"<pre><code>(.*)</code></pre>", re.S)
_BOX_CHAR = re.compile("[─-╿]")
_ASCII_BOX = re.compile(r"\+[-=]{3,}\+")
# A phone shows about 40 columns of monospace text; a narrower diagram fits.
_MIN_COLUMNS = 40
# Each diagram is a paid call: a page with more is not an article about them.
_MAX_PER_ARTICLE = 5
# Room for the model's reasoning and then the SVG (~1.5k tokens). Claude
# Haiku 5.5 reasons 8-12k tokens at effort "low" and up to 22k without one,
# and OpenRouter does not hold it to a reasoning budget: with 8000 it used
# them all and drew nothing. Only the tokens used are billed.
_MAX_TOKENS = 32000
# Shown in a diagram's place when drawing it failed; our own file, not a reply
_ERROR = (
    '<figure class="diagram diagram-error">'
    + files(__package__).joinpath("diagram-error.svg").read_text().strip()
    + "</figure>"
)

_PROMPT = """Redraw this text diagram as one SVG for a phone screen.

Rules:
- Keep every box, label, nesting level, separator and arrow. Add nothing.
- The screen is narrow: viewBox width 360, height as needed. Parts that stand
  side by side in the text go one below the other, each with its heading.
- Text: font-family sans-serif, font-size 13 or larger. Every label lies
  inside its box with at least 6 units of space to every line, and no label
  overlaps another. Plan each element's y range before you draw it.
- Colours: fill="none" and stroke="currentColor" for shapes,
  fill="currentColor" for text and arrowheads. A dashed separator in the text
  is a line with stroke-dasharray.
- Only the elements svg, g, rect, line, polyline, polygon, path, circle,
  ellipse, text, tspan, defs and marker. No style element, no CSS, no script,
  no links, no images.
- Reply with the SVG only, from <svg to </svg>.

Diagram:
```
{diagram}
```"""

_SVG_NS = "http://www.w3.org/2000/svg"
_ELEMENTS = {
    "svg", "g", "rect", "line", "polyline", "polygon", "path", "circle", "ellipse",
    "text", "tspan", "title", "desc", "defs", "marker",
}  # fmt: skip
_ATTRS = {
    "viewBox", "preserveAspectRatio", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r",
    "rx", "ry", "width", "height", "points", "d", "dx", "dy", "transform", "fill",
    "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
    "opacity", "fill-opacity", "stroke-opacity", "font-family", "font-size",
    "font-weight", "font-style", "text-anchor", "dominant-baseline", "id", "orient",
    "refX", "refY", "markerWidth", "markerHeight", "markerUnits", "marker-start",
    "marker-mid", "marker-end",
}  # fmt: skip
_PAINT = {"fill", "stroke"}
# Numbers, words, lists and transforms; nothing that quotes, escapes or
# starts a URL other than a reference inside the drawing.
_VALUE = re.compile(r"[\w\s.,%()#+-]*")
_LOCAL_URL = re.compile(r"url\(#([\w-]+)\)")


def is_diagram(text: str) -> bool:
    """Whether a <pre> block is a box drawing too wide for a phone."""
    lines = text.splitlines()
    if max((len(line.rstrip()) for line in lines), default=0) <= _MIN_COLUMNS:
        return False
    return len(_BOX_CHAR.findall(text)) >= 6 or len(_ASCII_BOX.findall(text)) >= 2


def _clean_value(name: str, value: str, prefix: str) -> str | None:
    value = " ".join(value.split())
    if not _VALUE.fullmatch(value):
        return None
    if "url(" in value:
        match = _LOCAL_URL.fullmatch(value)
        return f"url(#{prefix}-{match[1]})" if match else None
    if name in _PAINT and value not in ("none", "currentColor"):
        return "currentColor"
    if name == "id":
        return f"{prefix}-{value}"
    return value


def _copy(el: etree._Element, prefix: str) -> etree._Element | None:
    if not isinstance(el.tag, str):
        return None  # comments and processing instructions
    tag = etree.QName(el)
    if tag.namespace not in (None, _SVG_NS) or tag.localname not in _ELEMENTS:
        return None
    out = etree.Element(tag.localname)
    for name, value in el.attrib.items():
        attr = etree.QName(name)
        if attr.namespace is None and attr.localname in _ATTRS:
            clean = _clean_value(attr.localname, value, prefix)
            if clean is not None:
                out.set(attr.localname, clean)
    out.text = el.text
    for child in el:
        copy = _copy(child, prefix)
        if copy is not None:
            copy.tail = child.tail
            out.append(copy)
    return out


def clean_svg(reply: str, prefix: str) -> str | None:
    """The SVG in the model's reply, rebuilt from the allowlist, or None.
    ``prefix`` keeps its ids apart from those of other drawings on the page."""
    match = re.search(r"<svg\b.*</svg>", reply, re.S)
    if not match:
        return None
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(match[0].encode(), parser)
    except etree.XMLSyntaxError:
        return None
    svg = _copy(root, prefix)
    if svg is None or svg.tag != "svg" or not svg.get("viewBox") or svg.find(".//text") is None:
        return None
    # the page sizes it to the screen
    for name in ("width", "height", "x", "y"):
        svg.attrib.pop(name, None)
    svg.set("role", "img")
    return etree.tostring(svg, encoding="unicode").replace("<svg", f'<svg xmlns="{_SVG_NS}"', 1)


async def _draw(client: AsyncOpenAI, model: str, text: str, effort: str) -> str:
    kwargs = {"extra_body": {"reasoning": {"effort": effort}}} if effort else {}
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": _PROMPT.format(diagram=text)}],
        max_tokens=_MAX_TOKENS,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


async def draw_diagrams(blocks: list[str], settings: Settings) -> list[str]:
    """``blocks`` with each wide box drawing in a <pre> redrawn as SVG,
    where the model managed it."""
    if not settings.diagrams_enabled:
        return blocks
    found = [
        (i, text)
        for i, block in enumerate(blocks)
        if (m := _PRE.fullmatch(block)) and is_diagram(text := html.unescape(m[1]))
    ][:_MAX_PER_ARTICLE]
    if not found:
        return blocks
    client = AsyncOpenAI(
        base_url=settings.diagram_llm_base_url,
        api_key=settings.diagram_llm_token,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )
    out = list(blocks)
    for n, (i, text) in enumerate(found):
        try:
            reply = await _draw(
                client, settings.diagram_llm_model, text, settings.diagram_llm_effort
            )
            svg = clean_svg(reply, f"d{n}")
        except Exception as exc:  # out of credits, an API error, a timeout
            log.warning("draw_diagrams: drawing failed: %s", exc)
            svg = None
        else:
            if svg is None:
                log.warning("draw_diagrams: the reply held no usable SVG")
        out[i] = f'<figure class="diagram">{svg}</figure>' if svg else _ERROR
    return out
