import re
from dataclasses import dataclass


_TAG_PATTERNS = [
    r"\[[^\]]+\]",  # [!], [b1], [T+Eng], [Rev 1], ...
    r"\([^)]+\)",  # (USA), (Europe), (En,Fr,De), (v1.1), ...
    r"\{[^}]+\}",  # {something}
]


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_known_tags(name: str) -> str:
    out = name
    for pat in _TAG_PATTERNS:
        out = re.sub(pat, " ", out)
    return _normalize_spaces(out)


def title_from_rom_filename(filename: str) -> tuple[str, float]:
    """
    Parse a reasonable game title from a ROM filename.

    Returns (title, confidence) where confidence is 0..1.
    Works for both .gba and .nds files.
    """
    base = filename.rsplit("/", 1)[-1]
    base = re.sub(r"\.(gba|nds)$", "", base, flags=re.IGNORECASE)

    # Common separators
    candidate = base.replace("_", " ").replace(".", " ").replace("-", " ")
    candidate = _normalize_spaces(candidate)

    stripped = _strip_known_tags(candidate)

    if not stripped:
        return base, 0.0

    # Heuristics: lots of tags removed => still OK; very short titles => low confidence
    removed = len(candidate) - len(stripped)
    confidence = 0.7
    if removed > 10:
        confidence += 0.1
    if len(stripped) < 4:
        confidence = 0.2
    if re.fullmatch(r"\d+", stripped):
        confidence = 0.1
    confidence = max(0.0, min(1.0, confidence))
    return stripped, confidence


def get_rom_type(rom_path: str) -> str:
    """Detect ROM type from file extension."""
    lower = rom_path.lower()
    if lower.endswith(".gba"):
        return "gba"
    elif lower.endswith(".nds"):
        return "nds"
    return "unknown"


@dataclass
class BatchItem:
    rom_path: str
    sd_path: str
    title: str
    confidence: float
    year: str = ""
    subtitle: str = ""  # For NDS: displayed as "Released: {year}" or custom text
    sgdb_game_id: int | None = None
    icon_url: str | None = None
    logo_url: str | None = None
    icon_file: str | None = None
    label_file: str | None = None
    fit_mode: str = "fit"  # fit, fill, or stretch
    build_status: str = "pending"  # pending, building, success, failed

    @property
    def rom_type(self) -> str:
        return get_rom_type(self.rom_path)

    @property
    def needs_user_input(self) -> bool:
        return self.confidence < 0.5 or not self.title

    @property
    def needs_assets(self) -> bool:
        # GBA needs both icon and label; NDS only needs label (icon from ROM)
        if self.rom_type == "nds":
            return not self.label_file
        return not (self.icon_file and self.label_file)

