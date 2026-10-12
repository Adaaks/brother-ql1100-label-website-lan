"""Roll tracker: an estimate of how much of each roll is left.

Every roll you set up gets a number (#1, #2, ...) and remembers its size and how much has been used.
Each roll size (printer media id) has its own "loaded" roll, so swapping to a narrower roll and back
picks up where it left off. Continuous rolls are tracked in mm of tape, die-cut / round rolls in labels.
Prints, reprints and fed blank tape all count; a cut on its own uses no paper.
"""
import json
import os
import threading
import time

import media as M

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rolls.json")

# Brother's own roll sizes, offered as the starting value for a new roll: metres for continuous
# tape, labels for die-cut and round rolls. Third-party rolls are often longer (e.g. 30.48 m of 102 mm).
DEFAULTS = {
    "12": 30.48, "29": 30.48, "38": 30.48, "50": 30.48, "54": 30.48, "62": 30.48, "102": 15.24, "103": 30.48,
    "17x54": 400, "17x87": 300, "23x23": 1000, "29x42": 400, "29x90": 400, "38x90": 400, "39x48": 620,
    "52x29": 800, "60x86": 400, "62x29": 800, "62x100": 300, "102x51": 600, "102x152": 200, "103x164": 180,
    "d12": 1200, "d24": 1000, "d58": 100,
}


def unit(media):
    return "m" if media.continuous else "labels"


class Rolls:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"enabled": True, "next_id": 1, "rolls": [], "loaded": {}}
        try:
            with open(FILE, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, FILE)

    def _get(self, rid):
        return next((r for r in self.data["rolls"] if r["id"] == rid), None)

    def _loaded(self, media):
        return self._get(self.data["loaded"].get(media.id))

    @staticmethod
    def _view(r):
        """A roll as the page shows it: amounts in metres (continuous) or labels."""
        media = M.MEDIA.get(r["media"], M.DEFAULT)
        k = 1000 if media.continuous else 1           # stored in mm / labels
        left = max(0.0, r["total"] - r["used"])
        return {"id": r["id"], "media": r["media"], "name": media.name, "unit": unit(media),
                "total": round(r["total"] / k, 2), "left": round(left / k, 2),
                "pct": round(100 * left / r["total"], 1) if r["total"] else 0, "last_used": r.get("last_used")}

    # ------------------------------------------------------------------ reading
    def summary(self, media):
        """For the status pill: None when tracking is off."""
        with self.lock:
            if not self.data["enabled"]:
                return None
            r = self._loaded(media)
            return dict(self._view(r), tracked=True) if r else {"tracked": False, "unit": unit(media)}

    def state(self, media):
        with self.lock:
            r = self._loaded(media)
            same = [x for x in self.data["rolls"] if x["media"] == media.id]
            k = 1000 if media.continuous else 1          # a new roll is probably like the last one of this size
            default = round(max(same, key=lambda x: x["id"])["total"] / k, 2) if same else DEFAULTS.get(media.id)
            return {"enabled": self.data["enabled"], "media": media.id, "media_name": media.name, "unit": unit(media),
                    "default_total": default, "loaded": r["id"] if r else None,
                    "loaded_ids": list(self.data["loaded"].values()),
                    "next_id": self.data["next_id"],
                    "rolls": [self._view(x) for x in sorted(self.data["rolls"], key=lambda x: -x["id"])]}

    # ------------------------------------------------------------------ changes
    def set_enabled(self, on):
        with self.lock:
            self.data["enabled"] = bool(on)
            self._save()

    def add(self, media, total, left=None):
        """New roll of `total` (m or labels), `left` of it remaining (default: full). It becomes the loaded roll."""
        k = 1000 if media.continuous else 1
        total = float(total)
        if not 0 < total <= (1000 if media.continuous else 100000):
            raise ValueError("Enter the roll's length.")
        left = total if left in (None, "") else min(total, max(0.0, float(left)))
        with self.lock:
            rid = self.data["next_id"]
            self.data["next_id"] = rid + 1
            self.data["rolls"].append({"id": rid, "media": media.id, "total": total * k, "used": (total - left) * k,
                                       "added": time.time(), "last_used": None})
            self.data["loaded"][media.id] = rid
            self.data["enabled"] = True
            self._save()
            return rid

    def load(self, rid):
        with self.lock:
            r = self._get(rid)
            if not r:
                raise KeyError(rid)
            self.data["loaded"][r["media"]] = rid
            self._save()

    def set_left(self, rid, left):
        with self.lock:
            r = self._get(rid)
            if not r:
                raise KeyError(rid)
            k = 1000 if M.MEDIA.get(r["media"], M.DEFAULT).continuous else 1
            r["used"] = r["total"] - min(r["total"], max(0.0, float(left) * k))
            self._save()

    def delete(self, rid):
        with self.lock:
            self.data["rolls"] = [r for r in self.data["rolls"] if r["id"] != rid]
            self.data["loaded"] = {m: i for m, i in self.data["loaded"].items() if i != rid}
            self._save()

    def use(self, media, lengths_mm):
        """Paper used by one job: the length of every label in it (continuous), or one per label (die-cut)."""
        with self.lock:
            r = self._loaded(media)
            if not r or not self.data["enabled"]:
                return
            r["used"] += sum(lengths_mm) if media.continuous else len(lengths_mm)
            r["last_used"] = time.time()
            try:
                self._save()
            except OSError:
                pass


ROLLS = Rolls()
