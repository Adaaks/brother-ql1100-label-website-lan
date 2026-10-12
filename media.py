"""Brother DK media the QL-1100 supports.

All sizes come from Brother's "Raster Command Reference QL-1100/1110NWB/1115NWB" (v1.00):
section 2.3.2 (page sizes) and the pin tables in 2.3.5. Dots are 300 dpi; the print head has
1296 pins and `right` is the number of unused pins on the right of the print area.
"""

DPI = 300
HEAD_PINS = 1296
FEED = 35                    # continuous tape: blank feed at each end of a label (3 mm, the minimum)
MIN_TOTAL = 301              # continuous tape: shortest label, feed included (25.4 mm)


def mm(dots):
    return round(dots * 25.4 / DPI, 2)


class Media:
    def __init__(self, id, kind, status_w, status_l, total_w, print_w, right, total_l=0, print_l=0):
        self.id, self.kind = id, kind                 # kind: continuous | die-cut | round
        self.status_w, self.status_l = status_w, status_l   # what the printer reports (mm)
        self.total_w, self.print_w, self.right = total_w, print_w, right
        self.total_l, self.print_l = total_l, print_l
        assert HEAD_PINS - print_w - right >= 0

    @property
    def continuous(self):
        return self.kind == "continuous"

    @property
    def status_type(self):
        return 0x0A if self.continuous else 0x0B

    @property
    def min_lines(self):
        return MIN_TOTAL - 2 * FEED

    @property
    def name(self):
        if self.continuous:
            return f"{self.id} mm continuous roll" + (' (4")' if self.id == "102" else "")
        if self.kind == "round":
            return f"{self.id[1:]} mm round labels"
        w, l = self.id.split("x")
        return f"{w} × {l} mm labels"

    @property
    def nominal(self):
        """(width, length) in mm as printed on the box; length 0 for continuous rolls."""
        if self.continuous:
            return int(self.id), 0
        if self.kind == "round":
            return int(self.id[1:]), int(self.id[1:])
        w, l = self.id.split("x")
        return int(w), int(l)

    def geometry(self, lines):
        """Where a print image of `lines` raster rows sits on the physical label (mm)."""
        side = (self.total_w - self.print_w) / 2
        if self.continuous:
            paper = (self.total_w, lines + 2 * FEED)
            area = (side, FEED, self.print_w, lines)
            length = mm(lines + 2 * FEED)
        else:
            paper = (self.total_w, self.total_l)
            area = (side, (self.total_l - self.print_l) / 2, self.print_w, self.print_l)
            length = self.nominal[1]
        w, l = self.nominal
        label = (f"{w} × {round(length, 1)} mm" if self.continuous else
                 f"{w} mm round label" if self.kind == "round" else f"{w} × {l} mm label")
        return {"paper": [mm(v) for v in paper], "area": [mm(v) for v in area],
                "shape": {"continuous": "rect", "die-cut": "die", "round": "round"}[self.kind],
                "length_mm": round(length, 1), "label": label}

    def to_json(self):
        w, l = self.nominal
        return {"id": self.id, "kind": self.kind, "name": self.name, "width_mm": w, "length_mm": l,
                "paper_w_mm": mm(self.total_w), "print_w_mm": mm(self.print_w),
                "min_length_mm": round(mm(MIN_TOTAL), 1) if self.continuous else l}


_ALL = [
    # continuous length tape: id, kind, reported width, 0, tape dots, print dots, right pins
    Media("12", "continuous", 12, 0, 142, 106, 74),
    Media("29", "continuous", 29, 0, 342, 306, 50),
    Media("38", "continuous", 38, 0, 449, 413, 56),
    Media("50", "continuous", 50, 0, 590, 554, 56),
    Media("54", "continuous", 54, 0, 636, 590, 44),
    Media("62", "continuous", 62, 0, 732, 696, 56),
    Media("102", "continuous", 102, 0, 1200, 1164, 56),
    Media("103", "continuous", 104, 0, 1224, 1200, 38),
    # die-cut labels: ..., label length dots, print length dots
    Media("17x54", "die-cut", 17, 54, 201, 165, 44, 636, 566),
    Media("17x87", "die-cut", 17, 87, 201, 165, 44, 1026, 956),
    Media("23x23", "die-cut", 23, 23, 272, 236, 85, 272, 202),
    Media("29x42", "die-cut", 29, 42, 342, 306, 50, 495, 425),
    Media("29x90", "die-cut", 29, 90, 342, 306, 50, 1061, 991),
    Media("38x90", "die-cut", 38, 90, 449, 413, 56, 1061, 991),
    Media("39x48", "die-cut", 39, 48, 461, 425, 50, 565, 495),
    Media("52x29", "die-cut", 52, 29, 614, 578, 44, 341, 271),
    Media("60x86", "die-cut", 60, 87, 708, 672, 68, 1024, 954),
    Media("62x29", "die-cut", 62, 29, 732, 696, 56, 341, 271),
    Media("62x100", "die-cut", 62, 100, 732, 696, 56, 1179, 1109),
    Media("102x51", "die-cut", 102, 51, 1200, 1164, 56, 596, 526),
    Media("102x152", "die-cut", 102, 152, 1200, 1164, 56, 1804, 1660),
    Media("103x164", "die-cut", 104, 164, 1224, 1200, 38, 1941, 1822),
    Media("d12", "round", 12, 12, 142, 94, 156, 142, 94),
    Media("d24", "round", 24, 24, 284, 236, 85, 284, 236),
    Media("d58", "round", 58, 58, 688, 618, 94, 688, 618),
]
MEDIA = {m.id: m for m in _ALL}
DEFAULT = MEDIA["102"]


def from_status(media_type, width, length):
    """Maps the printer's reported media (type, width mm, length mm) to a Media, or None."""
    for m in _ALL:
        if m.status_type == media_type and m.status_w == width and (m.continuous or m.status_l == length):
            return m
    return None
