"""Store page change log: turn two snapshots of a store page into change rows."""
from __future__ import annotations

# Field -> how to render it in the change log.
TRACKED = ("name", "capsule", "short_description", "price", "discount", "release_date",
           "coming_soon", "early_access", "dlc", "trailers", "screenshots", "genres")


def page_state(details: dict) -> dict:
    """The comparable subset of a parsed appdetails result."""
    return {
        "name": details.get("name"),
        "capsule": details.get("capsule_version"),
        "header_image": details.get("header_image"),
        "short_description": details.get("short_description"),
        "price": details.get("price_cents"),
        "discount": details.get("discount_pct") or 0,
        "release_date": details.get("release_date_text"),
        "coming_soon": details.get("coming_soon"),
        "trailers": details.get("trailer_ids") or [],
        "screenshots": details.get("screenshot_count"),
        "genres": sorted(g for g in details.get("genres") or [] if g != "Early Access"),
        "early_access": bool(details.get("early_access")),
        "dlc": details.get("dlc_count") or 0,
    }


def _fmt(field: str, v) -> str | None:
    if v is None:
        return None
    if field == "price":
        return f"${v / 100:.2f}"
    if field == "discount":
        return f"{v}%"
    if field in ("trailers", "genres"):
        return ", ".join(map(str, v)) if v else "(none)"
    if field == "coming_soon":
        return "coming soon" if v else "released"
    if field == "early_access":
        return "Early Access" if v else "Full release"
    if field == "dlc":
        return f"{v} DLC"
    return str(v)


def diff_states(old: dict | None, new: dict) -> list[dict]:
    """Change rows between two page states. The first snapshot produces none."""
    if not old:
        return []
    rows = []
    for f in TRACKED:
        if f not in old:
            continue           # field added in a later version: nothing to compare against yet
        a, b = old.get(f), new.get(f)
        if f == "genres":      # Early Access has its own field; older states kept it in genres
            a = [g for g in a or [] if g != "Early Access"]
        if a == b:
            continue
        if f == "capsule" and (a is None or b is None):
            continue           # missing asset version is not a real change
        if f == "trailers":
            added = [t for t in b if t not in a]
            removed = [t for t in a if t not in b]
            if not added and not removed:
                continue       # same set, different order
            rows.append({"field": f,
                         "old_value": f"{len(a)} trailers",
                         "new_value": f"{len(b)} trailers"
                                      + (f" (+{len(added)} new)" if added else "")
                                      + (f" (−{len(removed)})" if removed else "")})
            continue
        if f == "capsule":
            rows.append({"field": f, "old_value": old.get("header_image") or a,
                         "new_value": new.get("header_image") or b})
            continue
        if f == "dlc" and a is not None and b is not None and b > a:
            rows.append({"field": f, "old_value": _fmt(f, a), "new_value": f"{b} DLC (+{b - a} new)"})
            continue
        if a == b:
            continue
        rows.append({"field": f, "old_value": _fmt(f, a), "new_value": _fmt(f, b)})
    return rows


def diff_tags(old: list[str] | None, new: list[str] | None, top: int = 10) -> dict | None:
    """Top-tag set changes (order shuffles inside the top 10 are ignored)."""
    if not old or not new:
        return None
    a, b = old[:top], new[:top]
    added = [t for t in b if t not in a]
    removed = [t for t in a if t not in b]
    if not added and not removed:
        return None
    return {"field": "tags",
            "old_value": ", ".join(removed) if removed else None,
            "new_value": ", ".join(added) if added else None}
