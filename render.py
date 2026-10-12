"""Label rendering for the Brother QL-1100, for whichever roll is loaded (see media.py).

Everything here works in printer dots (300 dpi). The returned canvases are exactly what gets printed:
the roll's print width across, and either an automatic length (continuous rolls) or the label's
fixed print length (die-cut and round labels).
"""
import glob
import json
import os
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

import media as M

DPI = 300
FEED_MARGIN = M.FEED   # dots the printer leaves blank before/after each continuous label (3 mm minimum)
MAX_LINES = 35000       # ~2.96 m, QL-1100 maximum

BASE = os.path.dirname(os.path.abspath(__file__))


def dots(mm):
    return int(round(float(mm) * DPI / 25.4))


def to_mm(d):
    return round(d * 25.4 / DPI, 1)


class RenderError(Exception):
    pass


class EmptyDesign(RenderError):
    """Nothing to render yet (not really an error - the UI shows a hint instead)."""


# --------------------------------------------------------------------------- fonts

FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
]
POPULAR = ["Arial", "Calibri", "Segoe UI", "Verdana", "Tahoma", "Trebuchet MS", "Times New Roman",
           "Georgia", "Cambria", "Courier New", "Consolas", "Comic Sans MS", "Impact", "Bahnschrift",
           "Arial Black", "Segoe Print", "Segoe Script"]
STYLE_MAP = {"regular": "r", "normal": "r", "roman": "r", "book": "r",
             "bold": "b", "italic": "i", "oblique": "i",
             "bold italic": "bi", "bold oblique": "bi"}
SKIP_FAMILIES = {"Marlett"}
CACHE_FILE = os.path.join(BASE, "fonts_cache.json")


class FontLibrary:
    def __init__(self):
        self.families = {}   # family -> {"r"/"b"/"i"/"bi": path}

    def _font_files(self):
        files = []
        for d in FONT_DIRS:
            if os.path.isdir(d):
                files += [p for p in glob.glob(os.path.join(d, "*"))
                          if p.lower().endswith((".ttf", ".otf", ".ttc"))]
        return sorted(files)

    def load(self):
        files = self._font_files()
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("files") == files:
                self.families = cache["families"]
                return
        except Exception:
            pass
        fams = {}
        for p in files:
            try:
                fam, style = ImageFont.truetype(p, 12).getname()
            except Exception:
                continue
            if not fam or fam in SKIP_FAMILIES:
                continue
            key = STYLE_MAP.get((style or "Regular").strip().lower())
            if key is None:            # Light, Semibold, Condensed... -> own family
                fam, key = f"{fam} {style}", "r"
            fams.setdefault(fam, {}).setdefault(key, p)
        self.families = {k: v for k, v in fams.items() if "r" in v or "b" in v}
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"files": files, "families": self.families}, f)
        except Exception:
            pass

    def names(self):
        popular = [n for n in POPULAR if n in self.families]
        rest = sorted((n for n in self.families if n not in popular), key=str.lower)
        return popular, rest

    def get(self, family, bold, italic):
        """Returns (path, synthesize_bold, synthesize_italic)."""
        styles = self.families.get(family) or self.families.get("Arial") or next(iter(self.families.values()))
        want = ("b" if bold else "") + ("i" if italic else "") or "r"
        if want in styles:
            return styles[want], False, False
        if want == "bi":
            if "b" in styles:
                return styles["b"], False, True
            if "i" in styles:
                return styles["i"], True, False
        base = styles.get("r") or next(iter(styles.values()))
        return base, bold, italic


FONTS = FontLibrary()


@lru_cache(maxsize=64)
def _font(path, size):
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=512)
def font_sample(family):
    """PNG of the family name drawn in that font (light text, transparent) for the font picker."""
    from io import BytesIO
    if family not in FONTS.families:
        return None
    font = ImageFont.truetype(FONTS.get(family, False, False)[0], 40)
    l, t, r, b = font.getbbox(family)
    asc, desc = font.getmetrics()
    w = max(1, min(900, r - min(0, l) + 4))
    img = Image.new("LA", (w, asc + desc + 4), (236, 0))
    ImageDraw.Draw(img).text((2 - min(0, l), 2), family, font=font, fill=(236, 255))
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- text

ITALIC_SHEAR = 0.21


class Style:
    def __init__(self, b, scale):
        self.size = max(4, int(round(float(b.get("size", 24)) * DPI / 72 * scale)))
        path, self.fake_bold, self.fake_italic = FONTS.get(
            b.get("font", "Arial"), bool(b.get("bold")), bool(b.get("italic")))
        self.font = _font(path, self.size)
        self.stroke = max(1, round(self.size / 28)) if self.fake_bold else 0
        self.underline = bool(b.get("underline"))
        self.strike = bool(b.get("strike"))
        self.align = b.get("align", "center")
        self.ascent, self.descent = self.font.getmetrics()
        spacing = min(3.0, max(0.6, float(b.get("line_spacing", 1.0))))
        self.pitch = max(1, int(round((self.ascent + self.descent) * spacing)))

    def width(self, text):
        w = self.font.getlength(text) + 2 * self.stroke
        if self.fake_italic:
            w += ITALIC_SHEAR * (self.ascent + self.descent)
        return w

    def render(self, text):
        """Renders one line (white bg, black ink). Baseline sits at self.ascent + stroke."""
        s, f = self.stroke, self.font
        h = self.ascent + self.descent + 2 * s
        if text:
            l, _, r, _ = f.getbbox(text, stroke_width=s, anchor="ls")
        else:
            l, r = 0, 0
        x0 = -min(0, l)
        w = int(max(r, f.getlength(text) + s) + x0) + 2
        img = Image.new("L", (max(1, w), h), 255)
        d = ImageDraw.Draw(img)
        base = self.ascent + s
        if text:
            d.text((x0, base), text, font=f, fill=0, anchor="ls", stroke_width=s, stroke_fill=0)
        thick = max(2, round(self.size / 16))
        tl = x0 + f.getlength(text)
        if self.underline and text:
            y = base + max(2, round(self.descent * 0.35))
            d.rectangle([x0, y, tl, y + thick - 1], fill=0)
        if self.strike and text:
            y = base - round(self.ascent * 0.3)
            d.rectangle([x0, y, tl, y + thick - 1], fill=0)
        if self.fake_italic:
            k = ITALIC_SHEAR
            extra = int(k * h) + 1
            img = img.transform((img.width + extra, h), Image.AFFINE, (1, k, -k * h, 0, 1, 0),
                                resample=Image.BICUBIC, fillcolor=255)
        # trim to the ink: fonts leave uneven space beside the letters (usually more on the right),
        # which would make centred text sit off-centre
        ink = ImageOps.invert(img).getbbox()
        return img.crop((ink[0], 0, ink[2], img.height)) if ink else img


def _wrap(style, para, max_w):
    if not para or style.width(para) <= max_w:
        return [para]
    lines, cur = [], ""
    for word in para.split(" "):
        trial = word if not cur else cur + " " + word
        if style.width(trial) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = ""
        # a single word too long for a line: break it by characters
        while word and style.width(word) > max_w:
            n = len(word)
            while n > 1 and style.width(word[:n]) > max_w:
                n -= 1
            lines.append(word[:n])
            word = word[n:]
        cur = word
    lines.append(cur)
    return lines


def _has_content(b):
    return bool(str(b.get("text", "")).strip())


def _layout(blocks, scale, wrap_w, box=0):
    """Stacks every block's lines (aligned within `box` px if wider). Returns (image, widest line px, total height px)."""
    rows = []      # (image, align, pitch)
    for b in blocks:
        st = Style(b, scale)
        for para in str(b.get("text", "")).replace("\r", "").split("\n"):
            for line in (_wrap(st, para, wrap_w) if wrap_w else [para]):
                rows.append((st.render(line), st.align, st.pitch))
    if not rows:
        raise EmptyDesign("Type some text first.")
    content_w = max(im.width for im, _, _ in rows)
    box_w = max(content_w, wrap_w or 0, box)
    total_h = sum(p for _, _, p in rows[:-1]) + rows[-1][0].height
    canvas = Image.new("L", (box_w, max(1, total_h)), 255)
    y = 0
    for im, align, pitch in rows:
        x = {"left": 0, "right": box_w - im.width}.get(align, (box_w - im.width) // 2)
        box = (x, y, x + im.width, y + im.height)
        if box[3] > canvas.height:
            canvas = ImageOps.pad(canvas, (canvas.width, box[3]), color=255, centering=(0, 0))
        canvas.paste(ImageChops.darker(canvas.crop(box), im), box)
        y += pitch
    return canvas, content_w, canvas.height


def _ink_bbox(img):
    return ImageOps.invert(img).point(lambda v: 255 if v > 40 else 0).getbbox()


def _round_pad(media, pad):
    """Round labels: keep content inside the circle (its inscribed square)."""
    return max(pad, int(media.print_w * (1 - 0.7071) / 2)) if media.kind == "round" else pad


def render_text(spec, media):
    """spec: {blocks:[{text,font,size,bold,italic,underline,strike,align,line_spacing}],
              orientation:'horizontal'|'vertical', fit:bool, padding_mm, length_mm (0=auto),
              border:bool, invert:bool}
    On continuous rolls the label is cut right after the text: padding only applies to the sides
    of the tape and the ends get just the printer's own feed margin. Die-cut labels have a fixed
    size, so the text is centred on the label.
    Returns (canvas, warnings, rotated) where canvas is the unrotated design (reads naturally);
    canvas.info["at_min"] is True when the label had to be lengthened to the printer minimum."""
    warnings = []
    blocks = [b for b in spec.get("blocks", []) if _has_content(b)]
    if not blocks:
        raise EmptyDesign("Type some text first.")
    vertical = spec.get("orientation") == "vertical"
    pw = media.print_w
    pad = dots(min(20, max(0, float(spec.get("padding_mm", 1.5)))))   # sides of the tape
    pad_end = 0                                                        # label ends (along the tape)
    border = bool(spec.get("border"))
    if border:
        pad = max(pad, dots(2.5))
        pad_end = max(pad_end, dots(2.5))
    if media.kind == "round":
        pad = pad_end = _round_pad(media, pad)
    if media.continuous:
        min_lines = media.min_lines
        fixed = float(spec.get("length_mm") or 0)
        fixed_lines = max(min_lines, dots(fixed) - 2 * FEED_MARGIN) if fixed > 0 else 0
    else:                                        # die-cut: the label's print length is fixed
        min_lines, fixed, fixed_lines = 0, 0, media.print_l

    across = pw - 2 * pad                                         # space across the tape
    along = (fixed_lines - 2 * pad_end) if fixed_lines else None  # space along the tape (None = unlimited)
    # In horizontal mode lines run across the tape; in vertical mode they run along it.
    max_w, max_h = (across, along) if not vertical else (along, across)

    fit = bool(spec.get("fit"))
    scale = 1.0
    wrap = None if (fit or vertical) else max_w
    if fit:
        def ink(scale):       # the size the text actually takes up on the label at this scale
            img, w, _ = _layout(blocks, scale, None)
            bb = _ink_bbox(img) or (0, 0, w, img.height)
            return img, (bb[2] - bb[0]) if vertical else w, bb[3] - bb[1]
        _, w, h = ink(1.0)
        if along is None:     # auto length: fill the label the text already needs, never a longer one
            along = max(min_lines - 2 * pad_end, w if vertical else h)
            max_w, max_h = (across, along) if not vertical else (along, across)
        scale = min(max_w / w, max_h / h)
        img, w, h = ink(scale)
        for _ in range(40):
            if w <= max_w and h <= max_h:
                break
            scale *= min(0.98, max_w / w, max_h / h)
            img, w, h = ink(scale)
        if not vertical:      # lay it out across the full tape width so left/centre/right alignment works
            img, w, h = _layout(blocks, scale, None, max_w)
    else:
        img, w, h = _layout(blocks, scale, wrap)
    for _ in range(0 if fit else 40):
        too_wide = max_w is not None and w > max_w
        too_tall = max_h is not None and h > max_h
        if not (too_wide or too_tall):
            break
        if not fit and _ == 0:
            warnings.append("Text was shrunk to fit on the label.")
        f = min(max_w / w if too_wide else 1, max_h / h if too_tall else 1)
        scale *= min(0.98, f)
        img, w, h = _layout(blocks, scale, wrap)

    bbox = _ink_bbox(img)
    if not bbox:
        raise RenderError("Nothing visible to print.")
    if vertical:
        img = img.crop(bbox)
    else:
        img = img.crop((0, bbox[1], img.width, bbox[3]))

    # place on the label canvas (pre-rotation: width = across for horizontal, along for vertical)
    natural = (img.width if vertical else img.height) + 2 * pad_end
    length = fixed_lines or max(min_lines, natural)
    if fixed > 0 and dots(fixed) - 2 * FEED_MARGIN < min_lines:
        warnings.append(f"Minimum label length is {to_mm(min_lines + 2 * FEED_MARGIN)} mm.")
    canvas_w, canvas_h = (length, pw) if vertical else (pw, length)
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    x = (canvas_w - img.width) // 2 if vertical else pad
    y = (canvas_h - img.height) // 2
    canvas.paste(img, (x, y))
    canvas = _finish(canvas, border, bool(spec.get("invert")), media.kind == "round")
    canvas.info["at_min"] = media.continuous and not fixed_lines and natural < min_lines
    return canvas, warnings, vertical


def _finish(canvas, border, invert, round_label=False):
    canvas = canvas.point(lambda v: 0 if v < 128 else 255)
    if border:
        t = dots(0.6)
        inset = dots(0.8)
        box = [inset, inset, canvas.width - 1 - inset, canvas.height - 1 - inset]
        if round_label:
            ImageDraw.Draw(canvas).ellipse(box, outline=0, width=t)
        else:
            ImageDraw.Draw(canvas).rounded_rectangle(box, radius=dots(2.5), outline=0, width=t)
    if invert:
        canvas = ImageOps.invert(canvas)
    if round_label:                              # nothing outside the circle
        mask = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, canvas.width - 1, canvas.height - 1], fill=255)
        canvas = Image.composite(canvas, Image.new("L", canvas.size, 255), mask)
    return canvas


# --------------------------------------------------------------------------- images / PDFs

def load_upload(data, filename):
    """Returns list of pages: dict(img=L image, dpi=float or None[, auto="vinted"])."""
    name = os.path.basename(filename or "").lower()
    pages = []
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.page_count == 0:
            raise RenderError("That PDF has no pages.")
        vinted = name.startswith(VINTED_PREFIXES)
        for i, page in enumerate(doc):
            if i >= 50:
                break
            if vinted or _is_label_sheet(page):
                try:
                    label = _vinted_page(page)
                except Exception:
                    label = None
                if label:
                    pages.append(label)
                    continue
            w_in, h_in = page.rect.width / 72, page.rect.height / 72
            dpi = DPI
            if max(w_in, h_in) * dpi > 6000:      # huge pages: keep memory sane
                dpi = 6000 / max(w_in, h_in)
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            pages.append({"img": img, "dpi": dpi})
        return pages
    from io import BytesIO
    try:
        src = Image.open(BytesIO(data))
    except Exception:
        raise RenderError("Unsupported file. Upload a PDF or an image (PNG, JPG, GIF, BMP, TIFF, WEBP).")
    n = getattr(src, "n_frames", 1)
    for i in range(min(n, 50) if getattr(src, "format", "") == "TIFF" else 1):
        src.seek(i)
        im = ImageOps.exif_transpose(src.copy())
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        im = im.convert("L")
        dpi = src.info.get("dpi", (None,))[0]
        try:
            dpi = float(dpi) if dpi and float(dpi) > 50 else None
        except Exception:
            dpi = None
        pages.append({"img": im, "dpi": dpi})
    return pages


# --------------------------------------------------------------------------- Vinted A4 sheets

VINTED_PREFIXES = ("vinted-label", "vinted-return")   # Vinted's PDFs are laid out for A4 paper

# Vinted's InPost labels come as an A4 sheet; recognise it from its text whatever the file is called:
# it must say "InPost" and carry the sheet's print/cut instructions.
SHEET_REQUIRED = "inpost"
SHEET_HINTS = ("print this sheet on a4", "drop off at inpost", "cut out the label", "cut-line", "cut line",
               "must print and attach this label", "attach the label to the parcel", "attach this label to your parcel")


def _is_label_sheet(page):
    """An A4 page that says InPost and has the 'print this sheet / cut out the label' instructions."""
    w, h = sorted((page.rect.width / 72 * 25.4, page.rect.height / 72 * 25.4))
    if not (195 <= w <= 225 and 280 <= h <= 310):            # A4 is 210 x 297 mm
        return False
    text = " ".join(page.get_text().lower().replace("’", "'").split())
    return SHEET_REQUIRED in text and any(k in text for k in SHEET_HINTS)


def _find_label(page, dpi=50, gap_mm=15):
    """The postage label is the inkiest block of content on the sheet, separated from the
    instructions and cut line by at least gap_mm of white space. Returns a Rect in points."""
    import pymupdf
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    mask = Image.frombytes("L", (pix.width, pix.height), pix.samples).point(lambda v: 255 if v < 160 else 0)
    w, h = mask.size
    gap = int(gap_mm / 25.4 * dpi)

    def bands(profile):
        out, start, last = [], None, None
        for i, v in enumerate(profile):
            if v:
                if start is None:
                    start = i
                elif i - last > gap:
                    out.append((start, last + 1))
                    start = i
                last = i
        if start is not None:
            out.append((start, last + 1))
        return out

    rows = [mask.crop((0, y, w, y + 1)).histogram()[255] for y in range(h)]
    if not any(rows):
        return None
    y0, y1 = max(bands(rows), key=lambda b: sum(rows[b[0]:b[1]]))
    cols = [mask.crop((x, y0, x + 1, y1)).histogram()[255] for x in range(w)]
    x0, x1 = max(bands(cols), key=lambda b: sum(cols[b[0]:b[1]]))
    k, m = 72 / dpi, 72 / 25.4          # px -> pt, 1 mm margin
    return pymupdf.Rect(x0 * k - m, y0 * k - m, x1 * k + m, y1 * k + m) & page.rect


def _upright_rotation(page, rect):
    """Clockwise degrees that turn the label's text upright, judged from the text direction."""
    weight = {0: 0, 90: 0, 180: 0, 270: 0}
    for block in page.get_text("dict", clip=rect)["blocks"]:
        for line in block.get("lines", []):
            cos, sin = line["dir"]
            n = sum(len(s["text"].strip()) for s in line["spans"])
            weight[(0 if cos > 0 else 180) if abs(cos) >= abs(sin) else (90 if sin > 0 else 270)] += n
    return (360 - max(weight, key=weight.get)) % 360 if any(weight.values()) else 0


def _vinted_page(page):
    """Crops the postage label out of a Vinted A4 sheet, upright, rendered from the vector PDF
    at 600 dpi so it keeps full quality when scaled to the printer's 300 dpi."""
    import pymupdf
    rect = _find_label(page)
    if not rect or rect.is_empty:
        return None
    dpi = min(600, 6000 / (max(rect.width, rect.height) / 72))
    pix = page.get_pixmap(dpi=dpi, clip=rect, colorspace=pymupdf.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    rot = _upright_rotation(page, rect)
    if rot:
        img = img.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}[rot])
    return {"img": img, "dpi": dpi, "auto": "vinted"}


def _auto_rotate(w_in, h_in, print_w):
    """Rotate 90 degrees when the label is too wide for the roll at actual size (e.g. 6x4" landscape)."""
    roll = print_w / DPI + 0.05
    if w_in > roll and (h_in <= roll or w_in > h_in):
        return 90
    return 0


def render_image(page, spec, media):
    """spec: {crop:[x0,y0,x1,y1] (0-1, optional), rotate:'auto'|0|90|180|270, trim:bool,
              size:'fit'|'actual'|'percent', percent, align, dither:bool, darkness:1-99,
              padding_mm, invert:bool, border:bool}
    Continuous rolls: fitted to the print width, auto length. Die-cut / round labels: fitted
    inside the label's print area and centred."""
    warnings = []
    img = page["img"]
    crop = spec.get("crop")
    if crop and len(crop) == 4:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in crop)
        box = (int(x0 * img.width), int(y0 * img.height), int(round(x1 * img.width)), int(round(y1 * img.height)))
        if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
            img = img.crop(box)
    if spec.get("trim", True):
        bbox = img.point(lambda v: 255 if v < 235 else 0).getbbox()
        if not bbox:
            raise RenderError("The selected area is blank." if crop else "This page looks blank.")
        img = img.crop(bbox)

    pw = media.print_w
    pad = dots(min(20, max(0, float(spec.get("padding_mm", 1.0)))))
    border = bool(spec.get("border"))
    if border:
        pad = max(pad, dots(2.5))
    pad = _round_pad(media, pad)
    box_w = pw - 2 * pad
    box_h = None if media.continuous else media.print_l - 2 * pad

    rot = spec.get("rotate", "auto")
    if rot != "auto":
        rot = int(rot) % 360
    elif media.continuous:
        dpi = page["dpi"]
        rot = _auto_rotate(img.width / dpi, img.height / dpi, pw) if dpi else 0
    else:                                        # die-cut: whichever way round shows it biggest
        upright = min(box_w / img.width, box_h / img.height)
        turned = min(box_w / img.height, box_h / img.width)
        rot = 90 if turned > upright * 1.03 else 0
    if rot:
        img = img.transpose({90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}[rot])

    fit = box_w / img.width if box_h is None else min(box_w / img.width, box_h / img.height)
    mode = spec.get("size", "fit")
    if mode == "actual":
        scale = DPI / (page["dpi"] or DPI)
        if scale > fit * 1.001:
            warnings.append("Actual size is bigger than the label, so it was scaled down to fit.")
            scale = fit
    elif mode == "percent":
        scale = fit * min(100, max(5, float(spec.get("percent", 100)))) / 100
    else:
        scale = fit
    target, h = max(8, int(round(img.width * scale))), max(1, int(round(img.height * scale)))
    if media.continuous and h > MAX_LINES - 2 * pad:
        raise RenderError("This image is too long to print (max ~3 m).")
    if (target, h) != img.size:
        img = img.resize((target, h), Image.LANCZOS)

    darkness = min(99, max(1, float(spec.get("darkness", 50))))
    if spec.get("dither"):
        # lighten slightly so dithered photos don't print muddy; darkness shifts the curve
        gamma = 1.0 + (50 - darkness) / 60
        img = img.point(lambda v: int(255 * (v / 255) ** max(0.3, gamma)))
        img = img.convert("1", dither=Image.FLOYDSTEINBERG).convert("L")
    else:
        t = int(darkness * 2.55)
        img = img.point(lambda v: 0 if v < t else 255)

    if media.continuous:
        length = max(media.min_lines, img.height + 2 * pad)
    else:
        length = media.print_l
    canvas = Image.new("L", (pw, length), 255)
    align = spec.get("align", "center")
    x = {"left": pad, "right": pw - pad - img.width}.get(align, (pw - img.width) // 2)
    canvas.paste(img, (x, (length - img.height) // 2))
    canvas = _finish(canvas, border, bool(spec.get("invert")), media.kind == "round")
    canvas.info["at_min"] = media.continuous and img.height + 2 * pad < media.min_lines
    return canvas, warnings


def page_preview(page, max_side=1400):
    """JPEG of the original page for the crop tool."""
    from io import BytesIO
    if "preview" not in page:
        img = page["img"].copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=85)
        page["preview"] = buf.getvalue()
    return page["preview"]


def to_print(canvas, rotated):
    """Converts a design canvas to the printer orientation (the roll's print width wide)."""
    return canvas.transpose(Image.ROTATE_90) if rotated else canvas
