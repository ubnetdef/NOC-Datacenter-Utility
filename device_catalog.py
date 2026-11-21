"""Load the bundled, read-only hardware template library."""

import json


def load_catalog(path):
    with open(path, encoding="utf-8") as file:
        catalog = json.load(file)
    if not isinstance(catalog, dict) or catalog.get("version") != 1 or not isinstance(catalog.get("models"), list):
        raise ValueError("Hardware catalog must contain a supported version and models array")
    models = {}
    for model in catalog["models"]:
        if not isinstance(model, dict):
            raise ValueError("Each hardware catalog entry must be an object")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in models:
            raise ValueError("Hardware model IDs must be unique nonempty strings")
        if not isinstance(model.get("name"), str) or not isinstance(model.get("manufacturer"), str):
            raise ValueError(f"Hardware model {model_id} needs a name and manufacturer")
        if not isinstance(model.get("height"), int) or isinstance(model["height"], bool) or not 1 <= model["height"] <= 60:
            raise ValueError(f"Hardware model {model_id} has an invalid rack height")
        variants = model.get("variants")
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"Hardware model {model_id} needs at least one configuration")
        variant_ids = set()
        for variant in variants:
            if (not isinstance(variant, dict) or not isinstance(variant.get("id"), str)
                    or not variant["id"] or variant["id"] in variant_ids
                    or not isinstance(variant.get("label"), str)
                    or not isinstance(variant.get("front_bays"), int)
                    or not isinstance(variant.get("rear_bays"), int)
                    or variant["front_bays"] < 0 or variant["rear_bays"] < 0
                    or variant["front_bays"] > 48 or variant["rear_bays"] > 8):
                raise ValueError(f"Hardware model {model_id} has an invalid configuration")
            if variant["front_bays"] and variant.get("drive_orientation") not in {"horizontal", "vertical"}:
                raise ValueError(f"Hardware model {model_id} needs a supported drive orientation")
            if variant["front_bays"]:
                drive_rows = variant.get("drive_rows")
                if (not isinstance(drive_rows, int) or isinstance(drive_rows, bool)
                        or not 1 <= drive_rows <= 2 or variant["front_bays"] % drive_rows):
                    raise ValueError(f"Hardware model {model_id} needs a valid front drive-row layout")
                drive_slot_width = variant.get("drive_slot_width")
                if (drive_slot_width is not None
                        and (not isinstance(drive_slot_width, int) or isinstance(drive_slot_width, bool)
                             or not 20 <= drive_slot_width <= 200)):
                    raise ValueError(f"Hardware model {model_id} has an invalid drive-bay width")
            variant_ids.add(variant["id"])
        front = model.get("front", {})
        if (not isinstance(front, dict)
                or front.get("control_side", "left") not in {"left", "right"}
                or ("control_width" in front and (
                    not isinstance(front["control_width"], int)
                    or isinstance(front["control_width"], bool)
                    or not 100 <= front["control_width"] <= 220))):
            raise ValueError(f"Hardware model {model_id} has an invalid front control layout")
        rear = model.get("rear", {})
        if not isinstance(rear, dict) or rear.get("psu_side", "right") not in {"left", "right"}:
            raise ValueError(f"Hardware model {model_id} has an invalid power-supply layout")
        models[model_id] = model
    return catalog
